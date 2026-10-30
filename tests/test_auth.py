"""Identity & access: token verification (§12 checklist), the RBAC policy, the
auth service (bearer, session, launch, grants, disable, caching)."""

from __future__ import annotations

import time

import httpx
import jwt
import pytest
import respx

from lessonforge.auth.directory import MemoryPrincipalDirectory
from lessonforge.auth.policy import PolicyError, RbacPolicy
from lessonforge.auth.principal import Perm
from lessonforge.auth.replay import MemoryReplayGuard
from lessonforge.auth.service import AuthService, SessionCodec, TTLCache
from lessonforge.auth.verifier import AuthError, JwtVerifier, TokenPurpose
from lessonforge.config import AuthConfig, IssuerConfig, RbacConfig
from tests.auth_helpers import ISSUER, JWKS_URL, KID, SESSION_SECRET, jwks, mint, rsa_key


def _auth_cfg(**kw) -> AuthConfig:
    issuer = {"name": "edboox", "issuer": ISSUER, "jwks_url": JWKS_URL,
              "role_map": {"student": "student", "teacher": "teacher", "service": "service"},
              **kw.pop("issuer", {})}
    kw.setdefault("jwks_min_refetch_s", 30)
    return AuthConfig(issuers=[IssuerConfig(**issuer)], session={"secret": SESSION_SECRET}, **kw)


@pytest.fixture
def jwks_route():
    with respx.mock(assert_all_called=False) as mock:
        route = mock.get(JWKS_URL).mock(return_value=httpx.Response(200, json=jwks()))
        yield route


@pytest.fixture
def verifier(jwks_route):
    return JwtVerifier(_auth_cfg())


# ── verifier ─────────────────────────────────────────────────────────────────
def test_valid_token_yields_identity_in_our_vocabulary(verifier):
    vt = verifier.verify(mint("student:42", "student", school="sch-7", grade=8, name="Sita"),
                         TokenPurpose.API)
    assert (vt.issuer, vt.subject, vt.role, vt.tenant_id, vt.grade, vt.display_name) == (
        "edboox", "student:42", "student", "sch-7", 8, "Sita")


@pytest.mark.parametrize("token_kw,code", [
    ({"aud": "someone-else"}, "auth.invalid"),
    ({"iss": "https://evil.test"}, "auth.invalid"),
    ({"ttl": -100}, "auth.expired"),
    ({"iat_offset": -2000, "ttl": 3000}, "auth.expired"),       # older than max_token_age_s
    ({"iat_offset": 600}, "auth.invalid"),                       # issued in the future
    ({"kid": "unknown-kid"}, "auth.invalid"),
    ({"key": rsa_key("attacker")}, "auth.invalid"),              # signed by someone else
])
def test_rejections(verifier, token_kw, code):
    with pytest.raises(AuthError) as err:
        verifier.verify(mint(**token_kw), TokenPurpose.API)
    assert err.value.code == code


def test_alg_none_and_hs256_confusion_are_rejected(verifier):
    unsigned = jwt.encode({"iss": ISSUER, "aud": "lessonforge", "sub": "teacher:1",
                           "iat": int(time.time()), "exp": int(time.time()) + 60,
                           "role": "teacher"}, key=None, algorithm="none")
    with pytest.raises(AuthError):
        verifier.verify(unsigned, TokenPurpose.API)
    # classic confusion: HMAC-sign with the issuer's PUBLIC key as the secret
    pub = jwks()["keys"][0]["n"]
    with pytest.raises(AuthError):
        verifier.verify(mint(key=pub, alg="HS256"), TokenPurpose.API)


def test_missing_required_claims_and_unmapped_role(verifier):
    with pytest.raises(AuthError) as err:
        verifier.verify(mint(role="principal"), TokenPurpose.API)
    assert err.value.code == "perm.denied" and err.value.status == 403   # never a fallback role
    no_sub = jwt.encode({"iss": ISSUER, "aud": "lessonforge", "iat": int(time.time()),
                         "exp": int(time.time()) + 60, "role": "teacher"}, rsa_key(),
                        algorithm="RS256", headers={"kid": KID})
    with pytest.raises(AuthError):
        verifier.verify(no_sub, TokenPurpose.API)


def test_launch_tokens_need_a_jti_and_a_short_age(verifier):
    with pytest.raises(AuthError):
        verifier.verify(mint(jti=None), TokenPurpose.LAUNCH)
    with pytest.raises(AuthError) as err:
        verifier.verify(mint(iat_offset=-300), TokenPurpose.LAUNCH)   # > launch_max_age_s
    assert err.value.code == "auth.expired"
    assert verifier.verify(mint(iat_offset=-300), TokenPurpose.API).subject   # fine for API


def test_unknown_kid_refetch_is_rate_limited(jwks_route, verifier):
    verifier.verify(mint(), TokenPurpose.API)
    assert jwks_route.call_count == 1
    for _ in range(5):                      # a flood of junk kids
        with pytest.raises(AuthError):
            verifier.verify(mint(kid=f"junk-{_}"), TokenPurpose.API)
    assert jwks_route.call_count == 1       # at most one refetch per jwks_min_refetch_s


def test_key_rotation_picks_up_a_new_kid(jwks_route):
    v = JwtVerifier(_auth_cfg(jwks_min_refetch_s=0))
    v.verify(mint(), TokenPurpose.API)
    jwks_route.mock(return_value=httpx.Response(200, json=jwks((KID, rsa_key()),
                                                               ("rotated", rsa_key("rotated")))))
    assert v.verify(mint(kid="rotated", key=rsa_key("rotated")), TokenPurpose.API).subject


def test_jwks_outage_is_503_until_keys_are_known():
    with respx.mock() as mock:
        mock.get(JWKS_URL).mock(side_effect=httpx.ConnectError("down"))
        with pytest.raises(AuthError) as err:
            JwtVerifier(_auth_cfg()).verify(mint(), TokenPurpose.API)
    assert err.value.status == 503 and err.value.code == "upstream.unavailable"


def test_hs256_fallback_issuer():
    cfg = AuthConfig(issuers=[IssuerConfig(name="legacy", issuer=ISSUER, algorithms=["HS256"],
                                           hs256_secret="h" * 40, role_map={"teacher": "teacher"})])
    v = JwtVerifier(cfg)
    assert v.verify(mint(key="h" * 40, alg="HS256"), TokenPurpose.API).issuer == "legacy"
    with pytest.raises(AuthError):        # RS256 not on this issuer's allowlist
        v.verify(mint(), TokenPurpose.API)


def test_issuer_config_validation():
    with pytest.raises(ValueError, match="none"):
        IssuerConfig(name="x", issuer="i", algorithms=["none"], jwks_url="u")
    with pytest.raises(ValueError, match="hs256_secret"):
        IssuerConfig(name="x", issuer="i", algorithms=["HS256"])
    with pytest.raises(ValueError, match="jwks_url"):
        IssuerConfig(name="x", issuer="i", algorithms=["RS256"])


# ── policy ───────────────────────────────────────────────────────────────────
def test_policy_maps_roles_to_permissions():
    policy = RbacPolicy.from_config(RbacConfig(), _auth_cfg())
    assert policy.permissions({"student"}) == {Perm.CHAT}
    assert policy.permissions({"teacher"}) == {Perm.CHAT, Perm.AUTHOR}
    assert policy.permissions({"superadmin"}) == set(Perm)
    assert policy.permissions({"nobody"}) == frozenset()


def test_policy_refuses_unsafe_or_unknown_config():
    with pytest.raises(PolicyError, match="superadmin"):
        RbacPolicy.from_config(RbacConfig(), _auth_cfg(
            issuer={"role_map": {"admin": "superadmin"}}))
    with pytest.raises(PolicyError, match="unknown permission"):
        RbacPolicy.from_config(RbacConfig(roles={"teacher": ["teleport"]}))
    with pytest.raises(PolicyError, match="does not define"):
        RbacPolicy.from_config(RbacConfig(roles={"student": ["chat"]}), _auth_cfg())


# ── service ──────────────────────────────────────────────────────────────────
@pytest.fixture
def auth(jwks_route):
    cfg = _auth_cfg(principal_cache_s=60)
    directory = MemoryPrincipalDirectory()
    return AuthService(cfg, verifier=JwtVerifier(cfg), directory=directory,
                       replay=MemoryReplayGuard(), policy=RbacPolicy.from_config(RbacConfig(), cfg),
                       sessions=SessionCodec(SESSION_SECRET, 60))


def test_bearer_resolves_principal_with_local_grants(auth):
    p = auth.from_bearer(mint("teacher:1", "teacher"))
    assert p.can(Perm.AUTHOR) and not p.can(Perm.CORPUS_MANAGE)
    auth.directory.grant("edboox", "teacher:1", "superadmin", granted_by="test")
    auth.invalidate()
    admin = auth.from_bearer(mint("teacher:1", "teacher"))
    assert admin.is_superadmin and admin.can(Perm.CORPUS_MANAGE) and admin.id == p.id


def test_edboox_can_never_assert_superadmin(auth):
    with pytest.raises(AuthError):
        auth.from_bearer(mint("teacher:9", "superadmin"))


def test_bearer_is_cached_so_requests_dont_write_every_time(auth):
    calls = {"n": 0}
    real = auth.directory.upsert

    def counting(vt):
        calls["n"] += 1
        return real(vt)
    auth.directory.upsert = counting
    for _ in range(5):
        auth.from_bearer(mint("teacher:2", "teacher"))
    assert calls["n"] == 1


def test_launch_is_single_use_and_mints_a_session(auth):
    token = mint("student:5", "student", grade=8)
    result = auth.launch(token)
    assert auth.from_session(result.session).id == result.principal.id
    with pytest.raises(AuthError, match="already used"):
        auth.launch(token)


def test_disabled_principal_is_403_and_bad_sessions_are_401(auth):
    auth.from_bearer(mint("student:6", "student"))
    auth.directory.set_disabled("edboox", "student:6", True)
    auth.invalidate()
    with pytest.raises(AuthError) as err:
        auth.from_bearer(mint("student:6", "student"))
    assert err.value.status == 403
    with pytest.raises(AuthError) as err:
        auth.from_session("not-a-session")
    assert err.value.status == 401
    forged = SessionCodec("x" * 40, 60).issue(auth.from_bearer(mint("teacher:3", "teacher")).id)
    with pytest.raises(AuthError):
        auth.from_session(forged)


def test_session_secret_must_be_strong():
    with pytest.raises(ValueError, match="32"):
        SessionCodec("short", 60)


def test_ttl_cache_expires_and_evicts():
    cache: TTLCache[str, int] = TTLCache(ttl_s=0.05, max_items=2)
    cache.put("a", 1)
    cache.put("b", 2)
    cache.put("c", 3)                     # evicts the least recently used
    assert cache.get("a") is None and cache.get("c") == 3
    time.sleep(0.06)
    assert cache.get("c") is None
