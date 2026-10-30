"""Admin console REST surface (everything except user management, which is admin_api.py).

Every route needs the caller's token to carry app_metadata.role == "admin".

Where the data comes from -- real signals where the platform has them, stored config otherwise:
  models, deployments, integrations, observability : live (inference, domain services, Supabase, NATS, MLflow)
  data-sources, messaging channels, security events: derived from the live alert stream
  audit-log, security events (admin actions)       : audit_log.py, written whenever an admin changes something
  domains, policies, admin settings                : record_store (Supabase), seeded from seed_data.py

Routes are registered both unversioned (what the admin client calls) and under /v1.
"""

import asyncio
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Dict, List, Literal, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator

import admin_api
import audit_log
import record_store
from auth import require_admin
from seed_data import DEFAULT_ADMIN_SETTINGS

AlertsFn = Callable[[int, Optional[str]], Awaitable[List[Dict[str, Any]]]]

router = APIRouter()

_http: Optional[httpx.AsyncClient] = None
_alerts_fn: Optional[AlertsFn] = None
_domain_services: Dict[str, str] = {}
_inference_url = ""
_alerts_subject = "*.alerts.*"
_nats_connected: Callable[[], bool] = lambda: False
_alerts_persisted: Callable[[], bool] = lambda: False
_started = time.time()

_last_sync: Dict[str, str] = {}
_prev_metrics: Dict[str, float] = {}


def configure(
    http: httpx.AsyncClient,
    alerts_fn: AlertsFn,
    domain_services: Dict[str, str],
    inference_url: str,
    alerts_subject: str,
    nats_connected: Callable[[], bool],
    alerts_persisted: Callable[[], bool],
) -> None:
    global _http, _alerts_fn, _domain_services, _inference_url, _alerts_subject, _nats_connected, _alerts_persisted
    _http, _alerts_fn, _domain_services, _inference_url = http, alerts_fn, domain_services, inference_url
    _alerts_subject, _nats_connected, _alerts_persisted = alerts_subject, nats_connected, alerts_persisted


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _parse(ts: str) -> Optional[datetime]:
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


async def _alerts(limit: int = 500, domain: Optional[str] = None) -> List[Dict[str, Any]]:
    assert _alerts_fn is not None, "admin_platform.configure() was not called"
    try:
        return await _alerts_fn(limit, domain)
    except Exception:  # noqa: BLE001 - an unavailable alert store shouldn't break the admin pages
        return []


async def _get_json(url: str, **kwargs: Any) -> Optional[Any]:
    assert _http is not None, "admin_platform.configure() was not called"
    try:
        resp = await _http.get(url, timeout=4.0, **kwargs)
        resp.raise_for_status()
        return resp.json()
    except (httpx.HTTPError, ValueError):
        return None


async def _inference_models() -> List[Dict[str, Any]]:
    data = await _get_json(f"{_inference_url}/models")
    return data if isinstance(data, list) else []


def _model_status(model: Dict[str, Any]) -> str:
    status = str(model.get("status", "")).lower()
    trained = bool(model.get("trained", True))
    if not trained:  # no trained artifact loaded: it can't serve real predictions whatever its status says
        return "offline"
    if status in ("healthy", "ready", "active", "online", "ok"):
        return "healthy"
    if status in ("degraded", "training", "warming", "stale"):
        return "degraded"
    return "offline" if not trained or status in ("offline", "down", "error") else "healthy"


# --- Models -----------------------------------------------------------------------------------


async def list_models(_: Dict[str, Any] = Depends(require_admin)) -> List[Dict[str, Any]]:
    """Live inference registry, plus who owns each model and when its current version first appeared."""
    models = await _inference_models()
    known = {r["id"]: r for r in await record_store.list_all("model-registry")}
    out = []
    for m in models:
        mid, version = m.get("id", ""), m.get("version", "")
        meta = known.get(mid)
        if meta is None or meta.get("version") != version:
            meta = {"id": mid, "version": version, "owner": (meta or {}).get("owner", "ML Platform"), "deployedAt": _iso(_now())}
            await record_store.put("model-registry", mid, meta)
        out.append({
            "id": mid,
            "name": m.get("name", mid),
            "domain": m.get("domain", ""),
            "version": version,
            "status": _model_status(m),
            "owner": meta["owner"],
            "deployedAt": meta["deployedAt"],
        })
    return out


# --- Domains / policies (stored config) -----------------------------------------------------


async def list_domains(_: Dict[str, Any] = Depends(require_admin)) -> List[Dict[str, Any]]:
    domains = await record_store.list_seeded("domains")
    counts: Dict[str, int] = {}
    for m in await _inference_models():
        counts[m.get("domain", "")] = counts.get(m.get("domain", ""), 0) + 1
    return [{**d, "modelCount": counts.get(d["name"], d.get("modelCount", 0))} for d in domains]


async def list_policies(_: Dict[str, Any] = Depends(require_admin)) -> List[Dict[str, Any]]:
    return await record_store.list_seeded("policies")


# --- Alert-stream derived ---------------------------------------------------------------------


async def list_channels(_: Dict[str, Any] = Depends(require_admin)) -> List[Dict[str, Any]]:
    alerts = await _alerts(500)
    cutoff = _now() - timedelta(hours=1)
    per_key: Dict[tuple, int] = {}
    for a in alerts:
        ts = _parse(a.get("timestamp", ""))
        key = (a.get("_subject") or _alerts_subject, a.get("domain", ""))
        per_key.setdefault(key, 0)
        if ts and ts >= cutoff:
            per_key[key] += 1
    for domain in (d["name"] for d in await record_store.list_seeded("domains") if d.get("state") == "active"):
        if not any(k[1] == domain for k in per_key):
            per_key[(_alerts_subject, domain)] = 0
    status = "connected" if _nats_connected() else "down"
    return [
        {"id": f"ch-{domain}-{i}", "subject": subject, "domain": domain,
         "throughputPerMin": round(count / 60, 2), "status": status}
        for i, ((subject, domain), count) in enumerate(sorted(per_key.items(), key=lambda kv: kv[0][1]))
    ]


async def list_data_sources(_: Dict[str, Any] = Depends(require_admin)) -> List[Dict[str, Any]]:
    out = []
    for d in await record_store.list_seeded("domains"):
        if d.get("state") != "active":
            continue
        alerts = await _alerts(500, d["name"])
        stamps = [t for t in (_parse(a.get("timestamp", "")) for a in alerts) if t]
        out.append({
            "id": f"ds-{d['name']}",
            "name": f"{d['name'].title()} alert stream",
            "domain": d["name"],
            "recordCount": len(alerts),
            "lastIngestedAt": _iso(max(stamps)) if stamps else "",
        })
    return out


async def list_security_events(_: Dict[str, Any] = Depends(require_admin)) -> List[Dict[str, Any]]:
    """Recorded admin/security events plus high and critical alerts from the live stream."""
    events = await audit_log.newest(audit_log.SECURITY, 100)
    for a in await _alerts(200):
        if a.get("severity") in ("high", "critical"):
            events.append({
                "id": a.get("id", ""),
                "type": a.get("event_type", "alert"),
                "severity": a["severity"],
                "actor": (a.get("source") or {}).get("service", "system"),
                "occurredAt": a.get("timestamp", ""),
            })
    return sorted(events, key=lambda e: e.get("occurredAt", ""), reverse=True)[:100]


async def list_audit_log(_: Dict[str, Any] = Depends(require_admin)) -> List[Dict[str, Any]]:
    return await audit_log.newest(audit_log.AUDIT)


# --- Live platform state ----------------------------------------------------------------------


async def _service_health() -> Dict[str, Optional[Dict[str, Any]]]:
    names = list(_domain_services)
    results = await asyncio.gather(*(_get_json(f"{_domain_services[n]}/health") for n in names))
    return dict(zip(names, results))


async def list_deployments(_: Dict[str, Any] = Depends(require_admin)) -> List[Dict[str, Any]]:
    health = await _service_health()
    inference = await _get_json(f"{_inference_url}/health")
    env = os.environ.get("ENVIRONMENT", "production")
    rows = [("gateway", {"version": "0.2.0"}), ("inference", inference)] + list(health.items())
    known = {r["id"]: r for r in await record_store.list_all("deployments")}
    out = []
    for name, info in rows:
        if name not in known:  # first time we see a service is the best "deployed at" we have
            known[name] = {"id": name, "firstSeenAt": _iso(_now())}
            await record_store.put("deployments", name, known[name])
        out.append({
            "id": name,
            "service": name,
            "version": str((info or {}).get("version", "unknown")),
            "environment": env,
            "status": "running" if info is not None else "failed",
            "deployedAt": known[name]["firstSeenAt"],
        })
    return out


async def list_integrations(_: Dict[str, Any] = Depends(require_admin)) -> List[Dict[str, Any]]:
    supabase_url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    mlflow = os.environ.get("MLFLOW_TRACKING_URI", "").rstrip("/")

    async def probe(url: str, **kwargs: Any) -> bool:
        return await _get_json(url, **kwargs) is not None

    async def raw_ok(url: str, **kwargs: Any) -> bool:
        assert _http is not None
        try:
            return (await _http.get(url, timeout=4.0, **kwargs)).status_code < 400
        except httpx.HTTPError:
            return False

    sup, inf, mlf = await asyncio.gather(
        probe(f"{supabase_url}/auth/v1/health", headers={"apikey": key}) if supabase_url and key else asyncio.sleep(0, False),
        probe(f"{_inference_url}/health"),
        raw_ok(f"{mlflow}/health") if mlflow else asyncio.sleep(0, False),
    )
    checks = [
        ("int-supabase", "Supabase", "auth + database", sup, bool(supabase_url and key)),
        ("int-nats", "NATS", "message bus", _nats_connected(), True),
        ("int-inference", "Inference service", "model serving", inf, True),
        ("int-mlflow", "MLflow", "model registry", mlf, bool(mlflow)),
    ]
    out = []
    for iid, name, kind, up, configured in checks:
        if up:
            _last_sync[iid] = _iso(_now())
        out.append({
            "id": iid, "name": name, "type": kind,
            "status": "connected" if up else ("error" if configured else "disconnected"),
            "lastSyncAt": _last_sync.get(iid, ""),
        })
    return out


async def list_metrics(_: Dict[str, Any] = Depends(require_admin)) -> List[Dict[str, Any]]:
    health = await _service_health()
    models = await _inference_models()
    alerts = await _alerts(500)
    cutoff = _now() - timedelta(hours=1)
    last_hour = sum(1 for a in alerts if (t := _parse(a.get("timestamp", ""))) and t >= cutoff)
    values = [
        ("domain-services-up", "Domain services up", sum(1 for v in health.values() if v is not None), "services"),
        ("models-healthy", "Healthy models", sum(1 for m in models if _model_status(m) == "healthy"), "models"),
        ("alerts-buffered", "Alerts on record", len(alerts), "alerts"),
        ("alerts-last-hour", "Alerts in the last hour", last_hour, "alerts"),
        ("nats-connected", "Message bus connected", 1 if _nats_connected() else 0, "1 = yes"),
        ("alerts-persisted", "Alerts persisted to database", 1 if _alerts_persisted() else 0, "1 = yes"),
        ("gateway-uptime", "Gateway uptime", round((time.time() - _started) / 60, 1), "min"),
    ]
    out = []
    for mid, name, value, unit in values:
        prev = _prev_metrics.get(mid)
        trend = "flat" if prev is None or prev == value else ("up" if value > prev else "down")
        _prev_metrics[mid] = value
        out.append({"id": mid, "name": name, "value": value, "unit": unit, "trend": trend})
    return out


async def overview(admin: Dict[str, Any] = Depends(require_admin)) -> Dict[str, Any]:
    domains = await record_store.list_seeded("domains")
    models = await _inference_models()
    try:
        users = (await admin_api._call("GET", "/users", params={"per_page": 200})).get("users", [])
        active_users = sum(1 for u in users if u.get("last_sign_in_at") and not u.get("banned_until"))
    except HTTPException:
        active_users = 0
    events = await list_security_events(admin)
    deployments = await list_deployments(admin)
    return {
        "activeDomains": sum(1 for d in domains if d.get("state") == "active"),
        "totalModels": len(models),
        "healthyModels": sum(1 for m in models if _model_status(m) == "healthy"),
        "activeUsers": active_users,
        "openSecurityEvents": sum(1 for e in events if e.get("severity") in ("high", "critical")),
        "runningDeployments": sum(1 for d in deployments if d["status"] == "running"),
    }


# --- System settings --------------------------------------------------------------------------


class SystemSettings(BaseModel):
    platformName: str = Field(min_length=1, max_length=100)
    maintenanceMode: bool
    dataRetentionDays: int = Field(ge=1, le=3650)
    defaultAlertSeverity: Literal["low", "medium", "high", "critical"]

    @field_validator("platformName")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("platformName must not be blank")
        return v


async def get_system_settings(_: Dict[str, Any] = Depends(require_admin)) -> Dict[str, Any]:
    stored = await record_store.get("admin-settings", "settings") or {}
    return {k: stored.get(k, v) for k, v in DEFAULT_ADMIN_SETTINGS.items()}


async def save_system_settings(
    body: SystemSettings, admin: Dict[str, Any] = Depends(require_admin)
) -> Dict[str, Any]:
    before = await get_system_settings(admin)
    after = body.model_dump()
    await record_store.put("admin-settings", "settings", {"id": "settings", **after})
    changed = [k for k in after if after[k] != before.get(k)]
    if changed:
        await audit_log.record(admin, "updated system settings", ", ".join(changed))
        if "maintenanceMode" in changed:
            await audit_log.record_security(
                "maintenance mode " + ("enabled" if after["maintenanceMode"] else "disabled"), "medium", admin
            )
    return after


_ROUTES = [
    ("/admin/overview", overview, ["GET"]),
    ("/domains", list_domains, ["GET"]),
    ("/admin/models", list_models, ["GET"]),
    ("/policies", list_policies, ["GET"]),
    ("/messaging/channels", list_channels, ["GET"]),
    ("/integrations", list_integrations, ["GET"]),
    ("/data-sources", list_data_sources, ["GET"]),
    ("/security/events", list_security_events, ["GET"]),
    ("/observability/metrics", list_metrics, ["GET"]),
    ("/deployments", list_deployments, ["GET"]),
    ("/audit-log", list_audit_log, ["GET"]),
    ("/admin/settings", get_system_settings, ["GET"]),
    ("/admin/settings", save_system_settings, ["PUT"]),
]

for _path, _handler, _methods in _ROUTES:
    router.add_api_route(_path, _handler, methods=_methods)
    router.add_api_route(f"/v1{_path}", _handler, methods=_methods, include_in_schema=False)
