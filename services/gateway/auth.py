"""
Auth dependency for the gateway.

Verifies Supabase-issued JWTs on incoming requests. Projects migrated to
JWT Signing Keys issue asymmetric tokens (ES256/RS256), verified against the
project's public JWKS endpoint; legacy projects issue HS256 tokens, verified
with SUPABASE_JWT_SECRET. The token's own `alg` header picks the path. Supabase Auth (signup/login/refresh) is the identity
provider; this module only *verifies* the tokens it issues -- no password
handling happens here.

Setup:
    1. Create a Supabase project: https://supabase.com/dashboard
    2. Project Settings -> API -> copy "Project URL". Only legacy (HS256)
       projects also need the "JWT Secret" (legacy JWT secret, not the
       anon/service keys); signing-keys projects need nothing else.
    3. Put them in .env (see .env.example in this repo's root):
         SUPABASE_URL=...
         SUPABASE_JWT_SECRET=...   # legacy HS256 only
    4. Clients (web/admin/desktop) authenticate directly against Supabase
       Auth and attach the resulting access token as:
         Authorization: Bearer <token>
       on every request to this gateway.
"""

import os
import time
from typing import Any, Dict, Optional

import jwt
from dotenv import load_dotenv
from fastapi import Depends, Header, HTTPException, Request

load_dotenv()  # picks up amcp-platform-services/.env when running outside Docker

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_JWT_SECRET = os.environ.get("SUPABASE_JWT_SECRET", "")

# Set to "1" locally to bypass verification while SUPABASE_* isn't
# configured yet -- keeps existing endpoints working during setup.
AUTH_DISABLED = os.environ.get("AUTH_DISABLED", "1") == "1"


_ASYMMETRIC_ALGS = ["ES256", "RS256", "EdDSA"]
_jwks_client: Optional[jwt.PyJWKClient] = None


def _get_jwks_client() -> jwt.PyJWKClient:
    global _jwks_client
    if _jwks_client is None:
        if not SUPABASE_URL:
            raise HTTPException(status_code=500, detail="SUPABASE_URL is not configured")
        # PyJWKClient caches keys, so this is one network fetch, not one per request.
        _jwks_client = jwt.PyJWKClient(f"{SUPABASE_URL.rstrip('/')}/auth/v1/.well-known/jwks.json")
    return _jwks_client


def _decode(token: str) -> Dict[str, Any]:
    try:
        alg = jwt.get_unverified_header(token).get("alg")
        if alg == "HS256":
            if not SUPABASE_JWT_SECRET:
                raise HTTPException(status_code=500, detail="SUPABASE_JWT_SECRET is not configured")
            return jwt.decode(token, SUPABASE_JWT_SECRET, algorithms=["HS256"], audience="authenticated")
        if alg in _ASYMMETRIC_ALGS:
            key = _get_jwks_client().get_signing_key_from_jwt(token).key
            return jwt.decode(token, key, algorithms=_ASYMMETRIC_ALGS, audience="authenticated")
        raise HTTPException(status_code=401, detail=f"unsupported token alg: {alg}")
    except jwt.PyJWTError as exc:
        raise HTTPException(status_code=401, detail=f"invalid token: {exc}") from exc


async def require_auth(authorization: Optional[str] = Header(default=None)) -> Dict[str, Any]:
    """FastAPI dependency: `Depends(require_auth)` on any route that needs a signed-in user."""
    if AUTH_DISABLED:
        return {"sub": "dev-bypass", "email": "dev@local"}

    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")

    token = authorization.split(" ", 1)[1]
    claims = _decode(token)
    return {
        "sub": claims.get("sub"),
        "email": claims.get("email"),
        "role": claims.get("role"),
        "app_role": (claims.get("app_metadata") or {}).get("role"),
    }


_denied_recently: Dict[str, float] = {}
_DENIED_LOG_INTERVAL = 60.0  # one security event per actor+route per minute, so a retry loop can't flood the log


async def _log_denied(user: Dict[str, Any], path: str) -> None:
    """Record a non-admin hitting an admin route. Never lets a logging problem change the 403."""
    key = f"{user.get('sub')}:{path}"
    now = time.monotonic()
    if now - _denied_recently.get(key, -_DENIED_LOG_INTERVAL) < _DENIED_LOG_INTERVAL:
        return
    _denied_recently[key] = now
    try:
        import audit_log  # local import: audit_log -> record_store -> alert_store, none of which import auth

        await audit_log.record_security(f"admin access denied: {path}", "high", user)
    except Exception:  # noqa: BLE001
        pass


async def require_admin(
    user: Dict[str, Any] = Depends(require_auth), request: Request = None  # type: ignore[assignment]
) -> Dict[str, Any]:
    """FastAPI dependency: only accounts with app_metadata.role == "admin" (set server-side, not user-editable)."""
    if AUTH_DISABLED:
        return user
    if user.get("app_role") != "admin":
        if request is not None:  # None when called directly (e.g. unit tests)
            await _log_denied(user, request.url.path)
        raise HTTPException(status_code=403, detail="admin role required")
    return user
