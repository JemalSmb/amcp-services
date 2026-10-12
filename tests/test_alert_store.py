"""Alert persistence: degrades to None/no-op when DB missing or failing."""

import json
from datetime import datetime, timezone

import alert_store


class Conn:
    def __init__(self, rows=None, fail=False):
        self.rows, self.fail, self.executed = rows or [], fail, []

    async def execute(self, *a):
        if self.fail:
            raise RuntimeError("db down")
        self.executed.append(a)

    async def fetch(self, *a):
        if self.fail:
            raise RuntimeError("db down")
        self.executed.append(a)
        return self.rows


class Pool:
    def __init__(self, conn): self.conn = conn
    def acquire(self):
        conn = self.conn

        class CM:
            async def __aenter__(self): return conn
            async def __aexit__(self, *a): return False
        return CM()


async def test_init_pool_unconfigured_leaves_memory_only(monkeypatch):
    monkeypatch.setattr(alert_store, "SUPABASE_DB_URL", "")
    monkeypatch.setattr(alert_store, "_pool", None)
    await alert_store.init_pool()
    assert alert_store._pool is None


async def test_init_pool_placeholder_url_ignored(monkeypatch):
    monkeypatch.setattr(alert_store, "SUPABASE_DB_URL", "postgres://u:REPLACE_ME@h/db")
    monkeypatch.setattr(alert_store, "_pool", None)
    await alert_store.init_pool()
    assert alert_store._pool is None


async def test_init_pool_unreachable_degrades(monkeypatch):
    import asyncpg

    async def boom(*a, **k): raise OSError("unreachable")
    monkeypatch.setattr(asyncpg, "create_pool", boom)
    monkeypatch.setattr(alert_store, "SUPABASE_DB_URL", "postgres://u:p@h/db")
    monkeypatch.setattr(alert_store, "_pool", None)
    await alert_store.init_pool()
    assert alert_store._pool is None


async def test_persist_noop_without_pool(monkeypatch):
    monkeypatch.setattr(alert_store, "_pool", None)
    await alert_store.persist("s", "d", {})


async def test_persist_writes_json(monkeypatch):
    conn = Conn()
    monkeypatch.setattr(alert_store, "_pool", Pool(conn))
    await alert_store.persist("fin.alerts.fraud", "finance", {"score": 0.9})
    _, subject, domain, payload = conn.executed[0]
    assert (subject, domain, json.loads(payload)) == ("fin.alerts.fraud", "finance", {"score": 0.9})


async def test_persist_swallows_db_errors(monkeypatch):
    monkeypatch.setattr(alert_store, "_pool", Pool(Conn(fail=True)))
    await alert_store.persist("s", "d", {})


async def test_recent_none_without_pool(monkeypatch):
    monkeypatch.setattr(alert_store, "_pool", None)
    assert await alert_store.recent() is None


async def test_recent_maps_rows_and_filters_domain(monkeypatch):
    ts = datetime(2026, 1, 1, tzinfo=timezone.utc)
    conn = Conn(rows=[{"subject": "s", "domain": "finance", "payload": json.dumps({"id": "1"}), "received_at": ts}])
    monkeypatch.setattr(alert_store, "_pool", Pool(conn))
    out = await alert_store.recent(limit=5, domain="finance")
    assert out == [{"id": "1", "_subject": "s", "_received_at": ts.isoformat()}]
    assert "where domain" in conn.executed[0][0] and conn.executed[0][1:] == ("finance", 5)


async def test_recent_returns_none_on_db_error(monkeypatch):
    monkeypatch.setattr(alert_store, "_pool", Pool(Conn(fail=True)))
    assert await alert_store.recent() is None
