"""
AMCP Backend Gateway

Aggregation API in front of the inference service and the domain
microservices, so a single dashboard can get a unified view instead of
polling each service directly.

Endpoints (all under /v1; unversioned aliases kept for back-compat, see
bottom of file):
    GET  /v1/health            -- gateway's own liveness (open)
    GET  /v1/models            -- proxies inference service's /models (open)
    GET  /v1/domains/status    -- health-checks each configured domain service (auth required)
    GET  /v1/alerts            -- recent alerts, DB-backed with in-memory fallback (auth required)
    POST /v1/domains/{name}/trigger -- relays a manual trigger to a domain service (auth required)

The dashboard's remaining endpoints (predictions, detections, incidents,
assets, automations, reports, notifications, profile, settings, help-docs and
POST /models/{id}/predict) live in platform_api.py.

Alerts are collected by subscribing to `*.alerts.*` on NATS, written to
Supabase Postgres (alert_store.py) as they arrive, and also kept in a
bounded in-memory buffer. /alerts reads from the DB first and falls back
to the in-memory buffer if SUPABASE_DB_URL isn't configured or the DB is
unreachable -- same degrade-gracefully pattern used elsewhere in this repo.
"""

import asyncio
import json
import logging
import os
from collections import deque
from typing import Any, Deque, Dict, List, Optional

import httpx
from dotenv import load_dotenv
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

import admin_api
import alert_store
import platform_api
import record_store
from auth import require_auth

load_dotenv()  # picks up amcp-platform-services/.env when running outside Docker

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

INFERENCE_URL = os.environ.get("INFERENCE_URL", "http://localhost:8000")
NATS_URL = os.environ.get("NATS_URL", "nats://localhost:4222")
ALERTS_SUBJECT = os.environ.get("ALERTS_SUBJECT", "*.alerts.*")
ALERTS_BUFFER_SIZE = int(os.environ.get("ALERTS_BUFFER_SIZE", "500"))

# Comma-separated list of allowed browser origins. Defaults to localhost dev
# ports; set to your real Vercel domain(s) before deploying, e.g.:
#   ALLOWED_ORIGINS=https://amcp-web-client.vercel.app,https://amcp-admin.vercel.app
ALLOWED_ORIGINS = [
    origin.strip()
    for origin in os.environ.get("ALLOWED_ORIGINS", "http://localhost:4200,http://localhost:4201").split(",")
    if origin.strip()
]

# Domain services this gateway knows how to health-check / relay to.
# Keyed by the name a dashboard would refer to the service as.
DOMAIN_SERVICES: Dict[str, str] = {
    "threat-detection": os.environ.get("THREAT_DETECTION_URL", "http://localhost:8001"),
    "phishing-detector": os.environ.get("PHISHING_DETECTOR_URL", "http://localhost:8003"),
    "fraud-detector": os.environ.get("FRAUD_DETECTOR_URL", "http://localhost:8011"),
    "process-anomaly-detector": os.environ.get("PROCESS_ANOMALY_DETECTOR_URL", "http://localhost:8021"),
}

limiter = Limiter(key_func=get_remote_address, default_limits=["60/minute"])

app = FastAPI(title="AMCP Backend Gateway", version="0.2.0")
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=True,
)

_http = httpx.AsyncClient(timeout=5.0)
_nats_client = None
_alerts: Deque[Dict[str, Any]] = deque(maxlen=ALERTS_BUFFER_SIZE)

v1 = APIRouter(prefix="/v1")


class TriggerRequest(BaseModel):
    features: Dict[str, float]
    source: Optional[Dict[str, str]] = None


@v1.get("/health")
async def health() -> Dict[str, Any]:
    return {
        "service": "gateway",
        "status": "ok",
        "environment": os.environ.get("ENVIRONMENT", "development"),
        "nats_connected": _nats_client is not None,
        "alerts_persisted": alert_store._pool is not None,
        "domains_configured": list(DOMAIN_SERVICES.keys()),
    }


@v1.get("/models")
@limiter.limit("30/minute")
async def list_models(request: Request) -> List[Dict[str, Any]]:
    try:
        response = await _http.get(f"{INFERENCE_URL}/models")
        response.raise_for_status()
        return response.json()
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"inference service unreachable: {exc}") from exc


@v1.get("/domains/status")
@limiter.limit("30/minute")
async def domains_status(request: Request, user: Dict[str, Any] = Depends(require_auth)) -> List[Dict[str, Any]]:
    async def check(name: str, base_url: str) -> Dict[str, Any]:
        try:
            response = await _http.get(f"{base_url}/health")
            response.raise_for_status()
            return {"name": name, "url": base_url, "status": "up", "detail": response.json()}
        except httpx.HTTPError as exc:
            return {"name": name, "url": base_url, "status": "down", "detail": str(exc)}

    results = await asyncio.gather(*(check(name, url) for name, url in DOMAIN_SERVICES.items()))
    return list(results)


async def _recent_alerts(limit: int, domain: Optional[str]) -> List[Dict[str, Any]]:
    persisted = await alert_store.recent(limit=limit, domain=domain)
    if persisted is not None:
        return persisted

    items = list(_alerts)
    if domain:
        items = [a for a in items if a.get("domain") == domain]
    return items[-limit:][::-1]


@v1.get("/alerts")
@limiter.limit("60/minute")
async def list_alerts(
    request: Request, limit: int = 100, domain: Optional[str] = None, user: Dict[str, Any] = Depends(require_auth)
) -> List[Dict[str, Any]]:
    return await _recent_alerts(limit, domain)


@v1.post("/domains/{name}/trigger")
@limiter.limit("10/minute")
async def trigger_domain(
    request: Request, name: str, body: TriggerRequest, user: Dict[str, Any] = Depends(require_auth)
) -> Dict[str, Any]:
    base_url = DOMAIN_SERVICES.get(name)
    if base_url is None:
        raise HTTPException(status_code=404, detail=f"unknown domain service '{name}'")
    try:
        response = await _http.post(f"{base_url}/trigger", json=body.model_dump())
        response.raise_for_status()
        return response.json()
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"domain service '{name}' unreachable: {exc}") from exc


app.include_router(v1)

# --- Unversioned aliases (deprecated) --------------------------------------
# Kept so anything still calling the pre-/v1 paths (older client builds)
# doesn't break outright. Remove once every client is confirmed on /v1.
legacy = APIRouter()
legacy.add_api_route("/health", health, methods=["GET"])
legacy.add_api_route("/models", list_models, methods=["GET"])
legacy.add_api_route("/domains/status", domains_status, methods=["GET"])
legacy.add_api_route("/alerts", list_alerts, methods=["GET"])
legacy.add_api_route("/domains/{name}/trigger", trigger_domain, methods=["POST"])
app.include_router(legacy)

# Dashboard REST surface: predictions, incidents, automations, settings, ... (platform_api.py)
platform_api.configure(_recent_alerts, _http, INFERENCE_URL)
app.include_router(platform_api.router)

# Admin user management (admin_api.py): admin role required.
admin_api.configure(_http)
app.include_router(admin_api.router)
app.include_router(admin_api.router, prefix="/v1")


async def _connect_nats() -> None:
    global _nats_client
    try:
        import nats
    except ImportError:
        logger.warning("nats-py not installed; /alerts will stay empty")
        return
    try:
        _nats_client = await nats.connect(NATS_URL, connect_timeout=2, max_reconnect_attempts=1)
        logger.info("connected to NATS at %s", NATS_URL)
    except Exception as exc:  # noqa: BLE001 - degrade to empty-alerts mode
        logger.warning("could not connect to NATS at %s: %s", NATS_URL, exc)
        _nats_client = None
        return

    async def _on_message(msg) -> None:
        try:
            event = json.loads(msg.data.decode())
        except json.JSONDecodeError:
            logger.warning("dropped malformed alert on %s", msg.subject)
            return
        event["_subject"] = msg.subject
        _alerts.append(event)
        await alert_store.persist(msg.subject, event.get("domain"), event)

    await _nats_client.subscribe(ALERTS_SUBJECT, cb=_on_message)
    logger.info("subscribed to alerts on %s", ALERTS_SUBJECT)


@app.on_event("startup")
async def startup() -> None:
    await alert_store.init_pool()
    await record_store.init()
    await _connect_nats()


@app.on_event("shutdown")
async def shutdown() -> None:
    if _nats_client is not None:
        await _nats_client.close()
    await alert_store.close_pool()
    await _http.aclose()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
