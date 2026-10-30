"""Admin console REST surface (admin_platform) and the audit trail."""

import time

import httpx
import jwt
import pytest
from fastapi.testclient import TestClient

import admin_api
import admin_platform
import alert_store
import audit_log
import auth
import gateway
import platform_api
import record_store

SECRET = "k" * 40
ADMIN_ROUTES = ["/admin/overview", "/domains", "/admin/models", "/policies", "/messaging/channels", "/integrations",
                "/data-sources", "/security/events", "/observability/metrics", "/deployments", "/audit-log",
                "/admin/settings"]

MODELS = [
    {"id": "m1", "name": "anomaly_detector", "domain": "cybersecurity", "version": "1.0.0", "status": "healthy", "trained": True},
    {"id": "m2", "name": "fraud_detector", "domain": "finance", "version": "2.1.0", "status": "training", "trained": True},
    {"id": "m3", "name": "broken", "domain": "finance", "version": "0.1.0", "status": "healthy", "trained": False},
]


def tok(sub="a1", email="root@corp.io", role="admin"):
    claims = {"sub": sub, "email": email, "aud": "authenticated", "exp": int(time.time()) + 600}
    if role:
        claims["app_metadata"] = {"role": role}
    return {"Authorization": "Bearer " + jwt.encode(claims, SECRET, algorithm="HS256")}


def alert(i, sev="high", domain="finance", ts="2026-01-01T00:00:00"):
    return {"id": f"a{i}", "domain": domain, "severity": sev, "timestamp": ts, "event_type": f"{domain}.alert",
            "_subject": f"{domain}.alerts.x", "source": {"service": f"{domain}-svc"}, "payload": {}}


@pytest.fixture
def env(monkeypatch):
    """Gateway client with inference/domain services mocked; `state` lets a test change what they return."""
    state = {"models": MODELS, "down": set(), "alerts": []}
    monkeypatch.setattr(auth, "AUTH_DISABLED", False)
    monkeypatch.setattr(auth, "SUPABASE_JWT_SECRET", SECRET)
    monkeypatch.setattr(alert_store, "_pool", None)
    monkeypatch.setattr(record_store, "_table_ready", False)
    record_store._mem.clear()
    gateway._alerts.clear()
    gateway.limiter.reset()
    admin_platform._last_sync.clear()
    admin_platform._prev_metrics.clear()

    def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if host in state["down"]:
            return httpx.Response(503)
        if request.url.path == "/models":
            return httpx.Response(200, json=state["models"])
        if request.url.path.endswith("/health"):
            return httpx.Response(200, json={"status": "ok", "version": "1.2.3"})
        return httpx.Response(404)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(admin_platform, "_http", http)
    monkeypatch.setattr(admin_platform, "_inference_url", "http://inference")
    monkeypatch.setattr(admin_platform, "_domain_services", {"fraud-detector": "http://fraud", "threat-detection": "http://threat"})
    monkeypatch.setattr(admin_platform, "_nats_connected", lambda: True)

    async def alerts(limit, domain):
        rows = state["alerts"]
        return [a for a in rows if not domain or a["domain"] == domain][:limit]

    monkeypatch.setattr(admin_platform, "_alerts_fn", alerts)
    return TestClient(gateway.app), state


@pytest.mark.parametrize("path", ADMIN_ROUTES)
def test_admin_routes_require_admin(env, path):
    client, _ = env
    assert client.get(path).status_code == 401
    assert client.get(path, headers=tok(role="viewer")).status_code == 403
    assert client.get(path, headers=tok(role=None)).status_code == 403


def test_v1_prefix_works(env):
    client, _ = env
    assert client.get("/v1/policies", headers=tok()).status_code == 200


def test_models_come_from_inference_with_registry_metadata(env):
    client, state = env
    rows = {m["id"]: m for m in client.get("/admin/models", headers=tok()).json()}
    assert rows["m1"]["status"] == "healthy" and rows["m2"]["status"] == "degraded" and rows["m3"]["status"] == "offline"
    assert rows["m1"]["owner"] == "ML Platform" and rows["m1"]["deployedAt"]
    first = rows["m1"]["deployedAt"]
    assert client.get("/admin/models", headers=tok()).json()[0]["deployedAt"] == first  # stable while version is unchanged
    state["models"] = [{**MODELS[0], "version": "1.1.0"}]
    assert client.get("/admin/models", headers=tok()).json()[0]["deployedAt"] >= first  # new version -> redeployed


def test_models_empty_when_inference_down(env):
    client, state = env
    state["down"].add("inference")
    assert client.get("/admin/models", headers=tok()).json() == []


def test_domains_seeded_with_live_model_counts(env):
    client, _ = env
    domains = {d["name"]: d for d in client.get("/domains", headers=tok()).json()}
    assert domains["cybersecurity"]["modelCount"] == 1 and domains["finance"]["modelCount"] == 2
    assert domains["agriculture"]["state"] == "planned" and domains["agriculture"]["modelCount"] == 0


def test_policies_seeded(env):
    client, _ = env
    assert len(client.get("/policies", headers=tok()).json()) == 3


def test_data_sources_and_channels_from_alert_stream(env):
    client, state = env
    state["alerts"] = [alert(1), alert(2, domain="cybersecurity", ts="2026-01-02T00:00:00")]
    ds = {d["domain"]: d for d in client.get("/data-sources", headers=tok()).json()}
    assert ds["finance"]["recordCount"] == 1 and ds["cybersecurity"]["lastIngestedAt"].startswith("2026-01-02")
    assert ds["manufacturing"]["recordCount"] == 0
    channels = client.get("/messaging/channels", headers=tok()).json()
    assert {c["domain"] for c in channels} >= {"finance", "cybersecurity", "manufacturing"}
    assert all(c["status"] == "connected" for c in channels)


def test_security_events_merge_alerts_and_admin_actions(env):
    client, state = env
    state["alerts"] = [alert(1, "critical"), alert(2, "low")]
    client.put("/admin/settings", headers=tok(), json={"platformName": "P", "maintenanceMode": True,
                                                      "dataRetentionDays": 30, "defaultAlertSeverity": "high"})
    types = [e["type"] for e in client.get("/security/events", headers=tok()).json()]
    assert "finance.alert" in types and "maintenance mode enabled" in types
    assert types.count("finance.alert") == 1  # the low-severity alert is not a security event


def test_settings_defaults_roundtrip_and_audit(env):
    client, _ = env
    h = tok()
    assert client.get("/admin/settings", headers=h).json()["platformName"] == "AMCP Platform"
    body = {"platformName": "  Acme  ", "maintenanceMode": False, "dataRetentionDays": 365, "defaultAlertSeverity": "high"}
    saved = client.put("/admin/settings", headers=h, json=body).json()
    assert saved["platformName"] == "Acme"
    assert client.get("/admin/settings", headers=h).json() == saved
    log = client.get("/audit-log", headers=h).json()
    assert log[0]["actor"] == "root@corp.io" and "platformName" in log[0]["target"]
    client.put("/admin/settings", headers=h, json=saved)  # no change -> no new audit entry
    assert len(client.get("/audit-log", headers=h).json()) == 1


@pytest.mark.parametrize("bad", [
    {"platformName": " ", "maintenanceMode": False, "dataRetentionDays": 30, "defaultAlertSeverity": "low"},
    {"platformName": "P", "maintenanceMode": False, "dataRetentionDays": 0, "defaultAlertSeverity": "low"},
    {"platformName": "P", "maintenanceMode": False, "dataRetentionDays": 99999, "defaultAlertSeverity": "low"},
    {"platformName": "P", "maintenanceMode": False, "dataRetentionDays": 30, "defaultAlertSeverity": "urgent"},
    {"platformName": "P", "maintenanceMode": "yes please", "dataRetentionDays": 30, "defaultAlertSeverity": "low"},
])
def test_settings_validation(env, bad):
    client, _ = env
    assert client.put("/admin/settings", headers=tok(), json=bad).status_code == 422
    assert client.get("/audit-log", headers=tok()).json() == []


def test_audit_log_records_automation_toggle_newest_first(env):
    client, _ = env
    client.post("/automations/au-7001/status", headers=tok(), json={"status": "disabled"})
    client.post("/automations/au-7001/status", headers=tok(), json={"status": "enabled"})
    log = client.get("/audit-log", headers=tok()).json()
    assert [e["action"] for e in log] == ["set automation enabled", "set automation disabled"]


def test_audit_log_records_user_management(env, monkeypatch):
    client, _ = env

    async def fake_call(method, path, **kw):
        return {"id": "u9", "email": "new@corp.io", "user_metadata": {"full_name": "New"}, "app_metadata": {"role": "viewer"}}

    monkeypatch.setattr(admin_api, "_call", fake_call)
    r = client.post("/admin/users", headers=tok(), json={"name": "New", "email": "new@corp.io", "password": "longenough1", "role": "viewer"})
    assert r.status_code == 201
    client.post("/admin/users/u9/status", headers=tok(), json={"status": "suspended"})
    actions = [e["action"] for e in client.get("/audit-log", headers=tok()).json()]
    assert actions == ["set user suspended", "created viewer user"]
    assert "user suspended" in [e["type"] for e in client.get("/security/events", headers=tok()).json()]


def test_deployments_reflect_service_health(env):
    client, state = env
    state["down"].add("threat")
    rows = {d["service"]: d for d in client.get("/deployments", headers=tok()).json()}
    assert rows["fraud-detector"]["status"] == "running" and rows["fraud-detector"]["version"] == "1.2.3"
    assert rows["threat-detection"]["status"] == "failed" and rows["threat-detection"]["version"] == "unknown"
    assert rows["gateway"]["status"] == "running" and rows["inference"]["status"] == "running"


def test_integrations_status(env, monkeypatch):
    client, state = env
    monkeypatch.setenv("SUPABASE_URL", "http://supa")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "svc")
    monkeypatch.delenv("MLFLOW_TRACKING_URI", raising=False)
    rows = {i["id"]: i for i in client.get("/integrations", headers=tok()).json()}
    assert rows["int-supabase"]["status"] == "connected" and rows["int-supabase"]["lastSyncAt"]
    assert rows["int-nats"]["status"] == "connected"
    assert rows["int-mlflow"]["status"] == "disconnected"  # not configured
    state["down"].add("inference")
    rows = {i["id"]: i for i in client.get("/integrations", headers=tok()).json()}
    assert rows["int-inference"]["status"] == "error"


def test_metrics_values_and_trend(env):
    client, state = env
    first = {m["id"]: m for m in client.get("/observability/metrics", headers=tok()).json()}
    assert first["domain-services-up"]["value"] == 2 and first["models-healthy"]["value"] == 1
    assert first["nats-connected"]["value"] == 1 and first["domain-services-up"]["trend"] == "flat"
    state["down"].add("threat")
    second = {m["id"]: m for m in client.get("/observability/metrics", headers=tok()).json()}
    assert second["domain-services-up"]["value"] == 1 and second["domain-services-up"]["trend"] == "down"


def test_overview(env, monkeypatch):
    client, state = env
    state["alerts"] = [alert(1, "critical")]

    async def fake_call(method, path, **kw):
        return {"users": [{"last_sign_in_at": "2026-01-01"}, {"last_sign_in_at": "2026-01-01", "banned_until": "x"}, {}]}

    monkeypatch.setattr(admin_api, "_call", fake_call)
    o = client.get("/admin/overview", headers=tok()).json()
    assert o == {"activeDomains": 3, "totalModels": 3, "healthyModels": 1, "activeUsers": 1,
                 "openSecurityEvents": 1, "runningDeployments": 4}
