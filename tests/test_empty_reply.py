"""A reply the gateway named and then ended with nothing in it.

S2A on gpt-realtime-2.1, 2026-09-29 (session s_1790701886_2a49cd): Morgan
answered every line until 150.8 s, then four participant commits in a row
(160, 172, 197 and 220 s) drew no reply, and the record held no event of any
kind about them: no reply_missing, no voice_error, no retry. Each reply had
been named by a response.created, which disarms REQUEST_UNANSWERED_S, and
ended 10-11 s later with no audio and no words. The runner had no turn open
behind it, and the 1:1 pump's response_done branch dropped it as a repeat of
a done it had already handled.

Driven here with the real bridge on a scripted gateway socket and the real
1:1 pump, in the state that suppressed those replies: a commit's reply in
flight (`_response_active`, `_requested`, `_response_created_id` set) and no
turn open in the runner (`_speaking` False).
"""

from __future__ import annotations

import asyncio
import base64
import functools
import json
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server import realtime_voice_session as rvs  # noqa: E402
from server.scenarios import load_scenario  # noqa: E402
from server.voice import realtime  # noqa: E402
from server.voice.realtime import RealtimeVoiceSession  # noqa: E402

GPT = "gpt-realtime-2.1"


class FakeStore:
    def __init__(self):
        self.events = []
        self.audio = {}
        self.user_audio = b""
        self.started_at = 1_772_460_000.0

    def event(self, type_, **fields):
        self.events.append(dict(type=type_, **fields))

    def append_assistant_audio(self, pcm, agent_id=None):
        self.audio[agent_id] = self.audio.get(agent_id, b"") + pcm

    def append_user_audio(self, pcm):
        self.user_audio += pcm

    def of(self, type_):
        return [e for e in self.events if e["type"] == type_]


class FakeEngine:
    def __init__(self, agent):
        self.agent = agent

    def _system_prompt(self, branches, note, group=False):
        return f"SYSTEM PROMPT for {self.agent.id}"


class FakeSession:
    def __init__(self, scenario_id):
        self.scenario = load_scenario(scenario_id, "p_test")
        self.is_group = self.scenario.mode == "group"
        self.engines = {a.id: FakeEngine(a) for a in self.scenario.cast}
        self.store = FakeStore()
        self.director = None
        self.triggered_branches = []
        self.shared_history = []
        self.steering_log = []
        self.broadcasts = []

    def append_user(self, text):
        self.shared_history.append({"speaker": "user", "text": text})

    def append_agent(self, agent_id, text):
        self.shared_history.append({"speaker": agent_id, "text": text})

    async def broadcast(self, payload):
        self.broadcasts.append(payload)

    async def auto_steer(self, *, delivered=None):
        return None


class FakeWS:
    def __init__(self):
        self.json = []
        self.binary = []

    async def send_json(self, payload):
        self.json.append(payload)

    async def send_bytes(self, payload):
        self.binary.append(payload)

    async def receive(self):
        return {"type": "websocket.disconnect"}

    def frames(self, type_):
        return [f for f in self.json if f.get("type") == type_]


class ScriptedGateway:
    """The gateway socket: frames appended to `script` are received in order;
    with none queued, recv waits (the bridge's poll times it out)."""

    def __init__(self):
        self.script = []
        self.sent = []

    async def recv(self):
        while not self.script:
            await asyncio.sleep(0.005)
        return self.script.pop(0)

    async def send(self, payload):
        self.sent.append(json.loads(payload))

    async def close(self):
        return None


def created(rid="resp_1"):
    return json.dumps({"type": "response.created", "response": {"id": rid}})


def empty_done(rid="resp_1", status="failed"):
    """The gateway's own end of a reply that produced nothing."""
    return json.dumps({"type": "response.done", "response": {
        "id": rid, "status": status, "output": [],
        "usage": {"output_tokens": 0}}})


def in_a_loop(fn):
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        return asyncio.run(fn(*a, **kw))
    return wrapper


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(realtime, "MODEL", GPT)
    monkeypatch.setattr(realtime, "RECV_POLL_S", 0.02)
    monkeypatch.setattr(realtime, "RESPONSE_STALL_S", 30.0)
    monkeypatch.setenv("TRANSCRIPT_GRACE_SECONDS", "0.2")


def one_to_one():
    session = FakeSession("S2A")
    ws = FakeWS()
    runner = rvs.RealtimeVoiceSessionRunner(session, ws)
    rt = RealtimeVoiceSession("be Morgan", model=GPT, voice="coral",
                              api_key="test-key")
    wire = ScriptedGateway()
    rt.ws = wire
    runner.rt = rt
    runner.room = None
    # The participant opened long ago (10.8 s in the S2A run): nothing is
    # held by _hold_first_reply.
    runner._awaiting_participant = False
    return runner, session, rt, wire


async def until(cond, timeout=3.0):
    t0 = time.monotonic()
    while not cond():
        if time.monotonic() - t0 > timeout:
            return False
        await asyncio.sleep(0.01)
    return True


async def commit_and_answer(runner, rt, wire, frames):
    """A participant commit whose reply the gateway names, then `frames`.
    Returns the frames the bridge sent after the reply was named."""
    assert await rt.commit_turn() is True
    wire.script.append(created("resp_1"))
    assert await until(lambda: rt._response_created_id == "resp_1")
    # The state the S2A replies were suppressed in.
    assert rt.responding and rt._requested
    assert runner._speaking is False
    mark = len(wire.sent)
    wire.script.extend(frames)
    return mark


def asked_again(wire, mark):
    after = wire.sent[mark:]
    items = [m for m in after if m.get("type") == "conversation.item.create"]
    texts = [c.get("text") for m in items
             for c in (m.get("item") or {}).get("content") or []]
    return texts, [m.get("type") for m in after].count("response.create")


@in_a_loop
async def test_an_empty_reply_to_a_commit_is_recorded_and_asked_for_again():
    runner, session, rt, wire = one_to_one()
    pump = asyncio.ensure_future(runner._pump_events(rt))
    try:
        await asyncio.sleep(0.05)
        mark = await commit_and_answer(runner, rt, wire, [empty_done("resp_1")])
        got = await until(lambda: session.store.of("reply_retry"))
    finally:
        pump.cancel()
    assert got, "an empty reply to the participant's line left no record and no retry"
    (missing,) = session.store.of("reply_missing")
    assert missing["shape"] == "empty_done"
    assert missing["response_status"] == "failed"
    assert missing["output_items"] == 0 and missing["output_tokens"] == 0
    (retry,) = session.store.of("reply_retry")
    assert retry["asked"] is True and retry["how"] == "nudge"
    texts, creates = asked_again(wire, mark)
    assert texts == [realtime.UNANSWERED_NUDGE] and creates == 1
    assert rt.responding, "the re-ask is the reply now in flight"
    assert session.store.of("assistant_turn") == []


@in_a_loop
async def test_a_reply_with_no_voice_and_no_words_is_recorded_and_asked_for_again(monkeypatch):
    """The same suppression by the bridge's audio-absent close: a frame that is
    neither audio nor transcript (a text-only delta), then nothing."""
    monkeypatch.setattr(realtime, "AUDIO_ABSENT_S", 0.3)
    runner, session, rt, wire = one_to_one()
    pump = asyncio.ensure_future(runner._pump_events(rt))
    try:
        await asyncio.sleep(0.05)
        mark = await commit_and_answer(runner, rt, wire, [json.dumps({
            "type": "response.output_text.delta", "response_id": "resp_1",
            "delta": "Okay."})])
        got = await until(lambda: session.store.of("reply_retry"))
    finally:
        pump.cancel()
    assert got, "a voiceless, wordless reply left no record and no retry"
    (missing,) = session.store.of("reply_missing")
    assert missing["shape"] == "absent_done"
    assert session.store.of("reply_retry")[0]["asked"] is True
    texts, creates = asked_again(wire, mark)
    assert texts == [realtime.UNANSWERED_NUDGE] and creates == 1
    assert session.store.of("audio_retry") == [], "not 'say it again': nothing was said"


@in_a_loop
async def test_the_retry_of_an_empty_reply_is_the_turns_only_one():
    runner, session, rt, wire = one_to_one()
    pump = asyncio.ensure_future(runner._pump_events(rt))
    try:
        await asyncio.sleep(0.05)
        await commit_and_answer(runner, rt, wire, [empty_done("resp_1")])
        assert await until(lambda: session.store.of("reply_retry"))
        wire.script.extend([created("resp_2"), empty_done("resp_2")])
        await until(lambda: len(session.store.of("reply_retry")) == 2)
    finally:
        pump.cancel()
    first, second = session.store.of("reply_retry")
    assert first["asked"] is True
    assert second["asked"] is False and second["why"] == "already_retried"



@in_a_loop
async def test_the_gateway_refusing_the_retrys_cancel_is_not_an_error_on_the_page():
    """retry_response leads with a response.cancel; after a reply that ended
    empty the gateway has nothing to cancel and says so. That refusal put the
    retry's flags down and sent the page an `error` ("Something went wrong")
    in front of the very reply the retry drew."""
    pcm = base64.b64encode(b"\x01\x00" * 4800).decode("ascii")
    runner, session, rt, wire = one_to_one()
    pump = asyncio.ensure_future(runner._pump_events(rt))
    try:
        await asyncio.sleep(0.05)
        await commit_and_answer(runner, rt, wire, [empty_done("resp_1")])
        assert await until(lambda: session.store.of("reply_retry"))
        wire.script.append(json.dumps({"type": "error", "error": {
            "type": "invalid_request_error", "code": "response_cancel_not_active",
            "message": "Cancellation failed: no active response found"}}))
        await asyncio.sleep(0.2)
        assert rt.responding and rt._retry_in_flight, "the retry is still the reply in flight"
        wire.script.extend([
            created("resp_2"),
            json.dumps({"type": "response.output_audio_transcript.delta",
                        "response_id": "resp_2", "delta": "What do you need?"}),
            json.dumps({"type": "response.output_audio.delta",
                        "response_id": "resp_2", "delta": pcm}),
            json.dumps({"type": "response.output_audio.done", "response_id": "resp_2"}),
            json.dumps({"type": "response.done", "response": {
                "id": "resp_2", "status": "completed"}}),
        ])
        await until(lambda: session.store.of("assistant_turn"))
    finally:
        pump.cancel()
    assert runner.ws.frames("error") == [], "the page was told something went wrong"
    (turn,) = session.store.of("assistant_turn")
    assert turn["text"] == "What do you need?"

def bridge_dones(frames):
    """The response_done events a commit's reply made of `frames` yields."""
    rt = RealtimeVoiceSession("x", model=GPT, voice="coral", api_key="test-key")
    wire = ScriptedGateway()
    rt.ws = wire

    async def go():
        assert await rt.commit_turn() is True
        wire.script.extend(frames)
        out = []
        agen = rt.events()
        try:
            while True:
                try:
                    ev = await asyncio.wait_for(agen.__anext__(), 0.5)
                except (asyncio.TimeoutError, StopAsyncIteration):
                    break
                if ev["type"] == "response_done":
                    out.append(ev)
        finally:
            await agen.aclose()
        return out

    return asyncio.run(go())


@pytest.mark.parametrize("frames", [
    # A function call and nothing else: handled as a tool call, not lost.
    [created(), json.dumps({"type": "response.function_call_arguments.done",
                            "name": "end_conversation", "call_id": "call_1",
                            "arguments": "{}"}),
     json.dumps({"type": "response.done", "response": {
         "id": "resp_1", "status": "completed",
         "output": [{"type": "function_call", "name": "end_conversation"}]}})],
    # A reply with its words and its voice.
    [created(),
     json.dumps({"type": "response.output_audio_transcript.delta", "delta": "Okay."}),
     json.dumps({"type": "response.output_audio.delta",
                 "delta": base64.b64encode(b"\x01\x00" * 4800).decode("ascii")}),
     json.dumps({"type": "response.output_audio.done"}),
     json.dumps({"type": "response.done", "response": {"id": "resp_1"}})],
    # A done the gateway never named: REQUEST_UNANSWERED_S's to judge, not this.
    [json.dumps({"type": "response.done", "response": {}})],
], ids=["function_call", "voiced", "unnamed"])
def test_only_a_named_reply_with_nothing_in_it_is_undelivered(frames):
    (done,) = bridge_dones(frames)
    assert "undelivered" not in done
