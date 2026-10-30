"""Token verification: is this JWT really from a trusted issuer, for us, and fresh?

:class:`TokenVerifier` is the port; :class:`JwtVerifier` the adapter (PyJWT). The
verifier only answers *who* — mapping identity to permissions is the policy's job.

Security properties (docs/EDBOOX_INTEGRATION.md §12):

- the issuer is chosen from config by the token's ``iss``; the signature is then
  checked with THAT issuer's keys and algorithm allowlist — the token's own
  ``alg`` header is never trusted (no ``none``, no RS→HS confusion);
- ``iss``, ``aud``, ``sub``, ``exp`` and ``iat`` are required; ``nbf`` honoured;
  ``iat`` older than the purpose's max age is rejected even if ``exp`` is far off;
- launch tokens must carry a ``jti`` (single use is enforced by the replay guard);
- JWKS keys are cached; an unknown ``kid`` triggers at most one refetch per
  ``jwks_min_refetch_s``, so a flood of junk ``kid``s cannot hammer the issuer.
"""

from __future__ import annotations

import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Any

import httpx
import jwt

from ..config import AuthConfig, IssuerConfig


class TokenPurpose(str, Enum):
    API = "api"          # Authorization: Bearer — ≤ max_token_age_s
    LAUNCH = "launch"    # one-time UI launch — ≤ launch_max_age_s, jti required


class AuthError(Exception):
    """Authentication failed. ``code`` is the stable API error code."""

    def __init__(self, code: str, message: str, *, status: int = 401) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


@dataclass(frozen=True, slots=True)
class VerifiedToken:
    issuer: str                 # OUR configured issuer name ('edboox')
    subject: str
    role: str                   # mapped into our role vocabulary
    tenant_id: str | None
    display_name: str | None
    grade: int | None
    jti: str | None
    expires_at: float
    issued_at: float


class TokenVerifier(ABC):
    @abstractmethod
    def verify(self, token: str, purpose: TokenPurpose) -> VerifiedToken:
        """Return the verified identity or raise :class:`AuthError`."""


class JwksCache:
    """Kid → key for one issuer, with TTL and a rate-limited refetch on a miss."""

    def __init__(self, url: str, *, ttl_s: int, min_refetch_s: int, timeout_s: float = 5.0,
                 http: httpx.Client | None = None) -> None:
        self.url = url
        self.ttl_s = ttl_s
        self.min_refetch_s = min_refetch_s
        self.timeout_s = timeout_s
        self._http = http
        self._keys: dict[str, Any] = {}
        self._fetched_at = 0.0
        self._lock = threading.Lock()

    def key(self, kid: str | None) -> Any:
        with self._lock:
            now = time.monotonic()
            stale = now - self._fetched_at > self.ttl_s
            if stale or (kid not in self._keys and now - self._fetched_at >= self.min_refetch_s):
                self._refresh(now)
            if kid is None and len(self._keys) == 1:
                return next(iter(self._keys.values()))
            if kid not in self._keys:
                raise AuthError("auth.invalid", "token signed with an unknown key")
            return self._keys[kid]

    def _refresh(self, now: float) -> None:
        try:
            client = self._http or httpx.Client(timeout=self.timeout_s)
            try:
                resp = client.get(self.url)
                resp.raise_for_status()
                jwks = jwt.PyJWKSet.from_dict(resp.json())
            finally:
                if self._http is None:
                    client.close()
        except Exception as exc:
            if self._keys:   # keep serving the keys we have; try again later
                self._fetched_at = now
                return
            raise AuthError("upstream.unavailable", f"cannot fetch issuer keys: {exc}",
                            status=503) from exc
        self._keys = {k.key_id: k.key for k in jwks.keys if k.key_id}
        if not self._keys and jwks.keys:   # a single key without a kid
            self._keys = {None: jwks.keys[0].key}  # type: ignore[dict-item]
        self._fetched_at = now


class JwtVerifier(TokenVerifier):
    def __init__(self, cfg: AuthConfig, *, http: httpx.Client | None = None) -> None:
        self.cfg = cfg
        self._issuers: dict[str, IssuerConfig] = {i.issuer: i for i in cfg.issuers}
        self._jwks: dict[str, JwksCache] = {
            i.issuer: JwksCache(i.jwks_url, ttl_s=cfg.jwks_cache_s,
                                min_refetch_s=cfg.jwks_min_refetch_s, http=http)
            for i in cfg.issuers if i.jwks_url
        }

    def verify(self, token: str, purpose: TokenPurpose) -> VerifiedToken:
        issuer = self._issuer_of(token)
        alg = self._header(token).get("alg", "")
        allowed = [a.upper() for a in issuer.algorithms]
        if str(alg).upper() not in allowed:
            raise AuthError("auth.invalid", f"algorithm {alg!r} not allowed for this issuer")
        key = issuer.hs256_secret if alg.upper() == "HS256" else self._jwks[issuer.issuer].key(
            self._header(token).get("kid"))
        try:
            claims = jwt.decode(
                token, key=key, algorithms=allowed, audience=issuer.audience,
                issuer=issuer.issuer, leeway=self.cfg.leeway_s,
                options={"require": ["exp", "iat", "iss", "aud", "sub"]},
            )
        except jwt.ExpiredSignatureError as exc:
            raise AuthError("auth.expired", "token expired") from exc
        except jwt.ImmatureSignatureError as exc:
            raise AuthError("auth.invalid", "token not yet valid") from exc
        except jwt.InvalidTokenError as exc:
            raise AuthError("auth.invalid", f"invalid token: {exc}") from exc

        max_age = (self.cfg.launch_max_age_s if purpose is TokenPurpose.LAUNCH
                   else self.cfg.max_token_age_s)
        issued_at = float(claims["iat"])
        now = time.time()
        if now - issued_at > max_age + self.cfg.leeway_s:
            raise AuthError("auth.expired", "token too old")
        if issued_at - now > self.cfg.leeway_s:
            raise AuthError("auth.invalid", "token issued in the future")
        jti = claims.get("jti")
        if purpose is TokenPurpose.LAUNCH and not jti:
            raise AuthError("auth.invalid", "launch token needs a jti")

        names = issuer.claims
        raw_role = claims.get(names.role)
        role = issuer.role_map.get(str(raw_role)) if raw_role is not None else None
        if role is None:
            raise AuthError("perm.denied", f"role {raw_role!r} is not allowed here", status=403)
        subject = str(claims["sub"]).strip()
        if not subject:
            raise AuthError("auth.invalid", "empty subject")
        return VerifiedToken(
            issuer=issuer.name,
            subject=subject,
            role=role,
            tenant_id=_opt_str(claims.get(names.tenant)),
            display_name=_opt_str(claims.get(names.name)),
            grade=_opt_int(claims.get(names.grade)),
            jti=_opt_str(jti),
            expires_at=float(claims["exp"]),
            issued_at=issued_at,
        )

    # ── helpers ──────────────────────────────────────────────────────────────
    @staticmethod
    def _header(token: str) -> dict[str, Any]:
        try:
            return jwt.get_unverified_header(token)
        except jwt.InvalidTokenError as exc:
            raise AuthError("auth.invalid", "malformed token") from exc

    def _issuer_of(self, token: str) -> IssuerConfig:
        try:
            # routing only: the claims are re-read with full verification below
            unverified = jwt.decode(token, options={"verify_signature": False})
        except jwt.InvalidTokenError as exc:
            raise AuthError("auth.invalid", "malformed token") from exc
        issuer = self._issuers.get(str(unverified.get("iss", "")))
        if issuer is None:
            raise AuthError("auth.invalid", "untrusted issuer")
        return issuer


def _opt_str(v: Any) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _opt_int(v: Any) -> int | None:
    try:
        return int(v) if v is not None and str(v).strip() != "" else None
    except (TypeError, ValueError):
        return None
