"""Bridge correctness: issue #23, pipeline 2026-09-23d.

Two defects in how the voice bridge hands a reply to the runner, both from
diag track3 and both turned into tests here from its offline replays
(fake_cancel_tail.py, fake_two_items.py):

  * THE CANCELLED TAIL. After a barge-in cancel, gpt-realtime-2.1 still sends
    0.15-0.4 s of audio, an audio .done, the transcript .done and the
    response.done (probe_cancel.json, measured live). Each delta re-bound the
    reply, raised the in-flight flags and reached the runner as the audio of
    a reply it had just closed: a second assistant_started, a false
    agent_audio_short, and a blip after the stop. The tail is now dropped in
    events() before any bookkeeping, written as cancelled_output_dropped, and
    the reply's done is stale.
  * THE TWO-ITEM REPLY. gpt-realtime-2.1 answered in two output items in 3 of
    6 probe replies. One transcript .done arrives per item, and each consumer
    applied it as the whole line, so the second item's line replaced the
    first in the record, the room's hold and the page caption. The bridge now
    hands on the whole reply (every item's line, joined), and marks the first
    delta of each later item so the runner puts a space at the seam.

Everything here is offline: no socket, no credential.
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

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server import llm  # noqa: E402
from server import realtime_voice_session as rvs  # noqa: E402
from server.scenarios import load_scenario  # noqa: E402
from server.voice import realtime as R  # noqa: E402

GPT = "gpt-realtime-2.1"
NATIVE = "nto.gemini-live-2.5-flash-native-audio"

# One gateway audio delta: 0.2 s of 24 kHz PCM16, the size the live gateway
# sends.
GW_PCM = b"\x01\x00" * 4800
A = base64.b64encode(GW_PCM).decode()
FRAME = 320                              # 20 ms at 16 kHz, as the worklet sends
LOUD = b"\x00\x20" * FRAME               # RMS 8192


def in_a_loop(fn):
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        return asyncio.run(fn(*a, **kw))
    return wrapper


@pytest.fixture(autouse=True)
def fast_polls(monkeypatch):
    monkeypatch.setattr(R, "RECV_POLL_S", 0.02)
    monkeypatch.setattr(R, "AUDIO_RETRY_QUIET_S", 0.05, raising=False)
    monkeypatch.setenv("TRANSCRIPT_GRACE_SECONDS", "0.2")


# --------------------------------------------------------------------------
# Frames, in the shapes measured on gpt-realtime-2.1 (diag track3)
# --------------------------------------------------------------------------

def created(rid):
    return {"type": "response.created", "response": {"id": rid}}


def item_added(rid, item):
    return {"type": "response.output_item.added", "response_id": rid,
            "item": {"id": item}}


def tdelta(rid, item, text):
    return {"type": "response.output_audio_transcript.delta",
            "response_id": rid, "item_id": item, "delta": text}


def adelta(rid, item):
    return {"type": "response.output_audio.delta", "response_id": rid,
            "item_id": item, "delta": A}


def audio_done(rid, item):
    return {"type": "response.output_audio.done", "response_id": rid,
            "item_id": item}


def tdone(rid, item, text):
    return {"type": "response.output_audio_transcript.done",
            "response_id": rid, "item_id": item, "transcript": text}


def done(rid, status="completed", reason=None):
    resp = {"id": rid, "status": status}
    if reason:
        resp["status_details"] = {"type": status, "reason": reason}
    return {"type": "response.done", "response": resp}


# fake_cancel_tail.py: two transcript chunks and two audio deltas before the
# barge-in, and what probe_cancel.json measured after it.
CANCEL_PRE = [created("resp_X"), item_added("resp_X", "item_1"),
              tdelta("resp_X", "item_1", "Okay, let's slow"),
              adelta("resp_X", "item_1"),
              tdelta("resp_X", "item_1", " this down and"),
              adelta("resp_X", "item_1")]
CANCEL_POST = [adelta("resp_X", "item_1"), audio_done("resp_X", "item_1"),
               tdone("resp_X", "item_1", "Okay, let's slow this down and"),
               done("resp_X", "cancelled", "client_cancelled")]


def two_item_reply(rid="resp_Y"):
    """fake_two_items.py: one reply, two items, each with its own .done."""
    frames = [created(rid)]
    for item, words in (("i1", ["Okay,", " thanks", " for", " that."]),
                        ("i2", ["Six", " months", " is", " a", " start."])):
        frames.append(item_added(rid, item))
        for w in words:
            frames += [tdelta(rid, item, w), adelta(rid, item)]
        frames += [audio_done(rid, item), tdone(rid, item, "".join(words))]
    frames.append(done(rid))
    return frames


SPOKEN = "Okay, thanks for that. Six months is a start."


class WireWS:
    """The gateway's end of one socket."""

    def __init__(self):
        self.sent: list = []
        self.q: asyncio.Queue = asyncio.Queue()

    async def send(self, raw):
        self.sent.append(json.loads(raw))

    async def recv(self):
        return await self.q.get()

    async def close(self):
        pass

    def feed(self, frames):
        for f in frames:
            self.q.put_nowait(json.dumps(f))

    def types(self):
        return [m["type"] for m in self.sent]


def bridge(model=GPT):
    rt = R.RealtimeVoiceSession("x", model=model, voice="", api_key="dummy")
    rt.ws = WireWS()
    return rt


async def pull(agen, n=None, *, until=None, timeout=1.0):
    """Events off an OPEN events() generator: `n` of them, or up to and
    including the first for which `until(ev)` is true. A timeout cancels the
    generator, so it is only ever the end of a test's reading."""
    out = []
    while n is None or len(out) < n:
        try:
            ev = await asyncio.wait_for(agen.__anext__(), timeout)
        except (asyncio.TimeoutError, StopAsyncIteration):
            break
        out.append(ev)
        if until is not None and until(ev):
            break
    return out


def types(evs):
    return [e["type"] for e in evs]


async def cancelled_tail(rt, post=CANCEL_POST):
    """Replay fake_cancel_tail: the reply, a barge-in, the tail. Returns the
    events before and after the cancel."""
    await rt.request_response()
    rt.ws.feed(CANCEL_PRE)
    agen = rt.events()
    try:
        before = await pull(agen, 4)       # created is not yielded
        await rt.cancel_response()
        rt.ws.feed(post)
        after = await pull(agen, until=lambda e: e["type"] == "response_done")
    finally:
        await agen.aclose()
    return before, after


# --------------------------------------------------------------------------
# 1. The cancelled tail, at the bridge
# --------------------------------------------------------------------------

@in_a_loop
async def test_the_tail_of_a_cancelled_reply_is_dropped_and_accounted_for():
    rt = bridge()
    before, after = await cancelled_tail(rt)
    assert types(before).count("agent_audio") == 2
    # Nothing of the tail reaches the runner as output of any reply ...
    assert not {"agent_audio", "agent_transcript", "agent_transcript_delta"} & set(types(after))
    # ... and nothing of it is lost either.
    (out,) = [e for e in after if e["type"] == "cancelled_output"]
    assert out["response_id"] == "resp_X"
    assert out["audio_ms"] == 200 and out["audio_deltas"] == 1
    assert out["transcripts"] == ["Okay, let's slow this down and"]
    assert out["late"] is False
    # The done is the cancelled reply's, stale: it closes no turn.
    d = after[-1]
    assert d["type"] == "response_done" and d["stale"] is True
    assert d["cancelled"] is True and d["retry_reason"] is None
    assert rt.stale_dones == 1


@in_a_loop
async def test_the_tail_raises_no_flag_so_nothing_is_latched():
    """The 45 s latch: a stale done after a tail that HAD raised the flags
    left them up. Dropped before the bookkeeping, the tail raises none."""
    rt = bridge()
    await cancelled_tail(rt)
    assert rt.responding is False
    assert rt.autofire_active is False
    assert rt._response_saw_output is False
    assert rt._response_created_id is None
    # The participant's next turn goes straight out.
    rt.ws.sent.clear()
    await rt.send_audio(b"\x01\x02" * 3200)
    await rt.commit_turn()
    assert "input_audio_buffer.commit" in rt.ws.types()


@in_a_loop
async def test_the_next_reply_is_untouched_by_a_tail_interleaved_with_it():
    """The tail can trail the next reply's response.created. It must neither
    be relayed as that reply's audio nor count as its first output."""
    rt = bridge()
    await rt.request_response()
    rt.ws.feed(CANCEL_PRE)
    agen = rt.events()
    try:
        await pull(agen, 4)
        await rt.cancel_response()
        await rt.send_audio(b"\x01\x02" * 3200)
        await rt.commit_turn()
        # A participant transcript behind the tail, only so there is an
        # event to read up to.
        rt.ws.feed([created("resp_B"), adelta("resp_X", "item_1"),
                    {"type": "conversation.item.input_audio_transcription.completed",
                     "item_id": "u1", "transcript": "Wait."}])
        mid = await pull(agen, until=lambda e: e["type"] == "user_transcript")
        assert rt._response_saw_output is False, "the tail counted as B's output"
        rt.ws.feed([tdone("resp_X", "item_1", "Okay, let's slow this down and"),
                    done("resp_X", "cancelled"),
                    tdelta("resp_B", "b1", "Fine."), adelta("resp_B", "b1"),
                    audio_done("resp_B", "b1"), tdone("resp_B", "b1", "Fine."),
                    done("resp_B")])
        rest = await pull(agen, until=lambda e: (e["type"] == "response_done"
                                                 and not e.get("stale")))
    finally:
        await agen.aclose()
    evs = mid + rest
    audio = [e for e in evs if e["type"] == "agent_audio"]
    assert [e["response_id"] for e in audio] == ["resp_B"]
    assert [e["text"] for e in evs if e["type"] == "agent_transcript"] == ["Fine."]
    dones = [e for e in evs if e["type"] == "response_done"]
    assert [bool(d.get("stale")) for d in dones] == [True, False]


@in_a_loop
async def test_a_frame_that_trails_the_cancelled_done_is_dropped_too():
    rt = bridge()
    await cancelled_tail(rt)
    rt.ws.feed([tdone("resp_X", "item_2", "We roll out")])
    agen = rt.events()
    try:
        evs = await pull(agen, until=lambda e: e["type"] == "cancelled_output")
    finally:
        await agen.aclose()
    assert "agent_transcript" not in types(evs)
    (late,) = [e for e in evs if e["type"] == "cancelled_output"]
    assert late["late"] is True and late["transcripts"] == ["We roll out"]


@in_a_loop
async def test_the_discard_can_be_switched_off(monkeypatch):
    """CANCELLED_OUTPUT_DISCARD=0 is the old relay, tail and all."""
    monkeypatch.setenv("CANCELLED_OUTPUT_DISCARD", "0")
    rt = bridge()
    _, after = await cancelled_tail(rt)
    assert "cancelled_output" not in types(after)
    assert "agent_audio" in types(after)


@in_a_loop
async def test_gemini_keeps_relaying_the_continuation():
    """Cancel is inert on Gemini and the runner treats what follows as the
    reply continuing (see _resume_seam); that route is not changed."""
    rt = bridge(NATIVE)
    _, after = await cancelled_tail(rt)
    assert "cancelled_output" not in types(after)
    assert "agent_audio" in types(after)


@in_a_loop
async def test_a_hold_keeps_its_tail():
    """The room's suppression cancel keeps the tail: it is what the hold is
    made of, and a grant can adopt it."""
    rt = bridge()
    await rt.request_response()
    rt.ws.feed(CANCEL_PRE)
    agen = rt.events()
    try:
        await pull(agen, 4)
        await rt.cancel_response(discard_tail=False)
        rt.ws.feed(CANCEL_POST)
        after = await pull(agen, until=lambda e: e["type"] == "response_done")
    finally:
        await agen.aclose()
    assert "cancelled_output" not in types(after)
    assert "agent_audio" in types(after)


@in_a_loop
async def test_a_retrys_answer_under_the_abandoned_id_is_still_relayed():
    """retry_response cancels too, and the gateway can answer the retry by
    resuming the abandoned reply under its own id. That answer is the line
    the participant is waiting for and must not be discarded."""
    rt = bridge()
    await rt.request_response()
    rt.ws.feed([created("resp_A"), tdelta("resp_A", "a1", "Well we're working")])
    agen = rt.events()
    try:
        await pull(agen, 1)
        assert await rt.retry_response() is True
        rt.ws.feed([tdelta("resp_A", "a2", "Thank you for that."),
                    adelta("resp_A", "a2"), audio_done("resp_A", "a2"),
                    tdone("resp_A", "a2", "Thank you for that."),
                    done("resp_A")])
        evs = await pull(agen, until=lambda e: e["type"] == "response_done")
    finally:
        await agen.aclose()
    assert "cancelled_output" not in types(evs)
    assert "agent_audio" in types(evs)
    # The item lines start over with the retry: the abandoned head is not
    # joined to the answer.
    assert [e["text"] for e in evs if e["type"] == "agent_transcript"] == [
        "Thank you for that."]
    assert not evs[-1].get("stale")


# --------------------------------------------------------------------------
# 2. Two-item replies, at the bridge
# --------------------------------------------------------------------------

@in_a_loop
async def test_a_two_item_reply_is_handed_on_whole():
    rt = bridge()
    await rt.request_response()
    rt.ws.feed(two_item_reply())
    agen = rt.events()
    try:
        evs = await pull(agen, until=lambda e: e["type"] == "response_done")
    finally:
        await agen.aclose()
    finals = [e for e in evs if e["type"] == "agent_transcript"]
    assert [e["text"] for e in finals] == ["Okay, thanks for that.", SPOKEN]
    assert [e["item_text"] for e in finals] == ["Okay, thanks for that.",
                                                "Six months is a start."]
    assert [e["items"] for e in finals] == [1, 2]
    deltas = [e for e in evs if e["type"] == "agent_transcript_delta"]
    # Only the opening chunk of the SECOND item is a seam.
    assert [d["text"] for d in deltas if d["item_start"]] == ["Six"]
    assert deltas[0]["first"] is True and deltas[0]["item_start"] is False
    assert evs[-1]["output_items"] == 2


def test_the_runner_puts_a_space_at_an_item_seam_and_nowhere_else():
    assert rvs._resume_seam(["that."], {"text": "Six", "item_start": True}) == " Six"
    assert rvs._resume_seam(["that. "], {"text": "Six", "item_start": True}) == "Six"
    assert rvs._resume_seam(["tha"], {"text": "t.", "item_start": False}) == "t."


@in_a_loop
async def test_frames_without_item_names_are_handed_on_as_before():
    rt = bridge()
    await rt.request_response()
    rt.ws.feed([created("r"),
                {"type": "response.output_audio_transcript.delta", "delta": "Hi."},
                {"type": "response.output_audio_transcript.done", "transcript": "Hi."},
                {"type": "response.output_audio_transcript.done", "transcript": "There."},
                done("r")])
    agen = rt.events()
    try:
        evs = await pull(agen, until=lambda e: e["type"] == "response_done")
    finally:
        await agen.aclose()
    assert [e["text"] for e in evs if e["type"] == "agent_transcript"] == ["Hi.", "There."]


# --------------------------------------------------------------------------
# 3. The runner, fed by the real bridge
# --------------------------------------------------------------------------

class FakeStore:
    def __init__(self):
        self.events = []
        self.audio = {}
        self.started_at = time.time()

    def event(self, type_, **fields):
        self.events.append(dict(type=type_, **fields))

    def append_user_audio(self, pcm):
        pass

    def append_assistant_audio(self, pcm, agent_id=None):
        self.audio[agent_id] = self.audio.get(agent_id, b"") + pcm

    def of(self, type_):
        return [e for e in self.events if e["type"] == type_]


class FakeEngine:
    def __init__(self, agent):
        self.agent = agent

    def _system_prompt(self, branches, note, group=False):
        return "SYSTEM"


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

    def append_user(self, text, **_):
        self.shared_history.append({"speaker": "user", "text": text})

    def append_agent(self, agent_id, text):
        self.shared_history.append({"speaker": agent_id, "text": text})

    async def broadcast(self, payload):
        pass

    async def auto_steer(self, *, delivered=None):
        return None


class PageWS:
    """The browser's end: `frames` of microphone audio once `go` is set, then
    a disconnect."""

    def __init__(self, frames=()):
        self._frames = list(frames)
        self.json = []
        self.binary = []

    async def receive(self):
        if self._frames:
            return {"type": "websocket.receive", "bytes": self._frames.pop(0)}
        return {"type": "websocket.disconnect"}

    async def send_json(self, payload):
        self.json.append(payload)

    async def send_bytes(self, payload):
        self.binary.append(payload)

    def frames(self, type_):
        return [f for f in self.json if f.get("type") == type_]


async def until(cond, timeout=2.0):
    end = time.time() + timeout
    while not cond():
        if time.time() > end:
            raise AssertionError("condition never held")
        await asyncio.sleep(0.01)


async def settle(runner):
    for _ in range(30):
        await asyncio.sleep(0)
    if runner._finalize_tasks:
        await asyncio.wait(list(runner._finalize_tasks), timeout=5)


def one_to_one(frames=()):
    session = FakeSession("S2A")
    ws = PageWS(frames)
    runner = rvs.RealtimeVoiceSessionRunner(session, ws)
    rt = bridge()
    runner.rt = rt
    runner.room = None
    return runner, session, ws, rt


@in_a_loop
async def test_a_barge_in_is_one_interrupted_turn_with_no_phantom_after_it():
    """fake_cancel_tail through the real runner: before this, the 0.2 s tail
    opened a second turn, flagged agent_audio_short, and played after the
    stop."""
    runner, session, ws, rt = one_to_one([LOUD] * 25)
    await rt.request_response()
    rt.ws.feed(CANCEL_PRE)
    pump = asyncio.ensure_future(runner._pump_events(rt))
    try:
        await until(lambda: len(ws.binary) == 2)
        await runner._client_to_model()          # the participant cuts in
        assert "response.cancel" in rt.ws.types()
        assert len(ws.frames("assistant_interrupted")) == 1
        rt.ws.feed(CANCEL_POST)
        await until(rt.ws.q.empty)
        await asyncio.sleep(0.3)
        await settle(runner)
    finally:
        pump.cancel()
    assert len(ws.frames("assistant_started")) == 1
    assert len(ws.binary) == 2, "the tail was played after the stop"
    turns = session.store.of("assistant_turn")
    assert len(turns) == 1 and turns[0]["interrupted"] is True
    assert turns[0]["text"] == "Okay, let's slow this down and"
    assert not session.store.of("agent_audio_short")
    (drop,) = session.store.of("cancelled_output_dropped")
    assert drop["where"] == "bridge" and drop["response_id"] == "resp_X"
    assert drop["audio_ms"] == 200
    assert drop["transcripts"] == ["Okay, let's slow this down and"]


@in_a_loop
async def test_the_runner_backstop_drops_audio_of_the_reply_it_cut_off():
    """Should the bridge ever miss one, the runner still does not open a turn
    for audio of the reply the participant just cut off; and it says so."""
    runner, session, ws, rt = one_to_one()

    class Tail:
        model = GPT
        discards_cancelled_output = True

        async def events(self):
            yield {"type": "agent_audio", "pcm": b"\x00" * 6400,
                   "response_id": "resp_X"}

    runner._barged_response_id = "resp_X"
    runner._speaking = False
    await runner._pump_events(Tail())
    assert ws.frames("assistant_started") == []
    (drop,) = session.store.of("cancelled_output_dropped")
    assert drop["where"] == "runner" and drop["audio_ms"] == 200


@in_a_loop
async def test_a_two_item_reply_is_one_turn_holding_both_lines():
    runner, session, ws, rt = one_to_one()
    await rt.request_response()
    rt.ws.feed(two_item_reply())
    pump = asyncio.ensure_future(runner._pump_events(rt))
    try:
        await until(lambda: session.store.of("assistant_turn"))
        await settle(runner)
    finally:
        pump.cancel()
    (turn,) = session.store.of("assistant_turn")
    assert turn["text"] == SPOKEN
    # The caption the page built from deltas has the seam, and the final
    # line it is handed is the whole reply.
    caption = "".join(f["text"] for f in ws.frames("assistant_text_delta"))
    assert caption == SPOKEN
    finals = ws.frames("assistant_text_final")
    assert finals[-1]["text"] == SPOKEN and finals[-1]["items"] == 2


class ReplayRT:
    """A member session that replays events recorded off the real bridge."""

    def __init__(self, evs):
        self.model = GPT
        self.pending_input = 0
        self.autofire_active = False
        self._evs = list(evs)
        self.cancels = []

    @property
    def responding(self):
        return False

    async def cancel_response(self, **kw):
        self.cancels.append(kw)

    async def events(self):
        for ev in self._evs:
            yield ev
            await asyncio.sleep(0)


class FakeRoom:
    def __init__(self, speaking=None):
        self.speaking = speaking

    async def hear(self, pcm, exclude=None):
        pass

    def session_for(self, agent_id):
        return None


async def recorded_two_item_reply():
    rt = bridge()
    await rt.request_response()
    rt.ws.feed(two_item_reply())
    agen = rt.events()
    try:
        return await pull(agen, until=lambda e: e["type"] == "response_done")
    finally:
        await agen.aclose()


@in_a_loop
async def test_a_room_member_records_both_items_too():
    session = FakeSession("S4A")
    ws = PageWS()
    runner = rvs.RealtimeVoiceSessionRunner(session, ws)
    agent = runner._resolve_agents()[0]
    runner.room = FakeRoom(speaking=agent.id)
    await runner._pump_member(agent, ReplayRT(await recorded_two_item_reply()))
    await settle(runner)
    (turn,) = session.store.of("assistant_turn")
    assert turn["text"] == SPOKEN
    caption = "".join(f["text"] for f in ws.frames("assistant_text_delta"))
    assert caption == SPOKEN


@in_a_loop
async def test_a_held_reply_keeps_the_item_seam_and_its_tail():
    """Suppressed while another member holds the floor: the hold is built
    with the seam, and the suppression cancel asks to keep the tail."""
    session = FakeSession("S4A")
    ws = PageWS()
    runner = rvs.RealtimeVoiceSessionRunner(session, ws)
    agent = runner._resolve_agents()[0]
    runner.room = FakeRoom(speaking="somebody_else")
    evs = [e for e in await recorded_two_item_reply()
           if e["type"] == "agent_transcript_delta"]
    rt = ReplayRT(evs)
    await runner._pump_member(agent, rt)
    assert rt.cancels == [{"discard_tail": False}]
    st = runner._member_states[agent.id]
    assert "".join(c for k, c in st.held if k == "text") == SPOKEN


# --------------------------------------------------------------------------
# 4. Provenance
# --------------------------------------------------------------------------

def test_what_the_bridge_does_is_on_the_record(monkeypatch):
    prov = llm.provenance(GPT)
    assert prov["pipeline_version"] >= "2026-09-23d"
    assert prov["cancelled_output"] == "discard"
    assert prov["agent_transcript_items"] == "joined"
    assert llm.provenance(NATIVE)["cancelled_output"] == "relay"
    monkeypatch.setenv("CANCELLED_OUTPUT_DISCARD", "0")
    assert llm.provenance(GPT)["cancelled_output"] == "relay"
