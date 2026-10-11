"""
Alert persistence, backed by Supabase Postgres.

Degrades the same way NATS does elsewhere in this service: if
SUPABASE_DB_URL isn't configured or the DB is unreachable, the gateway
keeps working off the in-memory buffer only, and this module logs a
warning instead of crashing startup.

Table (run once in the Supabase SQL editor -- Project -> SQL Editor):

    create table if not exists alerts (
        id bigint generated always as identity primary key,
        subject text not null,
        domain text,
        payload jsonb not null,
        received_at timestamptz not null default now()
    );
    create index if not exists alerts_domain_idx on alerts (domain);
    create index if not exists alerts_received_at_idx on alerts (received_at desc);
"""

import logging
import os
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

SUPABASE_DB_URL = os.environ.get("SUPABASE_DB_URL", "")

_pool = None


async def init_pool() -> None:
    global _pool
    if not SUPABASE_DB_URL or "REPLACE_ME" in SUPABASE_DB_URL:
        logger.warning("SUPABASE_DB_URL not configured; alerts will not be persisted (memory-only)")
        return
    try:
        import asyncpg

        _pool = await asyncpg.create_pool(SUPABASE_DB_URL, min_size=1, max_size=5, command_timeout=5)
        logger.info("connected to Supabase Postgres for alert persistence")
    except Exception as exc:  # noqa: BLE001 - degrade to memory-only
        logger.warning("could not connect to SUPABASE_DB_URL: %s", exc)
        _pool = None


async def close_pool() -> None:
    if _pool is not None:
        await _pool.close()


async def persist(subject: str, domain: Optional[str], payload: Dict[str, Any]) -> None:
    if _pool is None:
        return
    try:
        import json

        async with _pool.acquire() as conn:
            await conn.execute(
                "insert into alerts (subject, domain, payload) values ($1, $2, $3::jsonb)",
                subject,
                domain,
                json.dumps(payload),
            )
    except Exception as exc:  # noqa: BLE001 - never let a DB hiccup drop the request
        logger.warning("failed to persist alert: %s", exc)


async def recent(limit: int = 100, domain: Optional[str] = None) -> Optional[List[Dict[str, Any]]]:
    """Returns None (caller should fall back to the in-memory buffer) if the DB isn't configured."""
    if _pool is None:
        return None
    try:
        import json

        async with _pool.acquire() as conn:
            if domain:
                rows = await conn.fetch(
                    "select subject, domain, payload, received_at from alerts "
                    "where domain = $1 order by received_at desc limit $2",
                    domain,
                    limit,
                )
            else:
                rows = await conn.fetch(
                    "select subject, domain, payload, received_at from alerts order by received_at desc limit $1",
                    limit,
                )
        return [
            {**json.loads(row["payload"]), "_subject": row["subject"], "_received_at": row["received_at"].isoformat()}
            for row in rows
        ]
    except Exception as exc:  # noqa: BLE001 - fall back to in-memory buffer
        logger.warning("failed to read alerts from DB, falling back to memory buffer: %s", exc)
        return None
