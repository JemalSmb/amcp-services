"""DomainMicroservice runtime: validation, inference call, thresholds, NATS / HTTP-only."""

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from shared.base_service import DomainMicroservice
from shared.events import DomainEvent


def make(handler=None, threshold=0.5):
    svc = DomainMicroservice("fraud-detector", "finance", "finance_fraud_anomaly",
                             "fin.in", "fin.alerts.fraud", score_threshold=threshold)
    if handler:
        svc._http = httpx.AsyncClient(base_url="http://inf", transport=httpx.MockTransport(handler))
    return svc


def evt(features):
    return DomainEvent.create("finance", "fin.input", "info", {}, {"features": features})


def scoring(score, **extra):
    def h(request):
        return httpx.Response(200, json={"score": score, "label": "fraud_suspected", **extra})
    return h


# --- validation ---------------------------------------------------------------
async def test_validate_coerces_to_float():
    assert await make().validate_payload(evt({"a": "2", "b": 3})) == {"a": 2.0, "b": 3.0}


@pytest.mark.parametrize("payload", [{}, {"features": [1, 2]}, {"features": "x"}, {"features": None}])
async def test_validate_rejects_non_object(payload):
    e = DomainEvent.create("d", "t", "info", {}, payload)
    with pytest.raises(ValueError):
        await make().validate_payload(e)


async def test_validate_rejects_non_numeric_value():
    with pytest.raises(ValueError):
        await make().validate_payload(evt({"a": "abc"}))


# --- inference request formatting ----------------------------------------------
async def test_call_inference_posts_features_to_model_endpoint():
    seen = {}

    def h(request):
        seen["path"], seen["body"] = request.url.path, json.loads(request.content)
        return httpx.Response(200, json={"score": 0.1})

    await make(h).call_inference({"amount": 5.0})
    assert seen == {"path": "/models/finance_fraud_anomaly/predict", "body": {"features": {"amount": 5.0}}}


# --- thresholds -----------------------------------------------------------------
@pytest.mark.parametrize("score,sev", [(0.5, "medium"), (0.74, "medium"), (0.75, "high"), (0.89, "high"),
                                       (0.9, "critical"), (1.0, "critical")])
def test_alert_severity_bands(score, sev):
    out = make().format_result(evt({"a": 1}), {"score": score, "label": "x"})
    assert out.severity == sev


@pytest.mark.parametrize("score", [0.0, 0.49, 0.4999])
def test_no_alert_below_threshold(score):
    assert make().format_result(evt({"a": 1}), {"score": score}) is None


def test_custom_threshold_respected():
    assert make(threshold=0.8).format_result(evt({}), {"score": 0.79}) is None
    assert make(threshold=0.8).format_result(evt({}), {"score": 0.8}) is not None


def test_alert_metadata_contract():
    src = evt({"a": 1})
    out = make().format_result(src, {"score": 0.8, "label": "fraud_suspected", "trained": True, "warnings": ["w"]})
    assert out.event_type == "fraud-detector.alert" and out.domain == "finance"
    assert out.source == {"service": "fraud-detector", "model_id": "finance_fraud_anomaly"}
    assert out.payload == {"label": "fraud_suspected", "score": 0.8, "trained": True, "input_event_id": src.id}
    assert out.metadata == {"warnings": ["w"]}


# --- handle_event ------------------------------------------------------------------
async def test_handle_event_publishes_alert():
    svc = make(scoring(0.95))
    published = []

    class N:
        async def publish(self, subject, data): published.append((subject, json.loads(data)))

    svc._nats_client = N()
    out = await svc.handle_event(evt({"a": 1}))
    assert out.severity == "critical"
    assert published[0][0] == "fin.alerts.fraud" and published[0][1]["id"] == out.id


async def test_handle_event_below_threshold_publishes_nothing():
    assert await make(scoring(0.1)).handle_event(evt({"a": 1})) is None


async def test_handle_event_malformed_returns_none_without_calling_inference():
    def h(request):
        pytest.fail("inference must not be called")

    assert await make(h).handle_event(DomainEvent.create("d", "t", "info", {}, {})) is None


@pytest.mark.parametrize("handler", [
    lambda r: httpx.Response(500, json={}),
    lambda r: (_ for _ in ()).throw(httpx.ConnectError("down")),
])
async def test_inference_failure_is_swallowed(handler):
    assert await make(handler).handle_event(evt({"a": 1})) is None


async def test_publish_without_nats_is_noop_http_only():
    await make().publish(evt({}))  # must not raise


# --- NATS subscribe ---------------------------------------------------------------
async def test_connect_nats_degrades_when_broker_unreachable(monkeypatch):
    import nats  # noqa: F401
    async def fail(url): raise OSError("refused")
    monkeypatch.setattr("nats.connect", fail)
    svc = make()
    await svc.connect_nats()
    assert svc._nats_client is None


async def test_nats_subscription_handles_valid_and_malformed(monkeypatch):
    nats = pytest.importorskip("nats")
    cbs = {}

    class N:
        async def subscribe(self, subject, cb): cbs[subject] = cb
        async def publish(self, s, d): cbs.setdefault("out", []).append((s, d))

    async def ok(url): return N()
    monkeypatch.setattr(nats, "connect", ok)
    svc = make(scoring(0.9))
    await svc.connect_nats()
    cb = cbs["fin.in"]

    class Msg:
        def __init__(self, data): self.data = data

    await cb(Msg(b"{not json"))                # dropped, no raise
    await cb(Msg(b'{"payload": {}}'))          # missing domain/event_type -> dropped
    assert "out" not in cbs
    good = evt({"a": 1}).to_dict()
    await cb(Msg(json.dumps(good).encode()))
    assert cbs["out"][0][0] == "fin.alerts.fraud"


# --- HTTP surface -----------------------------------------------------------------
def test_health_endpoint():
    body = TestClient(make().app).get("/health").json()
    assert body == {"service": "fraud-detector", "domain": "finance",
                    "model_id": "finance_fraud_anomaly", "status": "ok"}


def test_trigger_returns_event_when_alerting():
    r = TestClient(make(scoring(0.8)).app).post("/trigger", json={"features": {"a": 1}})
    assert r.json()["published"] is True and r.json()["event"]["severity"] == "high"


def test_trigger_not_published_below_threshold():
    r = TestClient(make(scoring(0.1)).app).post("/trigger", json={"features": {"a": 1}})
    assert r.json() == {"published": False}


def test_trigger_rejects_invalid_body():
    assert TestClient(make().app).post("/trigger", json={"features": {"a": "x"}}).status_code == 422
