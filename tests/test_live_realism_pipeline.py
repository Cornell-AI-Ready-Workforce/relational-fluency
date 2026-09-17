"""What a LOST participant does to the voice pipeline, held to the live measurements.

Every earlier round measured naturalness through the TEXT model and drove the
realtime model with a cooperative script — and both hid all of this. Today's
round drove nto.gemini-live-2.5-flash through api.ai.it.cornell.edu with the
researcher's own hesitant lines ("Hello." / "What about it?" / "I need more
context. [900 ms] What do you mean?" / "Yeah. [1100 ms] I'll work on it, I
guess...") and found, on the wire:

  * THE PAUSE SPLIT. The gateway's default turn detection AND the runner's own
    900 ms VAD both closed the participant's turn inside a 700-1300 ms
    mid-thought pause. "I need more context. / What do you mean?" arrived as
    two transcripts and drew two replies; a trailing-off tail was dropped
    outright. A connect-time `turn_detection` of server_vad / 1500 ms was
    measured HONOURED (session.updated back, reply 2.51 s after speech end
    against 1.27 s bare; 2000 ms -> 2.99 s; 3000 ms -> no session.updated,
    the silent-mute shape). The runner's own turn end has to be held for the
    same window, or the runner becomes the thing that splits the turn.
  * THE FRAMING LINE THAT NEVER ARRIVES. Every `opening:` went out as a
    mid-session session.update, which this family ignores (0 acks in 21 runs),
    so a lost participant's first thirty seconds were "Hello." -> "The pack."
    The only brief the family reads is the one sent at connect.
  * THE DEAF ROOM. A group scene opened with pad + commit + response.create on
    a session that had heard nothing, which drew NO frame at all (5/5 rooms),
    and the unanswered create left `responding` latched so the lead's next
    grant was skipped as "already answering". A user text item + create drew a
    full in-character opening 4/4.
  * THE 47 s STALL. A commit + create the gateway ignored sat until the 45 s
    watchdog; to the participant that is a character that stopped hearing them.
  * THE GLUE. A reply the participant cut off keeps streaming (cancel is inert
    here) and its continuation was joined to the head with no space: "or notIt
    needed to be said".
  * THE DROP WITH NO WAY BACK. A gateway-side socket close ended the encounter;
    every recoverable fault before it had reached the page as an `error`
    frame, which the page reads as "the server stated a failure", and the
    second such drop hides the Reconnect button.
  * THE MANIFEST that named the text model for a voice encounter.

No network and no credentials here: the live evidence is quoted, not re-run,
and the pipeline is held to it with fakes in the shapes the runner uses.
"""

from __future__ import annotations

import array
import asyncio
import math
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server import group_room as gr  # noqa: E402
from server import realtime_voice_session as rvs  # noqa: E402
from server.scenarios import load_scenario  # noqa: E402
from server.voice import realtime as rt_mod  # noqa: E402
from tools import encounter_health  # noqa: E402


GEMINI = "nto.gemini-live-2.5-flash"
GPT = "gpt-realtime-2.1"

# The measured window; see the gemini row in REALTIME_FAMILIES.
MEASURED_WINDOW = {"type": "server_vad", "silence_duration_ms": 1500,
                   "prefix_padding_ms": 300}


# --------------------------------------------------------------------------
# Fakes, in the shapes the runner actually uses.
# --------------------------------------------------------------------------

class FakeStore:
    def __init__(self):
        self.events = []
        self.audio = {}
        self.user_audio = b""
        self.closed = False
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
    """The participant's socket: records what the runner sends, and hangs up
    the moment the runner asks it for audio."""

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
    """A bridge whose events() is fed by the test and ends when told to.

    `_closing` False at the end models the GATEWAY closing the socket, which is
    the case the runner has to survive; a session the runner closed itself
    sets it True through close()."""

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
        self.retry_nudges = []
        self.prompts = []
        self.participant_speaking = None
        self._q: asyncio.Queue = asyncio.Queue()
        FakeRT.built.append(self)

    @property
    def responding(self):
        return self._responding

    def clear_response_state(self):
        self._responding = False
        self.autofire_active = False

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

    async def prompt_response(self, text):
        self.prompts.append(text)
        self._responding = True

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


def make_runner(scenario_id):
    session = FakeSession(scenario_id)
    ws = FakeWS()
    return rvs.RealtimeVoiceSessionRunner(session, ws), session, ws


@pytest.fixture
def on_model(monkeypatch):
    def _set(model):
        monkeypatch.setattr(rt_mod, "MODEL", model)
        return model
    return _set


@pytest.fixture(autouse=True)
def _short_waits(monkeypatch):
    monkeypatch.setenv("TRANSCRIPT_GRACE_SECONDS", "0.2")
    monkeypatch.setenv("AUTOFIRE_WAIT", "0.05")
    monkeypatch.setenv("ROUTE_TRANSCRIPT_WAIT", "0.05")
    FakeRT.built = []


def pcm(ms: int, rms: int) -> bytes:
    n = 16000 * ms // 1000
    if rms == 0:
        return b"\x00" * (n * 2)
    amp = int(rms * math.sqrt(2))
    a = array.array("h", (int(amp * math.sin(i * 0.3)) for i in range(n)))
    return a.tobytes()


def marks_for(vad, segments):
    """Feed (ms, rms) segments in 20 ms frames; return the marks in order."""
    out = []
    for ms, rms in segments:
        data = pcm(ms, rms)
        for i in range(0, len(data), 640):
            m = vad.feed(data[i:i + 640])
            if m:
                out.append(m)
    return out


# --------------------------------------------------------------------------
# 1. The pause split: the measured window, on the wire and in the runner.
# --------------------------------------------------------------------------

def test_the_gemini_row_carries_the_measured_end_of_turn_window():
    """One table, one place: the window the gateway was measured to honour is a
    column of the family's row, beside the four findings already there. The
    gpt row asks for nothing — its server VAD is off and the runner's own bar
    is the only turn close, which is the path left exactly as it was."""
    gem = rt_mod.capabilities_for(GEMINI)
    gpt = rt_mod.capabilities_for(GPT)
    assert gem.end_of_turn == MEASURED_WINDOW
    assert gem.end_of_turn_silence_ms == 1500
    assert gpt.end_of_turn is None and gpt.end_of_turn_silence_ms == 0
    assert rt_mod.end_of_turn_for(GEMINI) == MEASURED_WINDOW
    assert rt_mod.end_of_turn_for(GPT) is None
    assert rt_mod.end_of_turn_silence_ms_for(GEMINI) == 1500


def test_a_session_the_runner_opens_on_gemini_sends_the_window_and_a_bare_one_does_not(on_model):
    """The runner's sessions carry the window; a session built by nobody in
    particular still sends the bare payload the earlier rounds pinned. And
    the gpt row's `turn_detection: null` wins over any window somebody sets,
    because that null is what keeps a gpt participant's turn in one piece."""
    on_model(GEMINI)
    runner, _, _ = make_runner("S2A")
    rt = runner._new_session(instructions="be imani", voice="Kore")
    assert rt._session_payload()["turn_detection"] == MEASURED_WINDOW

    bare = rt_mod.RealtimeVoiceSession(instructions="be imani", voice="Kore",
                                       model=GEMINI, api_key="x")
    assert "turn_detection" not in bare._session_payload()

    gpt = rt_mod.RealtimeVoiceSession(instructions="be imani", voice="alloy",
                                      model=GPT, api_key="x")
    gpt.turn_detection = MEASURED_WINDOW
    assert gpt._session_payload()["turn_detection"] is None


class ScriptedWS(FakeWS):
    """The browser's end of the socket: a fixed list of inbound frames, then a
    disconnect."""

    def __init__(self, frames):
        super().__init__()
        self._frames = list(frames)

    async def receive(self):
        if self._frames:
            return {"type": "websocket.receive", "bytes": self._frames.pop(0)}
        return {"type": "websocket.disconnect"}


def frames_of(segments):
    out = []
    for ms, level in segments:
        data = pcm(ms, level)
        out.extend(data[i:i + 640] for i in range(0, len(data), 640))
    return out


# The researcher's own line, as the microphone delivers it: speech, a 1200 ms
# mid-thought pause, speech, then a real end of turn.
HESITANT_LINE = [(600, 2000), (1200, 0), (600, 2000), (2000, 0)]


def test_the_runner_holds_a_turn_end_for_the_familys_window_on_gemini(on_model):
    """The runner's bar (900 ms) is left where it is — the barge-in gate, the
    "your turn" cue and the room path all keep their timing — and on the
    family with a 1500 ms gateway window a 1:1 turn end is HELD for the
    remaining 600 ms of quiet. On gpt nothing is held."""
    on_model(GEMINI)
    runner, _, _ = make_runner("S2A")
    assert runner.vad.silence_ms == 900
    assert runner._end_of_turn_confirm_ms() == 600
    on_model(GPT)
    runner, _, _ = make_runner("S2A")
    assert runner._end_of_turn_confirm_ms() == 0


def test_a_mid_thought_pause_inside_the_window_does_not_end_the_participants_turn(on_model):
    """Under the old rule the 1200 ms pause ended the turn and the character
    answered "I need more context." on its own; now the held end is withdrawn
    when speech resumes and ONE turn is committed, at the real end. A genuine
    2000 ms silence still ends it: end-of-turn detection is widened, not off."""
    on_model(GEMINI)
    session = FakeSession("S2A")
    ws = ScriptedWS(frames_of(HESITANT_LINE))
    runner = rvs.RealtimeVoiceSessionRunner(session, ws)
    runner.vad.noise_margin = 0     # the fixed bar; the room floor is not under test
    rt = FakeRT()
    rt.commits = 0

    async def commit_turn():
        rt.commits += 1
    rt.commit_turn = commit_turn
    runner.rt = rt
    asyncio.run(asyncio.wait_for(runner._client_to_model(), timeout=10))
    assert rt.commits == 1, f"the pause split the turn: {rt.commits} commits"
    assert len(session.store.of("turn_end_withdrawn")) == 1


def test_the_gpt_path_is_left_exactly_as_it_was(on_model):
    """No window on gpt, so the same line is closed by the runner's own bar
    each time it fires — the behaviour the earlier rounds measured and pinned."""
    on_model(GPT)
    session = FakeSession("S2A")
    ws = ScriptedWS(frames_of(HESITANT_LINE))
    runner = rvs.RealtimeVoiceSessionRunner(session, ws)
    runner.vad.noise_margin = 0
    rt = FakeRT()
    rt.commits = 0

    async def commit_turn():
        rt.commits += 1
    rt.commit_turn = commit_turn
    runner.rt = rt
    asyncio.run(asyncio.wait_for(runner._client_to_model(), timeout=10))
    assert rt.commits == 2
    assert not session.store.of("turn_end_withdrawn")


def test_a_room_puts_the_window_on_every_session_it_opens(monkeypatch):
    """The members answer the participant and the scribe transcribes them; a
    pause split on any one socket is a fragment in the record."""
    class Factory:
        made = []

        def __init__(self, instructions, voice, tools):
            self.instructions, self.voice, self.tools = instructions, voice, tools
            self.model = ""
            self.ws = object()
            Factory.made.append(self)

        async def connect(self, *, open_conversation=True):
            return None

        async def close(self):
            self.ws = None

    class A:
        def __init__(self, aid):
            self.id, self.name = aid, aid

    monkeypatch.setattr(gr, "RealtimeVoiceSession", Factory)
    room = gr.GroupRoom([A("dan"), A("priya")], instructions_for=lambda a: "brief",
                        voice_for=lambda a: "", model=GEMINI)
    asyncio.run(room.open())
    assert len(Factory.made) == 3
    assert all(s.turn_detection == MEASURED_WINDOW for s in Factory.made)

    Factory.made = []
    room = gr.GroupRoom([A("dan")], instructions_for=lambda a: "brief",
                        voice_for=lambda a: "", model=GPT)
    asyncio.run(room.open())
    assert all(not hasattr(s, "turn_detection") for s in Factory.made)


# --------------------------------------------------------------------------
# 2. The framing line goes where this family will read it, and the record says.
# --------------------------------------------------------------------------

def test_the_first_reply_framing_is_folded_into_the_connect_brief_on_gemini(on_model):
    """S2C's `opening:` ("The pack went out this morning and it goes out again
    Monday...") never reached Imani: it lived in the t1 cue, sent by a
    session.update this family ignores, so a lost participant's first thirty
    seconds were "Hello." -> "The pack." On this family the one brief the actor
    reads is the connect brief, so that is where the framing goes — as a
    first-reply note, because in a 1:1 the participant speaks first."""
    on_model(GEMINI)
    runner, session, _ = make_runner("S2A")
    opening = str(runner._interaction()["opening"]).strip()
    assert runner._fold_opening(runner.agent, group=False) is True
    brief = runner._instructions()
    assert opening in brief
    assert "FIRST REPLY" in brief and "never say any of this out loud" in brief
    (rec,) = session.store.of("opening_framing")
    assert rec["via"] == "connect_brief" and rec["agent_id"] == "morgan"
    assert rec["honours_session_update"] is False


def test_on_a_family_that_honours_updates_the_existing_path_stays_and_is_recorded(on_model):
    """gpt-realtime keeps the path it had: nothing in the connect brief, the
    t1 beat's cue carries the framing through a session.update the gateway
    acknowledges. The record still says which way it went."""
    on_model(GPT)
    runner, session, _ = make_runner("S2A")
    opening = str(runner._interaction()["opening"]).strip()
    assert runner._fold_opening(runner.agent, group=False) is False
    assert opening not in runner._instructions()
    (rec,) = session.store.of("opening_framing")
    assert rec["via"] == "trigger_cue" and rec["honours_session_update"] is True


def test_run_opens_the_first_session_with_the_framing_in_its_brief(on_model, monkeypatch):
    """Not just buildable: the session run() actually connects carries it."""
    on_model(GEMINI)
    monkeypatch.setattr(rvs, "RealtimeVoiceSession", FakeRT)
    runner, session, _ = make_runner("S2A")
    opening = str(runner._interaction()["opening"]).strip()
    asyncio.run(asyncio.wait_for(runner.run(), timeout=10))
    assert FakeRT.built, "run() opened no session"
    assert opening in FakeRT.built[0].instructions
    assert session.store.of("opening_framing")[0]["via"] == "connect_brief"


def test_a_group_scene_on_gemini_opens_with_a_prompt_and_says_so(on_model, monkeypatch):
    """The deaf room. On Gemini the lead's opening direction is folded into its
    connect brief by _open_room, the stage_direction row says `connect_brief`,
    and the lead is made to speak by a user text item + create — never by a
    commit of silence, which drew nothing 5/5."""
    on_model(GEMINI)
    monkeypatch.setattr(gr, "RealtimeVoiceSession", FakeRT)
    runner, session, _ = make_runner("S4A")
    lead = runner._resolve_agents()[0]

    async def scenario():
        await runner._open_room()
        rt = runner.room.session_for(lead.id)
        assert "You speak first and open the scene" in rt.instructions
        opened = asyncio.ensure_future(runner._open_group_scene())
        await asyncio.sleep(0.1)
        assert rt.prompts == [rt_mod.SCENE_OPEN_PROMPT]
        assert rt.pending_input == 0, "the scene was opened with a commit of silence"
        runner._response_done.set()
        await asyncio.wait_for(opened, timeout=5)
        await runner._close_room()

    asyncio.run(scenario())
    direction = [d for d in session.store.of("stage_direction") if d.get("opening")]
    assert direction and direction[0]["via"] == "connect_brief"
    assert direction[0]["acked"] is None, "no session.update was sent, so no ack is claimed"
    (framing,) = session.store.of("opening_framing")
    assert framing["via"] == "connect_brief" and framing["mode"] == "group"
    assert session.store.of("group_scene_opened")


def test_a_latched_unanswered_create_does_not_pass_for_a_reply_in_progress():
    """After the scene open's create drew nothing, `responding` stayed True for
    45 s and the lead's next grant — the first the participant had spoken
    into — was skipped as "already answering". A reply counts as started on
    evidence: an auto-fire, or output for the reply in flight."""
    class Latched:
        responding = True
        autofire_active = False
        _response_saw_output = False

    class Started(Latched):
        _response_saw_output = True

    class Autofired(Latched):
        responding = False
        autofire_active = True

    class CannotSay:
        responding = True
        autofire_active = False

    assert gr.GroupRoom._reply_evidently_started(Latched()) is False
    assert gr.GroupRoom._reply_evidently_started(Started()) is True
    assert gr.GroupRoom._reply_evidently_started(Autofired()) is True
    assert gr.GroupRoom._reply_evidently_started(CannotSay()) is True


# --------------------------------------------------------------------------
# 3. A reply that never begins is handed back in ten seconds, not forty-five.
# --------------------------------------------------------------------------

class SilentWire:
    """A gateway socket that never answers: recv waits forever, sends are kept."""

    def __init__(self):
        self.sent = []

    async def send(self, raw):
        self.sent.append(raw)

    async def recv(self):
        await asyncio.sleep(3600)

    async def close(self):
        return None


def test_a_requested_reply_that_never_begins_is_handed_back_as_reply_missing(monkeypatch):
    monkeypatch.setattr(rt_mod, "REQUEST_UNANSWERED_S", 0.15)
    monkeypatch.setattr(rt_mod, "RECV_POLL_S", 0.05)
    rt = rt_mod.RealtimeVoiceSession(instructions="x", voice="Puck",
                                     model=GEMINI, api_key="x")
    rt.ws = SilentWire()

    async def scenario():
        await rt.request_response()
        assert rt.responding
        t0 = time.time()
        async for ev in rt.events():
            if ev["type"] == "reply_missing":
                return ev, time.time() - t0
            if time.time() - t0 > 3:
                return None, time.time() - t0

    ev, took = asyncio.run(asyncio.wait_for(scenario(), timeout=6))
    assert ev is not None, "a reply that never began was never handed back"
    assert ev["retryable"] is True and took < 2.0
    assert not rt.responding, "the latch is down, so the next commit is not refused"


def test_the_runner_asks_once_more_with_a_prompt_and_writes_it_down(on_model):
    """The retry is a user text item the record shows — not "say it again",
    which is the wrong prompt for a character that has said nothing."""
    on_model(GEMINI)
    runner, session, _ = make_runner("S2A")
    rt = FakeRT()
    ev = {"type": "reply_missing", "waited_s": 10, "retryable": True}
    assert asyncio.run(runner._reply_missing(rt, "morgan", ev)) is True
    assert rt.retry_nudges == [rt_mod.UNANSWERED_NUDGE]
    (missing,) = session.store.of("reply_missing")
    (retry,) = session.store.of("reply_retry")
    assert missing["waited_s"] == 10
    assert retry["asked"] is True and retry["nudge"] == rt_mod.UNANSWERED_NUDGE

    # And not while the participant is talking: they are about to prompt it.
    runner.vad.speaking = True
    rt2 = FakeRT()
    assert asyncio.run(runner._reply_missing(rt2, "morgan", ev)) is False
    assert rt2.retry_nudges == []
    assert session.store.of("reply_retry")[-1]["why"] == "participant_speaking"


# --------------------------------------------------------------------------
# 4. The glue: a resumed reply gets its seam back, and only a resumed one.
# --------------------------------------------------------------------------

def test_a_resumed_reply_is_not_glued_to_the_head_the_participant_cut_off():
    head = ["Is the handoff fixed or not"]
    assert rvs._resume_seam(head, {"text": "It needed to be said", "first": True}) \
        == " It needed to be said"
    # An ordinary chunk boundary is mid-word and is left alone.
    assert rvs._resume_seam(["Is the hand"], {"text": "off fixed"}) == "off fixed"
    # A reply's own first chunk has nothing in front of it.
    assert rvs._resume_seam([], {"text": "It needed", "first": True}) == "It needed"
    # Both sides already spaced, or punctuation first: as delivered.
    assert rvs._resume_seam(["or not "], {"text": "It", "first": True}) == "It"
    assert rvs._resume_seam(["or not"], {"text": ". It", "first": True}) == ". It"


def test_the_bridge_marks_the_opening_chunk_of_each_reply(monkeypatch):
    """`first` is set from the bridge's own count of replies: after a
    response.created (and so after a cancel, which ends the reply it names)."""
    monkeypatch.setattr(rt_mod, "RECV_POLL_S", 0.05)
    frames = [
        '{"type": "response.created", "response": {"id": "r1"}}',
        '{"type": "response.output_audio_transcript.delta", "delta": "Is the hand", "response_id": "r1"}',
        '{"type": "response.output_audio_transcript.delta", "delta": "off fixed or not", "response_id": "r1"}',
        '{"type": "response.created", "response": {"id": "r2"}}',
        '{"type": "response.output_audio_transcript.delta", "delta": "It needed", "response_id": "r2"}',
    ]

    class Wire(SilentWire):
        async def recv(self):
            if frames:
                return frames.pop(0)
            await asyncio.sleep(3600)

    rt = rt_mod.RealtimeVoiceSession(instructions="x", voice="Puck",
                                     model=GEMINI, api_key="x")
    rt.ws = Wire()

    async def scenario():
        out = []
        async for ev in rt.events():
            if ev["type"] == "agent_transcript_delta":
                out.append((ev["text"], ev["first"]))
            if len(out) == 3:
                return out

    out = asyncio.run(asyncio.wait_for(scenario(), timeout=5))
    assert out == [("Is the hand", True), ("off fixed or not", False), ("It needed", True)]


# --------------------------------------------------------------------------
# 5. The drop: recoverable faults are notices, and a gateway close is survived.
# --------------------------------------------------------------------------

def test_recoverable_gateway_faults_reach_the_page_as_notices_not_errors(on_model):
    """The page reads ANY `error` frame as "the server stated a failure", and
    the second stated-failure drop hides Reconnect. A 47 s stall the runner
    survives must not spend the participant's retry; a refused key still must."""
    on_model(GEMINI)

    async def scenario():
        runner, session, ws = make_runner("S2A")
        rt = FakeRT()
        runner.rt = rt
        pump = asyncio.ensure_future(runner._pump(rt))
        rt.feed({"type": "error", "recoverable": True,
                 "message": "no reply from the gateway after 47s; abandoning the turn"})
        rt.feed({"type": "error", "message": "Invalid API key: nope"})
        await asyncio.sleep(0.05)
        rt.end()
        await asyncio.wait_for(pump, timeout=5)
        return session, ws

    session, ws = asyncio.run(scenario())
    assert len(session.store.of("voice_error")) == 2, "both are still recorded"
    notices = ws.frames("voice_notice")
    errors = ws.frames("error")
    assert len(notices) == 1 and "abandoning the turn" in notices[0]["message"]
    assert len(errors) == 1 and "API key" in errors[0]["message"]


def test_the_bridge_marks_its_stall_and_close_reports_recoverable(monkeypatch):
    monkeypatch.setattr(rt_mod, "RECV_POLL_S", 0.05)
    monkeypatch.setattr(rt_mod, "RESPONSE_STALL_S", 0.1)
    rt = rt_mod.RealtimeVoiceSession(instructions="x", voice="Puck",
                                     model=GEMINI, api_key="x")
    rt.ws = SilentWire()

    async def scenario():
        # An auto-fired reply that then goes silent: the general stall.
        rt._response_active = True
        rt._response_started_at = time.time()
        async for ev in rt.events():
            if ev["type"] == "error":
                return ev

    ev = asyncio.run(asyncio.wait_for(scenario(), timeout=5))
    assert ev["recoverable"] is True and "abandoning the turn" in ev["message"]


def test_a_socket_the_gateway_closes_is_rebuilt_inside_the_encounter(on_model, monkeypatch):
    """Live in the S1B baseline: "realtime connection lost: no close frame
    received or sent" at 84 s ended run(), the participant's socket closed
    behind it, and the page showed the drop card. The participant's socket is
    fine; the runner rebuilds the gateway session as the same character, with
    a scene note carrying what was said, and carries on — RECONNECT_LIMIT
    times, after which the old ending stands and the page is told `error`."""
    on_model(GEMINI)
    monkeypatch.setattr(rt_mod, "RECONNECT_LIMIT", 2)
    monkeypatch.setattr(rvs, "RealtimeVoiceSession", FakeRT)
    runner, session, ws = make_runner("S2A")
    session.shared_history[:] = [{"speaker": "user", "text": "Hello."},
                                 {"speaker": "morgan", "text": "The pack goes out Monday."}]
    first = FakeRT()
    runner.rt = first

    async def scenario():
        relay = asyncio.ensure_future(runner._model_to_client())
        # The gateway closes the first socket: events() ends, _closing False.
        first.end()
        for _ in range(100):
            await asyncio.sleep(0.02)
            if len(FakeRT.built) >= 2 and runner.rt is FakeRT.built[1]:
                break
        second = FakeRT.built[1]
        assert runner.rt is second
        assert "The line dropped" in second.instructions
        assert "The pack goes out Monday." in second.instructions
        second.end()
        for _ in range(100):
            await asyncio.sleep(0.02)
            if len(FakeRT.built) >= 3 and runner.rt is FakeRT.built[2]:
                break
        third = FakeRT.built[2]
        third.end()          # the third close is past the budget
        await asyncio.wait_for(relay, timeout=5)

    asyncio.run(scenario())
    reconnected = session.store.of("realtime_session_reconnected")
    assert [r["attempt"] for r in reconnected] == [1, 2]
    assert [f["kind"] for f in ws.frames("voice_notice")] == ["reconnected", "reconnected"]
    assert len(ws.frames("error")) == 1, "past the budget the participant is told"


def test_a_socket_the_runner_closed_itself_is_not_rebuilt(on_model, monkeypatch):
    """A character switch closes the outgoing session on purpose; that is an
    ordinary end of the pump and must not spawn a stranger."""
    on_model(GEMINI)
    monkeypatch.setattr(rvs, "RealtimeVoiceSession", FakeRT)
    runner, session, ws = make_runner("S2A")
    rt = FakeRT()
    runner.rt = rt

    async def scenario():
        relay = asyncio.ensure_future(runner._model_to_client())
        await asyncio.sleep(0.05)      # the relay is pumping rt
        await rt.close()
        await asyncio.wait_for(relay, timeout=5)

    asyncio.run(scenario())
    assert len(FakeRT.built) == 1
    assert not session.store.of("realtime_session_reconnected")


# --------------------------------------------------------------------------
# 6. The participant's line is numbered; the manifest names the voice model.
# --------------------------------------------------------------------------

def test_each_participant_utterance_is_numbered_on_the_frame_the_page_gets(on_model):
    on_model(GEMINI)
    runner, session, ws = make_runner("S2A")

    async def scenario():
        await runner._record_user_turn("I need more context.")
        await runner._record_user_turn("What do you mean?")

    asyncio.run(scenario())
    frames = ws.frames("user_transcript")
    assert [f["utterance"] for f in frames] == [1, 2]
    assert [e["utterance"] for e in session.store.of("user_turn")] == [1, 2]


def test_the_transcribers_placeholder_is_scrubbed_and_the_gap_is_recorded(monkeypatch, on_model):
    """Live: "{} Isle Do what I can." for "side... Uh, I'll do what I can."
    The "{}" is the gateway transcriber's own placeholder for speech it could
    not transcribe — the "{}" inside the researcher's lines — and there is no
    "{}" in any source of ours. The character must hear what the participant
    said, once, as said: the placeholder is scrubbed at the bridge, the line
    is flagged garbled, and a line that was nothing but placeholders is
    written down as an untranscribed turn rather than as nothing."""
    monkeypatch.setattr(rt_mod, "RECV_POLL_S", 0.05)
    frames = [
        '{"type": "conversation.item.input_audio_transcription.completed", "transcript": "{} Isle Do what I can."}',
        '{"type": "conversation.item.input_audio_transcription.completed", "transcript": "Yeah, I\'ll work on it."}',
        '{"type": "conversation.item.input_audio_transcription.completed", "transcript": "{}"}',
    ]

    class Wire(SilentWire):
        async def recv(self):
            if frames:
                return frames.pop(0)
            await asyncio.sleep(3600)

    rt = rt_mod.RealtimeVoiceSession(instructions="x", voice="Puck",
                                     model=GEMINI, api_key="x")
    rt.ws = Wire()

    async def scenario():
        out = []
        async for ev in rt.events():
            if ev["type"] == "user_transcript":
                out.append((ev["text"], ev["garbled"]))
            if len(out) == 3:
                return out

    out = asyncio.run(asyncio.wait_for(scenario(), timeout=5))
    assert out == [("Isle Do what I can.", True), ("Yeah, I'll work on it.", False), ("", True)]

    on_model(GEMINI)
    runner, session, ws = make_runner("S2A")

    async def record():
        await runner._record_user_turn("Isle Do what I can.", garbled=True)
        await runner._record_user_turn("", garbled=True)

    asyncio.run(record())
    (turn,) = session.store.of("user_turn")
    assert turn["garbled"] is True and "{}" not in turn["text"]
    assert ws.frames("user_transcript")[0]["garbled"] is True
    assert session.store.of("user_turn_untranscribed"), "a wholly lost line left no trace"


def test_the_manifest_of_a_voice_encounter_names_the_realtime_model(monkeypatch):
    from server import session as session_mod

    built = []

    class Store:
        def __init__(self, sid, **kw):
            self.kw = kw
            self.events = []
            self.spec_fingerprint = "fp"
            built.append(self)

        def event(self, type_, **fields):
            self.events.append(dict(type=type_, **fields))

    monkeypatch.setattr(session_mod, "SessionStore", Store)
    monkeypatch.setattr(session_mod, "text_client", lambda: object())
    monkeypatch.setattr(rt_mod, "MODEL", GEMINI)

    voice = session_mod.Session("S2A", capture_audio=True)
    text = session_mod.Session("S2A", capture_audio=False)
    assert built[0].kw["model"] == GEMINI, "the voice encounter's manifest names the text model"
    assert built[1].kw["model"] == text.model != GEMINI
    start = built[0].events[0]
    assert start["type"] == "session_start"
    assert start["realtime_model"] == GEMINI and start["text_model"] == voice.model


def test_encounter_health_reports_what_the_pipeline_did():
    events = [
        {"type": "opening_framing", "via": "connect_brief"},
        {"type": "reply_missing", "waited_s": 10},
        {"type": "reply_retry", "asked": True},
        {"type": "realtime_session_reconnected", "attempt": 1},
        {"type": "assistant_turn", "text": "", "audio_ms": 0},
        {"type": "assistant_turn", "text": "The pack.", "audio_ms": 400},
        {"type": "user_turn", "text": "What?"},
        {"type": "user_turn", "text": "Hello."},
        {"type": "user_turn", "text": "I need more context about the pack."},
    ]
    notes = "\n".join(encounter_health.pipeline_notes(events))
    assert "framing line: 1 via connect_brief" in notes
    assert "replies never begun: 1 (1 asked again)" in notes
    assert "reconnected inside the encounter" in notes
    assert "1 empty" in notes
    assert "three words or fewer: 2 of 3" in notes
