"""Dashboard REST surface (platform_api) and admin user management (admin_api)."""

import time

import httpx
import jwt
import pytest
from fastapi.testclient import TestClient

import admin_api
import alert_store
import auth
import gateway
import platform_api
import record_store

SECRET = "k" * 40


def tok(sub="u1", email="ann@example.com", app_role=None):
    claims = {"sub": sub, "email": email, "aud": "authenticated", "exp": int(time.time()) + 600}
    if app_role:
        claims["app_metadata"] = {"role": app_role}
    return {"Authorization": "Bearer " + jwt.encode(claims, SECRET, algorithm="HS256")}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(auth, "AUTH_DISABLED", False)
    monkeypatch.setattr(auth, "SUPABASE_JWT_SECRET", SECRET)
    monkeypatch.setattr(alert_store, "_pool", None)
    monkeypatch.setattr(record_store, "_table_ready", False)
    record_store._mem.clear()
    gateway._alerts.clear()
    gateway.limiter.reset()
    yield TestClient(gateway.app)


def alert(i, sev="high", domain="finance", score=0.9):
    return {"id": f"a{i}", "domain": domain, "severity": sev, "timestamp": f"2026-01-0{i}T00:00:00",
            "event_type": "fraud-detector.alert",
            "source": {"service": "fraud-detector", "model_id": "finance_fraud_anomaly"},
            "payload": {"label": "fraud_suspected", "score": score}}


def mock_inference(monkeypatch, handler):
    monkeypatch.setattr(platform_api, "_http", httpx.AsyncClient(transport=httpx.MockTransport(handler)))


# --- derived from alerts ---------------------------------------------------------
def test_predictions_detections_derived_from_alerts(client):
    gateway._alerts.extend([alert(1), alert(2)])
    preds = client.get("/predictions", headers=tok()).json()
    assert [p["id"] for p in preds] == ["a2", "a1"]  # newest first
    assert preds[0]["modelId"] == "finance_fraud_anomaly" and preds[0]["score"] == 0.9
    det = client.get("/detections", headers=tok()).json()
    assert det[0]["confidence"] == 0.9 and det[0]["signal"] == "fraud_suspected"


def test_incidents_only_high_and_critical(client):
    gateway._alerts.extend([alert(1, "low"), alert(2, "medium"), alert(3, "high"), alert(4, "critical"), alert(5, "weird")])
    ids = {i["linkedAlertId"] for i in client.get("/incidents", headers=tok()).json()}
    assert ids == {"a3", "a4"}


def test_bad_score_does_not_crash(client):
    a = alert(1)
    a["payload"]["score"] = "NaN-ish"
    gateway._alerts.append(a)
    assert client.get("/predictions", headers=tok()).json()[0]["score"] == 0.0


def test_v1_prefix_equivalent(client):
    gateway._alerts.append(alert(1))
    assert client.get("/v1/incidents", headers=tok()).json() == client.get("/incidents", headers=tok()).json()


def test_dashboard_routes_require_auth(client):
    for p in ["/predictions", "/detections", "/incidents", "/assets", "/settings", "/profile"]:
        assert client.get(p).status_code == 401


# --- manual predict -------------------------------------------------------------------
def test_predict_proxies_and_stores(client, monkeypatch):
    seen = {}

    def h(req):
        import json
        seen["url"], seen["body"] = str(req.url), json.loads(req.content)
        return httpx.Response(200, json={"model_id": "m", "domain": "finance", "score": 0.7, "label": "x"})

    mock_inference(monkeypatch, h)
    r = client.post("/models/m/predict", headers=tok(), json={"amount": 5})
    assert r.status_code == 200 and r.json()["score"] == 0.7 and r.json()["id"].startswith("pr-")
    assert seen["body"] == {"features": {"amount": 5}}
    assert client.get("/predictions", headers=tok()).json()[0]["id"] == r.json()["id"]


@pytest.mark.parametrize("resp,code", [(httpx.Response(404), 404), (httpx.Response(422, text="bad"), 422)])
def test_predict_propagates_inference_errors(client, monkeypatch, resp, code):
    mock_inference(monkeypatch, lambda r: resp)
    assert client.post("/models/m/predict", headers=tok(), json={"a": 1}).status_code == code


def test_predict_502_when_inference_down(client, monkeypatch):
    def h(r): raise httpx.ConnectError("x")
    mock_inference(monkeypatch, h)
    assert client.post("/models/m/predict", headers=tok(), json={"a": 1}).status_code == 502


# --- stored collections ------------------------------------------------------------------
@pytest.mark.parametrize("path,n", [("/assets", 3), ("/automations", 3), ("/investigations", 2),
                                    ("/reports", 3), ("/notifications", 3), ("/help-docs", 3)])
def test_seeded_collections(client, path, n):
    assert len(client.get(path, headers=tok()).json()) == n


def test_toggle_automation(client):
    r = client.post("/automations/au-7001/status", headers=tok(), json={"status": "disabled"})
    assert r.json()["status"] == "disabled"
    stored = {a["id"]: a["status"] for a in client.get("/automations", headers=tok()).json()}
    assert stored["au-7001"] == "disabled"


def test_toggle_automation_validation(client):
    assert client.post("/automations/au-7001/status", headers=tok(), json={"status": "x"}).status_code == 422
    assert client.post("/automations/zzz/status", headers=tok(), json={"status": "enabled"}).status_code == 404


# --- settings / profile (per user) -------------------------------------------------------------
def test_settings_default_save_and_isolation(client):
    a, b = tok("alice"), tok("bob")
    assert client.get("/settings", headers=a).json()["theme"] == "light"
    saved = client.put("/settings", headers=a, json={"theme": "dark", "evil": 1}).json()
    assert saved["theme"] == "dark" and "evil" not in saved
    assert client.get("/settings", headers=a).json()["theme"] == "dark"
    assert client.get("/settings", headers=b).json()["theme"] == "light"


def test_profile_defaults_from_email(client):
    p = client.get("/profile", headers=tok("u9", "jane.doe@corp.io")).json()
    assert p == {"id": "u9", "name": "jane.doe", "email": "jane.doe@corp.io", "role": "Analyst", "timezone": "UTC"}


# --- admin api -----------------------------------------------------------------------------------
@pytest.fixture
def admin(client, monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://x.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "svc")
    calls = []

    def install(handler):
        def h(req):
            calls.append(req)
            return handler(req)
        monkeypatch.setattr(admin_api, "_http", httpx.AsyncClient(transport=httpx.MockTransport(h)))

    return client, install, calls


def test_admin_requires_admin_role(admin):
    c, install, _ = admin
    assert c.get("/admin/users").status_code == 401
    assert c.get("/admin/users", headers=tok(app_role="analyst")).status_code == 403


def test_list_users_maps_status_and_role(admin):
    c, install, calls = admin
    install(lambda r: httpx.Response(200, json={"users": [
        {"id": "1", "email": "a@x.io", "last_sign_in_at": "t", "app_metadata": {"role": "admin"}},
        {"id": "2", "email": "b@x.io", "banned_until": "2999", "app_metadata": {"role": "root"}},
        {"id": "3", "email": "c@x.io", "user_metadata": {"full_name": "Cee", "client": "app"}},
    ]}))
    users = c.get("/v1/admin/users", headers=tok(app_role="admin")).json()
    assert [(u["status"], u["role"]) for u in users] == [("active", "admin"), ("suspended", "viewer"), ("invited", "viewer")]
    assert users[2]["name"] == "Cee" and users[2]["client"] == "app"
    assert calls[0].headers["apikey"] == "svc" and calls[0].url.path == "/auth/v1/admin/users"


def test_create_user_sets_role_server_side(admin):
    import json
    c, install, calls = admin
    install(lambda r: httpx.Response(200, json={"id": "9", "email": "n@x.io", "app_metadata": {"role": "analyst"}}))
    r = c.post("/admin/users", headers=tok(app_role="admin"),
               json={"name": "N", "email": "n@x.io", "password": "longenough", "role": "analyst"})
    assert r.status_code == 201
    body = json.loads(calls[0].content)
    assert body["app_metadata"] == {"role": "analyst"} and body["email_confirm"] is True


@pytest.mark.parametrize("bad", [
    {"name": "N", "email": "not-an-email", "password": "longenough"},
    {"name": "N", "email": "n@x.io", "password": "short"},
    {"name": "N", "email": "n@x.io", "password": "longenough", "role": "superuser"},
])
def test_create_user_validation(admin, bad):
    c, install, _ = admin
    assert c.post("/admin/users", headers=tok(app_role="admin"), json=bad).status_code == 422


def test_suspend_and_restore(admin):
    import json
    c, install, calls = admin
    install(lambda r: httpx.Response(200, json={"id": "1", "email": "a@x.io"}))
    c.post("/admin/users/1/status", headers=tok(app_role="admin"), json={"status": "suspended"})
    c.post("/admin/users/1/status", headers=tok(app_role="admin"), json={"status": "active"})
    assert json.loads(calls[0].content)["ban_duration"] == "876000h"
    assert json.loads(calls[1].content)["ban_duration"] == "none"


def test_admin_upstream_errors(admin):
    c, install, _ = admin
    install(lambda r: httpx.Response(422, json={"msg": "already registered"}))
    r = c.get("/admin/users", headers=tok(app_role="admin"))
    assert (r.status_code, r.json()["detail"]) == (422, "already registered")

    def down(r): raise httpx.ConnectError("x")
    install(down)
    assert c.get("/admin/users", headers=tok(app_role="admin")).status_code == 502


def test_admin_unconfigured_is_500(client, monkeypatch):
    monkeypatch.delenv("SUPABASE_SERVICE_ROLE_KEY", raising=False)
    assert client.get("/admin/users", headers=tok(app_role="admin")).status_code == 500
