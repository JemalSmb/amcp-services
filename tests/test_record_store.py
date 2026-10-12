import json

import alert_store
import record_store
from test_alert_store import Conn, Pool


import pytest


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    record_store._mem.clear()
    monkeypatch.setattr(record_store, "_table_ready", False)
    monkeypatch.setattr(alert_store, "_pool", None)


async def test_memory_put_get_list():
    await record_store.put("c", "1", {"id": "1", "v": 1})
    assert await record_store.get("c", "1") == {"id": "1", "v": 1}
    assert await record_store.get("c", "zzz") is None
    assert len(await record_store.list_all("c")) == 1


async def test_owner_isolation():
    await record_store.put("settings", "s", {"id": "s", "v": "a"}, owner="alice")
    assert await record_store.list_all("settings", owner="bob") == []
    assert (await record_store.get("settings", "s", owner="alice"))["v"] == "a"


async def test_list_seeded_seeds_once():
    first = await record_store.list_seeded("automations")
    assert {r["id"] for r in first} == {"au-7001", "au-7002", "au-7003"}
    first[0]["status"] = "changed"
    await record_store.put("automations", first[0]["id"], first[0])
    again = await record_store.list_seeded("automations")
    assert len(again) == 3 and any(r["status"] == "changed" for r in again)


async def test_list_seeded_unknown_collection_empty():
    assert await record_store.list_seeded("nope") == []


async def test_init_without_pool_stays_in_memory():
    await record_store.init()
    assert record_store._table_ready is False


async def test_init_creates_table_when_pool_available(monkeypatch):
    conn = Conn()
    monkeypatch.setattr(alert_store, "_pool", Pool(conn))
    await record_store.init()
    assert record_store._table_ready and "platform_records" in conn.executed[0][0]


async def test_init_failure_falls_back(monkeypatch):
    monkeypatch.setattr(alert_store, "_pool", Pool(Conn(fail=True)))
    await record_store.init()
    assert record_store._table_ready is False


async def test_put_writes_through_to_db(monkeypatch):
    conn = Conn()
    monkeypatch.setattr(alert_store, "_pool", Pool(conn))
    monkeypatch.setattr(record_store, "_table_ready", True)
    await record_store.put("c", "1", {"id": "1"}, owner="u")
    assert conn.executed[0][1:4] == ("c", "u", "1") and json.loads(conn.executed[0][4]) == {"id": "1"}


async def test_db_write_failure_keeps_memory_copy(monkeypatch):
    monkeypatch.setattr(alert_store, "_pool", Pool(Conn(fail=True)))
    monkeypatch.setattr(record_store, "_table_ready", True)
    await record_store.put("c", "1", {"id": "1"})
    assert await record_store.get("c", "1") == {"id": "1"}  # read also fails over to memory


async def test_list_reads_db_when_ready(monkeypatch):
    conn = Conn(rows=[{"data": json.dumps({"id": "db"})}])
    monkeypatch.setattr(alert_store, "_pool", Pool(conn))
    monkeypatch.setattr(record_store, "_table_ready", True)
    assert await record_store.list_all("c") == [{"id": "db"}]
