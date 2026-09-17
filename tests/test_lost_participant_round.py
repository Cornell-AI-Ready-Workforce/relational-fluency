"""The second lost-participant round, held to what the live model did.

Every earlier round measured the characters through the TEXT model; this one
drove nto.gemini-live-2.5-flash through api.ai.it.cornell.edu with the
researcher's own hesitant lines and the page's own audio path, and found:

  * THE DEAD SOCKET. In every 1:1 encounter longer than ~90 s the gateway went
    silent for good — no transcript, no reply, no frame of any kind — and
    dropped the TCP connection 22-38 s later with no close frame. Everything
    the participant said into it was lost, and the page then told them to say
    it again. The bridge now calls a request unanswered at 6 s (every healthy
    reply's first frame was within 2.6 s), the runner keeps the participant's
    audio since the gateway last heard them, re-asks with THAT rather than a
    text nudge, and when the retry goes unanswered too it rebuilds the session
    at once and replays the line into it.
  * THE SECOND OPENING. A text nudge for a dropped "Good morning." drew "You
    booked this meeting. What's on your mind." — the character answered the
    nudge, not the participant.
  * THE STAGE NOTE READ ALOUD. Drew's first line was "Next morning you and
    Drew reach the coffee machine together, speak, or not." — the interaction's
    `opening:` is a third-person description, and quoted in guillemets it was
    taken for a line.
  * THE RETRY STUB. "I sent it because it needs to be said. It wasn't getting
    fixed." re-asked for came back as "It's been an issue twice." (5 words for
    13): the nudge said "say it again" and did not say what.
  * THE HALF-SECOND OF BEX. A room member granted the floor mid-reply had
    already had 12.5 of 13 s of its audio thrown away by its own pump; the
    participant heard 0.5 s under a 29-word caption.
  * THE DOUBLED CAPTION. The gateway streamed one reply's transcript twice
    inside one response id; the record kept it once, the page showed it twice.
  * THE DEMAND LOOP. Drew asked "what are you going to do about it" four turns
    running, reworded each time, against content-free answers; Mel three; Alex
    three. The briefs said not to; they did not say what to do instead.

No network and no credentials here: the live evidence is quoted, not re-run.
"""

from __future__ import annotations

import asyncio
import functools
import json
import re
import struct
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server import realtime_voice_session as rvs  # noqa: E402
from server.scenarios import load_scenario  # noqa: E402
from server.voice import realtime as rt_mod  # noqa: E402

GEMINI = "nto.gemini-live-2.5-flash"


# --------------------------------------------------------------------------
# Fakes, in the shapes the runner uses.
# --------------------------------------------------------------------------

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


class FakeRT:
    """A bridge whose events() the test feeds; `_closing` False at the end
    models the GATEWAY closing the socket."""

    built: list = []

    def __init__(self, instructions="", voice="Puck", tools=None):
        self.ws = object()
        self.voice = voice
        self.model = rt_mod.MODEL
        self.instructions = instructions
        self.tools = tools
        self.autofire_active = False
        self.pending_input = 0
        self.debug_log = []
        self.closed = False
        self._closing = False
        self._responding = False
        self._response_saw_output = False
        self._retry_in_flight = False
        self.retry_nudges = []
        self.replays = []
        self.prompts = []
        self.participant_speaking = None
        self._q: asyncio.Queue = asyncio.Queue()
        FakeRT.built.append(self)

    @property
    def responding(self):
        return self._responding

    def clear_response_state(self):
        self._responding = False

    async def connect(self, *, open_conversation=True):
        return None

    async def close(self):
        self.closed = True
        self._closing = True
        self.ws = None
        self._q.put_nowait(None)

    async def update_instructions(self, instructions):
        self.instructions = instructions
        return False

    async def send_audio(self, pcm):
        self.pending_input += len(pcm)

    async def commit_input(self):
        self.pending_input = 0

    async def commit_turn(self):
        self.pending_input = 0

    async def request_response(self):
        self._responding = True

    async def cancel_response(self):
        self._responding = False

    async def retry_response(self, nudge=None):
        self.retry_nudges.append(nudge)
        return True

    async def replay_input(self, pcm):
        self.replays.append(pcm)
        return True

    async def prompt_response(self, text):
        self.prompts.append(text)

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


class SilentWire:
    """A gateway socket that records what is sent and never answers."""

    def __init__(self, frames=None):
        self.sent = []
        self.frames = list(frames or [])

    async def send(self, raw):
        self.sent.append(json.loads(raw))

    async def recv(self):
        if self.frames:
            return self.frames.pop(0)
        await asyncio.sleep(3600)

    async def close(self):
        return None

    def types(self):
        return [f["type"] for f in self.sent]


def make_runner(scenario_id):
    session = FakeSession(scenario_id)
    ws = FakeWS()
    return rvs.RealtimeVoiceSessionRunner(session, ws), session, ws


def in_a_loop(fn):
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        return asyncio.run(fn(*a, **kw))
    return wrapper


@pytest.fixture(autouse=True)
def _short_waits(monkeypatch):
    monkeypatch.setenv("TRANSCRIPT_GRACE_SECONDS", "0.2")
    monkeypatch.setenv("AUTOFIRE_WAIT", "0.05")
    monkeypatch.setattr(rt_mod, "MODEL", GEMINI)
    FakeRT.built = []


def pcm(ms: int, rms: int) -> bytes:
    n = 16000 * ms // 1000
    if rms == 0:
        return b"\x00\x00" * n
    return struct.pack("<%dh" % n, *([rms, -rms] * (n // 2) + [rms] * (n % 2)))


# --------------------------------------------------------------------------
# The bridge
# --------------------------------------------------------------------------

def test_a_request_is_called_unanswered_at_six_seconds():
    """Every healthy reply's first frame followed the request within 2.6 s
    across eleven live encounters; the sockets that never answered were dead
    for good. Ten seconds of waiting on one is dead air charged to the
    participant."""
    assert rt_mod.REQUEST_UNANSWERED_S <= 6.0
    assert rt_mod.REQUEST_UNANSWERED_S > 2.6 * 2, "still more than twice the slowest healthy reply"


def test_the_bridge_replays_the_participants_line_on_the_turns_one_retry():
    """replay_input: the line goes back as audio, in the pump's own 100 ms
    pieces, committed and asked for; it spends the turn's one retry and does
    not reset the budget the way a new participant turn would."""
    rt = rt_mod.RealtimeVoiceSession(instructions="x", voice="Puck", model=GEMINI, api_key="x")
    wire = SilentWire()
    rt.ws = wire
    speech = pcm(450, 2000)

    async def scenario():
        assert await rt.replay_input(speech) is True
        first = list(wire.types())
        assert await rt.replay_input(speech) is False, "one retry per turn"
        return first

    types = asyncio.run(scenario())
    appends = [t for t in types if t == "input_audio_buffer.append"]
    assert len(appends) == 5, types                       # 450 ms in 100 ms pieces
    assert types[0] == "response.cancel"
    assert types[-2:] == ["input_audio_buffer.commit", "response.create"]
    assert rt._retries_this_turn == 1 and rt._retry_in_flight and rt._requested
    assert rt._replay_in_flight, "the replay's own, shorter unanswered bar applies"
    assert rt._response_active and not rt._response_saw_output
    assert asyncio.run(rt.replay_input(b"")) is False, "nothing to replay is not a retry"


def test_a_transcript_the_gateway_streams_twice_reaches_the_runner_once(monkeypatch):
    """Measured: inside one response id the gateway sent the whole transcript,
    then sent it again from the first word. The record kept it once (the .done
    frame is authoritative); the page, built from deltas, showed it twice."""
    monkeypatch.setattr(rt_mod, "RECV_POLL_S", 0.05)
    line = "I stopped keeping the workarounds up. There's nothing to discuss. Does any of what I built survive the switch?"
    frames = [
        json.dumps({"type": "response.created", "response": {"id": "resp_1"}}),
        json.dumps({"type": "response.output_audio_transcript.delta", "response_id": "resp_1", "delta": "I stopped keeping the"}),
        json.dumps({"type": "response.output_audio_transcript.delta", "response_id": "resp_1", "delta": " workarounds up. There's nothing to discuss. D"}),
        json.dumps({"type": "response.output_audio_transcript.delta", "response_id": "resp_1", "delta": "oes any of what I built survive the switch?"}),
        # the second pass
        json.dumps({"type": "response.output_audio_transcript.delta", "response_id": "resp_1", "delta": "I stopped keeping the workarounds"}),
        json.dumps({"type": "response.output_audio_transcript.delta", "response_id": "resp_1", "delta": " up. There's nothing to discuss. Does any of w"}),
        json.dumps({"type": "response.output_audio_transcript.delta", "response_id": "resp_1", "delta": "hat I built survive the switch?"}),
        json.dumps({"type": "response.output_audio_transcript.done", "response_id": "resp_1", "transcript": line}),
    ]
    rt = rt_mod.RealtimeVoiceSession(instructions="x", voice="Puck", model=GEMINI, api_key="x")
    rt.ws = SilentWire(frames)

    async def scenario():
        deltas, kinds, whole = [], [], None
        async for ev in rt.events():
            kinds.append(ev["type"])
            if ev["type"] == "agent_transcript_delta":
                deltas.append(ev["text"])
            if ev["type"] == "agent_transcript":
                whole = ev["text"]
                return deltas, kinds, whole

    deltas, kinds, whole = asyncio.run(asyncio.wait_for(scenario(), timeout=5))
    assert "".join(deltas) == line, "the deltas the runner relays add up to the line once"
    assert whole == line
    assert kinds.count("transcript_restreamed") == 1
    assert rt.transcript_restreams == 1


def test_a_retry_the_gateway_ignored_is_reported_as_such(monkeypatch):
    """The error the bridge yields when a turn's retry drew nothing carries a
    flag the runner can act on: that socket is dead."""
    monkeypatch.setattr(rt_mod, "RECV_POLL_S", 0.05)
    monkeypatch.setattr(rt_mod, "AUDIO_ABSENT_S", 0.1)
    rt = rt_mod.RealtimeVoiceSession(instructions="x", voice="Puck", model=GEMINI, api_key="x")
    rt.ws = SilentWire()
    rt._response_active = True
    rt._retry_in_flight = True
    rt._response_saw_output = False
    rt._response_started_at = time.time() - 5

    async def scenario():
        out = []
        async for ev in rt.events():
            out.append(ev)
            if ev["type"] == "error":
                return out

    out = asyncio.run(asyncio.wait_for(scenario(), timeout=5))
    err = out[-1]
    assert err.get("recoverable") and err.get("retry_unanswered") is True
    assert "did not answer the retry" in err["message"]


# --------------------------------------------------------------------------
# The runner: keeping, compacting and replaying the participant's line
# --------------------------------------------------------------------------

def test_the_runner_keeps_the_participants_audio_and_compacts_it_for_replay():
    runner, session, ws = make_runner("S2A")
    assert runner._replay_speech() == b"", "nothing kept, nothing to replay"
    for chunk in (pcm(2000, 0), pcm(1000, 3000), pcm(2000, 0), pcm(500, 3000), pcm(3000, 0)):
        runner._keep_for_replay(chunk)
    kept_s = len(runner._replay_pcm) / 32000
    assert 8.4 <= kept_s <= 8.6
    out = runner._replay_speech()
    out_s = len(out) / 32000
    # 1.5 s of speech, the inner pause and the tail each cut to REPLAY_PAUSE_MS,
    # the 2 s of leading quiet gone.
    assert 1.5 <= out_s <= 1.5 + 2 * rvs.REPLAY_PAUSE_MS / 1000 + 0.05, out_s
    assert out[:2] != b"\x00\x00", "leading quiet is not replayed"
    runner._replay_pcm.clear()
    runner._keep_for_replay(pcm(4000, 0))
    assert runner._replay_speech() == b"", "room tone alone is never a turn"
    runner._replay_pcm.clear()
    runner._keep_for_replay(pcm(100, 3000))
    assert runner._replay_speech() == b"", "under REPLAY_MIN_SPEECH_MS is a click, not a line"
    # bounded
    for _ in range(60):
        runner._keep_for_replay(pcm(1000, 3000))
    assert len(runner._replay_pcm) <= int(rvs.REPLAY_KEEP_S * 16000) * 2


@in_a_loop
async def test_a_request_the_gateway_ignored_is_re_asked_with_the_participants_own_line():
    """Live: the text nudge for a dropped "Good morning." drew a SECOND scene
    opening. The retry now puts the participant's own audio back first; the
    nudge is what is left for a request with no speech behind it."""
    runner, session, ws = make_runner("S2A")
    rt = FakeRT()
    runner.rt = rt
    runner._keep_for_replay(pcm(700, 3000))
    asked = await runner._reply_missing(rt, "morgan", {"waited_s": 6, "retryable": True})
    assert asked is True
    assert len(rt.replays) == 1 and rt.retry_nudges == []
    (retry,) = session.store.of("reply_retry")
    assert retry["how"] == "replay" and retry["asked"] is True and retry["nudge"] is None
    assert retry["replay_ms"] >= 700
    # and with nothing said since the gateway last heard them: the text nudge
    runner._replay_pcm.clear()
    rt2 = FakeRT()
    assert await runner._reply_missing(rt2, "morgan", {"waited_s": 6, "retryable": True}) is True
    assert rt2.replays == [] and rt2.retry_nudges == [rt_mod.UNANSWERED_NUDGE]
    assert session.store.of("reply_retry")[-1]["how"] == "nudge"


@in_a_loop
async def test_a_heard_line_is_not_kept_for_replay():
    """A transcript from the gateway means it heard the participant: the
    buffer is spent, so a rebuilt session is never handed a line the old one
    already answered."""
    runner, session, ws = make_runner("S2A")
    rt = FakeRT()
    runner.rt = rt
    runner._keep_for_replay(pcm(700, 3000))
    rt.feed({"type": "user_transcript", "text": "Hello.", "garbled": False})
    rt.end()
    await runner._pump_events(rt)
    assert runner._replay_pcm == bytearray()
    assert session.store.of("user_turn")[0]["text"] == "Hello."


def test_a_socket_that_ignored_the_retry_is_rebuilt_at_once_and_the_line_replayed(monkeypatch):
    """The dead socket, end to end: the bridge reports the retry unanswered,
    the runner closes the socket itself (rather than waiting 22-38 s for the
    gateway to), _model_to_client rebuilds the session as the same character
    despite the close being ours, and the participant's unanswered line goes
    into the new session before anything else. The page is told the line was
    heard, not to say it again."""
    monkeypatch.setattr(rt_mod, "RECONNECT_LIMIT", 2)
    monkeypatch.setattr(rvs, "RealtimeVoiceSession", FakeRT)
    runner, session, ws = make_runner("S2A")
    first = FakeRT()
    runner.rt = first
    runner._keep_for_replay(pcm(900, 3000))

    async def scenario():
        relay = asyncio.ensure_future(runner._model_to_client())
        first.feed({"type": "error", "recoverable": True, "retry_unanswered": True,
                    "message": "the gateway did not answer the retry for 8s; the turn was abandoned"})
        for _ in range(100):
            await asyncio.sleep(0.02)
            if len(FakeRT.built) >= 2 and runner.rt is FakeRT.built[1]:
                break
        second = FakeRT.built[1]
        assert first.closed, "the runner closed the dead socket itself"
        assert runner.rt is second
        assert len(second.replays) == 1, "the unanswered line went into the new session"
        assert len(second.replays[0]) >= 900 * 32
        assert runner._replay_pcm == bytearray(), "spent"
        second.end()          # a real gateway close, nothing said since
        for _ in range(100):
            await asyncio.sleep(0.02)
            if len(FakeRT.built) >= 3 and runner.rt is FakeRT.built[2]:
                break
        third = FakeRT.built[2]
        assert third.replays == [], "nothing said since the gateway last heard them"
        third.end()
        await asyncio.wait_for(relay, timeout=5)

    asyncio.run(scenario())
    assert session.store.of("gateway_socket_abandoned"), "written down"
    rec = session.store.of("realtime_session_reconnected")
    assert [r["attempt"] for r in rec] == [1, 2]
    assert rec[0]["for_replay"] is True and rec[0]["replayed_ms"] >= 900
    assert rec[1]["for_replay"] is False and rec[1]["replayed_ms"] == 0
    assert session.store.of("participant_turn_replayed")[0]["replay_ms"] >= 900
    notices = ws.frames("voice_notice")
    kinds = [(n.get("kind"), n.get("replayed")) for n in notices if n.get("kind") == "reconnected"]
    assert kinds == [("reconnected", True), ("reconnected", False)]


def test_a_socket_the_runner_closed_for_a_character_switch_is_still_not_rebuilt(monkeypatch):
    monkeypatch.setattr(rvs, "RealtimeVoiceSession", FakeRT)
    runner, session, ws = make_runner("S2A")
    first = FakeRT()
    runner.rt = first

    async def scenario():
        relay = asyncio.ensure_future(runner._model_to_client())
        await asyncio.sleep(0.05)      # the relay is pumping rt
        await first.close()      # ours, deliberately
        await asyncio.wait_for(relay, timeout=5)

    asyncio.run(scenario())
    assert len(FakeRT.built) == 1 and not session.store.of("realtime_session_reconnected")


# --------------------------------------------------------------------------
# The framing note and the retry nudge
# --------------------------------------------------------------------------

def test_a_stage_note_opening_is_never_offered_as_a_line():
    """S1's second interactions describe the character in the third person;
    S2's openings ARE the character's line. Only the second kind is quoted."""
    runner, session, ws = make_runner("S1B")
    stage = "Next morning you and Drew reach the coffee machine together, speak, or not."
    assert runner._opening_is_stage_note(stage, "Drew") is True
    note = runner._first_reply_note(stage, "Drew")
    assert "«" not in note and "»" not in note
    assert "stage note" in note and "never read it out" in note
    assert "your own opening move" in note
    line = "Saw Drew's message last night, copying Priya and Tom? Out of line."
    assert runner._opening_is_stage_note(line, "Mel") is False, "Mel's line names Drew, not Mel"
    assert "«" + line + "»" in runner._first_reply_note(line, "Mel")
    imani = "The pack went out this morning and it goes out again Monday. That's where I'm starting from. Go on."
    assert runner._opening_is_stage_note(imani, "Morgan") is False
    assert "«" + imani + "»" in runner._first_reply_note(imani, "Morgan")
    # and the bank's other stage notes
    assert runner._opening_is_stage_note("You run into Sam by the elevators, an opening to say something, or not.", "Sam")
    assert runner._opening_is_stage_note("Wes comes past your desk about Friday, an opening to say something, or not.", "Wes")


def test_the_folded_opening_for_drew_carries_no_quoted_line():
    runner, session, ws = make_runner("S1B")
    drew = next(a for a in runner.cast if a.id == "drew")
    runner.segment = 1
    runner.agent, runner.agent_id = drew, drew.id
    runner._fold_opening(drew, group=False)
    assert runner._opening_agent == "drew"
    assert "«" not in runner._opening_note
    assert "coffee machine" in runner._opening_note
    assert "FIRST REPLY" in runner._instructions()


def test_the_audio_retry_names_the_line_it_stands_in_for():
    lost = "I sent it because it needs to be said. It wasn't getting fixed."
    nudge = rvs.RealtimeVoiceSessionRunner._retry_nudge_for(lost)
    assert nudge.startswith(rt_mod.AUDIO_RETRY_NUDGE) and lost in nudge
    assert rvs.RealtimeVoiceSessionRunner._retry_nudge_for("") == rt_mod.AUDIO_RETRY_NUDGE
    assert rvs.RealtimeVoiceSessionRunner._retry_nudge_for("word " * 80) == rt_mod.AUDIO_RETRY_NUDGE


@in_a_loop
async def test_the_retry_the_runner_issues_quotes_the_lost_line():
    runner, session, ws = make_runner("S2A")
    rt = FakeRT()
    runner.rt = rt
    ev = {"type": "response_done", "retry_reason": "absent", "retryable": True, "words": 13,
          "audio_ms": 0, "text": "I sent it because it needs to be said. It wasn't getting fixed."}
    assert await runner._retry_reply(rt, "morgan", ev) is True
    assert rt.retry_nudges and "I sent it because it needs to be said" in rt.retry_nudges[0]
    (retry,) = session.store.of("audio_retry")
    assert retry["reason"] == "absent"


# --------------------------------------------------------------------------
# The page and the room
# --------------------------------------------------------------------------

@in_a_loop
async def test_the_gateways_whole_line_reaches_the_page_as_the_caption():
    runner, session, ws = make_runner("S2A")
    rt = FakeRT()
    runner.rt = rt
    for ev in [
        {"type": "agent_audio", "pcm": b"\x01\x02" * 1600},
        {"type": "agent_transcript_delta", "text": "The pack", "first": True},
        {"type": "agent_transcript_delta", "text": " goes out Monday."},
        {"type": "agent_transcript", "text": "The pack goes out Monday."},
    ]:
        rt.feed(ev)
    rt.end()
    await runner._pump_events(rt)
    (final,) = ws.frames("assistant_text_final")
    assert final["text"] == "The pack goes out Monday." and final["agent_id"] == "morgan"


def test_the_page_takes_the_final_line_and_says_the_replayed_line_was_heard():
    page = (ROOT / "static" / "v2.html").read_text(encoding="utf-8")
    assert "m.type === 'assistant_text_final'" in page
    assert "currentTurn.fullText = m.text" in page
    assert "m.replayed" in page
    assert "They heard what you said just now" in page
    assert "say it again" in page, "the old note stands when nothing was replayed"


class FloorRoom:
    """A room whose floor the test moves between events."""

    def __init__(self, speaking=None):
        self.speaking = speaking
        self.heard = []

    async def hear(self, pcm, exclude=None):
        self.heard.append((exclude, len(pcm)))

    def session_for(self, agent_id):
        return None


class FloorRT(FakeRT):
    """Feeds events, and grants the floor to `grant_to` on `room` before the
    event at index `grant_at` is yielded."""

    def __init__(self, room, grant_to, grant_at):
        super().__init__()
        self.room, self.grant_to, self.grant_at = room, grant_to, grant_at

    async def events(self):
        n = 0
        while True:
            ev = await self._q.get()
            if ev is None:
                return
            if n == self.grant_at:
                self.room.speaking = self.grant_to
            n += 1
            yield ev


@in_a_loop
async def test_a_reply_that_ends_while_still_suppressed_leaves_no_held_audio_behind(monkeypatch):
    session = FakeSession("S3A")
    ws = FakeWS()
    runner = rvs.RealtimeVoiceSessionRunner(session, ws)
    alex = next(a for a in runner._resolve_agents() if a.id == "alex")
    room = FloorRoom(speaking="jordan")
    runner.room = room
    chunk = b"\x01\x02" * 3200
    rt = FloorRT(room, "alex", grant_at=99)
    for ev in [
        {"type": "agent_transcript_delta", "text": "Unsolicited", "first": True},
        {"type": "agent_audio", "pcm": chunk},
        {"type": "agent_audio", "pcm": chunk},
        {"type": "response_done", "audio_unterminated": False, "retried": False},
    ]:
        rt.feed(ev)
    rt.end()
    await runner._pump_member(alex, rt)
    assert ws.binary == [] and not session.store.of("assistant_turn")
    assert not session.store.of("held_audio_relayed")
    assert runner._member_turns.get("alex") is None


def test_room_members_are_told_a_colleagues_voice_is_not_the_participant():
    runner, session, ws = make_runner("S3A")
    text = runner._instructions()
    assert "never ask anyone to clarify" in text and "audible to you" in text
    one, _, _ = make_runner("S2A")
    assert "never ask anyone to clarify" not in one._instructions()


# --------------------------------------------------------------------------
# The briefs: the demand loop
# --------------------------------------------------------------------------

def _brief(scenario_id, agent_id):
    return next(a for a in load_scenario(scenario_id, "p").cast if a.id == agent_id).system_prompt


@pytest.mark.parametrize("sid,aid", [
    ("S1A", "riley"), ("S1A", "sam"), ("S1B", "mel"), ("S1B", "drew"),
    ("S3A", "alex"), ("S3B", "toni"), ("S3A", "alex"),
])
def test_the_brief_says_what_to_do_after_two_non_answers(sid, aid):
    """Live, against "what do you mean" / "yeah, I'll work on it", Drew put the
    same demand four turns running (reworded), Mel three, Alex three. The
    briefs forbade repeating; they did not say what replaces the third ask,
    and on the live model "don't" without "instead" is not a move. The rule
    is on both forms of each pair, so the pair stays interchangeable."""
    text = _brief(sid, aid)
    assert "Two turns of nothing from them" in text
    assert re.search(r"third\s+time", text)
    quoted = re.findall(r'"[^"]*"', text.split("Two turns of nothing from them", 1)[1][:600])
    assert not quoted, f"{sid}/{aid}: the loop-breaker hands the actor no line to recite: {quoted}"


@in_a_loop
async def test_a_second_reply_to_one_turn_gets_its_own_turn_in_a_room(monkeypatch):
    """Measured: the gateway answered "Jordan, what do you think?" twice, 1.5 s
    apart, and the second reply's opening chunk was appended to the first
    reply's buffer as late transcript — one bubble with the line twice, then
    an empty bubble under the second reply's audio."""
    monkeypatch.setenv("TRANSCRIPT_GRACE_SECONDS", "0.2")
    session = FakeSession("S3A")
    ws = FakeWS()
    runner = rvs.RealtimeVoiceSessionRunner(session, ws)
    jordan = next(a for a in runner._resolve_agents() if a.id == "jordan")
    runner.room = FloorRoom(speaking="jordan")
    chunk = b"\x01\x02" * 6400
    rt = FloorRT(runner.room, "jordan", grant_at=99)
    line = "I stopped keeping the workarounds up. Does any of what I built survive the switch?"
    for ev in [
        {"type": "agent_transcript_delta", "text": line, "first": True},
        {"type": "agent_audio", "pcm": chunk}, {"type": "agent_audio", "pcm": chunk},
        {"type": "agent_transcript", "text": line},
        {"type": "response_done", "audio_unterminated": False, "retried": False},
        # the second reply, opening before the first's finalize has finished
        {"type": "agent_transcript_delta", "text": line, "first": True},
        {"type": "agent_audio", "pcm": chunk}, {"type": "agent_audio", "pcm": chunk},
        {"type": "agent_transcript", "text": line},
        {"type": "response_done", "audio_unterminated": False, "retried": False},
    ]:
        rt.feed(ev)
    rt.end()
    await runner._pump_member(jordan, rt)
    if runner._finalize_tasks:
        await asyncio.wait(list(runner._finalize_tasks), timeout=5)
    assert len(ws.frames("assistant_started")) == 2
    deltas = [f["text"] for f in ws.frames("assistant_text_delta")]
    assert deltas == [line, line], "each reply's text under its own turn"
    turns = session.store.of("assistant_turn")
    assert [t["text"] for t in turns] == [line, line]
    assert session.store.of("second_reply_split")


def test_the_reconnect_note_names_the_other_characters_lines_as_theirs():
    runner, session, ws = make_runner("S3A")
    jordan = next(a for a in runner.cast if a.id == "jordan")
    runner.agent, runner.agent_id = jordan, jordan.id
    session.shared_history[:] = [
        {"speaker": "alex", "text": "So we're going to spend six weeks building processes that break."},
        {"speaker": "user", "text": "Jordan, what do you think?"},
        {"speaker": "jordan", "text": "None of it survives."},
    ]
    note = runner._reconnect_note()
    assert "- Alex: So we're going to spend six weeks" in note
    assert "- Them: Jordan, what do you think?" in note
    assert "- You: None of it survives." in note
    assert "- You: So we're going" not in note
