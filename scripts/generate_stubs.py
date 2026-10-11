"""
One-off generator for stub service directories: a README describing the
service's intended purpose (pulled from the domain.yaml manifests) plus a
placeholder service.py/Dockerfile. Run once; not part of the running
platform.
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# (domain, service_name, kind, description, ai_model_hint, port)
STUBS = [
    # cybersecurity — passive
    ("cybersecurity", "fuser-correlator", "passive", "Event correlation and attack chain detection", "correlation_model", 8002),
    ("cybersecurity", "iot-anomaly-detector", "passive", "IoT/OT device anomaly detection", "iot_anomaly_model", 8004),
    ("cybersecurity", "insider-monitor", "passive", "Insider threat behavior analysis", "insider_behavior_model", 8005),
    # cybersecurity — active
    ("cybersecurity", "ransomware-detection", "active", "Real-time ransomware detection and prevention", "ransomware_detector", 8101),
    ("cybersecurity", "malware-sandbox", "active", "Automated malware analysis sandbox", "malware_sandbox_model", 8102),
    ("cybersecurity", "attack-path-planner", "active", "Attack path prediction and blocking", "attack_path_model", 8103),
    ("cybersecurity", "data-exfiltration-preventer", "active", "DLP and data exfiltration prevention", "dlp_model", 8104),
    ("cybersecurity", "adho-deception", "active", "Honeypot and deception technology", "deception_model", 8105),
    # finance — passive
    ("finance", "aml-monitor", "passive", "Anti-money laundering pattern detection", "aml_pattern_detector", 8002),
    ("finance", "credit-risk-analyzer", "passive", "Real-time credit risk scoring", "credit_risk_model", 8003),
    ("finance", "market-anomaly-detector", "passive", "Detect unusual market patterns and manipulation", "market_anomaly_model", 8004),
    ("finance", "customer-behavior-analyzer", "passive", "Customer behavior analysis for personalization and risk", "behavior_model", 8005),
    ("finance", "regulatory-monitor", "passive", "Compliance monitoring and reporting", "compliance_nlp_model", 8006),
    # finance — active
    ("finance", "transaction-gateway", "active", "Intelligent transaction approval/blocking", None, 8101),
    ("finance", "risk-mitigator", "active", "Automated risk response and hedging", None, 8102),
    ("finance", "alert-investigator", "active", "AI-assisted alert triage and investigation", None, 8103),
    ("finance", "limit-manager", "active", "Dynamic credit and trading limit adjustment", None, 8104),
    ("finance", "report-generator", "active", "Automated regulatory report generation", None, 8105),
    ("finance", "portfolio-optimizer", "active", "AI portfolio rebalancing and optimization", None, 8106),
    # manufacturing — passive
    ("manufacturing", "equipment-health-monitor", "passive", "Predictive maintenance using vibration, temperature, and acoustic analysis", "equipment_failure_predictor", 8001),
    ("manufacturing", "quality-inspector", "passive", "AI vision-based defect detection and quality scoring", "defect_detector", 8002),
    ("manufacturing", "energy-optimizer", "passive", "Monitor and optimize energy consumption", "energy_optimization_model", 8004),
    ("manufacturing", "supply-chain-analyzer", "passive", "Demand forecasting and supply chain risk analysis", "demand_forecaster", 8005),
    ("manufacturing", "safety-monitor", "passive", "Worker safety monitoring and hazard detection", "safety_hazard_detector", 8006),
    # manufacturing — active
    ("manufacturing", "maintenance-scheduler", "active", "Automated maintenance scheduling and work order creation", None, 8101),
    ("manufacturing", "production-optimizer", "active", "Real-time production line optimization", None, 8102),
    ("manufacturing", "quality-controller", "active", "Automated quality hold and rework decisions", None, 8103),
    ("manufacturing", "inventory-manager", "active", "Smart inventory replenishment and allocation", None, 8104),
    ("manufacturing", "robot-coordinator", "active", "Autonomous robot fleet management", None, 8105),
    ("manufacturing", "safety-responder", "active", "Automated safety intervention and lockout", None, 8106),
]

README_TEMPLATE = """# {service_name}

**Domain:** {domain}
**Kind:** {kind}
**Status:** not implemented — structure stub only

{description}.

## Why this is a stub

This service is named in `amcp-platform-core/domains/{domain}/domain.yaml`
but has no trained model or working logic yet. `threat-detection`,
`phishing-detector` (cybersecurity), `fraud-detector` (finance), and
`process-anomaly-detector` (manufacturing) are the only services in this
repo currently wired end-to-end (NATS in -> inference call -> NATS out) —
use those as the reference implementation.

## To implement this service

1. Train and export a model for `{ai_model_hint}` in `amcp-ai-engine`,
   register it in the inference service's `SUPPORTED_MODELS`.
2. Replace `service.py` with a real `DomainMicroservice(...)` instance
   (see `services/cybersecurity/threat-detection/service.py` for the
   pattern) pointing at that model and this domain's NATS subjects.
3. Wire it into `docker-compose.services.yaml`.
"""

SERVICE_PY_TEMPLATE = '''"""
{service_name} ({domain}, {kind}) — NOT YET IMPLEMENTED

{description}.

See README.md in this directory for what's needed to turn this into a
real service, following the pattern in
services/cybersecurity/threat-detection/service.py.
"""

import os

from fastapi import FastAPI

app = FastAPI(title="AMCP {service_name} (stub)")


@app.get("/health")
async def health():
    return {{
        "service": "{service_name}",
        "domain": "{domain}",
        "status": "not_implemented",
    }}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", {port})))
'''

DOCKERFILE_TEMPLATE = """FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY services/{domain}/{service_name}/service.py /app/services/{domain}/{service_name}/service.py

ENV PYTHONPATH=/app
EXPOSE 8000

CMD ["python", "services/{domain}/{service_name}/service.py"]
"""


def main() -> None:
    for domain, service_name, kind, description, ai_model_hint, port in STUBS:
        service_dir = REPO_ROOT / "services" / domain / service_name
        service_dir.mkdir(parents=True, exist_ok=True)

        (service_dir / "README.md").write_text(
            README_TEMPLATE.format(
                service_name=service_name,
                domain=domain,
                kind=kind,
                description=description,
                ai_model_hint=ai_model_hint or "a new model",
            )
        )
        (service_dir / "service.py").write_text(
            SERVICE_PY_TEMPLATE.format(
                service_name=service_name,
                domain=domain,
                kind=kind,
                description=description,
                port=port,
            )
        )
        (service_dir / "Dockerfile").write_text(
            DOCKERFILE_TEMPLATE.format(domain=domain, service_name=service_name)
        )
        print(f"stubbed {domain}/{service_name}")


if __name__ == "__main__":
    main()
