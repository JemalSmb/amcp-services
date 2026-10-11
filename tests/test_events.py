from shared.events import DomainEvent


def _evt(**kw):
    base = dict(domain="finance", event_type="t", severity="high", source={"s": "x"}, payload={"a": 1})
    base.update(kw)
    return DomainEvent.create(**base)


def test_create_assigns_unique_id_and_utc_timestamp():
    a, b = _evt(), _evt()
    assert a.id != b.id
    assert a.timestamp.endswith("+00:00")


def test_to_dict_replaces_none_metadata_with_empty_dict():
    assert _evt().to_dict()["metadata"] == {}


def test_roundtrip():
    e = _evt(metadata={"w": []})
    assert DomainEvent.from_dict(e.to_dict()) == e


def test_from_dict_applies_defaults():
    e = DomainEvent.from_dict({"domain": "d", "event_type": "t"})
    assert e.severity == "info" and e.payload == {} and e.source == {} and e.id and e.timestamp


def test_from_dict_requires_domain_and_event_type():
    import pytest
    with pytest.raises(KeyError):
        DomainEvent.from_dict({"event_type": "t"})
