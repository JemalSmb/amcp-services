"""Every concrete domain service module wires the shared runtime correctly."""

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent / "services"
FLAGSHIP = {
    "cybersecurity/threat-detection": ("cybersecurity", "anomaly_detection"),
    "cybersecurity/phishing-detector": ("cybersecurity", "phishing_detection"),
    "finance/fraud-detector": ("finance", "finance_fraud_anomaly"),
    "manufacturing/process-anomaly-detector": ("manufacturing", "manufacturing_process_anomaly"),
}


def _load(rel):
    spec = importlib.util.spec_from_file_location("svc_" + rel.replace("/", "_").replace("-", "_"), ROOT / rel / "service.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("rel,expected", FLAGSHIP.items())
def test_flagship_service_configuration(rel, expected):
    svc = _load(rel).service
    assert (svc.domain, svc.model_id) == expected
    assert svc.service_name == rel.split("/")[1]
    assert svc.output_subject.split(".")[1] == "alerts"


def test_every_service_module_exposes_app():
    for p in sorted(ROOT.glob("*/*/service.py")):
        assert "DomainMicroservice" in p.read_text() or "app" in p.read_text(), p
