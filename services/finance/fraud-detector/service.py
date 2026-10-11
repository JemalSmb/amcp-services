"""
Fraud Detector Service (finance, passive)

Consumes transaction events on fin.transactions.incoming, scores them
against the finance_fraud_anomaly model, and publishes alerts to
fin.alerts.fraud when the model flags something above threshold.
"""

import logging
import os

from shared.base_service import DomainMicroservice

logging.basicConfig(level=logging.INFO)

service = DomainMicroservice(
    service_name="fraud-detector",
    domain="finance",
    model_id="finance_fraud_anomaly",
    input_subject="fin.transactions.incoming",
    output_subject="fin.alerts.fraud",
)

app = service.app

if __name__ == "__main__":
    service.run(port=int(os.environ.get("PORT", 8001)))
