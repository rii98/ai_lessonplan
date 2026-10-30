"""Real tokens for auth tests: an RSA issuer key, a JWKS document, and a minting
helper — so tests exercise genuine RS256 verification, never an auth bypass."""

from __future__ import annotations

import time
import uuid
from functools import lru_cache
from typing import Any

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

from lessonforge.config import Settings

ISSUER = "https://edboox.test"
JWKS_URL = "https://edboox.test/.well-known/jwks.json"
LOGIN_URL = "https://edboox.test/login"
KID = "test-key-1"
SESSION_SECRET = "s" * 48


@lru_cache(maxsize=4)
def rsa_key(name: str = "main") -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def jwks(*keys: tuple[str, rsa.RSAPrivateKey]) -> dict[str, Any]:
    keys = keys or ((KID, rsa_key()),)
    out = []
    for kid, key in keys:
        pub = jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key(), as_dict=True)
        out.append({**pub, "kid": kid, "use": "sig", "alg": "RS256"})
    return {"keys": out}


def mint(sub: str = "teacher:7", role: str | None = "teacher", *, school: str | None = "sch-7",
         grade: int | None = None, name: str | None = "Test User", iss: str = ISSUER,
         aud: str = "lessonforge", ttl: int = 600, iat_offset: int = 0, jti: str | None = "auto",
         kid: str = KID, key: Any = None, alg: str = "RS256", **extra: Any) -> str:
    now = int(time.time()) + iat_offset
    claims: dict[str, Any] = {"iss": iss, "aud": aud, "sub": sub, "iat": now, "exp": now + ttl}
    if role is not None:
        claims["role"] = role
    if school is not None:
        claims["school_id"] = school
    if grade is not None:
        claims["grade"] = grade
    if name is not None:
        claims["name"] = name
    if jti is not None:
        claims["jti"] = uuid.uuid4().hex if jti == "auto" else jti
    claims.update(extra)
    signing_key = key if key is not None else rsa_key()
    return jwt.encode(claims, signing_key, algorithm=alg, headers={"kid": kid})


def auth_settings(base: dict[str, Any], **overrides: Any) -> Settings:
    """Settings with a real issuer configured (memory persistence, claims context)."""
    cfg = {
        **base,
        "auth": {
            "issuers": [{"name": "edboox", "issuer": ISSUER, "jwks_url": JWKS_URL,
                         "audience": "lessonforge", "algorithms": ["RS256"],
                         "role_map": {"student": "student", "teacher": "teacher",
                                      "service": "service"}}],
            "login_url": LOGIN_URL, "jwks_min_refetch_s": 30,
            "session": {"secret": SESSION_SECRET, "secure": False},
        },
        "context": {"provider": "claims"},
    }
    cfg.update(overrides)
    return Settings(**cfg)
