"""AuthService — turns a credential into a :class:`Principal`.

Two carriers, one principal (D4):

- **Bearer** (Edboox UI / backend): a short-lived issuer token on every request.
- **Session cookie** (our UI): minted once by a verified, single-use *launch*
  token; holds only our principal id, signed with ``LF_SESSION_SECRET``.

Permissions are resolved server-side on every request from the directory (role +
local grants) and cached for ``principal_cache_s``, so a revoked grant or a
disabled principal stops working within that window — and a bearer request does
not cost a database write each time.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from collections.abc import Hashable
from dataclasses import dataclass, replace
from typing import Generic, TypeVar

import jwt

from ..config import AuthConfig
from .directory import PrincipalDirectory, PrincipalRecord
from .policy import RbacPolicy
from .principal import Principal
from .replay import ReplayGuard
from .verifier import AuthError, TokenPurpose, TokenVerifier, VerifiedToken

_SESSION_AUD = "lessonforge-session"
K = TypeVar("K", bound=Hashable)
V = TypeVar("V")


class TTLCache(Generic[K, V]):
    """A small, bounded, thread-safe TTL cache (LRU eviction when full)."""

    def __init__(self, ttl_s: float, max_items: int = 10_000) -> None:
        self.ttl_s = ttl_s
        self.max_items = max_items
        self._data: OrderedDict[K, tuple[float, V]] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: K) -> V | None:
        if self.ttl_s <= 0:
            return None
        with self._lock:
            hit = self._data.get(key)
            if hit is None:
                return None
            expires, value = hit
            if expires < time.monotonic():
                del self._data[key]
                return None
            self._data.move_to_end(key)
            return value

    def put(self, key: K, value: V) -> None:
        if self.ttl_s <= 0:
            return
        with self._lock:
            self._data[key] = (time.monotonic() + self.ttl_s, value)
            self._data.move_to_end(key)
            while len(self._data) > self.max_items:
                self._data.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()


@dataclass(frozen=True, slots=True)
class LaunchResult:
    principal: Principal
    token: VerifiedToken
    session: str            # the cookie value


class SessionCodec:
    """Our own HS256 session token: ``{sub: principal_id, iat, exp}``. A distinct
    audience keeps it from ever being mistaken for an issuer token."""

    def __init__(self, secret: str, ttl_min: int) -> None:
        if len(secret) < 32:
            raise ValueError("auth.session.secret must be at least 32 characters "
                             "(set LF_SESSION_SECRET to a random value)")
        self._secret = secret
        self.ttl_s = ttl_min * 60

    def issue(self, principal_id: str) -> str:
        now = int(time.time())
        return jwt.encode({"sub": principal_id, "iat": now, "exp": now + self.ttl_s,
                           "aud": _SESSION_AUD}, self._secret, algorithm="HS256")

    def read(self, value: str) -> str:
        try:
            claims = jwt.decode(value, self._secret, algorithms=["HS256"], audience=_SESSION_AUD,
                                options={"require": ["sub", "exp", "iat"]})
        except jwt.ExpiredSignatureError as exc:
            raise AuthError("auth.expired", "session expired") from exc
        except jwt.InvalidTokenError as exc:
            raise AuthError("auth.invalid", "invalid session") from exc
        return str(claims["sub"])


class AuthService:
    def __init__(self, cfg: AuthConfig, *, verifier: TokenVerifier, directory: PrincipalDirectory,
                 replay: ReplayGuard, policy: RbacPolicy, sessions: SessionCodec) -> None:
        self.cfg = cfg
        self.verifier = verifier
        self.directory = directory
        self.replay = replay
        self.policy = policy
        self.sessions = sessions
        self._by_token: TTLCache[tuple, Principal] = TTLCache(cfg.principal_cache_s)
        self._by_id: TTLCache[str, Principal] = TTLCache(cfg.principal_cache_s)

    # ── carriers ────────────────────────────────────────────────────────────
    def from_bearer(self, token: str) -> Principal:
        vt = self.verifier.verify(token, TokenPurpose.API)
        key = (vt.issuer, vt.subject, vt.role, vt.tenant_id, vt.display_name)
        cached = self._by_token.get(key)
        if cached is None:
            cached = self._principal(self.directory.upsert(vt))
            self._by_token.put(key, cached)
        return _with_grade(cached, vt.grade)

    def from_session(self, value: str) -> Principal:
        pid = self.sessions.read(value)
        cached = self._by_id.get(pid)
        if cached is None:
            rec = self.directory.get(pid)
            if rec is None:
                raise AuthError("auth.invalid", "unknown session principal")
            cached = self._principal(rec)
            self._by_id.put(pid, cached)
        return cached

    def launch(self, token: str) -> LaunchResult:
        """Verify a one-time launch token, burn its ``jti``, upsert the principal
        and mint a session. Never cached: a launch always reads fresh grants."""
        vt = self.verifier.verify(token, TokenPurpose.LAUNCH)
        if not self.replay.consume(vt.issuer, vt.jti or "", vt.expires_at):
            raise AuthError("auth.invalid", "launch token already used")
        principal = _with_grade(self._principal(self.directory.upsert(vt)), vt.grade)
        self._by_id.put(principal.id, principal)
        return LaunchResult(principal, vt, self.sessions.issue(principal.id))

    def invalidate(self) -> None:
        self._by_token.clear()
        self._by_id.clear()

    # ── record → principal ──────────────────────────────────────────────────
    def _principal(self, rec: PrincipalRecord) -> Principal:
        if rec.disabled:
            raise AuthError("perm.denied", "this account is disabled in LessonForge", status=403)
        roles = frozenset({rec.external_role, *rec.grants})
        return Principal(
            id=rec.id, issuer=rec.issuer, subject=rec.subject, role=rec.external_role,
            roles=roles, permissions=self.policy.permissions(roles),
            tenant_id=rec.tenant_id, display_name=rec.display_name,
        )


def _with_grade(p: Principal, grade: int | None) -> Principal:
    return p if grade is None or p.grade == grade else replace(p, grade=grade)
