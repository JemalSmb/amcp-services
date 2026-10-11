"""
Phishing Detector Service (cybersecurity, passive)

Consumes email/URL feature events on cyber.events.phishing, scores them
against the phishing_detection model, and publishes alerts to
cyber.alerts.phishing when the model flags something above threshold.
"""

import logging
import os

from shared.base_service import DomainMicroservice

logging.basicConfig(level=logging.INFO)

service = DomainMicroservice(
    service_name="phishing-detector",
    domain="cybersecurity",
    model_id="phishing_detection",
    input_subject="cyber.events.phishing",
    output_subject="cyber.alerts.phishing",
)

app = service.app

if __name__ == "__main__":
    service.run(port=int(os.environ.get("PORT", 8003)))
