"""Append-only audit trail and security-event log for the admin console.

Entries live in record_store (Supabase `platform_records`, in-memory fallback) under the
collections "audit-log" and "security-events". They are written whenever an admin changes
something, so the Audit page shows what really happened rather than sample data.
"""

import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Union

import record_store

AUDIT = "audit-log"
SECURITY = "security-events"
_MAX_RETURNED = 200


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def actor_name(actor: Union[str, Dict[str, Any]]) -> str:
    if isinstance(actor, dict):
        return str(actor.get("email") or actor.get("sub") or "unknown")
    return actor or "unknown"


async def record(actor: Union[str, Dict[str, Any]], action: str, target: str) -> None:
    entry = {
        "id": uuid.uuid4().hex,
        "actor": actor_name(actor),
        "action": action,
        "target": target,
        "occurredAt": _now(),
    }
    await record_store.put(AUDIT, entry["id"], entry)


async def record_security(event_type: str, severity: str, actor: Union[str, Dict[str, Any]]) -> None:
    entry = {
        "id": uuid.uuid4().hex,
        "type": event_type,
        "severity": severity,
        "actor": actor_name(actor),
        "occurredAt": _now(),
    }
    await record_store.put(SECURITY, entry["id"], entry)


async def newest(collection: str, limit: int = _MAX_RETURNED) -> List[Dict[str, Any]]:
    rows = await record_store.list_all(collection)
    return sorted(rows, key=lambda r: r.get("occurredAt", ""), reverse=True)[:limit]
