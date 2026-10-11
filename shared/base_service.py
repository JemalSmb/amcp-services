"""
Generic domain service runtime.

Implements the plan's minimum event flow:
    1. event comes in (NATS subscribe)
    2. service validates payload
    3. service calls the inference endpoint
    4. service formats the result
    5. service publishes a domain event (alert)

A concrete service is just this class configured with a domain, model_id,
and input/output NATS subjects — see services/cybersecurity/threat-detection
for a worked example. Also exposes a small FastAPI app (/health, /trigger)
so the flow can be exercised over HTTP without a NATS broker running.
"""

import asyncio
import json
import logging
import os
from typing import Any, Dict, Optional

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from .events import DomainEvent

logger = logging.getLogger(__name__)


class TriggerRequest(BaseModel):
    features: Dict[str, float]
    source: Optional[Dict[str, str]] = None


class DomainMicroservice:
    def __init__(
        self,
        service_name: str,
        domain: str,
        model_id: str,
        input_subject: str,
        output_subject: str,
        score_threshold: float = 0.5,
    ) -> None:
        self.service_name = service_name
        self.domain = domain
        self.model_id = model_id
        self.input_subject = input_subject
        self.output_subject = output_subject
        self.score_threshold = score_threshold

        self.inference_url = os.environ.get("INFERENCE_URL", "http://localhost:8000")
        self.nats_url = os.environ.get("NATS_URL", "nats://localhost:4222")

        self._nats_client = None
        self._http = httpx.AsyncClient(base_url=self.inference_url, timeout=10.0)

        self.app = FastAPI(title=f"AMCP {service_name}")
        self._register_routes()

    def _register_routes(self) -> None:
        @self.app.get("/health")
        async def health() -> Dict[str, Any]:
            return {
                "service": self.service_name,
                "domain": self.domain,
                "model_id": self.model_id,
                "status": "ok",
            }

        @self.app.post("/trigger")
        async def trigger(request: TriggerRequest) -> Dict[str, Any]:
            event = DomainEvent.create(
                domain=self.domain,
                event_type=f"{self.service_name}.input",
                severity="info",
                source=request.source or {"service": self.service_name},
                payload={"features": request.features},
            )
            result = await self.handle_event(event)
            if result is None:
                return {"published": False}
            return {"published": True, "event": result.to_dict()}

    async def validate_payload(self, event: DomainEvent) -> Dict[str, float]:
        """Basic payload validation. Raises ValueError on malformed input."""
        features = event.payload.get("features")
        if not isinstance(features, dict):
            raise ValueError("event.payload.features must be an object of name -> number")
        return {k: float(v) for k, v in features.items()}

    async def call_inference(self, features: Dict[str, float]) -> Dict[str, Any]:
        response = await self._http.post(f"/models/{self.model_id}/predict", json={"features": features})
        response.raise_for_status()
        return response.json()

    def format_result(self, event: DomainEvent, prediction: Dict[str, Any]) -> Optional[DomainEvent]:
        """Only emit an alert event when the model flags something notable."""
        score = prediction.get("score", 0.0)
        if score < self.score_threshold:
            return None

        severity = "critical" if score >= 0.9 else "high" if score >= 0.75 else "medium"
        return DomainEvent.create(
            domain=self.domain,
            event_type=f"{self.service_name}.alert",
            severity=severity,
            source={"service": self.service_name, "model_id": self.model_id},
            payload={
                "label": prediction.get("label"),
                "score": score,
                "trained": prediction.get("trained", False),
                "input_event_id": event.id,
            },
            metadata={"warnings": prediction.get("warnings", [])},
        )

    async def handle_event(self, event: DomainEvent) -> Optional[DomainEvent]:
        try:
            features = await self.validate_payload(event)
        except ValueError as exc:
            logger.warning("[%s] rejected event %s: %s", self.service_name, event.id, exc)
            return None

        try:
            prediction = await self.call_inference(features)
        except httpx.HTTPError as exc:
            logger.error("[%s] inference call failed: %s", self.service_name, exc)
            return None

        result = self.format_result(event, prediction)
        if result is not None:
            await self.publish(result)
        return result

    async def publish(self, event: DomainEvent) -> None:
        if self._nats_client is not None:
            await self._nats_client.publish(self.output_subject, json.dumps(event.to_dict()).encode())
            logger.info("[%s] published to %s: %s", self.service_name, self.output_subject, event.id)
        else:
            logger.info("[%s] (no NATS connection) would publish to %s: %s", self.service_name, self.output_subject, event.to_dict())

    async def connect_nats(self) -> None:
        try:
            import nats
        except ImportError:
            logger.warning("nats-py not installed; running in HTTP-only mode")
            return

        try:
            self._nats_client = await nats.connect(self.nats_url)
        except Exception as exc:  # noqa: BLE001 - degrade to HTTP-only mode
            logger.warning("could not connect to NATS at %s: %s", self.nats_url, exc)
            self._nats_client = None
            return

        async def _on_message(msg) -> None:
            try:
                data = json.loads(msg.data.decode())
                event = DomainEvent.from_dict(data)
            except (json.JSONDecodeError, KeyError) as exc:
                logger.warning("[%s] dropped malformed message: %s", self.service_name, exc)
                return
            await self.handle_event(event)

        await self._nats_client.subscribe(self.input_subject, cb=_on_message)
        logger.info("[%s] subscribed to %s, publishing to %s", self.service_name, self.input_subject, self.output_subject)

    async def shutdown(self) -> None:
        if self._nats_client is not None:
            await self._nats_client.close()
        await self._http.aclose()

    def run(self, host: str = "0.0.0.0", port: int = 8000) -> None:
        import uvicorn

        @self.app.on_event("startup")
        async def _startup() -> None:
            await self.connect_nats()

        @self.app.on_event("shutdown")
        async def _shutdown() -> None:
            await self.shutdown()

        uvicorn.run(self.app, host=host, port=port)
