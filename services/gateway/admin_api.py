"""Admin user management for the admin console, backed by the Supabase Admin API.

Requires SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY (server-side only). Every
route needs the caller's token to carry app_metadata.role == "admin".
"""

import os
from typing import Any, Dict, List, Literal, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

import audit_log
from auth import require_admin

router = APIRouter()

_http: Optional[httpx.AsyncClient] = None
_ROLES = ("admin", "analyst", "viewer")
_BAN = "876000h"  # ~100 years; "none" lifts the ban


class CreateUser(BaseModel):
    name: str
    email: str = Field(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    password: str = Field(min_length=8)
    role: Literal["admin", "analyst", "viewer"] = "viewer"


class SetStatus(BaseModel):
    status: Literal["active", "suspended"]


def configure(http: httpx.AsyncClient) -> None:
    global _http
    _http = http


def _admin_request(method: str, path: str, **kwargs: Any) -> Any:
    base = os.environ.get("SUPABASE_URL", "").rstrip("/")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    if not base or not key:
        raise HTTPException(status_code=500, detail="SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY not configured")
    assert _http is not None, "admin_api.configure() was not called"
    return _http.request(
        method,
        f"{base}/auth/v1/admin{path}",
        headers={"apikey": key, "Authorization": f"Bearer {key}"},
        timeout=10.0,
        **kwargs,
    )


def _to_admin_user(u: Dict[str, Any]) -> Dict[str, Any]:
    meta = u.get("user_metadata") or {}
    role = (u.get("app_metadata") or {}).get("role")
    if u.get("banned_until"):
        status = "suspended"
    elif not u.get("last_sign_in_at"):
        status = "invited"
    else:
        status = "active"
    return {
        "id": u["id"],
        "name": meta.get("full_name") or u.get("email", ""),
        "email": u.get("email", ""),
        "role": role if role in _ROLES else "viewer",
        "status": status,
        "client": "app" if meta.get("client") == "app" else "web",
        "lastLoginAt": u.get("last_sign_in_at") or "",
    }


async def _call(method: str, path: str, **kwargs: Any) -> Dict[str, Any]:
    try:
        resp = await _admin_request(method, path, **kwargs)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"supabase unreachable: {exc}") from exc
    if resp.status_code >= 400:
        detail = resp.json().get("msg") if resp.headers.get("content-type", "").startswith("application/json") else resp.text
        raise HTTPException(status_code=resp.status_code, detail=detail or "supabase error")
    return resp.json()


@router.get("/admin/users")
async def list_users(_: Dict[str, Any] = Depends(require_admin)) -> List[Dict[str, Any]]:
    data = await _call("GET", "/users", params={"per_page": 200})
    return [_to_admin_user(u) for u in data.get("users", [])]


@router.post("/admin/users", status_code=201)
async def create_user(body: CreateUser, admin: Dict[str, Any] = Depends(require_admin)) -> Dict[str, Any]:
    user = await _call(
        "POST",
        "/users",
        json={
            "email": body.email,
            "password": body.password,
            "email_confirm": True,
            "user_metadata": {"full_name": body.name, "client": "web"},
            "app_metadata": {"role": body.role},
        },
    )
    await audit_log.record(admin, f"created {body.role} user", body.email)
    return _to_admin_user(user)


@router.post("/admin/users/{user_id}/status")
async def set_user_status(
    user_id: str, body: SetStatus, admin: Dict[str, Any] = Depends(require_admin)
) -> Dict[str, Any]:
    user = await _call(
        "PUT",
        f"/users/{user_id}",
        json={"ban_duration": _BAN if body.status == "suspended" else "none"},
    )
    result = _to_admin_user(user)
    await audit_log.record(admin, f"set user {body.status}", result["email"] or user_id)
    if body.status == "suspended":
        await audit_log.record_security("user suspended", "medium", admin)
    return result
