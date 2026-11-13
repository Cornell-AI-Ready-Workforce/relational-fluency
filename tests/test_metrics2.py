"""PR 2 metrics through real route, adapter, storage and runner producers."""
from __future__ import annotations

import asyncio
import io
import json
import time
import types
import weakref

import pytest
from starlette.websockets import WebSocket

from server import app as appmod, metrics, realtime_voice_session as rvs
from server.session import Session
from server.storage import SessionStore
from test_participant_turn_integrity import runner_for, bridge, LOUD


@pytest.fixture
def sink(monkeypatch):
    out = io.StringIO()
    monkeypatch.setattr(metrics, "sys", types.SimpleNamespace(stdout=out))
    monkeypatch.setenv("METRICS_EMF", "1")
    monkeypatch.setattr(metrics, "_cohorts", {})
    monkeypatch.setattr(metrics, "_stores", weakref.WeakSet())

    def read(name=None):
        docs = [json.loads(line) for line in out.getvalue().splitlines() if line.startswith('{"_aws":')]
        return [d for d in docs if name is None or name in d]
    return read


def memory_store():
    """Real SessionStore.event, backed by an in-memory event file."""
    store = object.__new__(SessionStore)
    store.id = "s_1772460300_000099"
    store.run_id, store.cohort, store.participant_id = "aaaaaaaaaaaa", "study", "p_fake"
    store.started_at = time.time()
    store.events_fh = io.StringIO()
    store.user_audio, store.assistant_audio, store.timeline = None, {}, None
    metrics.session_created(store)
    return store


@pytest.fixture
def route_env(monkeypatch, sink):
    from server.scenarios import load_scenario
    session = object.__new__(Session)
    session.id, session.run_id = "s_1772460300_000099", "aaaaaaaaaaaa"
    session.store = memory_store()
    session.scenario = load_scenario("S1B")
    session.participant_ws = None
    session._closed = False
    cleanup = []
    mode = {"name": "returned", "close_fail": False}
    monkeypatch.setattr(appmod, "PARTICIPANT_KEY_REQUIRED", False)
    monkeypatch.setattr(appmod, "SESSION_KEY", "")
    monkeypatch.setattr(appmod, "_participant_may_capture", lambda pid: True)
    monkeypatch.setattr(appmod, "_run_context", lambda *a: {"run_id": session.run_id, "cohort": "study"})
    monkeypatch.setattr(appmod, "_arm_encounter_event_log", lambda s: None)
    monkeypatch.setattr(appmod.registry, "_sessions", {})

    def create(*a, **k):
        if mode["name"] == "missing_scenario":
            raise FileNotFoundError("missing scenario")
        return session

    def drop(sid):
        assert sink("VoiceRouteEnded") == [], "route metric must follow cleanup"
        cleanup.append(sid)
        session._closed = True
        session.store.events_fh.close()
        if mode["name"] == "cleanup_failed":
            raise OSError("cleanup failed")

    async def report(*a, **k):
        pass

    class Runner:
        def __init__(self, session, ws):
            if mode["name"] == "constructor_failed":
                raise ValueError("setup failure")
            self.session, self.ws = session, ws
            self.completed = self.client_disconnected = self._closed = False

        async def run(self):
            name = mode["name"]
            if name in ("completed", "completed_close", "cleanup_failed"):
                await rvs.RealtimeVoiceSessionRunner._send(self, {"type": "encounter_complete"})
            if name == "completed_close":
                await self.ws.close(code=1000)
            elif name in ("client", "client_then_close", "server_then_client"):
                if name == "server_then_client":
                    await self.ws.close(code=1000)
                await rvs.RealtimeVoiceSessionRunner._client_to_model(self)
                assert self.client_disconnected
                if name == "client_then_close":
                    await self.ws.close(code=1000)
            elif name == "runner_error":
                raise RuntimeError("runner failed")
            elif name == "cancelled":
                raise asyncio.CancelledError
            elif name == "withdrawn":
                drop(session.id)  # withdrawal closes the store first
                await appmod._close_capture_socket(session, session.id)
            elif name == "close_1011":
                await self.ws.close(code=1011)
            elif name == "raw_close":
                await self.ws.send({"type": "websocket.close", "code": 1011})
            elif name == "close_other":
                await self.ws.close(code=4408)
            elif name == "close_timeout":
                await asyncio.wait_for(self.ws.close(code=1000), timeout=0.001)
            elif name == "close_failed":
                await self.ws.close(code=1000)

    monkeypatch.setattr(appmod.registry, "create", create)
    monkeypatch.setattr(appmod.registry, "drop", drop)
    monkeypatch.setattr(appmod, "_report_encounter_failure", report)
    monkeypatch.setattr(appmod, "RealtimeVoiceSessionRunner", Runner)
    frames = []
    inbound = [{"type": "websocket.connect"}, {"type": "websocket.disconnect", "code": 1000}]

    async def receive():
        return inbound.pop(0)

    async def send(message):
        if message["type"] == "websocket.close":
            if mode["name"] == "close_timeout":
                await asyncio.Event().wait()
            if mode["name"] == "close_failed" or mode["close_fail"]:
                raise RuntimeError("close send failed")
        frames.append(message)

    ws = WebSocket({"type": "websocket", "path": "/ws/participant/voice", "headers": []}, receive, send)

    async def drive():
        await appmod.ws_participant_voice(ws, scenario="S1B", participant_id="p_fake",
                                         model=None, launch=None, key=None, run=session.run_id)
    return mode, drive, cleanup, frames, session, ws


@pytest.mark.parametrize("name,cause,close", [
    ("completed", "completed", "none"),
    ("returned", "runner_returned", "none"),
    ("missing_scenario", "setup_failed", "sent_1000"),
    ("constructor_failed", "setup_failed", "none"),
    ("runner_error", "runner_error", "none"),
    ("client", "client_disconnected", "client_first"),
    ("client_then_close", "client_disconnected", "client_first"),
    ("server_then_client", "client_disconnected", "sent_1000"),
    ("withdrawn", "withdrawn", "sent_1000"),
    ("completed_close", "completed", "sent_1000"),
    ("close_1011", "runner_returned", "sent_1011"),
    ("raw_close", "runner_returned", "sent_1011"),
    ("close_other", "runner_returned", "sent_other"),
    ("close_failed", "runner_error", "close_failed"),
    ("close_timeout", "runner_error", "close_failed"),
    ("cleanup_failed", "other", "none"),
    ("cancelled", "cancelled", "none"),
])
def test_voice_route_ends_once_after_cleanup(route_env, sink, name, cause, close):
    mode, drive, cleanup, frames, session, ws = route_env
    mode["name"] = name
    if name == "cancelled":
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(drive())
    elif name == "cleanup_failed":
        with pytest.raises(OSError):
            asyncio.run(drive())
    else:
        asyncio.run(drive())
    doc, = sink("VoiceRouteEnded")
    assert doc["VoiceRouteEnded"] == 1
    assert (doc["ExitCause"], doc["CloseOutcome"], doc["Cohort"]) == (cause, close, "study")
    if name != "missing_scenario":
        assert cleanup and session.store.events_fh.closed
    assert not hasattr(ws, "_metrics_voice_route")
    assert session.id not in json.dumps(doc) and "p_fake" not in json.dumps(doc)


@pytest.mark.parametrize("code", [4401, 4403])
def test_route_auth_rejects_before_setup(route_env, sink, monkeypatch, code):
    mode, drive, cleanup, frames, session, ws = route_env
    if code == 4401:
        monkeypatch.setattr(appmod, "PARTICIPANT_KEY_REQUIRED", True)
        monkeypatch.setattr(appmod, "SESSION_KEY", "test-required")
    else:
        monkeypatch.setattr(appmod, "_participant_may_capture", lambda pid: False)
    asyncio.run(drive())
    doc, = sink("VoiceRouteEnded")
    assert (doc["ExitCause"], doc["CloseOutcome"], doc["Cohort"]) == ("auth_rejected", "sent_other", "internal")
    assert frames[-1]["code"] == code and cleanup == []


def test_route_observer_failure_never_replaces_route_result(route_env, monkeypatch):
    _, drive, *_ = route_env
    monkeypatch.setattr(metrics, "emit", lambda *a, **k: (_ for _ in ()).throw(ValueError("metric failed")))
    asyncio.run(drive())


def test_metric_properties_centrally_on_every_document(sink, monkeypatch):
    from server import llm
    monkeypatch.setenv("BUILD_SHA", "abc1234")
    monkeypatch.setattr(llm, "PIPELINE_VERSION", "2026-11-12b")
    for name in metrics.UNITS:
        metrics.emit(name, Cohort="study")
    assert len(sink()) == len(metrics.UNITS)
    for doc in sink():
        assert doc["Build"] == "abc1234" and doc["PipelineVersion"] == "2026-11-12b"
        for keys in doc["_aws"]["CloudWatchMetrics"][0]["Dimensions"]:
            assert "Build" not in keys and "PipelineVersion" not in keys


@pytest.mark.parametrize("field", ["ExitCause", "CloseOutcome", "Kind", "Reason", "Outcome", "Purpose", "Upstream"])
def test_unknown_dimension_values_are_other_without_raw_properties(sink, field):
    metrics.emit("Bargein", **{field: "participant-private-text"})
    doc, = sink()
    assert doc[field] == "other" and "private" not in json.dumps(doc)


@pytest.mark.parametrize("result,outcome", [("A real sentence.", "speech"), ("", "no_speech"),
                                             (" [NO SPEECH] ", "no_speech"),
                                             ("failure", "failed"), ("cancel", "cancelled")])
def test_stt_terminal_outcome_once(result, outcome, sink):
    from server.voice.cascade import CascadeSocket
    store = memory_store()

    async def check():
        socket = CascadeSocket(api_key="fake", session_id=store.id)

        async def hear(pcm):
            if result == "failure":
                raise RuntimeError("private transcript failed")
            if result == "cancel":
                raise asyncio.CancelledError
            return result

        socket._hear = hear
        if result == "cancel":
            with pytest.raises(asyncio.CancelledError):
                await socket._transcribe("private-item", b"pcm")
        else:
            await socket._transcribe("private-item", b"pcm")
        await socket.close()

    asyncio.run(check())
    doc, = sink("SttResult")
    assert (doc["Outcome"], doc["Kind"], doc["Cohort"]) == (outcome, "unknown", "study")
    assert sink("VoiceError") == []
    assert "private" not in json.dumps(doc)


def cascade_bridge(store, label, heard):
    from server.voice import cascade, realtime
    socket = cascade.CascadeSocket(api_key="fake", session_id=store.id)

    async def hear(pcm):
        heard.append(label)
        return "I can explain the missed deadline."

    async def respond(rid):
        pass

    socket._hear, socket._respond = hear, respond
    rt = realtime.RealtimeVoiceSession("test", model="cascade-argon-elevenlabs", api_key="fake")
    rt.ws = socket
    return rt, socket


@pytest.mark.parametrize("kind", ["participant", "probe", "replay"])
def test_stt_kind_from_real_bridge_commit_paths(kind, sink):
    store, heard = memory_store(), []

    async def check():
        rt, socket = cascade_bridge(store, "one", heard)
        if kind == "replay":
            assert await rt.replay_input(LOUD * 10)
        else:
            await rt.send_audio(LOUD * 10)
            await rt.commit_input(probe=kind == "probe")
        await asyncio.gather(*socket._pending_stt)
        await socket.close()

    asyncio.run(check())
    doc, = sink("SttResult")
    assert doc["Kind"] == kind and doc["Outcome"] == "speech"
    assert heard == ["one"]


def test_room_scribe_hears_once_members_receive_text_grants(sink):
    from server.group_room import GroupRoom
    from server.scenarios import load_scenario
    store, heard = memory_store(), []

    async def check():
        agents = load_scenario("S4A").cast
        room = GroupRoom(agents, lambda a: "test", lambda a: "", model="cascade-argon-elevenlabs")
        scribe, socket = cascade_bridge(store, "scribe", heard)
        members = [cascade_bridge(store, a.id, heard) for a in agents]
        room.scribe = scribe
        room.sessions = {a.id: pair[0] for a, pair in zip(agents, members)}
        await room.hear(LOUD * 10)
        assert await room.close_participant_turn()
        await asyncio.gather(*socket._pending_stt)
        for agent in agents:
            assert await room.give_floor(agent.id)
        for _, member in members:
            await member.close()
        await socket.close()

    asyncio.run(check())
    assert heard == ["scribe"]
    doc, = sink("SttResult")
    assert doc["Kind"] == "participant"


def test_failed_stt_reaches_transcript_missing_not_voice_error(sink):
    store, heard = memory_store(), []

    async def check():
        rt, socket = cascade_bridge(store, "one", heard)

        async def failed(pcm):
            raise RuntimeError("hearing failed")

        socket._hear = failed
        await rt.send_audio(LOUD * 10)
        await rt.commit_input()
        await asyncio.gather(*socket._pending_stt)
        events = rt.events()
        try:
            ev = await asyncio.wait_for(events.__anext__(), 1)
            assert ev["type"] == "transcript_missing" and ev["why"] == "transcript_failed"
        finally:
            await events.aclose()
            await socket.close()

    asyncio.run(check())
    doc, = sink("SttResult")
    assert doc["Outcome"] == "failed" and sink("VoiceError") == []


@pytest.mark.parametrize("reason", ["no_speech", "implausible_rate", "probe_pad", "not_english",
                                     "low_transcriber_confidence", "replay_duplicate", "echo", "duplicate"])
def test_user_turn_gates_from_real_runner(reason, sink, monkeypatch):
    runner, session, _ = runner_for("S4A")
    session.store = memory_store()
    text, fields = "I can explain how the team missed the deadline and what happened after our meeting.", {"voiced_ms": 4000}

    async def checked(self, *a, **k):
        return {"outcome": "empty", "reason": "non_latin"} if reason == "not_english" else None

    monkeypatch.setattr(rvs.RealtimeVoiceSessionRunner, "_speech_check", checked)
    if reason == "no_speech":
        text = "..."
    elif reason == "implausible_rate":
        text, fields = "One two three four five six seven eight nine ten.", {"voiced_ms": 50, "voiced_span_ms": 50}
    elif reason == "probe_pad":
        fields["probe"] = True
    elif reason == "replay_duplicate":
        fields["replay"] = True
        runner._replay_marker, runner._transcripts_arrived = 0, 1
    elif reason == "low_transcriber_confidence":
        fields["confidence"] = {"mean_word_logprob": -8, "min_word_logprob": -9, "words": 6}
    elif reason == "not_english":
        text = "Tuurlijk."
    elif reason == "echo":
        runner._recent_agent_texts = [(time.time(), "private-agent", text)]
    elif reason == "duplicate":
        runner._last_user_norm = rvs._norm_speech(text)
        runner._last_user_text, runner._last_user_at = text, time.time()
    asyncio.run(runner._record_user_turn(text, item_id="private-item", **fields))
    doc, = sink("UserTurnSuppressed")
    assert doc["Reason"] == reason and doc["UserTurnSuppressed"] == 1
    assert text not in json.dumps(doc) and "private" not in json.dumps(doc)


def test_gate_counts_decisions_and_maps_future_reasons_to_other(sink):
    runner, session, _ = runner_for("S4A")
    session.store = memory_store()

    async def check():
        for _ in range(2):
            await runner._record_user_turn("...", item_id="same-turn", voiced_ms=1200)

    asyncio.run(check())
    session.store.event("user_turn_suppressed", reason="future-private-reason", text="private")
    assert [d["Reason"] for d in sink("UserTurnSuppressed")] == ["no_speech", "no_speech", "other"]


@pytest.mark.parametrize("outcome", ["audio_already_sent", "later_commit", "cancel_unsupported", "withdrawn"])
def test_suppressed_reply_branches_and_cleanup_is_not_bargein(outcome, sink):
    runner, session, ws = runner_for("S1B")
    session.store = memory_store()
    runner.room = None
    runner._speaking = True
    runner._turn_audio_bytes = 3200 if outcome == "audio_already_sent" else 0
    runner._agent_text = ["private words"]

    async def cancel():
        return None if outcome == "cancel_unsupported" else "private-response"

    runner.rt = types.SimpleNamespace(responding=True, autofire_active=False,
                                     last_commit_at=99 if outcome == "later_commit" else 0,
                                     cancel_unheard_reply=cancel)
    asyncio.run(runner._withdraw_reply("private transcript", "no_speech", 1, 1200))
    doc, = sink("SuppressedTurnReply")
    assert doc["Outcome"] == outcome and "private" not in json.dumps(doc)
    assert sink("Bargein") == []
    if outcome == "withdrawn":
        assert len(ws.frames("assistant_interrupted")) == 1


def test_unknown_suppressed_reply_maps_other(sink):
    store = memory_store()
    store.event("reply_to_suppressed_turn", kept="private-new-state")
    doc, = sink("SuppressedTurnReply")
    assert doc["Outcome"] == "other" and "private" not in json.dumps(doc)


def test_finished_suppressed_reply_without_byte_evidence_is_other(sink):
    runner, session, _ = runner_for("S1B")
    session.store = memory_store()
    runner.room, runner._speaking = None, False
    runner._agent_turn_began_at = 2
    runner.rt = types.SimpleNamespace(responding=False, autofire_active=False)
    asyncio.run(runner._withdraw_reply("private transcript", "no_speech", 1, 1200))
    doc, = sink("SuppressedTurnReply")
    assert doc["Outcome"] == "other"


@pytest.mark.parametrize("branch", ["one_to_one", "room_floor", "room_playback"])
def test_bargein_live_branches_only(branch, sink, monkeypatch):
    runner, session, ws = runner_for("S1B", frames=[LOUD, LOUD])
    session.store = memory_store()
    runner.rt = bridge()
    runner.vad = types.SimpleNamespace(feed=lambda pcm: None, barge_in=True, speaking=False,
                                      effective_threshold=lambda: 1)
    runner._was_barging = False
    runner._split_turn_continues = lambda: False
    runner._cut_last_played = lambda **k: None
    runner._queued_cuts_safe = lambda *a: []

    async def noop(*a, **k):
        pass

    runner._memory_after_cuts = noop
    runner._memory_cut_floor_holder = noop
    # Finalization is independent of detecting a live barge-in.
    runner._spawn_finalize = lambda coroutine: coroutine.close()
    if branch == "one_to_one":
        runner.room, runner._speaking = None, True
    else:
        runner._speaking = False
        runner._play_cursor = time.time() + 20
        runner.room = types.SimpleNamespace(speaking=runner.agent_id if branch == "room_floor" else None,
                                           session_for=lambda aid: runner.rt, hear=noop)
        runner._resolve_agents = lambda: [runner.agent]

    async def check():
        await runner._client_to_model()
        assert len(ws.frames("assistant_interrupted")) == 1, session.store.events_fh.getvalue()
        await runner._send({"type": "assistant_interrupted"})

    asyncio.run(check())
    doc, = sink("Bargein")
    assert doc["Bargein"] == 1 and doc["Cohort"] == "study"


def test_route_dimension_rollups_are_actually_emitted(sink):
    metrics.emit("VoiceRouteEnded", Cohort="study", ExitCause="runner_error", CloseOutcome="none")
    doc, = sink("VoiceRouteEnded")
    sets = doc["_aws"]["CloudWatchMetrics"][0]["Dimensions"]
    assert ["Cohort", "ExitCause"] in sets and ["Cohort", "CloseOutcome"] in sets
    assert all(set(keys) <= set(metrics.DIMENSIONS) for keys in sets)


def test_metric_output_failure_does_not_change_stt_or_storage(monkeypatch):
    from server.voice.cascade import CascadeSocket

    class FailedSink:
        def write(self, line):
            raise BaseException("metric sink failed")

    monkeypatch.setattr(metrics, "sys", types.SimpleNamespace(stdout=FailedSink()))
    store = memory_store()
    store.event("user_turn_suppressed", reason="no_speech")
    assert '"user_turn_suppressed"' in store.events_fh.getvalue()

    async def check():
        socket = CascadeSocket(api_key="fake")

        async def hear(pcm):
            return "A real utterance."

        socket._hear = hear
        await socket._transcribe("test-item", b"pcm")
        event = json.loads(socket._out.get_nowait())
        assert event["type"] == "conversation.item.input_audio_transcription.completed"
        await socket.close()

    asyncio.run(check())


def test_observer_construction_failure_preserves_route(route_env, monkeypatch):
    _, drive, *_ = route_env
    monkeypatch.setattr(metrics, "_VoiceRoute", lambda: (_ for _ in ()).throw(RuntimeError("observer failed")))
    asyncio.run(drive())


def test_auth_close_failure_is_still_observed_once(route_env, sink, monkeypatch):
    mode, drive, cleanup, *_ = route_env
    mode["close_fail"] = True
    monkeypatch.setattr(appmod, "_participant_may_capture", lambda pid: False)
    with pytest.raises(RuntimeError):
        asyncio.run(drive())
    doc, = sink("VoiceRouteEnded")
    assert (doc["ExitCause"], doc["CloseOutcome"]) == ("auth_rejected", "close_failed")
    assert not cleanup


def test_disconnect_before_accept_is_client_first(route_env, sink, monkeypatch):
    _, drive, cleanup, _, _, ws = route_env

    async def receive():
        return {"type": "websocket.disconnect", "code": 1000}

    monkeypatch.setattr(ws, "_receive", receive)
    with pytest.raises(RuntimeError):
        asyncio.run(drive())
    doc, = sink("VoiceRouteEnded")
    assert (doc["ExitCause"], doc["CloseOutcome"]) == ("client_disconnected", "client_first")
    assert not cleanup
