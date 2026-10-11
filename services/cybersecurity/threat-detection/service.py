"""
Threat Detection Service (cybersecurity, passive)

Consumes network/flow events on cyber.events.threats, scores them against
the anomaly_detection model, and publishes alerts to cyber.alerts.threats
when the model flags something above threshold.
"""

import logging
import os

from shared.base_service import DomainMicroservice

logging.basicConfig(level=logging.INFO)

service = DomainMicroservice(
    service_name="threat-detection",
    domain="cybersecurity",
    model_id="anomaly_detection",
    input_subject="cyber.events.threats",
    output_subject="cyber.alerts.threats",
)

app = service.app

if __name__ == "__main__":
    service.run(port=int(os.environ.get("PORT", 8001)))
