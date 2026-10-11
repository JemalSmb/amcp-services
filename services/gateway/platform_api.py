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
    help-docs: record_store (Supabase), seeded from seed_data.py on first use.
  - settings / profile: per signed-in user (keyed by the JWT `sub`).

Routes are registered both unversioned (what the client calls today) and
under /v1.
"""

import uuid
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, List, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

import record_store
from auth import require_auth
from seed_data import DEFAULT_SETTINGS

AlertsFn = Callable[[int, Optional[str]], Awaitable[List[Dict[str, Any]]]]

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
    return playbook


# --- Per-user: settings and profile -------------------------------------------


def _user_key(user: Dict[str, Any]) -> str:
    return str(user.get("sub") or "anonymous")


async def get_settings(user: Dict[str, Any] = Depends(require_auth)) -> Dict[str, Any]:
    stored = await record_store.get("settings", "settings", owner=_user_key(user))
    return {k: stored.get(k, v) for k, v in DEFAULT_SETTINGS.items()} if stored else dict(DEFAULT_SETTINGS)


async def save_settings(settings: Dict[str, Any], user: Dict[str, Any] = Depends(require_auth)) -> Dict[str, Any]:
    merged = {**DEFAULT_SETTINGS, **{k: v for k, v in settings.items() if k in DEFAULT_SETTINGS}}
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
    ("/help-docs", _stored_list("help-docs"), ["GET"]),
    ("/profile", get_profile, ["GET"]),
    ("/settings", get_settings, ["GET"]),
    ("/settings", save_settings, ["PUT"]),
]

for _path, _handler, _methods in _ROUTES:
    router.add_api_route(_path, _handler, methods=_methods)
    router.add_api_route(f"/v1{_path}", _handler, methods=_methods, include_in_schema=False)
