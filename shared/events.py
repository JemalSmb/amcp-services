"""
Shared event contract for all domain services.

Mirrors the DomainEvent/DomainAlert shapes defined in
amcp-platform-core/domains/_base/service_interface.py, kept as a local,
dependency-free copy so this repo doesn't need a runtime dependency on
amcp-platform-core.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Optional
import uuid


@dataclass
class DomainEvent:
    id: str
    timestamp: str
    domain: str
    event_type: str
    severity: str
    source: Dict[str, str]
    payload: Dict[str, Any]
    metadata: Optional[Dict[str, Any]] = field(default=None)

    @classmethod
    def create(
        cls,
        domain: str,
        event_type: str,
        severity: str,
        source: Dict[str, str],
        payload: Dict[str, Any],
        metadata: Optional[Dict[str, Any]] = None,
    ) -> "DomainEvent":
        return cls(
            id=str(uuid.uuid4()),
            timestamp=datetime.now(timezone.utc).isoformat(),
            domain=domain,
            event_type=event_type,
            severity=severity,
            source=source,
            payload=payload,
            metadata=metadata,
        )

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DomainEvent":
        return cls(
            id=data.get("id", str(uuid.uuid4())),
            timestamp=data.get("timestamp", datetime.now(timezone.utc).isoformat()),
            domain=data["domain"],
            event_type=data["event_type"],
            severity=data.get("severity", "info"),
            source=data.get("source", {}),
            payload=data.get("payload", {}),
            metadata=data.get("metadata"),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "timestamp": self.timestamp,
            "domain": self.domain,
            "event_type": self.event_type,
            "severity": self.severity,
            "source": self.source,
            "payload": self.payload,
            "metadata": self.metadata or {},
        }
