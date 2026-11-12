"""The frozen listener contract and the existing session JSONL event path."""
import asyncio
import inspect
import json
from types import SimpleNamespace

import pytest

from server import upstream_telemetry as telemetry


@pytest.fixture
def records(monkeypatch):
    monkeypatch.setattr(telemetry, "_listeners", [])
    seen = []
    telemetry.add_listener(seen.append)
    return seen


def test_frozen_signature_and_listener_fields(records, monkeypatch):
    signature = inspect.signature(telemetry.record)
    assert list(signature.parameters) == [
        "upstream", "route", "purpose", "outcome", "duration_ms", "status",
        "attempt", "queue_wait_ms", "session_id", "detail"]
    assert all(p.kind == inspect.Parameter.KEYWORD_ONLY
               for p in signature.parameters.values())
    monkeypatch.setattr(telemetry.time, "time", lambda: 1234.5)
    telemetry.record(upstream="gateway", route="models", purpose="preflight",
                     outcome="ok", duration_ms=12.0)
    assert records == [dict(
        upstream="gateway", route="models", purpose="preflight", outcome="ok",
        duration_ms=12.0, status=None, attempt=1, queue_wait_ms=None,
        session_id=None, detail=None, t=1234.5)]


def test_listener_failures_and_mutations_do_not_affect_other_listeners(records):
    def broken(event):
        event["outcome"] = "changed"
        raise asyncio.CancelledError()

    telemetry.add_listener(broken)
    later = []
    telemetry.add_listener(later.append)
    telemetry.record(upstream="gateway", route="messages", purpose="director",
                     outcome="http_error", duration_ms=2.0, status=429, attempt=2)
    assert records == later
    assert records[0]["outcome"] == "http_error"


def test_record_writes_through_session_store_to_events_jsonl(records, monkeypatch, tmp_path):
    from server.session import Session, registry
    from server.storage import SessionStore

    sid = "s_1234567890_abcdef"
    store = SessionStore.__new__(SessionStore)
    store.started_at = 1000.0
    store.events_path = tmp_path / "events.jsonl"
    session = Session.__new__(Session)
    session.store = store
    session._closed = False
    monkeypatch.setattr(registry, "_sessions", {sid: session})
    monkeypatch.setattr(telemetry.time, "time", lambda: 1001.25)
    with store.events_path.open("a", encoding="utf-8", buffering=1) as store.events_fh:
        telemetry.record(
            upstream="elevenlabs", route="tts_ws", purpose="tts", outcome="ok",
            duration_ms=40.5, status=101, attempt=2, queue_wait_ms=25.0,
            session_id=sid, detail="test")
        event = json.loads(store.events_path.read_text(encoding="utf-8"))
        assert event == {**records[0], "type": "upstream_attempt",
                         "t": 1.25, "wall": 1001.25}
    # A stale session, closed handle, or failed store cannot interrupt a call.
    telemetry.record(upstream="gateway", route="messages", purpose="steering",
                     outcome="ok", duration_ms=1.0, session_id=sid)
    session.store = SimpleNamespace(event=lambda *a, **kw: 1 / 0)
    telemetry.record(upstream="gateway", route="messages", purpose="steering",
                     outcome="ok", duration_ms=1.0, session_id=sid)
    telemetry.record(upstream="gateway", route="messages", purpose="steering",
                     outcome="ok", duration_ms=1.0, session_id="missing")
    assert len(records) == 4
    assert len(store.events_path.read_text(encoding="utf-8").splitlines()) == 1
