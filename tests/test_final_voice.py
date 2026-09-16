"""The runner's three go-live defects: disclosure, blind console, false record.

Each test here stands for something that looked like success. The gateway key
went into events.jsonl and down the participant's socket wearing the word
"error", so the disclosure was indistinguishable from ordinary logging. Every
mid-encounter failure was recorded and broadcast to nobody, so a researcher
watching an encounter whose director was dead saw the same screen as a healthy
one for the full 7-12 minutes. And a routing fallback to cast[0] was written as
`director_route`, which is the sentence an analyst reads as the director's
judgement.

No network and no credentials. Gateway failures are the real exception types
raised from a patched call site: anthropic.AuthenticationError with a
credential quoted in its body (which is what a LiteLLM 401 does), and
websockets' InvalidHeaderValue, which stringifies to the entire Authorization
header it refused. Nothing here opens a socket.
"""

from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path

import anthropic
import httpx
import pytest
from websockets.exceptions import InvalidHeaderValue

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server import director as director_mod  # noqa: E402
from server import group_room as gr  # noqa: E402
from server import realtime_voice_session as rvs  # noqa: E402
from server.scenarios import load_scenario  # noqa: E402


# The credential every test below tries to get published. Long enough to clear
# redact_key's needle floor and shaped like the real thing, so a test that
# passes because the value was too short to protect cannot happen.
KEY = "sk-cornell-LIVE-abcdef1234567890"

# The frame the researcher console, app.py's forwarder and this file all agree
# on. Named here so a field added on one side alone fails loudly rather than
# being dropped silently by the renderer.
FRAME_KEYS = {"type", "kind", "t", "agent_id", "detail", "severity"}


def a_401_that_quotes_the_key() -> Exception:
    """What a gateway rejecting a revoked key looks like coming out of the SDK.

    anthropic's APIStatusError stringifies to the response body verbatim, so a
    gateway that echoes back the credential it was sent puts it in str(exc) —
    reproduced here rather than assumed."""
    body = {"error": {"message": f"Invalid API key: {KEY}"}}
    response = httpx.Response(
        401, request=httpx.Request("POST", "http://gateway.invalid/v1/messages"),
        json=body,
    )
    return anthropic.AuthenticationError(
        f"Invalid API key: {KEY}", response=response, body=body
    )


def a_wrapped_key_header() -> Exception:
    """The other shape: a key pasted across two lines reaches the Authorization
    header, and websockets refuses it by quoting the whole header value."""
    return InvalidHeaderValue("Authorization", f"Bearer {KEY[:14]}\n{KEY[14:]}")


# --------------------------------------------------------------------------
# Fakes, in the shapes the runner actually uses.
# --------------------------------------------------------------------------

class FakeRT:
    def __init__(self, instructions="", voice="Puck", tools=None):
        self.ws = object()
        self.voice = voice
        self.model = "fake-realtime"
        self.autofire_active = False
        self.pending_input = 0
        self.debug_log = []
        self.instructions = [instructions]
        self.closed = False
        self._responding = False
        self._q: asyncio.Queue = asyncio.Queue()

    @property
    def responding(self):
        return self._responding

    def clear_response_state(self):
        self._responding = False

    async def connect(self, *, open_conversation=True):
        return None

    async def close(self):
        self.closed = True
        self.ws = None
        self._q.put_nowait(None)

    async def update_instructions(self, instructions):
        self.instructions.append(instructions)

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


class FakeStore:
    """The real SessionStore's surface, plus started_at so _event_t has a clock."""

    def __init__(self):
        self.events = []
        self.audio = {}
        self.user_audio = b""
        self.closed = False
        self.started_at = 1_772_460_000.0

    def event(self, type_, **fields):
        if self.closed:
            return
        self.events.append(dict(type=type_, **fields))

    def append_assistant_audio(self, pcm, agent_id=None):
        self.audio[agent_id] = self.audio.get(agent_id, b"") + pcm

    def append_user_audio(self, pcm):
        self.user_audio += pcm

    def types(self):
        return [e["type"] for e in self.events]

    def of(self, type_):
        return [e for e in self.events if e["type"] == type_]


class FakeEngine:
    def __init__(self, agent):
        self.agent = agent

    def _system_prompt(self, branches, note, group=False):
        return f"SYSTEM PROMPT for {self.agent.id}"


class FakeSession:
    def __init__(self, scenario_id, *, director=None):
        self.scenario = load_scenario(scenario_id, "p_test")
        self.is_group = self.scenario.mode == "group"
        self.engines = {a.id: FakeEngine(a) for a in self.scenario.cast}
        self.store = FakeStore()
        self.director = director
        self.triggered_branches = []
        self.shared_history = []
        self.steering_log = []
        self.broadcasts = []
        # Set by a test that wants auto_steer to fail the way the real one does.
        self.steer_failure = None

    def append_user(self, text):
        self.shared_history.append({"speaker": "user", "text": text})

    def append_agent(self, agent_id, text):
        self.shared_history.append({"speaker": agent_id, "text": text})

    async def broadcast(self, payload):
        self.broadcasts.append(payload)

    async def auto_steer(self, *, delivered=None):
        # Session.auto_steer catches its own exception and writes
        # auto_steer_error rather than raising, by design — a steering outage
        # must not break a live encounter. That swallowing is exactly why the
        # runner cannot see the failure by awaiting, so the fake reproduces it
        # instead of raising into the caller.
        if self.steer_failure is not None:
            from server.llm import redact_key
            self.store.event(
                "auto_steer_error", message=redact_key(str(self.steer_failure))
            )

    def frames(self, kind=None):
        out = [b for b in self.broadcasts
               if b.get("type") == "encounter_event"]
        return [b for b in out if kind is None or b.get("kind") == kind]


class FakeWS:
    def __init__(self):
        self.json = []
        self.binary = []

    async def send_json(self, payload):
        self.json.append(payload)

    async def send_bytes(self, payload):
        self.binary.append(payload)

    def frames(self, type_):
        return [f for f in self.json if f.get("type") == type_]


def make_runner(scenario_id, *, director=None):
    session = FakeSession(scenario_id, director=director)
    ws = FakeWS()
    return rvs.RealtimeVoiceSessionRunner(session, ws), session, ws


def blob(*parts) -> str:
    """Everything a test wants to prove is key-free, as one searchable string."""
    return repr(parts)


@pytest.fixture(autouse=True)
def _live_key_and_short_waits(monkeypatch):
    """A credential in the environment, which is the only state that makes the
    exact-value half of redact_key do anything, plus waits short enough that
    these tests assert on what was written rather than on patience."""
    monkeypatch.setenv("LITELLM_API_KEY", KEY)
    monkeypatch.setenv("TRANSCRIPT_GRACE_SECONDS", "0.3")
    monkeypatch.setenv("ROUTE_TRANSCRIPT_WAIT", "0.05")
    monkeypatch.setenv("AUTOFIRE_WAIT", "0.05")


# --------------------------------------------------------------------------
# 1. Disclosure: no gateway string leaves this file carrying the credential.
# --------------------------------------------------------------------------

def test_a_gateway_error_frame_puts_no_key_in_the_record_the_socket_or_the_console():
    """The 1:1 pump's error branch has three sinks at once: events.jsonl (which
    ships whole in the per-session download.zip), the participant's own browser,
    and now the researcher's live channel. All three used to carry the gateway's
    words verbatim, and a 401 body quotes the key it was sent."""
    async def scenario():
        runner, session, ws = make_runner("S1A")
        rt = FakeRT()
        runner.rt = rt
        pump = asyncio.ensure_future(runner._pump(rt))
        rt.feed({"type": "error",
                 "message": f"Invalid API key: {KEY}", "transient": False})
        await asyncio.sleep(0.05)
        rt.end()
        await asyncio.wait_for(pump, timeout=5)
        return runner, session, ws

    runner, session, ws = asyncio.run(scenario())

    stored = session.store.of("voice_error")
    assert stored, "the error must still be recorded; redaction is not silence"
    sent = ws.frames("error")
    assert sent, "the participant must still be told the call failed"
    console = session.frames("voice_error")
    assert console, "and the researcher watching must be told too"

    assert KEY not in blob(stored, sent, console)
    # Redaction has to leave a diagnosis behind, or the operator trades a
    # disclosure for an unreadable record and simply turns it off.
    assert "Invalid API key" in stored[0]["message"]
    assert stored[0]["message"] != f"Invalid API key: {KEY}"


def test_a_wrapped_key_in_an_authorization_header_is_scrubbed_across_the_break():
    """The second live shape. A key pasted across two lines survives the config
    strip, reaches the header, and comes back quoted whole — so redacting only
    up to the newline would leave the tail, which is still a live credential."""
    async def scenario():
        runner, session, ws = make_runner("S1A")
        rt = FakeRT()
        runner.rt = rt
        pump = asyncio.ensure_future(runner._pump(rt))
        rt.feed({"type": "error", "message": str(a_wrapped_key_header())})
        await asyncio.sleep(0.05)
        rt.end()
        await asyncio.wait_for(pump, timeout=5)
        return session, ws

    session, ws = asyncio.run(scenario())
    everything = blob(session.store.events, ws.json, session.broadcasts)
    assert KEY not in everything
    # The tail on its own is the half a newline-naive redactor leaves behind.
    assert KEY[14:] not in everything
    assert "Authorization" in session.store.of("voice_error")[0]["message"]


def test_no_exception_string_in_this_file_reaches_a_sink_unredacted():
    """A source guard, because the sinks outnumber the tests.

    There are two dozen writers in this module that interpolate an exception
    into an event field, and the disclosure needs only one of them to be added
    back without redact_key. This is what notices that."""
    src = (ROOT / "server" / "realtime_voice_session.py").read_text(
        encoding="utf-8"
    )
    bare = re.compile(r"message=(?!redact_key)(str\(|repr\(|ev\[|f\")")
    offenders = [
        (i, line.strip())
        for i, line in enumerate(src.splitlines(), 1)
        if bare.search(line)
    ]
    assert not offenders, (
        "every gateway-derived message= must go through llm.redact_key; "
        f"unwrapped: {offenders}"
    )


# --------------------------------------------------------------------------
# 2. The live failure channel.
# --------------------------------------------------------------------------

def test_the_failure_frame_matches_the_shape_the_console_renders():
    """app.py forwards this frame and researcher.html renders it by field name,
    so the three have to agree exactly. A renamed field is a warning strip that
    silently shows nothing."""
    async def scenario():
        runner, session, _ = make_runner("S1A")
        await runner._encounter_event(
            "transcript_missing", agent_id="riley",
            detail=f"Invalid API key: {KEY}", severity="error",
        )
        return session

    session = asyncio.run(scenario())
    (frame,) = session.frames()
    assert set(frame) == FRAME_KEYS
    assert frame["type"] == "encounter_event"
    assert frame["kind"] == "transcript_missing"
    assert frame["severity"] == "error"
    assert frame["agent_id"] == "riley"
    assert isinstance(frame["t"], float)
    # Same treatment as the stored event beside it: this frame leaves the
    # process for a browser exactly as the participant's error frames do.
    assert KEY not in frame["detail"]


def test_every_failure_the_audit_named_has_an_emitter():
    """The inventory, pinned from the emitting side.

    Each name below is a failure a researcher must see while the encounter is
    still stoppable, and each is also the events.jsonl event type for the same
    fault, so the console line and the archived row name one thing. A rename on
    this side alone leaves the console rendering a kind it has no label for."""
    src = (ROOT / "server" / "realtime_voice_session.py").read_text(
        encoding="utf-8"
    )
    emitted = set(re.findall(r"_encounter_event(?:_soon)?\(\s*\n?\s*\"(\w+)\"", src))
    # auto_steer_error is written by Session, not here, so it is forwarded by
    # type name through the store tee rather than by a literal call.
    emitted |= set(re.findall(r"forwarded = \{\"(\w+)\"", src))
    assert {
        "transcript_missing", "trigger_undelivered", "trigger_deferred",
        "auto_steer_error", "director_fallback", "steer_deferred",
        "scribe_pump_ended", "voice_error",
    } <= emitted, f"missing an emitter for: {emitted}"


def test_two_runners_on_one_session_do_not_double_report():
    """The store tee wraps an instance attribute, so wrapping a wrapper would
    count every steering outage twice. A console whose numbers are inflated by
    its own plumbing is one a researcher learns to discount."""
    async def scenario():
        runner, session, _ = make_runner("S1A")
        rvs.RealtimeVoiceSessionRunner(session, FakeWS())
        session.steer_failure = ValueError("no such knob: warmth")
        await runner._steer()
        await asyncio.sleep(0.05)
        return session

    session = asyncio.run(scenario())
    assert len(session.store.of("auto_steer_error")) == 1
    assert len(session.frames("auto_steer_error")) == 1


def test_a_reporting_failure_never_becomes_an_encounter_failure():
    """Every call site is inside a handler for something that already cost the
    participant part of an encounter. A monitoring frame that raises there
    would take the encounter down over the report of a smaller fault."""
    async def scenario():
        runner, session, _ = make_runner("S1A")

        async def broken(_payload):
            raise RuntimeError("the console socket set went away")
        session.broadcast = broken
        await runner._encounter_event("voice_error", detail="x")
        # And the store event beside it still lands.
        await runner._finalize_member_inner(runner.agent, "")
        return session

    session = asyncio.run(scenario())
    assert session.store.of("transcript_missing")


def test_an_agent_turn_with_no_text_is_broadcast_as_a_fault():
    """The transcript broadcast sits inside `if text`, so a turn that produced
    no transcript put nothing on the researcher's screen at all: the character
    simply went quiet, which reads as them choosing not to speak rather than as
    the transcript channel failing."""
    async def scenario():
        runner, session, _ = make_runner("S4A")
        dan = runner._resolve_agents()[0]
        await runner._finalize_member_inner(dan, "")
        return session, dan

    session, dan = asyncio.run(scenario())
    assert session.store.of("transcript_missing"), "the record already said so"
    (frame,) = session.frames("transcript_missing")
    assert frame["agent_id"] == dan.id
    assert frame["severity"] == "warn"
    assert not [b for b in session.broadcasts if b.get("type") == "transcript"], (
        "a turn with no text must not be broadcast as a transcript"
    )


def test_losing_the_rooms_participant_channel_is_announced_while_it_still_matters():
    """The scribe is the only participant transcript channel in a room. Past
    its death the record shows a participant who fell silent — a rateable ESCI
    behaviour — so the researcher has to hear about it in time to stop."""
    async def scenario():
        runner, session, ws = make_runner("S4A")
        rt = FakeRT()
        pump = asyncio.ensure_future(runner._pump_scribe(rt))
        await asyncio.sleep(0.02)
        rt.end()
        await asyncio.wait_for(pump, timeout=5)
        return session, ws

    session, ws = asyncio.run(scenario())
    assert session.store.of("scribe_pump_ended")
    (frame,) = session.frames("scribe_pump_ended")
    assert frame["severity"] == "error"
    assert "participant" in frame["detail"]


def test_a_steering_review_that_errors_every_turn_is_visible_on_the_console():
    """Session.auto_steer swallows its own failure by design, so `_steer` cannot
    tell a review that broke from one that found nothing to change. A steering
    pass erroring every turn means no gear moved for the whole encounter, which
    is precisely the quiet false success the console exists to expose."""
    async def scenario():
        runner, session, _ = make_runner("S1A")
        session.steer_failure = a_401_that_quotes_the_key()
        await runner._steer()
        # The forwarder hops through the event loop, as a store write from a
        # synchronous caller must.
        await asyncio.sleep(0.05)
        return session

    session = asyncio.run(scenario())
    assert session.store.of("auto_steer_error")
    (frame,) = session.frames("auto_steer_error")
    assert frame["severity"] == "error"
    assert KEY not in blob(frame, session.store.events)


def test_a_gear_shift_that_did_not_reach_the_actor_says_so_live():
    """The steering panel shows the shift the moment it is made. Only this
    frame says it has not been delivered yet, and a researcher who has just
    moved a gear otherwise reads the next unchanged turn as the shift having
    had no effect."""
    async def scenario():
        runner, session, _ = make_runner("S1A")
        rt = FakeRT()
        rt._responding = True
        runner.rt = rt

        async def one_shift(*, delivered=None):
            session.steering_log.append({"type": "steering", "knob": "warmth"})
        session.auto_steer = one_shift

        await runner._steer()
        return session

    session = asyncio.run(scenario())
    assert session.store.of("steer_deferred")
    (frame,) = session.frames("steer_deferred")
    assert frame["kind"] == "steer_deferred"


# --------------------------------------------------------------------------
# 3. director_route: a fallback recorded as a fallback.
# --------------------------------------------------------------------------

class OneCallDirector:
    """A director whose route() raises on the first call and works on the next.

    This is the shape that broke the flag: the first call fails, the room picks
    a speaker by rotation, and the direct-address branch below then makes a
    SECOND route() call whose success used to overwrite the fallback record."""

    model = "fake-director"

    def __init__(self, failure, then):
        self.failure = failure
        self.then = then
        self.calls = 0

    async def route(self, history, text):
        self.calls += 1
        if self.calls == 1:
            raise self.failure
        return [{"agent_id": a} for a in self.then]


def _room_with_fake_members(runner):
    """A room whose members answer instantly, so a turn runs in milliseconds
    rather than spending the 45 s floor timeout."""
    room = gr.GroupRoom(
        runner._resolve_agents(),
        instructions_for=lambda a: "x",
        voice_for=lambda a: "Puck",
    )
    for a in runner._resolve_agents():
        room.sessions[a.id] = FakeRT()

    async def give_floor(agent_id):
        room.speaking = agent_id
        runner._response_done.set()
        return room.sessions.get(agent_id)
    room.give_floor = give_floor

    async def nothing():
        return None
    runner._advance_when_spent = nothing
    runner.room = room
    return room


def test_a_dead_gateway_is_recorded_as_a_fallback_not_as_the_directors_judgement():
    """Director._fallback plays cast[0] and tags the entry `fallback: True`. The
    runner used to drop the tag, so on every turn the gateway 401'd the event an
    analyst reads as the routing record asserted a decision nobody made — and
    cast[0] is the dominant character in every group spec, so a flaky gateway
    surfaced in the data as inflated dominance rather than as an outage."""
    class RefusingClient:
        class messages:
            @staticmethod
            async def create(**kwargs):
                raise a_401_that_quotes_the_key()

    async def scenario():
        runner, session, _ = make_runner("S3A")
        session.director = director_mod.Director(
            session.scenario, client=RefusingClient(),
            on_event=session.store.event,
        )
        runner.director = session.director
        _room_with_fake_members(runner)
        # An agent has spoken, so route() cannot take its opener fast path and
        # the gateway is really called.
        session.append_agent("alex", "Let's start with the numbers.")
        runner._last_user_text = "I am not sure that follows."
        await asyncio.wait_for(runner._run_group_turn(), timeout=10)
        return session

    session = asyncio.run(scenario())

    (route,) = session.store.of("director_route")
    assert route["fallback"] is True, (
        "a gateway outage must not be written as a routing decision"
    )
    assert route["fallback_reason"] == "director_error"
    assert route["fallback_detail"], "and it must say what failed"
    assert route["speakers"][0] == session.scenario.cast[0].id
    # The whole trail, live channel included, and none of it carries the key.
    assert KEY not in blob(session.store.events, session.broadcasts)
    (frame,) = session.frames("director_fallback")
    assert frame["severity"] == "error"


def test_a_later_successful_route_cannot_erase_the_earlier_failure():
    """The first route() raised, so the speaker was picked by rotation. A second
    call that happens to succeed does not turn that rotation into a director
    decision, and clearing the flag here would write exactly the false record
    the flag exists to prevent."""
    async def scenario():
        runner, session, _ = make_runner("S3A")
        runner.director = OneCallDirector(
            a_401_that_quotes_the_key(), then=["casey"]
        )
        _room_with_fake_members(runner)
        runner._last_user_text = "I am not sure that follows."
        await asyncio.wait_for(runner._run_group_turn(), timeout=10)
        return session, runner

    session, runner = asyncio.run(scenario())
    assert runner.director.calls == 2, (
        "the test is only meaningful if the second route() actually ran"
    )
    (route,) = session.store.of("director_route")
    assert route["fallback"] is True
    assert route["fallback_reason"] == "director_route_raised"
    assert KEY not in blob(session.store.events, session.broadcasts)


def test_a_healthy_director_is_not_libelled_as_a_fallback():
    """The other direction, and the one that keeps the flag worth reading: a
    turn the director actually decided must record fallback=False."""
    class GoodDirector:
        model = "fake-director"

        async def route(self, history, text):
            return [{"agent_id": "casey"}]

    async def scenario():
        runner, session, _ = make_runner("S3A")
        runner.director = GoodDirector()
        _room_with_fake_members(runner)
        runner._last_user_text = "I am not sure that follows."
        await asyncio.wait_for(runner._run_group_turn(), timeout=10)
        return session

    session = asyncio.run(scenario())
    (route,) = session.store.of("director_route")
    assert route["fallback"] is False
    assert route["fallback_reason"] is None
    assert route["fallback_detail"] is None
    assert not session.frames("director_fallback")


# --------------------------------------------------------------------------
# 4. The console's own restraint: one frame per burst, and never on the
#    participant's audio path.
#
# The live failure channel above was added to make a silent encounter audible.
# Its first version then reintroduced the bug class it was built against, from
# the other end: the 1:1 pump's error branch emitted a frame per gateway error,
# audio deltas arrive every few tens of milliseconds, and researcher.html
# re-renders the strip on every frame and shows a monotonic total - so one flaky
# stream read as thousands of failures, and app.py's ENCOUNTER_EVENT_REPLAY_LIMIT
# bounds only the replay, never the live socket. The room and scribe pumps had
# no throttle at all, and a room multiplies it by every member of the cast.
#
# Two properties are pinned here. A burst is reported ONCE with a count, on all
# three pumps, and the count is never lost - not at a reply boundary, not when
# the stream dies mid-burst. And no researcher socket is ever awaited from a
# pump, because those loops relay the participant's audio.
# --------------------------------------------------------------------------

CORRUPT = "discarded a corrupt audio frame from the gateway: bad checksum"


def _totals(frames):
    """The closing frames of bursts: the ones carrying an (xN) count."""
    return [f for f in frames if re.search(r"\(x\d+;", f["detail"] or "")]


def test_a_corrupt_stream_reports_its_shape_to_the_console_not_its_length():
    """P1. Sixty discarded chunks are one fault with a size, not sixty faults.

    The participant socket already got this right - one notice per reply - and
    the console frame was emitted before that gate, so the two surfaces
    disagreed by a factor of sixty about the same stream. A count a researcher
    learns to discount is worth less than no strip at all, which is the argument
    the store tee's own idempotence comment makes two hundred lines away.
    """
    async def scenario():
        runner, session, ws = make_runner("S1A")
        rt = FakeRT()
        runner.rt = rt
        pump = asyncio.ensure_future(runner._pump(rt))
        for _ in range(60):
            rt.feed({"type": "error", "transient": True, "message": CORRUPT})
        await asyncio.sleep(0.2)
        mid_burst = list(session.frames("voice_error"))
        rt.feed({"type": "response_done"})     # the reply boundary
        await asyncio.sleep(0.2)
        rt.end()
        await asyncio.wait_for(pump, timeout=5)
        return session, ws, mid_burst

    session, ws, mid_burst = asyncio.run(scenario())

    assert len(mid_burst) == 1, (
        f"{len(mid_burst)} console frames for one bad stream; the strip counts "
        "every one of them and shows the total as a failure count"
    )
    assert mid_burst[0]["severity"] == "warn"
    # The record is the thing that must keep every occurrence: a row per chunk
    # is how an analyst sees how much audio the stream actually cost.
    assert len(session.store.of("voice_error")) == 60, (
        "the per-chunk record must not be collapsed to match the console"
    )
    # And the participant's own throttle is untouched.
    assert len(ws.frames("error")) == 1

    frames = session.frames("voice_error")
    assert len(frames) == 2, (
        "one frame opening the burst and one closing it with the total: "
        f"got {[f['detail'] for f in frames]}"
    )
    (total,) = _totals(frames)
    assert "(x60;" in total["detail"], (
        "suppression that never says how much it suppressed is the silence this "
        f"file exists to argue against: {total['detail']}"
    )


def test_each_reply_is_its_own_burst_on_the_console():
    """The count belongs to the reply it happened in.

    Carrying it across a reply boundary would report the first reply's damage
    against the second, and never rearming would hide a second corrupt stream
    behind the first - the console equivalent of the one-notice-per-encounter
    bug the participant-side latch is rearmed to avoid.
    """
    async def scenario():
        runner, session, ws = make_runner("S1A")
        rt = FakeRT()
        runner.rt = rt
        pump = asyncio.ensure_future(runner._pump(rt))
        for _ in range(2):
            for _ in range(4):
                rt.feed({"type": "error", "transient": True, "message": CORRUPT})
            rt.feed({"type": "response_done"})
            await asyncio.sleep(0.2)
        rt.end()
        await asyncio.wait_for(pump, timeout=5)
        return session

    session = asyncio.run(scenario())
    frames = session.frames("voice_error")
    assert len(frames) == 4, (
        f"two bursts, two opening frames and two totals: {len(frames)}"
    )
    totals = _totals(frames)
    assert len(totals) == 2
    assert all("(x4;" in f["detail"] for f in totals), (
        "each reply's total must count that reply and no other: "
        f"{[f['detail'] for f in totals]}"
    )


def test_a_room_does_not_multiply_one_bad_stream_by_its_cast():
    """_pump_member had no throttle whatsoever, and there is one of these pumps
    per character. A group room's console was the 1:1 flood times the cast."""
    async def scenario():
        runner, session, ws = make_runner("S4A")
        dan = runner._resolve_agents()[0]
        rt = FakeRT()
        pump = asyncio.ensure_future(runner._pump_member(dan, rt))
        for _ in range(20):
            rt.feed({"type": "error", "transient": True, "message": CORRUPT})
        await asyncio.sleep(0.2)
        mid_burst = list(session.frames("voice_error"))
        rt.end()
        await asyncio.wait_for(pump, timeout=5)
        await asyncio.sleep(0.05)
        return session, mid_burst, dan

    session, mid_burst, dan = asyncio.run(scenario())
    assert len(mid_burst) == 1, f"{len(mid_burst)} frames for one member's stream"
    assert mid_burst[0]["agent_id"] == dan.id, (
        "the frame must still say which character's stream failed"
    )
    assert len(session.store.of("voice_error")) == 20
    (total,) = _totals(session.frames("voice_error"))
    assert "(x20;" in total["detail"]


def test_the_scribes_stream_is_coalesced_too():
    """The third copy of the same loop, and the one whose failures matter most:
    the scribe is a room's only participant transcript channel."""
    async def scenario():
        runner, session, ws = make_runner("S4A")
        rt = FakeRT()
        pump = asyncio.ensure_future(runner._pump_scribe(rt))
        for _ in range(15):
            rt.feed({"type": "error", "transient": True, "message": CORRUPT})
        await asyncio.sleep(0.2)
        mid_burst = list(session.frames("voice_error"))
        rt.end()
        await asyncio.wait_for(pump, timeout=5)
        await asyncio.sleep(0.05)
        return session, mid_burst

    session, mid_burst = asyncio.run(scenario())
    assert len(mid_burst) == 1, f"{len(mid_burst)} frames for one scribe stream"
    assert len(session.store.of("voice_error")) == 15
    (total,) = _totals(session.frames("voice_error"))
    assert "(x15;" in total["detail"]
    # The death of the channel is a different fault and keeps its own frame.
    assert session.frames("scribe_pump_ended")


def test_a_terminal_error_is_never_coalesced_into_the_burst_before_it():
    """The console owes the researcher the care the participant socket already
    takes: an error the session did NOT survive must arrive as itself, behind
    the total of the survivable ones rather than counted in with them."""
    async def scenario():
        runner, session, ws = make_runner("S1A")
        rt = FakeRT()
        runner.rt = rt
        pump = asyncio.ensure_future(runner._pump(rt))
        for _ in range(5):
            rt.feed({"type": "error", "transient": True, "message": CORRUPT})
        rt.feed({"type": "error", "message": "realtime connection lost: 1006"})
        await asyncio.sleep(0.2)
        rt.end()
        await asyncio.wait_for(pump, timeout=5)
        return session

    session = asyncio.run(scenario())
    frames = session.frames("voice_error")
    assert len(frames) == 3, (
        "the burst's first frame, the burst's total, and the fault that ended "
        f"it: {[f['detail'] for f in frames]}"
    )
    (total,) = _totals(frames)
    assert "(x5;" in total["detail"]
    assert frames[-1]["severity"] == "error"
    assert "connection lost" in frames[-1]["detail"]
    # Ordering matters on a strip read at a glance: the chunks, then the death.
    assert frames.index(total) < len(frames) - 1


def test_a_burst_cut_short_by_teardown_still_reports_its_total():
    """The stream that was failing when the pump died is exactly the one whose
    size a researcher never got to see. Coalescing that loses the count on the
    way out is the recoverable-fault-into-permanent-silence trade this codebase
    refuses everywhere else."""
    async def scenario():
        runner, session, ws = make_runner("S1A")
        rt = FakeRT()
        runner.rt = rt
        pump = asyncio.ensure_future(runner._pump(rt))
        for _ in range(9):
            rt.feed({"type": "error", "transient": True, "message": CORRUPT})
        await asyncio.sleep(0.2)
        pump.cancel()
        await asyncio.gather(pump, return_exceptions=True)
        await asyncio.sleep(0.05)
        return session

    session = asyncio.run(scenario())
    totals = _totals(session.frames("voice_error"))
    assert totals, "the burst ended with the pump and was never totalled"
    assert "(x9;" in totals[0]["detail"]


def test_a_wedged_console_socket_cannot_stall_the_participants_audio():
    """The second harm, and the one that is not about counting.

    _pump handles agent_audio and error in one async-for, and Session.broadcast
    awaits send_json once per attached researcher socket. Awaiting a console
    frame from that loop therefore puts a researcher's browser on the
    participant's audio path: a console that has stopped reading its socket
    stalls the conversation it is watching. That was impossible before this
    channel existed and it must be impossible again.
    """
    async def scenario():
        runner, session, ws = make_runner("S1A")
        rt = FakeRT()
        runner.rt = rt
        # A researcher whose browser has stopped reading: the send never
        # returns. No network call anywhere here - the socket is a fake that
        # simply does not complete.
        wedged = asyncio.Event()
        reached_console = []

        async def never_returns(payload):
            reached_console.append(payload)
            await wedged.wait()
        session.broadcast = never_returns

        pump = asyncio.ensure_future(runner._pump(rt))
        rt.feed({"type": "error", "transient": True, "message": CORRUPT})
        rt.feed({"type": "agent_audio", "pcm": b"\x00\x01" * 40})
        await asyncio.sleep(0.3)
        heard = list(ws.binary)
        pump.cancel()
        wedged.set()
        await asyncio.gather(pump, return_exceptions=True)
        return heard, reached_console

    heard, reached_console = asyncio.run(scenario())
    assert reached_console, (
        "the test is only meaningful if the frame really did reach the wedged "
        "socket and block there"
    )
    assert heard, (
        "the participant heard nothing while a researcher's socket was blocked: "
        "a console must never be able to backpressure the encounter it watches"
    )


def test_a_console_frame_is_not_a_task_the_loop_may_collect():
    """The repo's own idiom, followed here too.

    Session.spawn_auto_steer keeps its tasks in a set and says why: asyncio
    holds only a weak reference to a task nobody else holds, so a bare
    ensure_future can be collected before it has sent anything. Every
    auto_steer_error and every dead-pump report travels this path, and a frame
    lost that way is a failure report that leaves no trace at all - the console
    then asserts health by omission, which is the one thing it exists to stop.
    """
    async def scenario():
        runner, session, _ = make_runner("S1A")
        gate = asyncio.Event()

        async def slow(payload):
            await gate.wait()
        session.broadcast = slow

        runner._encounter_event_soon("voice_error", detail="pump: it died")
        in_flight = set(runner._console_tasks)
        gate.set()
        await asyncio.sleep(0.05)
        return in_flight, set(runner._console_tasks)

    in_flight, after = asyncio.run(scenario())
    assert len(in_flight) == 1, "the frame's task is held while it is in flight"
    assert not after, "and released once it has been sent, so the set is bounded"


def test_two_different_faults_on_one_stream_are_two_frames_not_one_miscount():
    """Coalescing is for repeats, and only the message says what a repeat is.

    binascii's failures quote the failing chunk ("Incorrect padding", then
    "number of data characters (17)"), so distinct texts are the ordinary shape
    of this burst. Keyed on the stream alone, the second fault was never shown
    AND was counted into a total printed against the first's words - a console
    stating, confidently, that something happened twice which happened once.
    """
    async def scenario():
        runner, session, ws = make_runner("S1A")
        rt = FakeRT()
        runner.rt = rt
        pump = asyncio.ensure_future(runner._pump(rt))
        for tail in ("Incorrect padding",
                     "Invalid base64-encoded string: "
                     "number of data characters (17)"):
            rt.feed({"type": "error", "transient": True,
                     "message": f"discarded a corrupt audio frame from the "
                                f"gateway: {tail}"})
        await asyncio.sleep(0.2)
        rt.feed({"type": "response_done"})     # the reply boundary
        await asyncio.sleep(0.2)
        rt.end()
        await asyncio.wait_for(pump, timeout=5)
        return session

    session = asyncio.run(scenario())
    details = [f["detail"] for f in session.frames("voice_error")]
    assert any("Incorrect padding" in d for d in details), (
        f"the second distinct fault never reached the console at all: {details}"
    )
    assert any("(17)" in d for d in details), f"and neither did the first: {details}"
    assert not _totals(session.frames("voice_error")), (
        "each fault happened exactly once and nothing was suppressed, so a "
        f"frame saying 'repeats were kept off the console' is untrue: {details}"
    )


def test_the_scribes_death_frame_lands_below_the_total_of_what_killed_it():
    """researcher.html renders in arrival order and the two frames share a `t`.

    Awaiting the channel-death frame while the burst total was still only
    scheduled put the death of the channel ABOVE the count of the errors that
    killed it, and nothing downstream can put that back.
    """
    async def scenario():
        runner, session, ws = make_runner("S4A")
        rt = FakeRT()
        pump = asyncio.ensure_future(runner._pump_scribe(rt))
        for _ in range(3):
            rt.feed({"type": "error", "transient": True, "message": CORRUPT})
        await asyncio.sleep(0.2)
        rt.end()
        await asyncio.wait_for(pump, timeout=5)
        await asyncio.sleep(0.1)
        return session

    session = asyncio.run(scenario())
    frames = session.frames()
    (total,) = _totals(session.frames("voice_error"))
    death = [f for f in frames if f["kind"] == "scribe_pump_ended"]
    assert death, "the channel's death must still be announced"
    assert frames.index(total) < frames.index(death[0]), (
        "the strip reads: the chunks, their total, then the death they caused - "
        f"got {[f['kind'] + ': ' + (f['detail'] or '') for f in frames]}"
    )


def test_run_does_not_return_until_its_console_frames_have_actually_left():
    """A frame that was only scheduled is not a frame that was sent.

    Dispatching console frames as tasks is what keeps a researcher's socket off
    the participant's audio path, but nothing drained them: app.py drops the
    session the moment run() returns, so at any console round trip worth the
    name the burst total and the terminal error - the last two spawned, and the
    two a researcher most needs - were lost with no trace that they existed.
    """
    async def scenario(monkeypatch):
        runner, session, ws = make_runner("S1A")
        rt = FakeRT()
        runner.rt = rt
        monkeypatch.setattr(rvs, "RealtimeVoiceSession", lambda **k: rt)

        async def one_round_trip_away(payload):
            # A researcher's browser at 250ms, not a wedged one: the send
            # completes, it just does not complete instantly.
            await asyncio.sleep(0.25)
            session.broadcasts.append(payload)
        session.broadcast = one_round_trip_away

        async def client_to_model():
            await asyncio.sleep(0.05)      # the participant hangs up
        async def watchdog():
            await asyncio.sleep(30)
        async def model_to_client():
            await runner._pump(rt)

        runner._client_to_model = client_to_model
        runner._silence_watchdog = watchdog
        runner._model_to_client = model_to_client

        for _ in range(3):
            rt.feed({"type": "error", "transient": True, "message": CORRUPT})
        rt.feed({"type": "error", "message": "realtime connection lost: 1006"})
        await asyncio.wait_for(runner.run(), timeout=10)
        # app.py drops the session here, synchronously.
        return session

    with pytest.MonkeyPatch.context() as mp:
        session = asyncio.run(scenario(mp))

    frames = session.frames("voice_error")
    details = [f["detail"] for f in frames]
    assert any(CORRUPT in (d or "") for d in details), (
        f"the burst's opening frame never left: {details}"
    )
    assert _totals(frames), f"the burst's total never left: {details}"
    assert any(f["severity"] == "error" and "connection lost" in (f["detail"] or "")
               for f in frames), (
        f"the fault that ended the encounter never left: {details}"
    )
