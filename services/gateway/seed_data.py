"""
Initial content for the dashboard collections that aren't derived from live
alerts. Written into `platform_records` (record_store.py) the first time a
collection is read while empty; the dashboard can then change it (e.g.
automation toggles, settings) and the changes persist.

Shapes match the UI models in the web-client's core/models/platform.models.ts.
"""

from typing import Any, Dict, List

SEEDS: Dict[str, List[Dict[str, Any]]] = {
    "assets": [
        {"id": "as-6001", "name": "auth-gateway-01", "domain": "cybersecurity", "type": "service", "risk": "high", "lastSeenAt": "2026-09-16T08:12:00Z"},
        {"id": "as-6002", "name": "trading-api", "domain": "finance", "type": "service", "risk": "medium", "lastSeenAt": "2026-09-16T08:09:00Z"},
        {"id": "as-6003", "name": "line-3-plc", "domain": "manufacturing", "type": "device", "risk": "low", "lastSeenAt": "2026-09-16T07:50:00Z"},
    ],
    "automations": [
        {"id": "au-7001", "name": "Lock account on credential stuffing", "domain": "cybersecurity", "trigger": "anomaly_detection: high severity", "status": "enabled", "lastRunAt": "2026-09-16T08:06:30Z", "runCount": 12},
        {"id": "au-7002", "name": "Freeze account on fraud score > 0.9", "domain": "finance", "trigger": "finance_fraud_anomaly: score > 0.9", "status": "enabled", "lastRunAt": "2026-09-16T07:41:15Z", "runCount": 4},
        {"id": "au-7003", "name": "Notify maintenance on process drift", "domain": "manufacturing", "trigger": "manufacturing_process_anomaly: any", "status": "disabled", "lastRunAt": "2026-09-15T22:11:00Z", "runCount": 21},
    ],
    "investigations": [
        {"id": "iv-5001", "title": "Q3 credential abuse review", "domain": "cybersecurity", "status": "in_progress", "owner": "S. Rao", "incidentIds": ["in-3001"], "updatedAt": "2026-09-16T08:20:00Z"},
        {"id": "iv-5002", "title": "Trading anomaly root cause", "domain": "finance", "status": "open", "owner": "M. Chen", "incidentIds": ["in-3002"], "updatedAt": "2026-09-16T07:45:00Z"},
    ],
    "reports": [
        {"id": "rp-8001", "title": "Weekly security posture summary", "domain": "cybersecurity", "generatedAt": "2026-09-15T06:00:00Z", "period": "Sep 8 - Sep 15"},
        {"id": "rp-8002", "title": "Fraud detection performance", "domain": "finance", "generatedAt": "2026-09-15T06:00:00Z", "period": "Sep 8 - Sep 15"},
        {"id": "rp-8003", "title": "Platform-wide event summary", "domain": "platform", "generatedAt": "2026-09-15T06:00:00Z", "period": "Sep 8 - Sep 15"},
    ],
    "notifications": [
        {"id": "no-9001", "message": "New critical alert in finance domain", "read": False, "createdAt": "2026-09-16T07:41:00Z"},
        {"id": "no-9002", "message": 'Automation "Lock account on credential stuffing" ran successfully', "read": False, "createdAt": "2026-09-16T08:06:35Z"},
        {"id": "no-9003", "message": "Weekly report generated", "read": True, "createdAt": "2026-09-15T06:01:00Z"},
    ],
    "help-docs": [
        {"id": "hd-1", "title": "Getting started with the dashboard", "category": "Basics", "summary": "Overview of the model status, alerts, and predictions views."},
        {"id": "hd-2", "title": "Understanding alert severity", "category": "Alerts", "summary": "How severity levels are assigned and what they mean."},
        {"id": "hd-3", "title": "Configuring automation playbooks", "category": "Automation", "summary": "How to enable, disable, and trigger response playbooks."},
    ],
}

DEFAULT_SETTINGS: Dict[str, Any] = {
    "theme": "system",
    "emailAlerts": True,
    "notificationEmail": "",
    "emailDigest": "off",
    "alertSeverityThreshold": "medium",
    "browserNotifications": False,
    "refreshIntervalSeconds": 30,
}
