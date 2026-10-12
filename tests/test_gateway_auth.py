"""JWT verification: HS256 path, asymmetric path, admin role gate."""

import time

import jwt
import pytest
from fastapi import HTTPException

import auth

SECRET = "s3cret-s3cret-s3cret-s3cret-0123456789"


@pytest.fixture(autouse=True)
def enforced(monkeypatch):
    monkeypatch.setattr(auth, "AUTH_DISABLED", False)
    monkeypatch.setattr(auth, "SUPABASE_JWT_SECRET", SECRET)


def token(secret=SECRET, aud="authenticated", exp=3600, **claims):
    body = {"sub": "u1", "email": "a@b.c", "role": "authenticated", "aud": aud, "exp": int(time.time()) + exp, **claims}
    return jwt.encode(body, secret, algorithm="HS256")


async def test_disabled_auth_bypasses(monkeypatch):
    monkeypatch.setattr(auth, "AUTH_DISABLED", True)
    assert (await auth.require_auth(None))["sub"] == "dev-bypass"


@pytest.mark.parametrize("header", [None, "", "Basic abc", "Bearer"])
async def test_missing_or_malformed_header_401(header):
    with pytest.raises(HTTPException) as e:
        await auth.require_auth(header)
    assert e.value.status_code == 401


async def test_valid_token_returns_claims():
    user = await auth.require_auth("Bearer " + token(app_metadata={"role": "admin"}))
    assert user == {"sub": "u1", "email": "a@b.c", "role": "authenticated", "app_role": "admin"}


async def test_bearer_scheme_case_insensitive():
    assert (await auth.require_auth("bearer " + token()))["sub"] == "u1"


@pytest.mark.parametrize("bad", [
    lambda: token(exp=-10),                 # expired
    lambda: token(secret="x" * 40),         # wrong signature
    lambda: token(aud="anon"),              # wrong audience
    lambda: token()[:-3] + "abc",           # tampered
    lambda: "garbage",
])
async def test_invalid_tokens_401(bad):
    with pytest.raises(HTTPException) as e:
        await auth.require_auth("Bearer " + bad())
    assert e.value.status_code == 401


async def test_hs256_without_secret_is_500(monkeypatch):
    monkeypatch.setattr(auth, "SUPABASE_JWT_SECRET", "")
    with pytest.raises(HTTPException) as e:
        await auth.require_auth("Bearer " + token())
    assert e.value.status_code == 500


async def test_alg_none_rejected():
    t = jwt.encode({"sub": "u", "aud": "authenticated"}, None, algorithm="none")
    with pytest.raises(HTTPException) as e:
        await auth.require_auth("Bearer " + t)
    assert e.value.status_code == 401


async def test_asymmetric_token_verified_via_jwks(monkeypatch):
    from cryptography.hazmat.primitives.asymmetric import ec

    key = ec.generate_private_key(ec.SECP256R1())
    t = jwt.encode({"sub": "u2", "aud": "authenticated", "exp": int(time.time()) + 60}, key, algorithm="ES256")

    class Signing:
        def __init__(self, k): self.key = k

    class Client:
        def get_signing_key_from_jwt(self, tok): return Signing(key.public_key())

    monkeypatch.setattr(auth, "_get_jwks_client", lambda: Client())
    assert (await auth.require_auth("Bearer " + t))["sub"] == "u2"


async def test_asymmetric_without_supabase_url_is_500(monkeypatch):
    from cryptography.hazmat.primitives.asymmetric import ec

    monkeypatch.setattr(auth, "SUPABASE_URL", "")
    monkeypatch.setattr(auth, "_jwks_client", None)
    t = jwt.encode({"sub": "u"}, ec.generate_private_key(ec.SECP256R1()), algorithm="ES256")
    with pytest.raises(HTTPException) as e:
        await auth.require_auth("Bearer " + t)
    assert e.value.status_code == 500


async def test_require_admin():
    ok = {"sub": "u", "app_role": "admin"}
    assert await auth.require_admin(ok) == ok
    with pytest.raises(HTTPException) as e:
        await auth.require_admin({"sub": "u", "app_role": "analyst"})
    assert e.value.status_code == 403
