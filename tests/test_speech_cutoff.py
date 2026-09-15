"""The bug a person heard: "it would sound really good then get cut off early".

Nothing in this suite caught it, and the reason is worth stating before the
tests: every channel the study records was COMPLETE for the turns that were cut
off. The transcript is the model's own text. The assistant WAV holds exactly the
frames the runner relayed. The event trail has no error in it. The loss happened
after all three — in the browser, which was handed the audio and then threw it
away, and upstream, where the gateway stopped sending mid-reply — so a test that
asserts on what the server wrote can pass forever while the participant hears
half a sentence.

So these tests assert on the two things nothing was asserting on:

  1. WHEN a character is cut off. A `speech_started` while a character is
     speaking cancels the reply and tells the page to drop every audio buffer it
     has already scheduled. On the shipped detector, a fan at RMS 600 produced
     one of those every 260 ms and keyboard clatter one every 2 s, with nobody
     saying a word. Both directions are checked here, because the obvious
     over-correction — an agent nobody can interrupt — would be a worse bug than
     the one being fixed.

  2. WHETHER the audio a turn was given is enough audio for the words it says.
     That comparison did not exist anywhere, which is why an 11% upstream
     truncation rate went unnoticed through five rounds of measured work.

Measured live on nto.gemini-live-2.5-flash through api.ai.it.cornell.edu while
these were written: false cut-offs 5 in 22 agent turns before, 0 in 19 after;
genuine interjections still cancelled the speaker, 6 of 12 after against 2 of 9
before, at 0.03-5.1 s from the moment both were talking.
"""

from __future__ import annotations

import asyncio
import random
import shutil
import struct
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server import realtime_voice_session as rvs  # noqa: E402
from server.voice import turn_audio  # noqa: E402
from server.voice.realtime import SilenceDetector  # noqa: E402
from server.scenarios import load_scenario  # noqa: E402

V2 = ROOT / "static" / "v2.html"

RATE = 16000
FRAME_MS = 20
N = int(RATE * FRAME_MS / 1000)
QUIET = b"\x00" * (N * 2)


def noise(rms: int, rng: random.Random) -> bytes:
    """One 20 ms frame of broadband noise at roughly `rms`."""
    if rms <= 0:
        return QUIET
    s = [max(-32768, min(32767, int(rng.gauss(0, rms)))) for _ in range(N)]
    return struct.pack("<%dh" % N, *s)


def speech_frame(level: int = 6000) -> bytes:
    """One 20 ms frame at a talking level. A square wave, because what the
    detector reads is RMS and nothing else."""
    return struct.pack("<%dh" % N, *([level, -level] * (N // 2)))


# --------------------------------------------------------------------------
# 1. The room must not cut a character off.
# --------------------------------------------------------------------------

def _bed_barges(frames) -> int:
    """How many times this bed would have cancelled a speaking character."""
    det = SilenceDetector()
    fires = was = 0
    for f in frames:
        det.feed(f)
        if det.barge_in and not was:
            fires += 1
        was = det.barge_in
    return fires


def test_a_fan_does_not_cut_a_character_off():
    """Steady room noise at RMS 600 (-36 dBFS) is over the shipped fixed
    threshold of 500, so the shipped detector called it speech within 260 ms and
    went on calling it speech. Every one of those was a barge-in."""
    rng = random.Random(3)
    bed = [noise(600, rng) for _ in range(50 * 60)]     # 60 s
    assert _bed_barges(bed) == 0


def test_keyboard_clatter_does_not_cut_a_character_off():
    """40 ms bursts at RMS 1500 every 300 ms. The shipped rule zeroed its
    silence run on every frame over threshold, so the clatter never got the
    900 ms of unbroken quiet that would have discarded it and accumulated,
    40 ms at a time, to the 250 ms that opens a turn."""
    rng = random.Random(4)
    bed = []
    for i in range(50 * 60):
        bed.append(noise(1500 if (i % 15) < 2 else 60, rng))
    assert _bed_barges(bed) == 0


def test_breathing_does_not_cut_a_character_off():
    """500 ms of low broadband every 4 s, which is sustained enough to clear the
    duration test and must be rejected on level."""
    rng = random.Random(5)
    bed = [noise(700 if (i % 200) < 25 else 40, rng) for i in range(50 * 60)]
    assert _bed_barges(bed) == 0


def test_the_participant_can_still_interrupt():
    """The other direction, and the one that matters more: an agent nobody can
    interrupt would be a worse bug than the one being fixed. Real speech, over a
    fan, must still cancel the character — promptly."""
    rng = random.Random(6)
    det = SilenceDetector()
    for _ in range(50 * 6):                    # 6 s of fan, so the floor is known
        det.feed(noise(600, rng))
    assert det.barge_in is False

    for i in range(50):                        # then somebody talks
        det.feed(speech_frame())
        if det.barge_in:
            assert (i + 1) * FRAME_MS <= 600, "barge-in must land inside 600 ms"
            break
    else:
        pytest.fail("real speech over a fan never cut the character off")


def test_a_quiet_room_behaves_exactly_as_before():
    """The adaptation must not move the bar in the room this study is actually
    run in. With digital silence behind it the floor is zero, so the threshold
    is the shipped fixed one and a turn still opens after min_speech_ms."""
    det = SilenceDetector()
    for _ in range(50 * 5):
        det.feed(QUIET)
    assert det.effective_threshold() == det.threshold
    marks = [det.feed(speech_frame()) for _ in range(20)]
    assert marks.count("speech_started") == 1
    # The first frame on which the accumulated speech reaches min_speech_ms,
    # which is where the shipped detector put it too.
    opened_at_ms = (marks.index("speech_started") + 1) * FRAME_MS
    assert opened_at_ms == 260 and opened_at_ms - FRAME_MS < det.min_speech_ms


def test_the_adaptation_can_be_switched_off():
    """One env var returns the shipped fixed-threshold behaviour exactly, which
    is what makes this safe to ship to a deployment that disagrees with it."""
    det = SilenceDetector(noise_margin=0)
    rng = random.Random(7)
    for _ in range(50 * 10):
        det.feed(noise(600, rng))
    assert det.effective_threshold() == det.threshold


def test_the_floor_can_never_make_a_participant_uninterruptible():
    """The failure mode of an adaptive threshold is an estimate that climbs
    until nothing clears it. However loud the room is measured to be, the bar
    stops at a level ordinary speech clears."""
    det = SilenceDetector()
    rng = random.Random(8)
    for _ in range(50 * 20):
        det.feed(noise(20000, rng))
    assert det.effective_threshold() <= det.max_threshold
    assert det.max_threshold < 6000


# --------------------------------------------------------------------------
# 2. The same decision, through the runner, which is where it is acted on.
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
    def __init__(self, frames=()):
        self.json = []
        self.binary = []
        self._frames = list(frames)

    async def send_json(self, payload):
        self.json.append(payload)

    async def send_bytes(self, payload):
        self.binary.append(payload)

    async def receive(self):
        if not self._frames:
            return {"type": "websocket.disconnect"}
        return {"type": "websocket.receive", "bytes": self._frames.pop(0)}

    def frames(self, type_):
        return [f for f in self.json if f.get("type") == type_]


class FakeRT:
    def __init__(self):
        self.ws = object()
        self.voice = "Puck"
        self.model = "fake"
        self.pending_input = 0
        self.autofire_active = False
        self.cancels = 0
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


def one_to_one_runner(frames):
    session = FakeSession("01_missed_deadlines")
    ws = FakeWS(frames)
    runner = rvs.RealtimeVoiceSessionRunner(session, ws)
    runner.rt = FakeRT()
    runner.room = None
    runner._speaking = True          # a character is talking right now
    return runner, session, ws


def in_a_loop(fn):
    def wrapper(*a, **kw):
        return asyncio.run(fn(*a, **kw))
    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = fn.__doc__
    return wrapper


@in_a_loop
async def test_room_noise_alone_never_reaches_the_page_as_an_interruption():
    """The whole mechanism, end to end on the server side: noise on the mic, a
    character speaking, and the frame that makes the browser drop the audio it
    has already been given must not be sent."""
    rng = random.Random(9)
    frames = []
    for i in range(50 * 12):                     # 12 s of typing over a fan
        frames.append(noise(1500 if (i % 15) < 2 else 500, rng))
    runner, session, ws = one_to_one_runner(frames)
    await runner._client_to_model()
    assert ws.frames("assistant_interrupted") == []
    assert runner.rt.cancels == 0
    assert runner._speaking is True


@in_a_loop
async def test_a_participant_who_speaks_still_stops_the_character():
    """And the same path, with a person actually talking."""
    runner, session, ws = one_to_one_runner([speech_frame()] * 40)
    await runner._client_to_model()
    assert ws.frames("assistant_interrupted"), "the participant could not interrupt"
    assert runner.rt.cancels == 1
    assert runner._speaking is False


# --------------------------------------------------------------------------
# 3. Is this line's audio enough audio for this line's words?
# --------------------------------------------------------------------------

def test_a_half_delivered_line_is_recognised():
    """The shape measured live 21 times in 191 gateway replies: a complete
    sentence and under a second of voice."""
    bad = turn_audio.shortfall(
        "So are we just talking about the sprint or is there anything else", 920)
    assert bad and bad["words"] == 13
    assert bad["delivered_fraction"] < 0.25


def test_an_ordinary_line_is_not_flagged():
    """A detector that fires on healthy turns teaches people to ignore it."""
    assert turn_audio.shortfall("Thanks for having me. I appreciate it.", 2400) is None
    assert turn_audio.shortfall("I understand there were some misses.", 1900) is None


def test_short_lines_and_silent_turns_are_left_alone():
    """A one-word beat carries no useful ratio, and a turn with no audio at all
    is a different failure that is already reported elsewhere."""
    assert turn_audio.shortfall("Right.", 100) is None
    assert turn_audio.shortfall("Fine. I am listening.", 0) is None


def test_the_scan_reads_an_encounter_and_skips_what_it_should():
    """The post-hoc half, for tools/encounter_health.py: an encounter full of
    half-delivered turns is not usable as data, whatever else is right about it.

    Interrupted turns are skipped because the participant talking over a
    character is the behaviour two of the scenarios exist to score, and turns
    recorded before `audio_ms` existed are skipped rather than condemned on a
    field they predate.
    """
    events = [
        {"type": "session_start"},
        {"type": "assistant_turn", "agent_id": "a", "audio_ms": 920,
         "text": "So are we just talking about the sprint or is there anything else"},
        {"type": "assistant_turn", "agent_id": "b", "audio_ms": 200,
         "interrupted": True,
         "text": "I hear your concern and I am aware of the misses I am committed to"},
        {"type": "assistant_turn", "agent_id": "c",
         "text": "An archived turn with no audio accounting at all, recorded before"},
        {"type": "assistant_turn", "agent_id": "d", "audio_ms": 2400,
         "text": "Thanks for having me. I appreciate it."},
    ]
    found = turn_audio.scan_events(events)
    assert [f["agent_id"] for f in found] == ["a"]


@in_a_loop
async def test_a_turn_records_the_audio_the_participant_was_actually_sent():
    """`audio_ms` on the turn is the whole instrument: without it the
    comparison above has nothing to run on, live or afterwards."""
    session = FakeSession("01_missed_deadlines")
    ws = FakeWS()
    runner = rvs.RealtimeVoiceSessionRunner(session, ws)
    runner.rt = FakeRT()
    await runner._finalize_turn(
        runner.agent_id, runner.agent, runner.rt,
        ["Thanks for having me. I appreciate it."], None,
        audio_bytes=2400 * 32,          # 2.4 s at the client's 16 kHz PCM16
    )
    turns = session.store.of("assistant_turn")
    assert len(turns) == 1 and turns[0]["audio_ms"] == 2400
    assert session.store.of("agent_audio_short") == []


@in_a_loop
async def test_a_turn_whose_audio_stopped_early_says_so_in_the_record():
    """The one row that would have told the researcher what they were hearing,
    and which side of the gateway lost it."""
    session = FakeSession("01_missed_deadlines")
    ws = FakeWS()
    runner = rvs.RealtimeVoiceSessionRunner(session, ws)
    runner.rt = FakeRT()
    line = "So are we just talking about the sprint or is there anything else"
    await runner._finalize_turn(
        runner.agent_id, runner.agent, runner.rt, [line], None,
        audio_bytes=920 * 32, audio_unterminated=True,
    )
    short = session.store.of("agent_audio_short")
    assert len(short) == 1
    assert short[0]["audio_ms"] == 920
    assert short[0]["gateway_abandoned_audio"] is True
    assert session.store.of("assistant_turn")[0]["audio_ms"] == 920


# --------------------------------------------------------------------------
# 4. A room must not amputate a reply the participant is already hearing.
# --------------------------------------------------------------------------

class FakeRoom:
    def __init__(self, speaking=None):
        self.speaking = speaking
        self.heard = []

    async def hear(self, pcm, exclude=None):
        self.heard.append(pcm)

    def session_for(self, agent_id):
        return None


def group_runner():
    session = FakeSession("S4A")
    ws = FakeWS()
    runner = rvs.RealtimeVoiceSessionRunner(session, ws)
    return runner, session, ws


AUDIO = b"\x01\x02" * 800          # 50 ms at 16 kHz PCM16


@in_a_loop
async def test_a_reply_the_participant_is_hearing_is_not_cut_off_by_the_floor():
    """Measured live: 2 of 19 relayed group turns had the floor move while the
    character was still speaking, and the pump then dropped the rest of the
    reply — mid-word, with the tail missing from the record as well as from the
    room. One of them was written to the record as "I just want to".

    The participant is already listening to this character. Moving the floor
    cannot un-hear that; it can only cut the sentence in half.
    """
    runner, session, ws = group_runner()
    agent = runner._resolve_agents()[0]
    room = FakeRoom(speaking=agent.id)
    runner.room = room
    rt = FakeRT()
    rt.feed({"type": "agent_audio", "pcm": AUDIO})
    pump = asyncio.ensure_future(runner._pump_member(agent, rt))
    for _ in range(20):
        await asyncio.sleep(0)
    assert ws.binary == [AUDIO], "the head of the reply was relayed"

    room.speaking = "somebody_else"          # the floor moves mid-reply
    rt.feed({"type": "agent_audio", "pcm": AUDIO})
    for _ in range(20):
        await asyncio.sleep(0)
    rt.end()
    await pump

    assert ws.binary == [AUDIO, AUDIO], "the tail of the reply was amputated"
    assert session.store.of("unsolicited_response_suppressed") == []
    assert rt.cancels == 0


@in_a_loop
async def test_the_head_of_a_reply_that_gains_the_floor_survives():
    """The mirror image, also measured live (g3 s_1789350511_879452): the head
    of a reply was suppressed, the floor then reached that same reply, 1.5 s of
    the character's voice played — and the turn was recorded as text="" with
    transcript_missing True, which is a flag that tells a rater "the audio
    played and its text was lost" hung on a line the runner was holding."""
    runner, session, ws = group_runner()
    agent = runner._resolve_agents()[0]
    room = FakeRoom(speaking="somebody_else")
    runner.room = room
    rt = FakeRT()
    rt.feed({"type": "agent_transcript_delta", "text": "I just"})
    pump = asyncio.ensure_future(runner._pump_member(agent, rt))
    for _ in range(20):
        await asyncio.sleep(0)
    assert session.store.of("unsolicited_response_suppressed"), "head suppressed"

    room.speaking = agent.id                  # the floor arrives
    rt.feed({"type": "agent_audio", "pcm": AUDIO})
    rt.feed({"type": "agent_transcript_delta", "text": " want to say"})
    rt.feed({"type": "response_done"})
    for _ in range(30):
        await asyncio.sleep(0)
    rt.end()
    await pump
    if runner._finalize_tasks:
        await asyncio.wait(list(runner._finalize_tasks), timeout=5)

    turns = session.store.of("assistant_turn")
    assert len(turns) == 1, turns
    assert turns[0]["text"].startswith("I just want to say")
    assert turns[0]["transcript_missing"] is False


# --------------------------------------------------------------------------
# 5. The browser, which is where most of the audio was actually lost.
# --------------------------------------------------------------------------

DRAIN_HARNESS = r"""/* static/v2.html hands its audio to the clock, not to the speaker.
   playPcmChunk schedules every chunk on one monotonic `playbackTime` pointer,
   which runs ahead of real time because the gateway delivers a reply many times
   faster than a person says it. So when the encounter ends, the browser is
   still HOLDING seconds of the last line.

   endSession used to destroy that backlog on the spot: playEl.pause() is
   synchronous and AudioContext.close() stops every source scheduled ahead of
   it. Measured on the live runs this harness was written beside, the unplayed
   backlog at the moment the server calls a turn finished ran to 8.3 s, median
   0.7 s — and `encounter_complete` is sent from the same finally block that
   closes a turn.

   Drives the page's own endSession and asserts that what it was handed is
   still playing afterwards. */
'use strict';
const path = require('path');
const { bootV2, vm } = require(path.join(__dirname, 'stub.js'));

(async () => {
  const b = bootV2(process.argv[2], '?session=x');
  const set = (code) => vm.runInContext(code, b.ctx);
  const get = (code) => vm.runInContext(code, b.ctx);

  // A minimal WebAudio graph: enough for the page's scheduler to be real.
  set(`
    __closed = 0; __paused = 0; __stopped = 0;
    __now = 0;
    audioCtx = {
      get currentTime() { return __now; },
      sampleRate: 16000,
      state: 'running',
      destination: {},
      createBuffer(ch, len, rate) {
        return { duration: len / rate, length: len, sampleRate: rate,
                 getChannelData: () => new Float32Array(len) };
      },
      createBufferSource() {
        return { buffer: null, connect() {}, start(t) { this.at = t; },
                 stop() { __stopped++; }, onended: null };
      },
      close() { __closed++; this.state = 'closed'; },
      addEventListener() {},
    };
    playDest = { stream: {} };
    playEl = { pause() { __paused++; }, srcObject: {}, paused: false, currentTime: 1 };
    playElUsable = true;
    playbackTime = 0;
    started = true;
    sessionId = 's_test';
    sessionMode = 'single';
    // One agent turn, mid-delivery, with its caption half revealed.
    currentTurn = { agent_id: 'a', el: null, fullText: 'a full sentence that is still being spoken',
                    shown: 'a full', startCtx: null, endCtx: null, done: false,
                    latency: null, metaAdded: false, sources: [] };
    speechQueue = [currentTurn];
    __turn = currentTurn;
  `);

  // Six seconds of audio, delivered in a burst, as the gateway actually does.
  set(`
    for (let i = 0; i < 60; i++) playPcmChunk(new Int16Array(1600).buffer);
  `);
  const backlog = get('playbackTime - audioCtx.currentTime');
  if (!(backlog > 5)) throw new Error('harness: no backlog to lose, got ' + backlog);

  // The encounter ends inside that backlog, which is where it always ends.
  set('endSession({ debrief: false })');
  await b.clock.advance(1);

  if (get('__closed') !== 0) throw new Error('the audio context was closed on top of ' + backlog + 's of unplayed audio');
  if (get('__paused') !== 0) throw new Error('playback was paused on top of ' + backlog + 's of unplayed audio');
  if (get('__stopped') !== 0) throw new Error('scheduled audio was stopped at the end of the encounter');

  // The caption is not left frozen half-way through a sentence the participant
  // can hear finishing.
  const shown = get('__turn.shown');
  if (!shown || shown.length < 10) throw new Error('caption frozen at: ' + shown);

  // And it is bounded: the graph is released once the backlog has played.
  await b.clock.advance(20000);
  if (get('__closed') !== 1) throw new Error('the audio graph was never released');
  if (get('__paused') !== 1) throw new Error('playback was never stopped');

  console.log('DRAIN OK backlog=' + backlog.toFixed(2) + 's');
})().catch(e => { console.error('FAIL: ' + ((e && e.stack) || e)); process.exit(1); });
"""


def _node():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed; the page harness needs it")
    return node


def test_the_end_of_an_encounter_does_not_cut_off_the_last_line(tmp_path):
    """The single most reliable instance of the reported symptom: it happened
    at the end of every encounter, to whatever was still playing."""
    from test_client_blockers import DOM_STUB      # the shared thin browser

    (tmp_path / "stub.js").write_text(DOM_STUB, encoding="utf-8")
    harness = tmp_path / "harness.js"
    harness.write_text(DRAIN_HARNESS, encoding="utf-8")
    proc = subprocess.run([_node(), str(harness), str(V2)],
                          capture_output=True, text=True, encoding="utf-8",
                          timeout=180)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "DRAIN OK" in proc.stdout


def test_the_drain_is_bounded_in_the_page_itself():
    """A page that waits forever for audio that is never coming is a tab that
    never lets go of the camera. The bound is a constant, and it is small."""
    src = V2.read_text(encoding="utf-8")
    assert "AUDIO_DRAIN_MAX_S" in src
    line = [l for l in src.splitlines() if "const AUDIO_DRAIN_MAX_S" in l]
    assert len(line) == 1
    seconds = int(line[0].split("=")[1].strip().rstrip(";"))
    assert 2 <= seconds <= 30

