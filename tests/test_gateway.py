"""Gateway HTTP surface: versioning, auth, rate limits, CORS, aggregation, fallback."""

import httpx
import pytest
from fastapi.testclient import TestClient

import alert_store
import auth
import gateway


def upstream(routes):
    """routes: {(method, url_prefix): handler|Response|Exception}"""
    def h(request):
        for (method, prefix), val in routes.items():
            if request.method == method and str(request.url).startswith(prefix):
                if isinstance(val, Exception):
                    raise val
                return val(request) if callable(val) else val
        return httpx.Response(404)
    return httpx.AsyncClient(transport=httpx.MockTransport(h))


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(auth, "AUTH_DISABLED", False)
    monkeypatch.setattr(auth, "SUPABASE_JWT_SECRET", "k" * 40)
    monkeypatch.setattr(alert_store, "_pool", None)
    gateway._alerts.clear()
    gateway.limiter.reset()
    c = TestClient(gateway.app)
    yield c
    gateway.limiter.reset()


@pytest.fixture
def bearer():
    import time, jwt
    t = jwt.encode({"sub": "u", "aud": "authenticated", "exp": int(time.time()) + 600}, "k" * 40, algorithm="HS256")
    return {"Authorization": f"Bearer {t}"}


# --- health / versioning -----------------------------------------------------------
def test_health_reports_environment_and_domains(client, monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "production")
    body = client.get("/v1/health").json()
    assert body["status"] == "ok" and body["environment"] == "production"
    assert set(body["domains_configured"]) == set(gateway.DOMAIN_SERVICES)
    assert body["alerts_persisted"] is False


def test_health_environment_defaults_to_development(client, monkeypatch):
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    assert client.get("/v1/health").json()["environment"] == "development"


@pytest.mark.parametrize("path", ["/health", "/models", "/domains/status", "/alerts"])
def test_unversioned_aliases_still_resolve(client, path):
    gateway._http = upstream({("GET", "http://localhost"): httpx.Response(200, json=[])})
    assert client.get(path, headers={"Authorization": "Bearer x"}).status_code in (200, 401)
    assert client.get(path).status_code != 404


# --- auth ----------------------------------------------------------------------------
@pytest.mark.parametrize("method,path", [("GET", "/v1/alerts"), ("GET", "/v1/domains/status"),
                                         ("POST", "/v1/domains/fraud-detector/trigger"),
                                         ("GET", "/predictions"), ("GET", "/v1/admin/users")])
def test_protected_routes_reject_anonymous(client, method, path):
    r = client.request(method, path, json={"features": {}} if method == "POST" else None)
    assert r.status_code == 401


def test_protected_route_rejects_bad_token(client):
    assert client.get("/v1/alerts", headers={"Authorization": "Bearer nope"}).status_code == 401


def test_models_is_open(client):
    gateway._http = upstream({("GET", gateway.INFERENCE_URL): httpx.Response(200, json=[{"id": "m"}])})
    assert client.get("/v1/models").json() == [{"id": "m"}]


def test_admin_route_forbids_non_admin(client, bearer):
    assert client.get("/v1/admin/users", headers=bearer).status_code == 403


# --- models proxy / domains ----------------------------------------------------------
def test_models_502_when_inference_down(client):
    gateway._http = upstream({("GET", gateway.INFERENCE_URL): httpx.ConnectError("down")})
    assert client.get("/v1/models").status_code == 502


def test_domains_status_up_and_down(client, bearer):
    up = gateway.DOMAIN_SERVICES["fraud-detector"]

    def h(req):
        if str(req.url).startswith(up):
            return httpx.Response(200, json={"status": "ok"})
        raise httpx.ConnectError("refused")

    gateway._http = httpx.AsyncClient(transport=httpx.MockTransport(h))
    out = {d["name"]: d["status"] for d in client.get("/v1/domains/status", headers=bearer).json()}
    assert out["fraud-detector"] == "up"
    assert {v for k, v in out.items() if k != "fraud-detector"} == {"down"}


# --- trigger -----------------------------------------------------------------------------
def test_trigger_forwards_body(client, bearer):
    seen = {}

    def h(req):
        import json
        seen["url"], seen["body"] = str(req.url), json.loads(req.content)
        return httpx.Response(200, json={"published": False})

    gateway._http = httpx.AsyncClient(transport=httpx.MockTransport(h))
    r = client.post("/v1/domains/fraud-detector/trigger", headers=bearer, json={"features": {"a": 1}})
    assert r.json() == {"published": False}
    assert seen["url"].endswith("/trigger") and seen["body"]["features"] == {"a": 1.0}


def test_trigger_unknown_service_404(client, bearer):
    assert client.post("/v1/domains/nope/trigger", headers=bearer, json={"features": {}}).status_code == 404


def test_trigger_downstream_down_502(client, bearer):
    gateway._http = upstream({("POST", "http://"): httpx.ConnectError("x")})
    assert client.post("/v1/domains/fraud-detector/trigger", headers=bearer, json={"features": {}}).status_code == 502


def test_trigger_validates_body(client, bearer):
    assert client.post("/v1/domains/fraud-detector/trigger", headers=bearer, json={"features": {"a": "x"}}).status_code == 422


def test_trigger_rate_limited_after_10(client, bearer):
    gateway._http = upstream({("POST", "http://"): httpx.Response(200, json={})})
    codes = [client.post("/v1/domains/fraud-detector/trigger", headers=bearer, json={"features": {}}).status_code
             for _ in range(12)]
    assert codes[:10] == [200] * 10 and codes[10:] == [429, 429]


# --- alerts ---------------------------------------------------------------------------------
def test_alerts_fallback_to_memory_newest_first_and_domain_filter(client, bearer):
    for i, d in enumerate(["finance", "cyber", "finance"]):
        gateway._alerts.append({"id": i, "domain": d})
    assert [a["id"] for a in client.get("/v1/alerts", headers=bearer).json()] == [2, 1, 0]
    assert [a["id"] for a in client.get("/v1/alerts?domain=finance", headers=bearer).json()] == [2, 0]
    assert len(client.get("/v1/alerts?limit=1", headers=bearer).json()) == 1


def test_alerts_prefers_database(client, bearer, monkeypatch):
    async def recent(limit, domain): return [{"id": "db"}]
    monkeypatch.setattr(alert_store, "recent", recent)
    gateway._alerts.append({"id": "mem"})
    assert client.get("/v1/alerts", headers=bearer).json() == [{"id": "db"}]


def test_alerts_buffer_is_bounded():
    assert gateway._alerts.maxlen == gateway.ALERTS_BUFFER_SIZE


# --- CORS -----------------------------------------------------------------------------------
def test_cors_allows_configured_origin(client):
    r = client.get("/v1/health", headers={"Origin": "http://localhost:4200"})
    assert r.headers["access-control-allow-origin"] == "http://localhost:4200"


def test_cors_blocks_unlisted_origin(client):
    r = client.get("/v1/health", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in r.headers


def test_cors_preflight_unlisted_origin_rejected(client):
    r = client.options("/v1/alerts", headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "GET"})
    assert r.status_code == 400
