"""Metric extraction through the production event, lifecycle and archive paths."""
from __future__ import annotations

import asyncio
import json
import io
import time
import types
from pathlib import Path
import weakref

import pytest

from server import metrics


def documents(capsys):
    return [json.loads(line) for line in capsys.readouterr().out.splitlines()
            if line.startswith('{"_aws":')]


def samples(docs, name):
    return [doc[name] for doc in docs if name in doc]


@pytest.fixture(autouse=True)
def reset_metrics(monkeypatch):
    monkeypatch.setenv("METRICS_EMF", "1")
    from server import upstream_telemetry
    monkeypatch.setattr(upstream_telemetry, "_listeners", [])
    monkeypatch.setattr(metrics, "_cohorts", {})
    monkeypatch.setattr(metrics, "_stores", weakref.WeakSet())
    monkeypatch.setattr(metrics, "_upstream_module", None)


@pytest.fixture
def stores(tmp_path, monkeypatch):
    from server import storage, conversation_audio
    monkeypatch.setattr(storage, "DATA_DIR", tmp_path)
    monkeypatch.setattr(storage, "SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(storage, "PARTICIPANTS_DIR", tmp_path / "participants")
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "index.db")
    monkeypatch.setattr(conversation_audio, "finish_later", lambda path: None)
    created = []

    def make(group="study", run_id="a" * 12):
        store = storage.SessionStore(
            f"s_1772460300_{len(created):06x}", scenario="S1A", model="test",
            participant_id=None, capture_audio=False, cohort=group, run_id=run_id)
        created.append(store)
        return store

    yield make
    for store in created:
        if store.events_fh is not None:
            store.close()


def test_emf_shape_raw_sample_rollups_and_no_identifiers(capsys):
    before = int(time.time() * 1000)
    metrics.emit("UpstreamLatency", 12.5, Purpose="actor", Upstream="gateway",
                 Outcome="ok", Cohort="study", session_id="secret-session",
                 participant_id="secret-person", run_id="secret-run", Route="secret-route")
    doc, = documents(capsys)
    assert before <= doc["_aws"]["Timestamp"] <= int(time.time() * 1000)
    directive, = doc["_aws"]["CloudWatchMetrics"]
    assert directive["Namespace"] == "RelationalFluency"
    assert directive["Metrics"] == [{"Name": "UpstreamLatency", "Unit": "Milliseconds"}]
    assert [] in directive["Dimensions"] and ["Cohort"] in directive["Dimensions"]
    assert ["Upstream"] in directive["Dimensions"]
    for dimension_set in directive["Dimensions"]:
        assert set(dimension_set) <= set(metrics.DIMENSIONS)
        assert all(isinstance(doc[key], str) for key in dimension_set)
    assert doc["UpstreamLatency"] == 12.5
    assert "secret" not in json.dumps(doc)
    assert set(doc) == {"_aws", "UpstreamLatency", "Purpose", "Upstream", "Outcome", "Cohort"}


@pytest.mark.parametrize("name,value,dims", [
    ("UnboundedMetric", 1, {}), ("ReplyMissing", float("nan"), {}),
    ("ReplyMissing", float("inf"), {}), ("ReplyMissing", -1, {}),
    ("ReplyMissing", True, {}), ("ReplyMissing", None, {}),
    ("ReplyMissing", 1, {"Cohort": "participant-123"}),
    ("ReplyMissing", 1, {"Outcome": "exception text with identifiers"}),
    ("UpstreamRequests", 1, {"Purpose": "unbounded"}),
    ("UpstreamRequests", 1, {"Upstream": "unknown-host"}),
])
def test_unbounded_or_invalid_samples_are_rejected(capsys, name, value, dims):
    metrics.emit(name, value, **dims)
    assert documents(capsys) == []


def test_emitter_disabled_and_swallows_output_and_encoding_errors(monkeypatch, capsys):
    monkeypatch.setenv("METRICS_EMF", "0")
    metrics.emit("ReplyMissing")
    assert documents(capsys) == []
    monkeypatch.setenv("METRICS_EMF", "1")
    with monkeypatch.context() as patch:
        patch.setattr(metrics.sys.stdout, "write", lambda *a, **k: (_ for _ in ()).throw(OSError("closed")))
        assert metrics.emit("ReplyMissing") is None
    monkeypatch.setattr(metrics.json, "dumps", lambda *a, **k: (_ for _ in ()).throw(ValueError("bad")))
    assert metrics.emit("ReplyMissing") is None


@pytest.mark.parametrize("group,run_id,expected", [
    ("study", "a" * 12, "study"), ("internal", "a" * 12, "internal"),
    ("unattributed", "a" * 12, "study"), (None, "a" * 12, "study"),
    ("study", None, "internal"), ("unattributed", None, "internal"),
    (None, None, "internal"),
])
def test_started_and_lifetime_once_from_store(stores, capsys, group, run_id, expected):
    store = stores(group, run_id=run_id)
    metrics.session_created(store)
    store.started_at = time.time() - 3
    store.close()
    store.close()
    docs = documents(capsys)
    assert samples(docs, "EncounterStarted") == [1]
    assert len(samples(docs, "EncounterDurationS")) == 1
    assert 3 <= samples(docs, "EncounterDurationS")[0] < 5
    assert all(doc["Cohort"] == expected for doc in docs)
    assert store.id not in metrics._cohorts


def test_event_counters_and_bounded_voice_kinds(stores, capsys):
    from tools.sim import analyze
    assert set(metrics.BENIGN_VOICE_ERRORS) == set(analyze.REFUSED_CREATE + analyze.BENIGN_CANCEL)
    store = stores()
    capsys.readouterr()
    store.event("reply_missing", agent_id="private-member")
    store.event("assistant_turn", text="a later recovery")
    store.event("room_member_dropped", agent_id="private-member")
    store.event("room_member_lost", agent_id="private-member")
    for kind in metrics.BENIGN_VOICE_ERRORS:
        store.event("voice_error", message=kind)
    for message in ("timed out waiting", "socket closed", "audio invalid", "private error text"):
        store.event("voice_error", message=message)
    docs = documents(capsys)
    assert samples(docs, "ReplyMissing") == [1]
    assert samples(docs, "RoomMemberDropped") == [1]
    assert samples(docs, "RoomMemberLost") == [1]
    assert samples(docs, "VoiceError") == [1, 1, 1, 1]
    assert {d["Outcome"] for d in docs if "VoiceError" in d} == {"timeout", "connection", "audio", "other"}
    assert "private" not in json.dumps(docs)


def test_earliest_playback_per_session_speech_end_and_missing_ack(stores, capsys):
    first, second = stores(), stores("internal")
    capsys.readouterr()
    for store, speech_end, played in [(first, 1, None), (first, 1, 5), (first, 1, 3),
                                      (first, 1, 4), (first, 2, None), (first, 2, 1),
                                      (first, None, 1), (second, 1, 2)]:
        store.event("turn_timing", vad_speech_end=speech_end, first_audio_played=played)
    assert documents(capsys) == []
    first.close()
    second.close()
    docs = documents(capsys)
    assert samples(docs, "SpeechEndToFirstPlayed") == [2000, 1000]
    assert samples(docs, "FirstPlayedMissing") == [1]
    metrics.session_closed(first, 99)
    assert documents(capsys) == []


def test_real_turn_timer_flush_and_duplicate_acks(stores, capsys, monkeypatch):
    from server.turn_timing import TurnTimer
    store = stores()
    timer = TurnTimer(store)
    clock = [store.started_at + 1]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    timer.speech_end()
    turn = timer.started("private-member")
    clock[0] += 2
    timer.ack({"phase": "start", "turn": turn})
    timer.ack({"phase": "start", "turn": turn})
    timer.flush()
    timer.flush()
    store.close()
    assert samples(documents(capsys), "SpeechEndToFirstPlayed") == [2000]


@pytest.fixture
def run_files(tmp_path, monkeypatch):
    from server import runs
    monkeypatch.setattr(runs, "DATA_DIR", tmp_path)
    monkeypatch.setattr(runs, "RUNS_DIR", tmp_path / "runs")

    def make(run_id, participant="same-person"):
        run = {"run_id": run_id, "participant_id": participant, "index": 0,
               "scenarios": [{"id": "S1A"}, {"id": "S2A"}], "completed": [],
               "cohort": "study", "withdrawn": None}
        runs.save(run)
        return run
    return runs, make


def test_completion_only_after_durable_advance_and_idempotent(run_files, capsys):
    runs, make = run_files
    make("a" * 12)
    runs.advance("a" * 12, "encounter-one")
    runs.advance("a" * 12, "encounter-one")
    runs.advance("a" * 12, "encounter-two")
    runs.advance("a" * 12, "encounter-three")  # already at end
    assert samples(documents(capsys), "EncounterCompleted") == [1, 1]
    assert len(runs.get("a" * 12)["completed"]) == 2


def test_failed_save_does_not_count_completion(run_files, monkeypatch, capsys):
    runs, make = run_files
    make("a" * 12)
    monkeypatch.setattr(runs, "save", lambda run: (_ for _ in ()).throw(OSError("disk")))
    with pytest.raises(OSError):
        runs.advance("a" * 12, "encounter-one")
    assert documents(capsys) == []


def test_withdrawal_copies_and_repeat_are_one_encounter(run_files, capsys):
    runs, make = run_files
    make("a" * 12)
    make("b" * 12)
    runs.withdraw("a" * 12, "encounter-one")
    runs.withdraw("a" * 12, "encounter-one")
    runs.withdraw("b" * 12, "encounter-one")
    runs.advance("a" * 12, "encounter-one")
    docs = documents(capsys)
    assert samples(docs, "EncounterWithdrawn") == [1]
    assert samples(docs, "EncounterCompleted") == []
    assert runs.get("b" * 12)["withdrawn"]["session_id"] == "encounter-one"


def test_withdrawal_without_encounter_does_not_invent_one(run_files, capsys):
    runs, make = run_files
    make("a" * 12)
    runs.withdraw("a" * 12)
    assert documents(capsys) == []


def test_archive_failure_is_from_completed_upload_result(tmp_path, capsys):
    from server import archive
    (tmp_path / "manifest.json").write_text('{"cohort":"study","run_id":"aaaaaaaaaaaa"}', encoding="utf-8")
    (tmp_path / "record.json").write_text("{}", encoding="utf-8")

    class S3:
        def upload_file(self, *args):
            if "record.json" in args[0]:
                raise RuntimeError("denied")

    result = archive.archive_session(tmp_path, client=S3(), bucket="test")
    docs = documents(capsys)
    assert not result["ok"] and samples(docs, "ArchiveFailed") == [1]
    assert docs[0]["Cohort"] == "study"
    (tmp_path / "record.json").unlink()
    assert archive.archive_session(tmp_path, client=S3(), bucket="test")["ok"]
    assert documents(capsys) == []


def test_actual_lag_report_hook_and_gauge_interval(monkeypatch, capsys):
    from server import loop_lag

    async def check():
        sleeps = []

        async def gauge_sleep(seconds):
            sleeps.append(seconds)
            raise asyncio.CancelledError

        monkeypatch.setattr(metrics.asyncio, "sleep", gauge_sleep)
        with pytest.raises(asyncio.CancelledError):
            await metrics._gauge_loop(types.SimpleNamespace(list_ids=lambda: ["a", "b"]))
        assert sleeps == [60]
        ticks = iter([0, 0, 1, 1])
        monkeypatch.setattr(loop_lag.time, "perf_counter", lambda: next(ticks))
        count = 0

        async def lag_sleep(seconds):
            nonlocal count
            count += 1
            if count > 1:
                raise asyncio.CancelledError

        monkeypatch.setattr(loop_lag.asyncio, "sleep", lag_sleep)
        with pytest.raises(asyncio.CancelledError):
            await loop_lag.LoopLagProbe(interval_s=0.05, report_every_s=0.5)._run()

    asyncio.run(check())
    docs = documents(capsys)
    assert samples(docs, "ActiveSessions") == [2]
    assert samples(docs, "LoopLagP99") == [950]
    assert samples(docs, "LoopLagMax") == [950]


def test_real_upstream_listener_wiring_once_and_no_session_event_double_count(stores, monkeypatch, capsys):
    from server import app as appmod, upstream_telemetry
    from server.session import Session
    store = stores()
    session = object.__new__(Session)
    session.store, session._closed = store, False
    monkeypatch.setitem(appmod.registry._sessions, store.id, session)
    capsys.readouterr()

    async def check():
        await metrics.start(appmod.registry)
        first_tasks = (metrics._gauge_task, metrics._sweep_task)
        await metrics.start(appmod.registry)
        assert (metrics._gauge_task, metrics._sweep_task) == first_tasks
        assert upstream_telemetry._listeners == [metrics.upstream_attempt]
        upstream_telemetry.record(upstream="gateway", route="chat_completions_stream", purpose="actor",
                                  outcome="http_error", duration_ms=3, status=429, session_id=store.id,
                                  detail="private error")
        upstream_telemetry.record(upstream="gateway", route="chat_completions_stream", purpose="actor",
                                  outcome="ok", duration_ms=5, attempt=2, session_id=store.id)
        upstream_telemetry.record(upstream="elevenlabs", route="tts_ws", purpose="tts", outcome="ok",
                                  duration_ms=20, queue_wait_ms=100, session_id=store.id)
        upstream_telemetry.record(upstream="elevenlabs", route="tts_ws", purpose="tts", outcome="cancelled",
                                  duration_ms=0, queue_wait_ms=90, session_id=store.id, detail="slot_wait")
        await metrics.stop()
        assert all(task.done() for task in first_tasks)

    asyncio.run(check())
    docs = documents(capsys)
    assert len(docs) == 4
    assert samples(docs, "UpstreamRequests") == [1, 1, 1]
    assert samples(docs, "UpstreamErrors") == [1, 0, 0]
    assert samples(docs, "UpstreamLatency") == [3, 5, 20]
    assert samples(docs, "TtsQueueWait") == [100, 90]
    assert samples(docs, "UpstreamCancelled") == [0, 0, 0, 1]
    assert all(d["Cohort"] == "study" for d in docs)
    assert store.id not in json.dumps(docs) and "private" not in json.dumps(docs)
    events = [json.loads(line) for line in store.events_path.read_text(encoding="utf-8").splitlines()]
    assert [e["type"] for e in events] == ["upstream_attempt"] * 4
    assert [e["duration_ms"] for e in events] == [3, 5, 20, 0]
    assert events[-1]["detail"] == "slot_wait"
    hooks = appmod.app.router.on_startup
    assert hooks.index(appmod._start_metrics_on_startup) < hooks.index(appmod._run_preflights_on_startup)
    assert metrics.stop in appmod.app.router.on_shutdown


@pytest.mark.parametrize("outcome", ["ok", "http_error", "timeout", "closed", "exception", "cancelled"])
def test_each_upstream_outcome_is_an_attempt(outcome, capsys):
    metrics.upstream_attempt(dict(upstream="gateway", route="models", purpose="preflight",
                                  outcome=outcome, duration_ms=1, session_id=None))
    docs = documents(capsys)
    assert samples(docs, "UpstreamRequests") == [1]
    assert samples(docs, "UpstreamErrors") == [int(outcome in {"http_error", "timeout", "exception", "closed"})]
    assert samples(docs, "UpstreamCancelled") == [int(outcome == "cancelled")]
    assert len(docs) == 1
    assert docs[0]["Cohort"] == "internal"


@pytest.mark.parametrize("detail", [None, "slot_wait", "slot_wait_other"])
def test_slot_wait_is_not_an_upstream_request(detail, capsys):
    from server import upstream_telemetry
    metrics.install_upstream_listener()
    upstream_telemetry.record(upstream="elevenlabs", route="tts_ws", purpose="tts",
                              outcome="cancelled", duration_ms=12, queue_wait_ms=120, detail=detail)
    docs = documents(capsys)
    assert len(docs) == 1
    assert samples(docs, "UpstreamRequests") == ([] if detail == "slot_wait" else [1])
    assert samples(docs, "UpstreamErrors") == ([] if detail == "slot_wait" else [0])
    assert samples(docs, "UpstreamLatency") == ([] if detail == "slot_wait" else [12])
    assert samples(docs, "TtsQueueWait") == [120]
    assert samples(docs, "UpstreamCancelled") == [1]


def test_one_stdout_write_and_all_metrics_per_upstream_attempt(monkeypatch):
    class Sink(io.StringIO):
        def __init__(self):
            super().__init__()
            self.writes = []

        def write(self, line):
            self.writes.append(line)
            return super().write(line)

    sink = Sink()
    monkeypatch.setattr(metrics.sys, "stdout", sink)
    metrics.upstream_attempt(dict(upstream="elevenlabs", route="tts_ws", purpose="tts",
                                  outcome="cancelled", duration_ms=12, queue_wait_ms=20))
    assert len(sink.writes) == 1 and sink.writes[0].endswith("\n")
    doc = json.loads(sink.getvalue())
    names = {m["Name"] for m in doc["_aws"]["CloudWatchMetrics"][0]["Metrics"]}
    assert names == {"UpstreamRequests", "UpstreamErrors", "UpstreamCancelled", "UpstreamLatency", "TtsQueueWait"}
    assert doc["UpstreamRequests"] == doc["UpstreamCancelled"] == 1
    assert doc["UpstreamErrors"] == 0
    assert ["Upstream"] in doc["_aws"]["CloudWatchMetrics"][0]["Dimensions"]


def test_playback_finalizes_on_next_speech_with_earliest_member_ack(stores, capsys, monkeypatch):
    from server.turn_timing import TurnTimer
    from tools.sim.analyze import ANSWER_GRACE_S
    assert metrics.ANSWER_GRACE_S == ANSWER_GRACE_S
    store, other = stores(), stores()
    now = [store.started_at + 1]
    monkeypatch.setattr(time, "time", lambda: now[0])
    timer = TurnTimer(store)
    timer.speech_end()
    # One member's later record reports an earlier ACK, still within the hold.
    store.event("turn_timing", vad_speech_end=1, first_audio_played=5)
    other.event("turn_timing", vad_speech_end=1, first_audio_played=2)
    now[0] += 6
    store.event("turn_timing", vad_speech_end=1, first_audio_played=3)
    assert samples(documents(capsys), "SpeechEndToFirstPlayed") == []
    timer.speech_end()
    doc, = documents(capsys)
    assert doc["SpeechEndToFirstPlayed"] == 2000
    assert doc["_aws"]["Timestamp"] == int((store.started_at + 3) * 1000)
    assert not store.events_fh.closed and other._metrics_turns == {1: 2}
    # Once finalized, late records and close cannot add another sample.
    store.event("turn_timing", vad_speech_end=1, first_audio_played=2)
    store.close()
    assert samples(documents(capsys), "SpeechEndToFirstPlayed") == []


def test_sweep_finalizes_after_hold_without_next_speech_or_session_close(stores, capsys, monkeypatch):
    store = stores()
    now = [store.started_at + 5]
    monkeypatch.setattr(time, "time", lambda: now[0])
    metrics.speech_end(store, 1)
    store.event("turn_timing", vad_speech_end=1, first_audio_played=4)
    capsys.readouterr()
    count = 0

    async def sleep(seconds):
        nonlocal count
        assert seconds == 1
        count += 1
        if count == 1:
            now[0] = store.started_at + 30.9
        elif count == 2:
            assert samples(documents(capsys), "SpeechEndToFirstPlayed") == []
            now[0] = store.started_at + 31
        else:
            raise asyncio.CancelledError

    monkeypatch.setattr(metrics.asyncio, "sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(metrics._sweep_loop())
    doc, = documents(capsys)
    assert doc["SpeechEndToFirstPlayed"] == 3000
    assert doc["_aws"]["Timestamp"] == int((store.started_at + 4) * 1000)
    assert not store.events_fh.closed and store._metrics_turns == {}
    metrics.sweep_playback()
    store.close()
    assert samples(documents(capsys), "SpeechEndToFirstPlayed") == []


def test_next_speech_can_finalize_a_play_start_before_play_end(stores, capsys, monkeypatch):
    from server.turn_timing import TurnTimer
    store = stores()
    now = [store.started_at + 1]
    monkeypatch.setattr(time, "time", lambda: now[0])
    timer = TurnTimer(store)
    timer.speech_end()
    turn = timer.started("member")
    now[0] += 2
    timer.ack({"phase": "start", "turn": turn})
    now[0] += 1
    capsys.readouterr()
    timer.speech_end()
    assert samples(documents(capsys), "SpeechEndToFirstPlayed") == [2000]
    timer.ack({"phase": "end", "turn": turn})
    store.close()
    assert samples(documents(capsys), "SpeechEndToFirstPlayed") == []


def test_missing_playback_skips_only_replies_cut_before_playback(stores, capsys):
    store = stores()
    store.event("turn_timing", vad_speech_end=1, first_audio_played=None, play_end_interrupted=True)
    store.event("turn_timing", vad_speech_end=2, first_audio_played=None, play_end_interrupted=False)
    store.event("turn_timing", vad_speech_end=3, first_audio_played=4, play_end_interrupted=True)
    store.event("turn_timing", vad_speech_end=4, first_audio_played=None, play_end_interrupted=True)
    store.event("turn_timing", vad_speech_end=4, first_audio_played=5, play_end_interrupted=False)
    store.close()
    docs = documents(capsys)
    assert samples(docs, "FirstPlayedMissing") == [1]
    assert samples(docs, "SpeechEndToFirstPlayed") == [1000, 1000]


def test_speech_ends_do_not_create_background_tasks(stores, capsys):
    store = stores()

    async def check():
        def metrics_tasks():
            # GC can schedule unrelated client cleanup on this same loop.
            return {task for task in asyncio.all_tasks()
                    if (code := getattr(task.get_coro(), "cr_code", None)) is not None
                    and code.co_filename == metrics.__file__}

        await metrics.start(types.SimpleNamespace(list_ids=lambda: []))
        try:
            tasks = metrics_tasks()
            assert tasks == {metrics._gauge_task, metrics._sweep_task}
            for ended in range(1, 101):
                metrics.speech_end(store, ended)
                store.event("turn_timing", vad_speech_end=ended, first_audio_played=ended + 1)
            assert metrics_tasks() == tasks
            assert not metrics._sweep_task.done()
        finally:
            await metrics.stop()

    asyncio.run(check())
    assert len(samples(documents(capsys), "SpeechEndToFirstPlayed")) == 99


@pytest.mark.parametrize("group", ["study", "unattributed", None])
def test_run_missing_cohort_and_unattributed_are_study(run_files, capsys, group):
    runs, make = run_files
    run = make("a" * 12)
    if group is None:
        run.pop("cohort")
    else:
        run["cohort"] = group
    runs.save(run)
    runs.advance("a" * 12, "first")
    runs.withdraw("a" * 12, "second")
    docs = documents(capsys)
    assert samples(docs, "EncounterCompleted") == [1]
    assert samples(docs, "EncounterWithdrawn") == [1]
    assert all(doc["Cohort"] == "study" for doc in docs)
