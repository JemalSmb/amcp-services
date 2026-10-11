"""
Process Anomaly Detector Service (manufacturing, passive)

Consumes machine sensor events on mfg.sensors.process, scores them
against the manufacturing_process_anomaly model, and publishes alerts to
mfg.alerts.process when the model flags something above threshold.
"""

import logging
import os

from shared.base_service import DomainMicroservice

logging.basicConfig(level=logging.INFO)

service = DomainMicroservice(
    service_name="process-anomaly-detector",
    domain="manufacturing",
    model_id="manufacturing_process_anomaly",
    input_subject="mfg.sensors.process",
    output_subject="mfg.alerts.process",
)

app = service.app

if __name__ == "__main__":
    service.run(port=int(os.environ.get("PORT", 8003)))
