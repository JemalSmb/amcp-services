"""
Dashboard-facing REST surface (predictions, detections, incidents, assets,
automations, ...) that the web client's PlatformApiService calls.

Responses use the UI's camelCase models directly (see the web-client's
core/models/platform.models.ts), so the client needs no mapping.

Where the data comes from:
  - predictions / detections / incidents: derived from the live alert stream
    (the same alerts served by /v1/alerts). Manual predictions made through
    POST /models/{id}/predict are stored too and merged into /predictions.
  - automations / investigations / assets / reports / notifications /
    help-docs: Markdown files in the private Supabase Storage bucket
    HELP_DOCS_SUPABASE_BUCKET/HELP_DOCS_SUPABASE_PREFIX (uploaded by
    amcp-platform-clients/scripts/sync-help-docs-to-supabase.mjs), read here with
    the service role key -- the same server-side pattern as the model artifacts.
    Falls back to the seeded record_store list if Storage isn't configured.
  - settings / profile: per signed-in user (keyed by the JWT `sub`).

Routes are registered both unversioned (what the client calls today) and
under /v1.
"""

import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from zoneinfo import available_timezones
from typing import Any, Awaitable, Callable, Dict, List, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

import audit_log
import record_store
from auth import require_auth
from seed_data import DEFAULT_SETTINGS

AlertsFn = Callable[[int, Optional[str]], Awaitable[List[Dict[str, Any]]]]

logger = logging.getLogger(__name__)

router = APIRouter()

_alerts_fn: Optional[AlertsFn] = None
_http: Optional[httpx.AsyncClient] = None
_inference_url = ""

_SEVERITIES = ("low", "medium", "high", "critical")
_MAX_STORED_PREDICTIONS = 200


def configure(alerts_fn: AlertsFn, http: httpx.AsyncClient, inference_url: str) -> None:
    global _alerts_fn, _http, _inference_url
    _alerts_fn, _http, _inference_url = alerts_fn, http, inference_url


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _alerts(limit: int = 200) -> List[Dict[str, Any]]:
    assert _alerts_fn is not None, "platform_api.configure() was not called"
    return await _alerts_fn(limit, None)


def _model_id(alert: Dict[str, Any]) -> str:
    payload = alert.get("payload") or {}
    source = alert.get("source") or {}
    return payload.get("model_id") or source.get("model_id") or source.get("service") or ""


def _score(alert: Dict[str, Any]) -> float:
    try:
        return float((alert.get("payload") or {}).get("score", 0.0))
    except (TypeError, ValueError):
        return 0.0


def _label(alert: Dict[str, Any]) -> str:
    return str((alert.get("payload") or {}).get("label", alert.get("event_type", "alert")))


def _severity(alert: Dict[str, Any]) -> str:
    sev = str(alert.get("severity", "low")).lower()
    return sev if sev in _SEVERITIES else "low"


def _prediction_from_alert(alert: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": alert.get("id", ""),
        "modelId": _model_id(alert),
        "domain": alert.get("domain", ""),
        "score": _score(alert),
        "label": _label(alert),
        "createdAt": alert.get("timestamp", ""),
    }


# --- Derived from alerts ------------------------------------------------------


async def predictions(user: Dict[str, Any] = Depends(require_auth)) -> List[Dict[str, Any]]:
    derived = [_prediction_from_alert(a) for a in await _alerts()]
    manual = await record_store.list_all("predictions")
    return sorted(derived + manual, key=lambda p: p.get("createdAt", ""), reverse=True)[:200]


async def detections(user: Dict[str, Any] = Depends(require_auth)) -> List[Dict[str, Any]]:
    return [
        {
            "id": f"de-{a.get('id', '')}",
            "modelId": _model_id(a),
            "domain": a.get("domain", ""),
            "signal": _label(a),
            "confidence": _score(a),
            "detectedAt": a.get("timestamp", ""),
        }
        for a in await _alerts()
    ]


async def incidents(user: Dict[str, Any] = Depends(require_auth)) -> List[Dict[str, Any]]:
    """One open incident per high/critical alert."""
    return [
        {
            "id": f"in-{a.get('id', '')}",
            "title": f"{_label(a)} ({_model_id(a) or a.get('domain', '')})",
            "domain": a.get("domain", ""),
            "severity": _severity(a),
            "status": "open",
            "assignee": "Unassigned",
            "openedAt": a.get("timestamp", ""),
            "linkedAlertId": a.get("id", ""),
        }
        for a in await _alerts()
        if _severity(a) in ("high", "critical")
    ]


# --- Manual prediction (proxied to inference) ---------------------------------


async def predict(model_id: str, payload: Dict[str, Any], user: Dict[str, Any] = Depends(require_auth)) -> Dict[str, Any]:
    assert _http is not None
    # The UI sends the feature dict itself; inference expects {"features": {...}}.
    features = payload.get("features", payload)
    try:
        response = await _http.post(f"{_inference_url}/models/{model_id}/predict", json={"features": features})
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"inference service unreachable: {exc}") from exc
    if response.status_code == 404:
        raise HTTPException(status_code=404, detail=f"unknown model '{model_id}'")
    if response.status_code >= 400:
        raise HTTPException(status_code=response.status_code, detail=response.text)
    body = response.json()
    result = {
        "id": f"pr-{uuid.uuid4().hex[:12]}",
        "modelId": body.get("model_id", model_id),
        "domain": body.get("domain", ""),
        "score": body.get("score", 0.0),
        "label": body.get("label", ""),
        "createdAt": _now(),
    }
    await record_store.put("predictions", result["id"], result)
    return result


# --- Stored collections -------------------------------------------------------


def _stored_list(collection: str) -> Callable[..., Awaitable[List[Dict[str, Any]]]]:
    async def handler(user: Dict[str, Any] = Depends(require_auth)) -> List[Dict[str, Any]]:
        return await record_store.list_seeded(collection)

    handler.__name__ = f"list_{collection.replace('-', '_')}"
    return handler


# --- Help docs (Supabase Storage, served through the API) ---------------------


def _docs_object_url(file_name: str) -> str:
    base = os.environ.get("SUPABASE_URL", "").rstrip("/")
    bucket = os.environ.get("HELP_DOCS_SUPABASE_BUCKET", "client-docs").strip("/")
    prefix = os.environ.get("HELP_DOCS_SUPABASE_PREFIX", "help").strip("/")
    path = f"{prefix}/{file_name}" if prefix else file_name
    return f"{base}/storage/v1/object/{bucket}/{path}"


async def _fetch_doc_object(file_name: str) -> Optional[bytes]:
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    if not (os.environ.get("SUPABASE_URL") and key and _http is not None):
        return None
    try:
        resp = await _http.get(
            _docs_object_url(file_name), headers={"Authorization": f"Bearer {key}", "apikey": key}, timeout=15
        )
        resp.raise_for_status()
        return resp.content
    except Exception as exc:  # noqa: BLE001 - caller falls back / 404s
        logger.warning("could not fetch help doc '%s' from Supabase Storage: %s", file_name, exc)
        return None


async def _help_manifest() -> Optional[List[Dict[str, Any]]]:
    raw = await _fetch_doc_object("manifest.json")
    if raw is None:
        return None
    try:
        entries = json.loads(raw)
    except ValueError:
        return None
    return entries if isinstance(entries, list) else None


async def list_help_docs(user: Dict[str, Any] = Depends(require_auth)) -> List[Dict[str, Any]]:
    manifest = await _help_manifest()
    if manifest is None:
        return await record_store.list_seeded("help-docs")
    return [
        {
            "id": e["id"],
            "title": e.get("title", ""),
            "category": e.get("category", ""),
            "summary": e.get("summary", ""),
            "url": f"/help-docs/{e['id']}",
            "source": "api",
        }
        for e in manifest
        if isinstance(e, dict) and e.get("id")
    ]


async def get_help_doc(doc_id: str, user: Dict[str, Any] = Depends(require_auth)) -> Dict[str, Any]:
    manifest = await _help_manifest() or []
    entry = next((e for e in manifest if isinstance(e, dict) and e.get("id") == doc_id), None)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"unknown help doc '{doc_id}'")
    # fileName comes from our own manifest, but never allow path traversal out of the prefix.
    file_name = os.path.basename(str(entry.get("fileName", "")))
    raw = await _fetch_doc_object(file_name) if file_name else None
    if raw is None:
        raise HTTPException(status_code=404, detail=f"help doc '{doc_id}' content unavailable")
    return {
        "id": entry["id"],
        "title": entry.get("title", ""),
        "category": entry.get("category", ""),
        "summary": entry.get("summary", ""),
        "url": f"/help-docs/{entry['id']}",
        "source": "api",
        "content": raw.decode("utf-8", errors="replace"),
    }


class AutomationStatusBody(BaseModel):
    status: str


async def toggle_automation(
    automation_id: str, body: AutomationStatusBody, user: Dict[str, Any] = Depends(require_auth)
) -> Dict[str, Any]:
    if body.status not in ("enabled", "disabled"):
        raise HTTPException(status_code=422, detail="status must be 'enabled' or 'disabled'")
    await record_store.list_seeded("automations")
    playbook = await record_store.get("automations", automation_id)
    if playbook is None:
        raise HTTPException(status_code=404, detail=f"unknown automation '{automation_id}'")
    playbook["status"] = body.status
    await record_store.put("automations", automation_id, playbook)
    await audit_log.record(user, f"set automation {body.status}", playbook.get("name", automation_id))
    return playbook


# --- Per-user: settings and profile -------------------------------------------


def _user_key(user: Dict[str, Any]) -> str:
    return str(user.get("sub") or "anonymous")


async def get_settings(user: Dict[str, Any] = Depends(require_auth)) -> Dict[str, Any]:
    stored = await record_store.get("settings", "settings", owner=_user_key(user))
    return {k: stored.get(k, v) for k, v in DEFAULT_SETTINGS.items()} if stored else dict(DEFAULT_SETTINGS)


_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
_SETTING_CHOICES = {
    "theme": ("light", "dark", "system"),
    "emailDigest": ("off", "daily", "weekly"),
    "alertSeverityThreshold": _SEVERITIES,
}
_BOOL_SETTINGS = ("emailAlerts", "browserNotifications")


def _validate_settings(incoming: Dict[str, Any]) -> Dict[str, Any]:
    """Keep only known keys and reject bad values with a 422 naming the field."""
    clean = {k: v for k, v in incoming.items() if k in DEFAULT_SETTINGS}
    for key, choices in _SETTING_CHOICES.items():
        if key in clean and clean[key] not in choices:
            raise HTTPException(status_code=422, detail=f"{key} must be one of {', '.join(choices)}")
    for key in _BOOL_SETTINGS:
        if key in clean and not isinstance(clean[key], bool):
            raise HTTPException(status_code=422, detail=f"{key} must be true or false")
    if "refreshIntervalSeconds" in clean:
        value = clean["refreshIntervalSeconds"]
        if isinstance(value, bool) or not isinstance(value, int) or not 5 <= value <= 3600:
            raise HTTPException(status_code=422, detail="refreshIntervalSeconds must be between 5 and 3600")
    if "notificationEmail" in clean:
        email = clean["notificationEmail"]
        if not isinstance(email, str) or len(email) > 254 or (email.strip() and not _EMAIL_RE.match(email.strip())):
            raise HTTPException(status_code=422, detail="notificationEmail must be a valid email address")
        clean["notificationEmail"] = email.strip()
    return clean


async def save_settings(settings: Dict[str, Any], user: Dict[str, Any] = Depends(require_auth)) -> Dict[str, Any]:
    merged = {**DEFAULT_SETTINGS, **_validate_settings(settings)}
    # `id` is required by record_store.get(); it is not part of AppSettings, so drop it on the way out.
    await record_store.put("settings", "settings", {"id": "settings", **merged}, owner=_user_key(user))
    return merged


async def get_profile(user: Dict[str, Any] = Depends(require_auth)) -> Dict[str, Any]:
    email = user.get("email") or ""
    stored = await record_store.get("profile", "profile", owner=_user_key(user)) or {}
    return {
        "id": _user_key(user),
        "name": stored.get("name") or (email.split("@")[0] if email else "User"),
        "email": email,
        "role": stored.get("role") or "Analyst",
        "timezone": stored.get("timezone") or "UTC",
    }


class ProfileUpdateBody(BaseModel):
    name: str
    timezone: str


async def update_profile(body: ProfileUpdateBody, user: Dict[str, Any] = Depends(require_auth)) -> Dict[str, Any]:
    name = body.name.strip()
    if not name or len(name) > 100:
        raise HTTPException(status_code=422, detail="name must be 1-100 characters")
    if body.timezone != "UTC" and body.timezone not in available_timezones():
        raise HTTPException(status_code=422, detail="timezone must be a valid IANA timezone, e.g. Europe/Lisbon")
    stored = await record_store.get("profile", "profile", owner=_user_key(user)) or {}
    # Role is assigned by an admin, never by the user -- only name and timezone are editable here.
    await record_store.put(
        "profile",
        "profile",
        {**stored, "id": "profile", "name": name, "timezone": body.timezone},
        owner=_user_key(user),
    )
    return await get_profile(user)


# --- Route table (unversioned + /v1) ------------------------------------------

_ROUTES = [
    ("/predictions", predictions, ["GET"]),
    ("/detections", detections, ["GET"]),
    ("/incidents", incidents, ["GET"]),
    ("/models/{model_id}/predict", predict, ["POST"]),
    ("/investigations", _stored_list("investigations"), ["GET"]),
    ("/assets", _stored_list("assets"), ["GET"]),
    ("/automations", _stored_list("automations"), ["GET"]),
    ("/automations/{automation_id}/status", toggle_automation, ["POST"]),
    ("/reports", _stored_list("reports"), ["GET"]),
    ("/notifications", _stored_list("notifications"), ["GET"]),
    ("/help-docs", list_help_docs, ["GET"]),
    ("/help-docs/{doc_id}", get_help_doc, ["GET"]),
    ("/profile", get_profile, ["GET"]),
    ("/profile", update_profile, ["PUT"]),
    ("/settings", get_settings, ["GET"]),
    ("/settings", save_settings, ["PUT"]),
]

for _path, _handler, _methods in _ROUTES:
    router.add_api_route(_path, _handler, methods=_methods)
    router.add_api_route(f"/v1{_path}", _handler, methods=_methods, include_in_schema=False)
