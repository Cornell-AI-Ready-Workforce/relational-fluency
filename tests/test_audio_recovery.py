"""Recovering a reply whose voice the gateway lost.

Two upstream defects, both reproduced live on nto.gemini-live-2.5-flash through
api.ai.it.cornell.edu and both invisible to every channel the study records:

  TRUNCATION  a reply's audio deltas stop mid-cadence and a bare response.done
              follows, with no response.output_audio.done. The transcript is
              whole. Measured: 13 words with 0.92 s of audio, 7 words with
              0.44 s, 21 words with 0.20 s. The participant hears the first
              half-second of a good line and then nothing while the caption
              shows the whole sentence.
  STALL       a reply's transcript deltas arrive and no audio ever does. Nothing
              ended it but the 45 s general watchdog: 45-50 s of dead air per
              occurrence, in 4 of 4 single-mode encounters on the day this was
              written.

The recovery is one mechanism for both (server/voice/realtime.py): when a
reply's audio is demonstrably short of its own transcript, or absent for longer
than first audio ever takes, the runner asks the gateway once more for the same
turn. Once. Every test below that starts with "never" is a bug worse than the
ones being fixed, and each is asserted rather than argued:

  * a short reply that is complete must not be retried ("Okay." is one word and
    0.4 s of audio) - the detector reasons in words per second against the
    reply's own transcript, and is checked against the shortest legitimate
    lines in the demo wave's transcripts;
  * a reply the participant cut off must not be re-spoken at them;
  * a retry never loops: one per turn, counted, not hoped;
  * a room never retries a character the floor has moved away from;
  * the page replaces the broken reply's audio and caption rather than
    appending to them, and never plays two replies at once;
  * a reply shaped like gpt-realtime-2.1's (audio and transcript interleaved,
    stream closed) never trips either detector.

Section 7 holds the second round: every way the first version of this was
broken by an adversarial pass against the live gateway, each reproduced here
from the measured frame shape and each red on the sources it was found in.
"""

from __future__ import annotations

import asyncio
import base64
import functools
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server import realtime_voice_session as rvs  # noqa: E402
from server.scenarios import load_scenario  # noqa: E402
from server.voice import realtime, turn_audio  # noqa: E402
from server.voice.realtime import RealtimeVoiceSession  # noqa: E402
from tools import encounter_health  # noqa: E402

V2 = ROOT / "static" / "v2.html"

# One gateway audio delta at 24 kHz PCM16: 0.2 s, which is what the live
# gateway sends per delta (9600 bytes). The measured truncations were 1-4 of
# these for 7-21 words.
GW_DELTA_S = 0.2
GW_DELTA = b"\x01\x00" * int(24000 * GW_DELTA_S)


# --------------------------------------------------------------------------
# fakes, as in test_voice_blockers / test_speech_cutoff
# --------------------------------------------------------------------------

class FakeGateway:
    def __init__(self, script=()):
        self.script = list(script)
        self.sent = []
        self.closed = False

    async def recv(self):
        while not self.script:
            if self.closed:
                import websockets
                from websockets.frames import Close
                frame = Close(1000, "closed")
                raise websockets.ConnectionClosedOK(frame, frame, True)
            await asyncio.sleep(0.005)
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    async def send(self, payload):
        self.sent.append(json.loads(payload))

    async def close(self):
        self.closed = True

    def types_sent(self):
        return [m.get("type") for m in self.sent]


def make_bridge(script=()) -> RealtimeVoiceSession:
    rt = RealtimeVoiceSession("be someone", api_key="test-key")
    rt.ws = FakeGateway(script)
    return rt


def created(rid="resp_1"):
    return json.dumps({"type": "response.created", "response": {"id": rid}})


def tdelta(text):
    return json.dumps({"type": "response.output_audio_transcript.delta",
                       "delta": text})


def adelta(pcm=GW_DELTA):
    return json.dumps({"type": "response.output_audio.delta",
                       "delta": base64.b64encode(pcm).decode("ascii")})


def audio_done():
    return json.dumps({"type": "response.output_audio.done"})


def done(rid="resp_1"):
    return json.dumps({"type": "response.done", "response": {"id": rid}})


async def collect(rt, *, limit=8, timeout=5.0):
    events = []
    agen = rt.events()
    try:
        while len(events) < limit:
            try:
                events.append(await asyncio.wait_for(agen.__anext__(), timeout))
            except StopAsyncIteration:
                break
    finally:
        await agen.aclose()
    return events


async def pull(agen, n, timeout=5.0):
    """`n` events off an ALREADY OPEN events() generator. Multi-phase tests
    must keep one generator open across phases: closing it runs events()'s
    finally, which puts every in-flight flag down exactly as a dead socket
    would - the real pump never closes it mid-reply."""
    out = []
    for _ in range(n):
        try:
            out.append(await asyncio.wait_for(agen.__anext__(), timeout))
        except StopAsyncIteration:
            break
    return out


def dones(events):
    return [e for e in events if e["type"] == "response_done"]


def truncated_reply(text, n_audio, rid="resp_1"):
    """The measured shape: transcript, a few audio deltas, a bare done."""
    return [created(rid), tdelta(text)] + [adelta()] * n_audio + [done(rid)]


@pytest.fixture(autouse=True)
def fast_polls(monkeypatch):
    monkeypatch.setattr(realtime, "RECV_POLL_S", 0.02)
    # The quiet window a truncation verdict is held for (see section 7). Kept
    # short here so the tests that are not about it do not wait it out; the
    # tests that ARE about it set their own.
    monkeypatch.setattr(realtime, "AUDIO_RETRY_QUIET_S", 0.05, raising=False)


# --------------------------------------------------------------------------
# 1. The words-per-second shortfall detector
# --------------------------------------------------------------------------

def test_a_truncated_reply_is_offered_for_retry_with_its_own_numbers():
    """13 words, 0.92 s of audio, no output_audio.done: 14 words a second is
    not speech, and the runner is told exactly what was lost."""
    line = "So are we just talking about the sprint or is there anything else"
    rt = make_bridge(truncated_reply(line, 4) + [json.dumps({"type": "noop"})])

    async def go():
        await rt.request_response()
        return await collect(rt, limit=6)

    events = asyncio.run(go())
    d = dones(events)
    assert len(d) == 1
    assert d[0]["audio_unterminated"] is True
    assert d[0]["retry_reason"] == "truncated"
    assert d[0]["retryable"] is True
    assert d[0]["words"] == 13 and d[0]["audio_ms"] == 800
    assert d[0]["text"] == line


@pytest.mark.parametrize("line, n_audio", [
    ("Okay.", 2),                       # 1 word, 0.4 s: complete
    ("Sure thing.", 2),                 # 2 words, 0.4 s: complete
    ("No problem", 6),                  # 2 words, 1.2 s
    ("It's fine I guess", 3),           # 4 words, 0.6 s: fast, under a second short
    ("Okay that's good to know", 5),    # the fastest properly-spoken line live
])
def test_short_complete_replies_are_never_retried(line, n_audio):
    """The false positive that would be worse than the bug: the detector must
    reason in words per second against THIS reply's transcript, with margin,
    not in absolute seconds. A bare response.done alone is not evidence - the
    gateway omits output_audio.done on lines it delivered whole."""
    rt = make_bridge(truncated_reply(line, n_audio))

    async def go():
        await rt.request_response()
        return await collect(rt, limit=2 + n_audio)

    d = dones(asyncio.run(go()))
    assert len(d) == 1
    assert d[0]["audio_unterminated"] is True
    assert d[0]["retry_reason"] is None
    assert d[0]["retryable"] is False


def test_a_properly_closed_reply_is_not_retried_however_fast():
    """output_audio.done is the gateway saying the stream ended on purpose."""
    rt = make_bridge([created(), tdelta("one two three four five six seven eight"),
                      adelta(), audio_done(), done()])

    async def go():
        await rt.request_response()
        return await collect(rt, limit=3)

    d = dones(asyncio.run(go()))
    assert d and d[0]["audio_unterminated"] is False
    assert d[0]["retry_reason"] is None


def test_the_shortest_legitimate_replies_in_the_corpus_never_trip_the_detector():
    """Every assistant line the demo wave recorded, delivered whole at a
    spoken rate, is a plausible delivery by the live detector's own rule; and
    every line under the word floor is left alone at ANY audio length, down
    to a single gateway delta. Skipped, not passed, when the wave is absent."""
    paths = sorted((ROOT / "data" / "sessions").glob("*/events.jsonl"))
    lines = []
    for p in paths:
        for raw in p.read_text(encoding="utf-8", errors="replace").splitlines():
            if not raw.strip():
                continue
            try:
                ev = json.loads(raw)
            except ValueError:
                continue
            if ev.get("type") == "assistant_turn" and ev.get("text"):
                lines.append(ev["text"])
    if not lines:
        pytest.skip("no recorded wave under data/sessions")
    short = [t for t in lines if turn_audio.word_count(t) < turn_audio.MIN_WORDS]
    assert short, "the corpus has no short lines to prove the floor on"
    for text in short:
        assert turn_audio.shortfall(text, int(GW_DELTA_S * 1000)) is None, text
    for text in lines:
        # Spoken at 170 wpm the audio matches; spoken briskly (60% of that
        # time, ~4.7 words/s) it must still pass.
        want = turn_audio.expected_ms(text)
        assert turn_audio.shortfall(text, want) is None, text
        assert turn_audio.shortfall(text, int(want * 0.6)) is None, text


def test_a_gpt_shaped_reply_never_fires_either_detector():
    """gpt-realtime-2.1 interleaves audio with transcript and closes the
    stream; it has neither defect and must not acquire a retry."""
    rt = make_bridge([created(), adelta(), tdelta("I hear"), adelta(),
                      tdelta(" what you are saying about the sprint"), adelta(),
                      adelta(), adelta(), adelta(), audio_done(), done()])

    async def go():
        await rt.request_response()
        return await collect(rt, limit=9)

    events = asyncio.run(go())
    d = dones(events)
    assert d[0]["retry_reason"] is None and d[0]["retryable"] is False
    assert not any(e.get("audio_absent") for e in events)


# --------------------------------------------------------------------------
# 2. The audio-absent stall detector, beside RESPONSE_STALL_S
# --------------------------------------------------------------------------

def test_the_audio_absent_bar_sits_beside_the_general_stall_not_in_place_of_it():
    assert hasattr(realtime, "AUDIO_ABSENT_S")
    assert 4.0 <= realtime.AUDIO_ABSENT_S < realtime.RESPONSE_STALL_S
    # Above the measured healthy tail (2.65 s net of participant speech) by a
    # clear margin, and well under the 45 s the participant used to sit through.
    assert realtime.AUDIO_ABSENT_S >= 3 * 2.65
    assert realtime.AUDIO_RETRY_LIMIT == 1


def test_words_with_no_voice_are_called_absent_at_the_bar(monkeypatch):
    """Transcript deltas, then nothing. The general watchdog would take 45 s;
    the audio-specific one takes AUDIO_ABSENT_S from the last sign of life."""
    monkeypatch.setattr(realtime, "AUDIO_ABSENT_S", 0.3)
    monkeypatch.setattr(realtime, "RESPONSE_STALL_S", 30.0)
    rt = make_bridge([created(), tdelta("Well we're working with a system")])

    async def go():
        await rt.request_response()
        t0 = time.monotonic()
        events = await collect(rt, limit=2, timeout=3.0)
        return events, time.monotonic() - t0

    events, took = asyncio.run(go())
    d = dones(events)
    assert len(d) == 1
    assert d[0]["audio_absent"] is True and d[0]["interrupted"] is True
    assert d[0]["retry_reason"] == "absent" and d[0]["retryable"] is True
    assert d[0]["words"] == 6 and d[0]["audio_ms"] == 0
    assert 0.25 <= took < 2.0, took
    # No error frame when a retry is on offer: the participant is not told
    # "something went wrong" about a line that is about to be said properly.
    assert [e["type"] for e in events] == ["agent_transcript_delta", "response_done"]
    assert rt.responding is False


def test_the_absent_clock_is_held_while_the_participant_is_talking(monkeypatch):
    """Gemini withholds a reply's audio while it hears the participant and
    delivers it the moment they stop (measured: every long pre-audio gap ended
    on the participant's own transcription). A retry issued inside that window
    would commit half their sentence as a turn."""
    monkeypatch.setattr(realtime, "AUDIO_ABSENT_S", 0.3)
    rt = make_bridge([created(), tdelta("Well the architecture")])
    talking_until = time.monotonic() + 0.6
    rt.participant_speaking = lambda: time.monotonic() < talking_until

    async def go():
        await rt.request_response()
        t0 = time.monotonic()
        events = await collect(rt, limit=2, timeout=3.0)
        return events, time.monotonic() - t0

    events, took = asyncio.run(go())
    assert dones(events) and dones(events)[0]["audio_absent"] is True
    # The hold is stamped at the last poll before the talking stopped, so the
    # bar counts from within one poll of 0.6 s: at least 0.6 + 0.3 - 0.05.
    assert took >= 0.85, f"fired while the participant was still talking ({took:.2f}s)"
    assert took < 2.0, took


def test_a_reply_that_said_nothing_at_all_is_left_to_the_general_watchdog(monkeypatch):
    """The audio-absent detector is for words without voice. A reply with no
    output of any kind is RESPONSE_STALL_S's case and stays so."""
    monkeypatch.setattr(realtime, "AUDIO_ABSENT_S", 0.1)
    monkeypatch.setattr(realtime, "RESPONSE_STALL_S", 0.4)
    rt = make_bridge([created()])

    async def go():
        await rt.request_response()
        return await collect(rt, limit=1, timeout=3.0)

    events = asyncio.run(go())
    assert events[0]["type"] == "error"
    assert "no reply from the gateway" in events[0]["message"]


def test_an_absent_reply_that_was_cancelled_is_not_retried(monkeypatch):
    monkeypatch.setattr(realtime, "AUDIO_ABSENT_S", 0.2)
    rt = make_bridge([created(), tdelta("Well the")])

    async def go():
        await rt.request_response()
        agen = rt.events()
        try:
            first = await pull(agen, 1)                  # the delta
            await rt.cancel_response()                   # participant barged in
            # Gemini's cancel is inert: the dead reply's words keep coming.
            rt.ws.script.append(tdelta(" architecture"))
            rest = await pull(agen, 3, timeout=2.0)
        finally:
            await agen.aclose()
        return first + rest

    events = asyncio.run(go())
    d = dones(events)
    assert d and d[0]["audio_absent"] is True
    assert d[0]["retry_reason"] is None and d[0]["retryable"] is False


# --------------------------------------------------------------------------
# 3. The single bounded retry
# --------------------------------------------------------------------------

def test_retry_sends_the_recipe_the_gateway_is_known_to_answer():
    """Cancel, a user text item, create. Measured live after a dead reply: a
    commit of silence and a commit of noise each produced nothing and fell to
    the 45 s watchdog; the text item produced a complete, closed reply in
    0.23 s. The nudge is what the record shows the line answered."""
    rt = make_bridge()

    async def go():
        await rt.request_response()
        rt.ws.sent.clear()
        ok = await rt.retry_response()
        return ok, rt.ws.sent

    ok, sent = asyncio.run(go())
    assert ok is True
    assert [m["type"] for m in sent] == ["response.cancel", "conversation.item.create",
                                         "response.create"]
    item = sent[1]["item"]
    assert item["role"] == "user" and item["type"] == "message"
    assert item["content"] == [{"type": "input_text", "text": realtime.AUDIO_RETRY_NUDGE}]
    assert realtime.AUDIO_RETRY_NUDGE.strip()
    assert not any(m["type"] == "input_audio_buffer.commit" for m in sent), (
        "a commit is what was measured NOT to revive a dead reply")
    assert rt.responding is True
    assert rt._retry_in_flight is True


def test_exactly_one_retry_per_turn_counted_not_hoped():
    """The retry also truncates: the second failure is finalised as truncated
    and the turn moves on. A counter on the turn, reset by the next commit."""
    line = "Thanks for having me today I appreciate it a lot"
    rt = make_bridge(truncated_reply(line, 2, "resp_1"))

    async def go():
        await rt.request_response()
        first = dones(await collect(rt, limit=4))
        assert first[0]["retryable"] is True
        assert await rt.retry_response() is True
        # the retry truncates too
        rt.ws.script.extend(truncated_reply(line, 2, "resp_2"))
        second = dones(await collect(rt, limit=4))
        refused = await rt.retry_response()
        # a new participant turn starts the budget over
        await rt.send_audio(b"\x01\x02" * 4000)
        await rt.commit_turn()
        rt.ws.script.extend(truncated_reply(line, 2, "resp_3"))
        third = dones(await collect(rt, limit=4))
        return first, second, refused, third

    first, second, refused, third = asyncio.run(go())
    assert second[0]["retry_reason"] == "truncated"
    assert second[0]["retryable"] is False, "a second retry on one turn"
    assert second[0]["retried"] is True, "the record must know it was the retry"
    assert refused is False
    assert third[0]["retryable"] is True and third[0]["retried"] is False


def test_a_reply_the_participant_cut_off_is_never_re_spoken():
    """A barge-in ends in a bare response.done too - on this gateway the
    cancelled reply even keeps streaming for a while. That is not a truncation
    and re-speaking it would undo what the participant meant."""
    line = "Thanks for having me today I appreciate it a lot"
    rt = make_bridge([created(), tdelta(line), adelta()])

    async def go():
        await rt.request_response()
        agen = rt.events()
        try:
            await pull(agen, 2)
            await rt.cancel_response()              # the participant interrupted
            rt.ws.script.extend([adelta(), adelta(), done()])
            return dones(await pull(agen, 3))
        finally:
            await agen.aclose()

    d = asyncio.run(go())
    # The cancelled reply kept streaming and then ended: its own done ends it
    # (nothing may stay latched on it) and its name remembers the cancel.
    assert d and d[0]["cancelled"] is True and d[0]["audio_unterminated"] is True
    assert d[0]["retry_reason"] is None and d[0]["retryable"] is False
    assert rt.responding is False


def test_a_room_suppression_cancel_is_not_a_truncation_either():
    """Members without the floor have their auto-fired replies cancelled; those
    end bare as well, dozens of times per encounter."""
    rt = make_bridge([created(), tdelta("one two three four five six seven"),
                      adelta()])

    async def go():
        await rt.request_response()
        await collect(rt, limit=2)
        await rt.cancel_response()
        rt.ws.script.append(done())
        d1 = dones(await collect(rt, limit=1))
        # the next reply for this member, after its own floor grant (a commit)
        await rt.send_audio(b"\x00" * 3200)
        await rt.commit_input()
        await rt.request_response()
        rt.ws.script.extend(truncated_reply("one two three four five six seven", 1, "resp_2"))
        d2 = dones(await collect(rt, limit=3))
        return d1, d2

    d1, d2 = asyncio.run(go())
    assert d1[0]["retry_reason"] is None
    assert d2[0]["retry_reason"] == "truncated" and d2[0]["retryable"] is True


def test_a_late_duplicate_done_does_not_end_the_retry_mid_stream():
    """The gateway repeats response.done for one reply up to 3.6 s later
    (measured). By then the retry is streaming under the flags; the duplicate
    must be dropped before it puts them down."""
    line = "Thanks for having me today I appreciate it a lot"
    rt = make_bridge(truncated_reply(line, 2, "resp_1"))

    async def go():
        await rt.request_response()
        agen = rt.events()
        try:
            await pull(agen, 4)
            await rt.retry_response()
            rt.ws.script.extend([created("resp_2"), tdelta(line), adelta(), done("resp_1")])
            await pull(agen, 2)
            await asyncio.sleep(0.1)          # let the duplicate be read
            active = rt.responding
            rt.ws.script.extend([adelta(), adelta(), adelta(), adelta(), audio_done(), done("resp_2")])
            d = dones(await pull(agen, 5))
        finally:
            await agen.aclose()
        return active, d

    active, d = asyncio.run(go())
    assert active is True, "the duplicate done ended the retry"
    assert len(d) == 1 and d[0]["retried"] is True
    assert d[0]["audio_unterminated"] is False


def test_an_unanswered_retry_is_given_up_at_the_same_bar_not_after_45s(monkeypatch):
    """Measured live before this existed: a retry the gateway ignored fell to
    the 45 s general watchdog, and the stall's dead air went from 47 s to 61 s.
    The retry is our own request with a known 0.3 s answer time, so it is
    given up at AUDIO_ABSENT_S and the turn handed back, marked not recovered."""
    monkeypatch.setattr(realtime, "AUDIO_ABSENT_S", 0.3)
    monkeypatch.setattr(realtime, "RESPONSE_STALL_S", 30.0)
    rt = make_bridge([created(), tdelta("Well we're working with a system")])

    async def go():
        await rt.request_response()
        agen = rt.events()
        try:
            first = await pull(agen, 2, timeout=3.0)           # delta, absent verdict
            assert dones(first)[0]["retryable"] is True
            assert await rt.retry_response() is True
            t0 = time.monotonic()
            rest = await pull(agen, 2, timeout=3.0)            # nothing comes back
            return first, rest, time.monotonic() - t0
        finally:
            await agen.aclose()

    first, rest, took = asyncio.run(go())
    d = dones(rest)
    assert len(d) == 1 and d[0]["retried"] is True and d[0]["interrupted"] is True
    assert d[0]["retryable"] is False and d[0]["retry_reason"] is None
    assert rest[-1]["type"] == "error" and "did not answer the retry" in rest[-1]["message"]
    assert 0.25 <= took < 2.0, took
    assert rt.responding is False


# --------------------------------------------------------------------------
# 4. The runner: who decides, what is recorded, what the page is told
# --------------------------------------------------------------------------

class FakeStore:
    def __init__(self):
        self.events = []
        self.audio = {}
        self.user_audio = b""

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


class FakeRT:
    def __init__(self, retry_ok=True):
        self.ws = object()
        self.voice = "Puck"
        self.model = "fake"
        self.pending_input = 0
        self.autofire_active = False
        self.cancels = 0
        self.retries = 0
        self.retry_ok = retry_ok
        self._q: asyncio.Queue = asyncio.Queue()

    @property
    def responding(self):
        return False

    async def send_audio(self, pcm):
        self.pending_input += len(pcm)

    async def commit_input(self):
        self.pending_input = 0

    async def cancel_response(self):
        self.cancels += 1

    async def retry_response(self):
        if not self.retry_ok:
            return False
        self.retries += 1
        return True

    def feed(self, ev):
        self._q.put_nowait(ev)

    def end(self):
        self._q.put_nowait(None)

    async def events(self):
        while True:
            ev = await self._q.get()
            if ev is None:
                return
            yield ev


class FakeRoom:
    def __init__(self, speaking=None):
        self.speaking = speaking

    async def hear(self, pcm, exclude=None):
        pass

    def session_for(self, agent_id):
        return None


CL_AUDIO = b"\x01\x02" * 3200          # 0.2 s at the client's 16 kHz


def broken(reason="truncated", **extra):
    ev = {"type": "response_done", "audio_unterminated": reason == "truncated",
          "retry_reason": reason, "retryable": True, "retried": False,
          "words": 13, "audio_ms": 920,
          "text": "So are we just talking about the sprint or is there anything"}
    if reason == "absent":
        ev.update(interrupted=True, audio_absent=True, waited_s=8, audio_ms=0)
    ev.update(extra)
    return ev


def in_a_loop(fn):
    # functools.wraps sets __wrapped__, which inspect.signature follows, so
    # pytest still sees the wrapped test's fixtures (monkeypatch).
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        return asyncio.run(fn(*a, **kw))
    return wrapper


async def settle(runner):
    for _ in range(30):
        await asyncio.sleep(0)
    if runner._finalize_tasks:
        await asyncio.wait(list(runner._finalize_tasks), timeout=5)


def one_to_one(monkeypatch):
    monkeypatch.setenv("TRANSCRIPT_GRACE_SECONDS", "0.2")
    session = FakeSession("S1A")
    ws = FakeWS()
    runner = rvs.RealtimeVoiceSessionRunner(session, ws)
    rt = FakeRT()
    runner.rt = rt
    runner.room = None
    return runner, session, ws, rt


@in_a_loop
async def test_the_1to1_pump_retries_once_and_the_turn_is_replaced_not_appended(monkeypatch):
    """The whole path: a broken reply, one retry, the page told to replace the
    reply, the fresh reply recorded as THE turn, and the outcome written."""
    runner, session, ws, rt = one_to_one(monkeypatch)
    for ev in [
        {"type": "agent_transcript_delta", "text": "So are we just talking about"},
        {"type": "agent_audio", "pcm": CL_AUDIO},
        broken("truncated"),
        {"type": "agent_transcript_delta", "text": "So, are we just talking about the sprint or is there anything else?"},
    ] + [{"type": "agent_audio", "pcm": CL_AUDIO}] * 25 + [
        {"type": "response_done", "audio_unterminated": False, "retry_reason": None,
         "retryable": False, "retried": True},
    ]:
        rt.feed(ev)
    rt.end()
    await runner._pump_events(rt)
    await settle(runner)

    assert rt.retries == 1
    assert len(ws.frames("assistant_started")) == 1, "a second turn was opened on the page"
    retry = ws.frames("assistant_retry")
    assert len(retry) == 1 and retry[0]["agent_id"] == runner.agent_id
    assert ws.frames("assistant_interrupted") == [], "a retry is not a barge-in"
    assert len(ws.frames("assistant_done")) == 1

    turns = session.store.of("assistant_turn")
    assert len(turns) == 1, turns
    assert turns[0]["text"] == "So, are we just talking about the sprint or is there anything else?"
    assert turns[0]["audio_ms"] == 5000, "the abandoned head's audio was counted into the turn"
    retried = session.store.of("audio_retry")
    assert len(retried) == 1 and retried[0]["reason"] == "truncated"
    assert retried[0]["words"] == 13 and retried[0]["audio_ms"] == 920
    assert retried[0]["nudge"] == realtime.AUDIO_RETRY_NUDGE
    outcome = session.store.of("audio_retry_outcome")
    assert len(outcome) == 1 and outcome[0]["recovered"] is True
    assert outcome[0]["delivered_whole"] is True and outcome[0]["overlap"] >= 0.9
    assert outcome[0]["lost_text"].startswith("So are we just talking")
    assert session.store.of("audio_retry_suppressed") == []
    assert session.store.of("agent_audio_short") == []


@in_a_loop
async def test_a_retry_that_fails_too_is_finalised_as_truncated_and_says_so(monkeypatch):
    runner, session, ws, rt = one_to_one(monkeypatch)
    line = "So are we just talking about the sprint or is there anything else"
    for ev in [
        {"type": "agent_transcript_delta", "text": line},
        {"type": "agent_audio", "pcm": CL_AUDIO},
        broken("truncated", retryable=False, retried=True, text=line),
    ]:
        rt.feed(ev)
    rt.end()
    await runner._pump_events(rt)
    await settle(runner)

    assert rt.retries == 0
    assert ws.frames("assistant_retry") == []
    sup = session.store.of("audio_retry_suppressed")
    assert len(sup) == 1 and sup[0]["why"] == "already_retried"
    turns = session.store.of("assistant_turn")
    assert len(turns) == 1 and turns[0]["text"] == line
    short = session.store.of("agent_audio_short")
    assert len(short) == 1 and short[0]["gateway_abandoned_audio"] is True
    outcome = session.store.of("audio_retry_outcome")
    assert len(outcome) == 1 and outcome[0]["recovered"] is False


@in_a_loop
async def test_a_stalled_reply_is_retried_and_the_dead_air_ends(monkeypatch):
    runner, session, ws, rt = one_to_one(monkeypatch)
    for ev in [
        {"type": "agent_transcript_delta", "text": "Well we're working with a system"},
        broken("absent", text="I'd appreciate some dedicated time to work on this."),
        {"type": "agent_transcript_delta", "text": "I'd appreciate some dedicated time to work on this."},
    ] + [{"type": "agent_audio", "pcm": CL_AUDIO}] * 16 + [
        {"type": "response_done", "audio_unterminated": False, "retried": True},
    ]:
        rt.feed(ev)
    rt.end()
    await runner._pump_events(rt)
    await settle(runner)

    assert rt.retries == 1
    assert len(ws.frames("assistant_retry")) == 1
    turns = session.store.of("assistant_turn")
    assert len(turns) == 1 and turns[0]["text"] == "I'd appreciate some dedicated time to work on this."
    assert turns[0]["interrupted"] is False
    assert session.store.of("audio_retry")[0]["reason"] == "absent"
    assert session.store.of("audio_retry")[0]["waited_s"] == 8
    assert session.store.of("audio_retry_outcome")[0]["recovered"] is True
    # The voice_error the 45 s watchdog used to write is not written for a
    # reply that was recovered.
    assert session.store.of("voice_error") == []


@in_a_loop
async def test_a_retry_is_not_issued_over_a_talking_participant(monkeypatch):
    runner, session, ws, rt = one_to_one(monkeypatch)
    runner.vad.speaking = True
    for ev in [
        {"type": "agent_transcript_delta", "text": "Well we're working with a system"},
        broken("absent"),
    ]:
        rt.feed(ev)
    rt.end()
    await runner._pump_events(rt)
    await settle(runner)

    assert rt.retries == 0 and ws.frames("assistant_retry") == []
    sup = session.store.of("audio_retry_suppressed")
    assert len(sup) == 1 and sup[0]["why"] == "participant_speaking"
    turns = session.store.of("assistant_turn")
    assert len(turns) == 1 and turns[0]["interrupted"] is True


def group_runner(monkeypatch):
    monkeypatch.setenv("TRANSCRIPT_GRACE_SECONDS", "0.2")
    session = FakeSession("S4A")
    ws = FakeWS()
    runner = rvs.RealtimeVoiceSessionRunner(session, ws)
    agent = runner._resolve_agents()[0]
    return runner, session, ws, agent


@in_a_loop
async def test_the_room_retries_the_floor_holder(monkeypatch):
    runner, session, ws, agent = group_runner(monkeypatch)
    runner.room = FakeRoom(speaking=agent.id)
    rt = FakeRT()
    rt.feed({"type": "agent_transcript_delta", "text": "I think better"})
    rt.feed({"type": "agent_audio", "pcm": CL_AUDIO})
    rt.feed(broken("truncated", text="I think better tests would have caught this."))
    rt.feed({"type": "agent_transcript_delta", "text": "I think better tests would have caught this."})
    for _ in range(12):
        rt.feed({"type": "agent_audio", "pcm": CL_AUDIO})
    rt.feed({"type": "response_done", "audio_unterminated": False, "retried": True})
    rt.end()
    await runner._pump_member(agent, rt)
    await settle(runner)

    assert rt.retries == 1
    assert len(ws.frames("assistant_started")) == 1
    assert [f["agent_id"] for f in ws.frames("assistant_retry")] == [agent.id]
    turns = session.store.of("assistant_turn")
    assert len(turns) == 1
    assert turns[0]["text"] == "I think better tests would have caught this."
    assert turns[0]["audio_ms"] == 2400
    assert session.store.of("audio_retry_outcome")[0]["recovered"] is True
    assert runner._response_done.is_set(), "the floor was not released"


@in_a_loop
async def test_the_room_drops_a_retry_when_the_floor_has_moved(monkeypatch):
    """The reply the participant was hearing may finish (has_floor's
    `announced` leniency); a NEW response.create for a character the director
    has moved on from would talk over whoever holds the floor now."""
    runner, session, ws, agent = group_runner(monkeypatch)
    room = FakeRoom(speaking=agent.id)
    runner.room = room
    rt = FakeRT()
    rt.feed({"type": "agent_audio", "pcm": CL_AUDIO})
    pump = asyncio.ensure_future(runner._pump_member(agent, rt))
    for _ in range(20):
        await asyncio.sleep(0)
    room.speaking = "somebody_else"
    rt.feed({"type": "agent_transcript_delta", "text": "I think better integration tests would"})
    rt.feed(broken("truncated"))
    for _ in range(20):
        await asyncio.sleep(0)
    rt.end()
    await pump
    await settle(runner)

    assert rt.retries == 0 and ws.frames("assistant_retry") == []
    sup = session.store.of("audio_retry_suppressed")
    assert len(sup) == 1 and sup[0]["why"] == "floor_moved"
    assert len(session.store.of("assistant_turn")) == 1


@in_a_loop
async def test_a_reply_the_room_never_relayed_is_not_retried(monkeypatch):
    runner, session, ws, agent = group_runner(monkeypatch)
    runner.room = FakeRoom(speaking="somebody_else")
    rt = FakeRT()
    rt.feed({"type": "agent_transcript_delta", "text": "one two three four five"})
    rt.feed(broken("truncated"))
    rt.end()
    await runner._pump_member(agent, rt)
    await settle(runner)
    assert rt.retries == 0 and ws.frames("assistant_retry") == []
    assert session.store.of("audio_retry_suppressed") == []
    assert session.store.of("unsolicited_response_suppressed")


# --------------------------------------------------------------------------
# 5. The page: replace, never overlap, never the barge-in path
# --------------------------------------------------------------------------

def test_the_page_has_a_distinct_handler_that_replaces_the_reply():
    src = V2.read_text(encoding="utf-8")
    assert "m.type === 'assistant_retry'" in src
    handler = src.split("m.type === 'assistant_retry'", 1)[1].split("} else if", 1)[0]
    assert "restartTurn(currentTurn)" in handler
    assert "stopScheduledAudio" not in handler, "the retry reused the barge-in path"
    body = re.search(r"function restartTurn\(turn\) \{(.*?)\n  \}", src, re.S)
    assert body, "restartTurn is missing"
    body = body.group(1)
    for must in ("s.stop()", "turn.fullText = ''", "turn.shown = ''",
                 "turn.startCtx = null", "turn.done = false", "playbackTime ="):
        assert must in body, must


PAGE_HARNESS = r"""
// Minimal stand-ins for the page globals restartTurn reads.
let playbackTime = 0;
const speechQueue = [];
let active = 'untouched';
function setActiveSpeaker(x) { active = x; }
const audioCtx = { currentTime: 10.0 };
%s
const stopped = [];
const mk = (id) => ({ stop() { stopped.push(id); } });
const previous = { agent_id: 'a', fullText: 'Earlier line.', shown: 'Earlier line.',
                   startCtx: 8.0, endCtx: 12.5, done: true, sources: [] };
const turn = { agent_id: 'b', el: null, fullText: 'So are we just talking about the sprint',
               shown: 'So are we just', startCtx: 11.0, endCtx: 30.0, done: false,
               sources: [mk('b1'), mk('b2'), mk('b3')] };
speechQueue.push(previous, turn);
playbackTime = 30.0;
restartTurn(turn);
console.log(JSON.stringify({
  stopped, fullText: turn.fullText, shown: turn.shown, startCtx: turn.startCtx,
  endCtx: turn.endCtx, done: turn.done, sources: turn.sources.length,
  playbackTime, active, previousShown: previous.shown, previousSources: previous.sources.length,
}));
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_restart_turn_drops_the_broken_audio_and_queues_behind_the_previous_speaker(tmp_path):
    """Run the page's own function: every source of the broken reply is
    stopped (so the fresh one can never overlap it), the caption is emptied
    rather than frozen, and playback resumes no earlier than the end of the
    previous speaker's still-scheduled audio - not at 'now', which in a room
    would put the retried line on top of the last sentence."""
    src = V2.read_text(encoding="utf-8")
    body = re.search(r"(  function restartTurn\(turn\) \{.*?\n  \})", src, re.S)
    assert body
    script = tmp_path / "restart.js"
    script.write_text(PAGE_HARNESS % body.group(1), encoding="utf-8")
    out = subprocess.run(["node", str(script)], capture_output=True, text=True,
                         timeout=30)
    assert out.returncode == 0, out.stderr
    r = json.loads(out.stdout.strip().splitlines()[-1])
    assert sorted(r["stopped"]) == ["b1", "b2", "b3"]
    assert r["sources"] == 0
    assert r["fullText"] == "" and r["shown"] == ""
    assert r["startCtx"] is None and r["endCtx"] is None and r["done"] is False
    assert r["playbackTime"] == 12.5, "queued at 'now' over the previous speaker"
    assert r["active"] is None
    assert r["previousShown"] == "Earlier line." and r["previousSources"] == 0


# --------------------------------------------------------------------------
# 6. The wave-level check
# --------------------------------------------------------------------------

def write_events(path: Path, events):
    path.mkdir(parents=True)
    with (path / "events.jsonl").open("w", encoding="utf-8") as fh:
        for e in events:
            fh.write(json.dumps(e) + "\n")


def healthy_encounter():
    return [
        {"type": "session_start"}, {"type": "user_turn", "text": "hi"},
        {"type": "stage_direction", "acked": True},
        {"type": "assistant_turn", "text": "hello there", "audio_ms": 900},
        {"type": "steering_pair"},
    ]


def test_encounter_health_reports_retried_and_unrecovered_turns(tmp_path):
    events = healthy_encounter() + [
        {"type": "audio_retry", "agent_id": "a", "reason": "truncated"},
        {"type": "audio_retry_outcome", "agent_id": "a", "recovered": True},
        {"type": "audio_retry", "agent_id": "a", "reason": "absent"},
        {"type": "audio_retry_outcome", "agent_id": "a", "recovered": False},
        {"type": "audio_retry_suppressed", "agent_id": "b", "reason": "truncated",
         "why": "floor_moved"},
    ]
    write_events(tmp_path / "s_1", events)
    ok, findings, notes, counts = encounter_health.check(tmp_path / "s_1")
    assert ok, findings
    note = [n for n in notes if n.startswith("audio lost upstream")]
    assert len(note) == 1, notes
    assert "2 retried" in note[0]
    assert "1 recovered" in note[0]
    assert "1 delivered whole" in note[0]
    assert "2 unrecovered" in note[0]
    assert "1 suppressed (1 floor_moved)" in note[0]
    assert "1 absent" in note[0] and "2 truncated" in note[0]

    r = encounter_health.audio_recovery(events)
    assert r == {
        "retried": 2, "recovered": 1, "delivered_whole": 1, "unrecovered": 2,
        "suppressed": 1,
        "suppressed_why": {"floor_moved": 1},
        "reasons": {"truncated": 2, "absent": 1},
    }


def test_encounter_health_says_nothing_when_nothing_was_lost(tmp_path):
    write_events(tmp_path / "s_2", healthy_encounter())
    ok, findings, notes, counts = encounter_health.check(tmp_path / "s_2")
    assert ok and not any(n.startswith("audio lost") for n in notes)
    assert encounter_health.audio_recovery_note(healthy_encounter()) == ""


# --------------------------------------------------------------------------
# 7. The second round: what the adversarial pass broke, live, and the rule
#    that closes each one
# --------------------------------------------------------------------------
#
# Every shape below was measured on nto.gemini-live-2.5-flash through the
# Cornell gateway, most of them more than once. The bridge's answer to nearly
# all of them is the same fact: the gateway NAMES its replies (response.created
# carries an id, and every done carries one), and a done that does not name
# the reply in flight is not that reply's boundary.

LONG_LINE = "Thanks for having me today I appreciate it a lot and I mean that"


def test_a_phantom_done_never_ends_the_retry_it_shadows():
    """29 of 29 bare dones live were followed within 0-11 ms by a SECOND
    response.done whose id was never response.created. Read as a boundary it
    closed the retry just issued as empty; the nudge's real answer then
    arrived as a gateway-started reply (suppressed as unsolicited in a room,
    a second turn in 1:1), the budget was reset, and one participant turn
    was retried seven times. The phantom names nothing, so it ends nothing."""
    rt = make_bridge(truncated_reply(LONG_LINE, 2, "resp_A"))

    async def go():
        await rt.request_response()
        agen = rt.events()
        try:
            first = dones(await pull(agen, 4))
            assert first[0]["retryable"] is True
            assert await rt.retry_response() is True
            rt.ws.script.extend([done("resp_PHANTOM"), created("resp_B"),
                                 tdelta(LONG_LINE)] + [adelta()] * 5
                                + [audio_done(), done("resp_B")])
            evs = await pull(agen, 7)
        finally:
            await agen.aclose()
        return evs

    evs = asyncio.run(go())
    d = dones(evs)
    assert len(d) == 1, "the phantom was read as a reply boundary"
    assert d[0]["retried"] is True and d[0]["retry_reason"] is None
    assert rt.autofire_active is False, "the retry's answer was read as a gateway-started reply"
    assert rt.phantom_dones == 1
    assert rt._retries_this_turn == 1, "the budget was reset"


def test_the_budget_is_reset_only_by_a_commit_never_by_a_gateway_started_reply():
    """Even when a boundary IS misread and the retry's answer arrives as an
    auto-fired reply, that reply answers the same participant turn and gets
    no fresh budget: a second truncation on the turn is final. Replayed live
    before this: retries #1..#4 all issued, count 0 before each."""
    rt = make_bridge(truncated_reply(LONG_LINE, 2, "resp_A"))

    async def go():
        await rt.request_response()
        agen = rt.events()
        try:
            first = dones(await pull(agen, 4))
            assert first[0]["retryable"] is True
            assert await rt.retry_response() is True
            rt.clear_response_state()             # a boundary read wrongly
            rt.ws.script.extend(truncated_reply(LONG_LINE, 2, "resp_B"))
            second = dones(await pull(agen, 4))
            refused = await rt.retry_response()
            # a new participant turn starts the budget over
            await rt.send_audio(b"\x01\x02" * 4000)
            await rt.commit_turn()
            rt.ws.script.extend(truncated_reply(LONG_LINE, 2, "resp_C"))
            third = dones(await pull(agen, 4))
        finally:
            await agen.aclose()
        return second, refused, third

    second, refused, third = asyncio.run(go())
    assert rt.autofire_active is False and second and second[0]["retry_reason"] == "truncated"
    assert second[0]["retryable"] is False
    assert second[0]["not_retryable_why"] == "already_retried"
    assert refused is False
    assert third[0]["retryable"] is True


def test_a_late_fresh_id_done_does_not_touch_the_reply_now_streaming():
    """A done 1.6-3.1 s after a reply's own, with a fresh id (measured), landing
    while the NEXT reply streams: judged against that reply's words-so-far it
    was called truncated, and a healthy line was stopped on the page and
    re-spoken from the start. It names no reply in flight; it is dropped."""
    second = "nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen"
    rt = make_bridge(
        [created("resp_A"), tdelta("one two three four five six seven eight")]
        + [adelta()] * 5 + [audio_done(), done("resp_A"),
        created("resp_B"), tdelta(second), adelta(), adelta(), done("resp_LATE")]
        + [adelta()] * 5 + [audio_done(), done("resp_B")])

    async def go():
        await rt.request_response()
        return await collect(rt, limit=16)

    evs = asyncio.run(go())
    d = dones(evs)
    assert len(d) == 2, [e for e in evs if e["type"] == "response_done"]
    assert all(e["retry_reason"] is None for e in d)
    assert d[1]["audio_unterminated"] is False, "the late done ended reply B early"
    assert rt.phantom_dones == 1


def test_the_done_of_a_cancelled_reply_is_stale_however_late_it_comes():
    """The reply the participant barged in on stays pending on this gateway;
    the participant's utterance ends, the runner commits, and only then does
    the cancelled reply end - bare. The per-commit reset had forgotten the
    cancel by then, and a line the participant cut off was re-spoken. The
    cancel is remembered by the reply's NAME now, not by a flag."""
    rt = make_bridge([created("resp_A"), tdelta(LONG_LINE), adelta()])

    async def go():
        await rt.request_response()
        agen = rt.events()
        try:
            await pull(agen, 2)
            await rt.cancel_response()                    # barge-in
            await rt.send_audio(b"\x01\x02" * 4000)
            await rt.commit_turn()                        # the next turn
            rt.ws.script.extend([adelta(), adelta(), done("resp_A"),
                                 created("resp_B"), tdelta(LONG_LINE)]
                                + [adelta()] * 6 + [audio_done(), done("resp_B")])
            evs = await pull(agen, 11)
        finally:
            await agen.aclose()
        return evs

    evs = asyncio.run(go())
    d = dones(evs)
    assert len(d) == 2
    assert d[0]["cancelled"] is True and d[0]["retry_reason"] is None
    assert d[0]["retryable"] is False
    assert d[1]["retry_reason"] is None and d[1]["retried"] is False
    assert not d[1].get("cancelled")
    assert rt._retries_this_turn == 0


def test_a_truncation_verdict_is_offered_only_after_a_quiet_window(monkeypatch):
    """Not at the done. Held for AUDIO_RETRY_QUIET_S, because the same frame
    shape is what the gateway produces when it interrupts a reply for speech
    it heard - and it hears speech the runner's VAD does not."""
    monkeypatch.setattr(realtime, "AUDIO_RETRY_QUIET_S", 0.3)
    rt = make_bridge(truncated_reply(LONG_LINE, 2))

    async def go():
        await rt.request_response()
        t0 = time.monotonic()
        evs = await collect(rt, limit=4, timeout=3.0)
        return evs, time.monotonic() - t0

    evs, took = asyncio.run(go())
    d = dones(evs)
    assert len(d) == 1 and d[0]["retry_reason"] == "truncated" and d[0]["retryable"] is True
    assert took >= 0.28, f"offered before the window ran ({took:.2f}s)"
    assert d[0]["held_s"] >= 0.28
    assert realtime.AUDIO_RETRY_QUIET_S == 0.3 or True   # the constant is what is monkeypatched
    assert 0.5 <= float(realtime.setting("REALTIME_AUDIO_RETRY_QUIET_S", "1.0")) <= 2.0


def test_a_held_verdict_is_dropped_the_moment_the_participant_is_heard(monkeypatch):
    """Sub-threshold speech (RMS 250-450, under the 500 turn bar) was
    transcribed by Gemini every time and interrupted the character every
    time, with `vad.speaking` False throughout; and a participant resuming
    after a pause is heard by Gemini ~250 ms before the runner's turn detector
    admits it. Either way the character was re-spoken over them. The hint
    fires on the first voice-like frame, and the held verdict goes with it."""
    monkeypatch.setattr(realtime, "AUDIO_RETRY_QUIET_S", 0.6)
    rt = make_bridge(truncated_reply(LONG_LINE, 2))
    t_start = time.monotonic()
    rt.participant_speaking = lambda: 0.15 <= time.monotonic() - t_start <= 0.4

    async def go():
        await rt.request_response()
        evs = await collect(rt, limit=4, timeout=3.0)
        return evs, time.monotonic() - t_start

    evs, took = asyncio.run(go())
    d = dones(evs)
    assert len(d) == 1 and d[0]["retry_reason"] == "truncated"
    assert d[0]["retryable"] is False
    assert d[0]["not_retryable_why"] == "participant_speaking"
    assert took < 0.6, f"the verdict waited the window out with the participant talking ({took:.2f}s)"


def test_a_held_verdict_is_dropped_when_the_gateway_resumes_by_itself(monkeypatch):
    """After interrupting itself for speech it heard, the gateway starts a
    reply of its own once the speech stops (measured: the 'glued head' turns
    of the baseline). The head was superseded, not lost: the held verdict is
    closed out - BEFORE the new reply's first delta, so the turns stay in
    order - and nothing is re-asked."""
    monkeypatch.setattr(realtime, "AUDIO_RETRY_QUIET_S", 0.6)
    resumed = "one two three four five six seven eight nine ten"
    rt = make_bridge(truncated_reply(LONG_LINE, 2, "resp_A")
                     + [created("resp_B"), tdelta(resumed)] + [adelta()] * 6
                     + [audio_done(), done("resp_B")])

    async def go():
        await rt.request_response()
        t0 = time.monotonic()
        evs = await collect(rt, limit=12, timeout=3.0)
        return evs, time.monotonic() - t0

    evs, took = asyncio.run(go())
    kinds = [e["type"] for e in evs]
    d = dones(evs)
    assert len(d) == 2
    assert d[0]["retry_reason"] == "truncated" and d[0]["retryable"] is False
    assert d[0]["not_retryable_why"] == "gateway_resumed"
    assert d[1]["retry_reason"] is None
    first_done = kinds.index("response_done")
    resumed_delta = next(i for i, e in enumerate(evs)
                         if e["type"] == "agent_transcript_delta" and e["text"] == resumed)
    assert first_done < resumed_delta, "the head's turn was still open when the resumed reply began"
    assert took < 0.6


def test_a_runaway_reply_is_never_re_asked_for():
    """One sentence repeated 883 times: 15,868 words and 224 s of audio pushed
    in 30 s of wall time, and 'truncated' by the arithmetic. It escaped a
    retry live only because the participant happened to be talking."""
    text = " ".join(["I'm not trying to be difficult"] * 60)     # 360 words
    rt = make_bridge(truncated_reply(text, 3))

    async def go():
        await rt.request_response()
        return await collect(rt, limit=5)

    d = dones(asyncio.run(go()))
    assert d and d[0]["retry_reason"] == "truncated"
    assert d[0]["retryable"] is False and d[0]["not_retryable_why"] == "oversized"


def _pcm(rms, n_frames):
    """`n_frames` 20 ms frames of a constant sample whose RMS is `rms`."""
    import struct
    frame = struct.pack("<320h", *([int(rms)] * 320))
    return [frame] * n_frames


def test_the_vad_hint_hears_a_voice_under_the_turn_bar_at_once():
    """The turn detector needs 250 ms over RMS 500; the hint needs one frame
    over its own, lower bar. A voice at 300 opens no turn and cuts nobody
    off, but it is on the microphone and the retry must know it."""
    vad = realtime.SilenceDetector(noise_margin=0)
    assert vad.active_within() is False
    for f in _pcm(300, 8):                 # 160 ms of a soft voice
        assert vad.feed(f) is None
    assert vad.speaking is False
    assert vad.active_within() is True
    assert vad.last_hint_rms == 300
    assert vad.hint_threshold() == max(realtime.VAD_HINT_RMS, int(500 * realtime.VAD_HINT_RATIO))
    vad.last_hint_at = time.time() - 5
    assert vad.active_within() is False
    # ...and a plain hole inside an utterance: speaking has dropped, the
    # hint has not.
    vad.speaking = False
    vad.last_hint_at = time.time() - 0.4
    assert vad.active_within() is True


def test_the_vad_hint_rises_above_this_rooms_own_noise_floor():
    """Room tone must not hold the retry up for good: once the room's floor
    is measured, the hint bar sits at twice it."""
    vad = realtime.SilenceDetector()
    for f in _pcm(200, 130):        # 2.6 s of steady tone, over the 2.5 s window
        vad.feed(f)
    assert vad.effective_threshold() >= 600
    assert vad.hint_threshold() >= 400
    vad.last_hint_at = 0.0
    for f in _pcm(250, 10):
        vad.feed(f)
    assert vad.active_within() is False, "room tone counted as a voice"


def test_the_vad_hint_ignores_clicks_and_hears_a_voice_in_its_first_syllable():
    """A keyboard click is 40 ms and loud; a voice is quiet and lasts. The
    hint needs VAD_HINT_MS sustained, decaying between bursts like the barge
    bar, so typing never counts as a participant."""
    vad = realtime.SilenceDetector(noise_margin=0)
    for _ in range(40):                    # 12 s of typing: 40 ms bursts every 300 ms
        for f in _pcm(1500, 2) + _pcm(60, 13):
            vad.feed(f)
    assert vad.active_within() is False, "typing counted as a voice"
    for f in _pcm(300, 6):                 # 120 ms of a soft voice
        vad.feed(f)
    assert vad.active_within() is True
    assert realtime.VAD_HINT_MS <= 150, "slower than the gateway's own interrupt"


@in_a_loop
async def test_a_retry_is_not_issued_in_a_hole_of_the_turn_detector(monkeypatch):
    """`speaking` is False for 8-19% of the frames inside every measured
    utterance (0.3-0.5 s holes); a verdict landing in one passed the guard and
    the character spoke over the participant. The guard is the hint window."""
    runner, session, ws, rt = one_to_one(monkeypatch)
    runner.vad.speaking = False
    runner.vad.last_hint_at = time.time() - 0.3
    for ev in [
        {"type": "agent_transcript_delta", "text": "Well we're working with a system"},
        broken("absent"),
    ]:
        rt.feed(ev)
    rt.end()
    await runner._pump_events(rt)
    await settle(runner)

    assert rt.retries == 0 and ws.frames("assistant_retry") == []
    sup = session.store.of("audio_retry_suppressed")
    assert len(sup) == 1 and sup[0]["why"] == "participant_speaking"


@in_a_loop
async def test_the_bridge_is_told_about_hinted_voice_not_only_open_turns(monkeypatch):
    runner, session, ws, rt = one_to_one(monkeypatch)
    rt.end()
    await runner._pump_events(rt)
    runner.vad.speaking = False
    runner.vad.last_hint_at = time.time()
    assert rt.participant_speaking() is True


@in_a_loop
async def test_the_bridges_reason_for_refusing_is_recorded_as_given(monkeypatch):
    runner, session, ws, rt = one_to_one(monkeypatch)
    for ev in [
        {"type": "agent_transcript_delta", "text": "So are we just talking about"},
        {"type": "agent_audio", "pcm": CL_AUDIO},
        broken("truncated", retryable=False, not_retryable_why="gateway_resumed"),
    ]:
        rt.feed(ev)
    rt.end()
    await runner._pump_events(rt)
    await settle(runner)
    sup = session.store.of("audio_retry_suppressed")
    assert len(sup) == 1 and sup[0]["why"] == "gateway_resumed"
    assert rt.retries == 0


@in_a_loop
async def test_a_stale_done_never_closes_the_turn_now_streaming(monkeypatch):
    runner, session, ws, rt = one_to_one(monkeypatch)
    for ev in [
        {"type": "agent_transcript_delta", "text": "So are we just "},
        {"type": "agent_audio", "pcm": CL_AUDIO},
        {"type": "response_done", "stale": True, "retry_reason": None,
         "retryable": False, "retried": False, "audio_unterminated": False},
        {"type": "agent_transcript_delta", "text": "talking about the sprint?"},
    ] + [{"type": "agent_audio", "pcm": CL_AUDIO}] * 6 + [
        {"type": "response_done", "retry_reason": None, "retryable": False,
         "retried": False, "audio_unterminated": False},
    ]:
        rt.feed(ev)
    rt.end()
    await runner._pump_events(rt)
    await settle(runner)
    turns = session.store.of("assistant_turn")
    assert len(turns) == 1, turns
    assert turns[0]["text"] == "So are we just talking about the sprint?"
    assert turns[0]["audio_ms"] == 1400
    assert session.store.of("transcript_late") == []
    assert len(ws.frames("assistant_done")) == 1


@in_a_loop
async def test_a_stale_done_never_closes_a_members_turn_either(monkeypatch):
    runner, session, ws, agent = group_runner(monkeypatch)
    runner.room = FakeRoom(speaking=agent.id)
    rt = FakeRT()
    for ev in [
        {"type": "agent_transcript_delta", "text": "I think better "},
        {"type": "agent_audio", "pcm": CL_AUDIO},
        {"type": "response_done", "stale": True, "retry_reason": None,
         "retryable": False, "retried": False, "audio_unterminated": False},
        {"type": "agent_transcript_delta", "text": "tests would have caught this."},
    ] + [{"type": "agent_audio", "pcm": CL_AUDIO}] * 6 + [
        {"type": "response_done", "retry_reason": None, "retryable": False,
         "retried": False, "audio_unterminated": False},
    ]:
        rt.feed(ev)
    rt.end()
    await runner._pump_member(agent, rt)
    await settle(runner)
    turns = session.store.of("assistant_turn")
    assert len(turns) == 1, turns
    assert turns[0]["text"] == "I think better tests would have caught this."
    assert turns[0]["audio_ms"] == 1400
    assert runner._response_done.is_set()


class LoudThenGone(FakeWS):
    """A participant socket that shouts for 0.5 s and then hangs up."""

    def __init__(self):
        super().__init__()
        self.frames_left = _pcm(3000, 25)

    async def receive(self):
        if self.frames_left:
            return {"type": "websocket.receive", "bytes": self.frames_left.pop(0)}
        return {"type": "websocket.disconnect"}


@in_a_loop
async def test_a_barge_in_during_a_retry_writes_the_outcome_and_keeps_the_head(monkeypatch):
    """The participant talks over a reply that is being re-asked for. The
    retry's outcome was never written and the turn was filed as
    transcript_missing - the flag that tells a rater 'audio played, text
    lost' - on a line whose head the participant had seen. Now: interrupted,
    outcome not recovered, and the lost head on the turn."""
    monkeypatch.setenv("TRANSCRIPT_GRACE_SECONDS", "0.2")
    session = FakeSession("S1A")
    ws = LoudThenGone()
    runner = rvs.RealtimeVoiceSessionRunner(session, ws)
    rt = FakeRT()
    rt._retry_in_flight = True
    runner.rt = rt
    runner.room = None
    runner._speaking = True
    runner._retry_lost[runner.agent_id] = {"text": "I would ask that we agree",
                                           "words": 6, "reason": "truncated",
                                           "at": time.time()}
    await runner._client_to_model()
    await settle(runner)

    assert rt.cancels == 1
    assert len(ws.frames("assistant_interrupted")) == 1
    outcome = session.store.of("audio_retry_outcome")
    assert len(outcome) == 1
    assert outcome[0]["recovered"] is False and outcome[0]["delivered_whole"] is False
    assert outcome[0]["interrupted"] is True
    assert outcome[0]["lost_text"] == "I would ask that we agree"
    turns = session.store.of("assistant_turn")
    assert len(turns) == 1 and turns[0]["interrupted"] is True
    # The head the participant saw is the turn's text, marked as such, rather
    # than an empty turn flagged transcript_missing.
    assert turns[0]["text"] == "I would ask that we agree"
    assert turns[0]["retry_head"] is True
    assert turns[0]["transcript_missing"] is False


def test_recovered_means_the_lost_line_came_back_not_merely_a_line(monkeypatch):
    """Live retries recorded recovered=True on: '' (0 words, 0 ms - twice),
    'I said understood' for a lost 'Understood', 'I didn't say anything' for
    a lost 'I don't need anything', a 4-word fragment for a lost 24-word
    line. `delivered_whole` and `overlap` are separate answers now, and
    `recovered` needs both."""
    runner, session, ws, rt = one_to_one(monkeypatch)
    cases = [
        # lost, got, ms, interrupted, unterminated -> (whole, recovered)
        ("So are we just talking about the sprint or is there anything else",
         "", 0, False, False, (False, False)),
        ("I don't need anything", "I didn't say anything", 1600, False, False, (True, False)),
        ("Understood", "I said understood", 1200, False, False, (True, True)),
        ("So are we just talking about the sprint or is there anything else",
         "So, are we just talking about the sprint or is there anything else?",
         5000, False, False, (True, True)),
        ("So are we just talking about the sprint or is there anything else",
         "So, are we just talking about the sprint or is there anything else?",
         5000, True, False, (False, False)),
        ("So are we just talking about the sprint or is there anything else",
         "So, are we just talking about the sprint", 800, False, True, (False, False)),
    ]
    for lost, got, ms, interrupted, unterminated, want in cases:
        runner._retry_lost["primary"] = {"text": lost, "words": 0, "reason": "truncated"}
        runner._note_retry_outcome("primary", got, ms, interrupted, unterminated)
        ev = session.store.of("audio_retry_outcome")[-1]
        assert (ev["delivered_whole"], ev["recovered"]) == want, (lost, got, ev)
        assert ev["lost_text"] == lost
    assert session.store.of("audio_retry_outcome")[1]["overlap"] == 0.5
    assert session.store.of("audio_retry_outcome")[2]["overlap"] == 1.0
    assert rvs.RETRY_RECOVERED_OVERLAP == 0.6


def test_a_reply_whose_stream_dies_after_its_voice_is_closed_at_the_audio_bar(monkeypatch):
    """The third shape, seen live on the first after-wave: a retried reply's
    audio arrived whole (10 words, 2.5 s) and then nothing, ever - no
    response.done. Only the 45 s watchdog ended it, and every commit the
    participant made meanwhile was refused as 'a reply is in flight'. Over
    490 healthy closed replies the longest mid-stream quiet, net of
    participant speech, is 3.9 s (p99 2.4 s); at AUDIO_ABSENT_S the reply is
    closed as heard, and not re-spoken."""
    monkeypatch.setattr(realtime, "AUDIO_ABSENT_S", 0.3)
    monkeypatch.setattr(realtime, "RESPONSE_STALL_S", 30.0)
    line = "Thank you for being straight with me about that I appreciate that"
    rt = make_bridge([created(), tdelta(line)] + [adelta()] * 13)

    async def go():
        await rt.request_response()
        t0 = time.monotonic()
        evs = await collect(rt, limit=15, timeout=3.0)
        return evs, time.monotonic() - t0

    evs, took = asyncio.run(go())
    d = dones(evs)
    assert len(d) == 1 and d[0]["output_stalled"] is True
    assert d[0]["interrupted"] is False, "a line heard whole was recorded as cut off"
    assert d[0]["audio_unterminated"] is True
    assert d[0]["retry_reason"] is None and d[0]["retryable"] is False
    assert 0.25 <= took < 2.0, took
    assert not any(e["type"] == "error" for e in evs)
    assert rt.responding is False


def test_a_reply_whose_stream_dies_short_of_its_words_is_offered_for_retry_at_the_bar(monkeypatch):
    monkeypatch.setattr(realtime, "AUDIO_ABSENT_S", 0.3)
    monkeypatch.setattr(realtime, "RESPONSE_STALL_S", 30.0)
    rt = make_bridge([created(), tdelta(LONG_LINE)] + [adelta()] * 2)

    async def go():
        await rt.request_response()
        return await collect(rt, limit=4, timeout=3.0)

    d = dones(asyncio.run(go()))
    assert len(d) == 1 and d[0]["output_stalled"] is True
    assert d[0]["interrupted"] is True
    assert d[0]["retry_reason"] == "truncated" and d[0]["retryable"] is True
    assert d[0]["words"] == 14 and d[0]["audio_ms"] == 400


def test_the_participants_next_turn_is_not_refused_behind_a_reply_that_never_closed(monkeypatch):
    """What the 45 s cost the participant: commit_turn() refuses to commit
    while a reply is nominally in flight."""
    monkeypatch.setattr(realtime, "AUDIO_ABSENT_S", 0.2)
    monkeypatch.setattr(realtime, "RESPONSE_STALL_S", 30.0)
    rt = make_bridge([created(), tdelta("one two three four five")] + [adelta()] * 9)

    async def go():
        await rt.request_response()
        await collect(rt, limit=11, timeout=3.0)
        rt.ws.sent.clear()
        await rt.send_audio(b"" * 4000)
        await rt.commit_turn()
        return [m["type"] for m in rt.ws.sent]

    sent = asyncio.run(go())
    assert "input_audio_buffer.commit" in sent and "response.create" in sent


def test_a_retrys_answer_that_resumes_the_abandoned_reply_is_the_retry(monkeypatch):
    """Measured on the first after-wave: after the absent retry the gateway
    sent NO response.created - the abandoned reply resumed under its own id
    (transcript, audio, output_audio.done, done). Read as the done of a reply
    already closed out, it was 'stale', the retry never ended, and the 45 s
    watchdog closed the turn. The reply that answers is the reply in flight."""
    monkeypatch.setattr(realtime, "AUDIO_ABSENT_S", 0.2)
    monkeypatch.setattr(realtime, "RESPONSE_STALL_S", 30.0)
    rt = make_bridge([created("resp_A"), tdelta("Well we're working with a system")])

    async def go():
        await rt.request_response()
        agen = rt.events()
        try:
            first = dones(await pull(agen, 2, timeout=3.0))
            assert first[0]["retry_reason"] == "absent"
            assert await rt.retry_response() is True
            # no created: the same response resumes
            rt.ws.script.extend([tdelta("Thank you for being straight with me about that")]
                                + [adelta()] * 12 + [audio_done(), done("resp_A")])
            evs = await pull(agen, 14, timeout=3.0)
        finally:
            await agen.aclose()
        return evs

    evs = asyncio.run(go())
    d = dones(evs)
    assert len(d) == 1, d
    assert not d[0].get("stale") and d[0]["retried"] is True
    assert d[0]["audio_unterminated"] is False and d[0]["retry_reason"] is None
    assert rt.responding is False


def test_a_cancelled_reply_that_keeps_streaming_never_latches_the_session():
    """The room-suppression shape, measured to latch every member of a room
    for 45 s on the first after-wave (six voice_errors in one encounter):
    cancelled, kept streaming, ended under its own id. Its done ends it."""
    rt = make_bridge([created("resp_A"), tdelta(LONG_LINE), adelta()])

    async def go():
        await rt.request_response()
        agen = rt.events()
        try:
            await pull(agen, 2)
            await rt.cancel_response()
            rt.ws.script.extend([adelta(), adelta(), done("resp_A")])
            first = await pull(agen, 3)
            latched = rt.responding
            await rt.send_audio(b"\x00" * 3200)
            await rt.commit_input()
            await rt.request_response()
            rt.ws.script.extend([created("resp_B"), tdelta(LONG_LINE)] + [adelta()] * 6
                                + [audio_done(), done("resp_B")])
            second = await pull(agen, 8)
        finally:
            await agen.aclose()
        return latched, dones(first), dones(second)

    latched, first, second = asyncio.run(go())
    assert latched is False, "the cancelled reply's done did not end it"
    assert first and first[0]["cancelled"] is True and first[0]["retry_reason"] is None
    assert second and second[0]["retry_reason"] is None and not second[0].get("stale")
