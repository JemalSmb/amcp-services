"""
Generic JSON record storage for the dashboard's non-alert collections
(automations, settings, assets, ...), backed by Supabase Postgres.

Shares the connection pool opened by alert_store. Same degrade-gracefully
pattern as the rest of the gateway: if the DB isn't configured or a query
fails, records live in an in-process dict instead (lost on restart).

The table is created on startup if it doesn't exist, so no manual SQL step:

    create table if not exists platform_records (
        collection text not null,
        owner text not null default '',   -- '' = shared, else the user's id
        id text not null,
        data jsonb not null,
        updated_at timestamptz not null default now(),
        primary key (collection, owner, id)
    );
"""

import json
import logging
from typing import Any, Dict, List, Optional

import alert_store
from seed_data import SEEDS

logger = logging.getLogger(__name__)

_mem: Dict[tuple, Dict[str, Dict[str, Any]]] = {}
_table_ready = False

_CREATE = """
create table if not exists platform_records (
    collection text not null,
    owner text not null default '',
    id text not null,
    data jsonb not null,
    updated_at timestamptz not null default now(),
    primary key (collection, owner, id)
);
alter table platform_records enable row level security;
"""


async def init() -> None:
    """Call after alert_store.init_pool()."""
    global _table_ready
    pool = alert_store._pool
    if pool is None:
        return
    try:
        async with pool.acquire() as conn:
            await conn.execute(_CREATE)
        _table_ready = True
        logger.info("platform_records table ready")
    except Exception as exc:  # noqa: BLE001 - fall back to memory
        logger.warning("could not prepare platform_records, using memory store: %s", exc)


def _db():
    return alert_store._pool if _table_ready else None


async def put(collection: str, record_id: str, data: Dict[str, Any], owner: str = "") -> None:
    _mem.setdefault((collection, owner), {})[record_id] = data
    pool = _db()
    if pool is None:
        return
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                "insert into platform_records (collection, owner, id, data) values ($1, $2, $3, $4::jsonb) "
                "on conflict (collection, owner, id) do update set data = excluded.data, updated_at = now()",
                collection,
                owner,
                record_id,
                json.dumps(data),
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("failed to write %s/%s to DB (kept in memory): %s", collection, record_id, exc)


async def delete(collection: str, record_id: str, owner: str = "") -> None:
    _mem.get((collection, owner), {}).pop(record_id, None)
    pool = _db()
    if pool is None:
        return
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                "delete from platform_records where collection = $1 and owner = $2 and id = $3",
                collection,
                owner,
                record_id,
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("failed to delete %s/%s from DB: %s", collection, record_id, exc)


async def list_all(collection: str, owner: str = "") -> List[Dict[str, Any]]:
    pool = _db()
    if pool is not None:
        try:
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    "select data from platform_records where collection = $1 and owner = $2 order by id",
                    collection,
                    owner,
                )
            return [json.loads(r["data"]) for r in rows]
        except Exception as exc:  # noqa: BLE001
            logger.warning("failed to read %s from DB, using memory: %s", collection, exc)
    return list(_mem.get((collection, owner), {}).values())


async def get(collection: str, record_id: str, owner: str = "") -> Optional[Dict[str, Any]]:
    for row in await list_all(collection, owner):
        if row.get("id") == record_id:
            return row
    return None


async def list_seeded(collection: str, owner: str = "") -> List[Dict[str, Any]]:
    """list_all, but writes SEEDS[collection] first if the collection is empty."""
    rows = await list_all(collection, owner)
    if rows or collection not in SEEDS:
        return rows
    for record in SEEDS[collection]:
        await put(collection, record["id"], record, owner)
    return await list_all(collection, owner)
