"""Participant-turn integrity: issues #21 and #24, pipeline 2026-09-23c.

What reached the record as the participant's words, and what should not have:

  * the silence watchdog's own pad, transcribed ("Thank you very much." in
    S2A, 2026-09-23), recorded as a participant turn and rated by steering;
  * transcripts of near-silence ("." / "Um...") recorded as said;
  * every commit holding everything the microphone sent since the last one
    (up to 44 s), because nothing ever cleared the gateway's buffer;
  * "I have a feeling that we're not on the same page right now." deleted as
    a near-duplicate of "Okay, I think we have..." (3 of 5 words);
  * a room turn queued behind a held floor routed on nothing;
  * a replayed line able to become a second copy of one participant turn.

Everything here is offline. The two facts about the wire these tests rely on
were measured on gpt-realtime-2.1 through the repo's own send_audio on
2026-09-23 (scratchpad impl/P2/probe_items.py): input_audio_buffer.committed
carries an item_id and the transcript of that commit carries the same one, and
input_audio_buffer.clear is answered with input_audio_buffer.cleared and the
cleared audio does not appear in the next commit's transcript.
"""
from __future__ import annotations

import asyncio
import functools
import json
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server import director as director_mod  # noqa: E402
from server import encounter_record  # noqa: E402
from server import llm  # noqa: E402
from server import realtime_voice_session as rvs  # noqa: E402
from server import steering as steering_mod  # noqa: E402
from server.group_room import GroupRoom  # noqa: E402
from server.scenarios import load_scenario  # noqa: E402
from server.voice import realtime as R  # noqa: E402

GPT = "gpt-realtime-2.1"                 # buffer is ours: VAD off, we commit
GEMINI = "nto.gemini-live-2.5-flash"     # the gateway's VAD commits
NATIVE = "nto.gemini-live-2.5-flash-native-audio"   # a room with a second transcriber

FRAME = 320                              # 20 ms at 16 kHz, as the worklet sends
LOUD = b"\x00\x20" * FRAME               # RMS 8192
QUIET = b"\x00\x00" * FRAME


def in_a_loop(fn):
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        return asyncio.run(fn(*a, **kw))
    return wrapper


# --------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------

class WireWS:
    """The gateway's end of one socket: keeps every frame the bridge sends and
    hands events() whatever the test feeds it."""

    def __init__(self):
        self.sent: list = []
        self.q: asyncio.Queue = asyncio.Queue()

    async def send(self, raw):
        self.sent.append(json.loads(raw))

    async def recv(self):
        return await self.q.get()

    async def close(self):
        pass

    def feed(self, **ev):
        self.q.put_nowait(json.dumps(ev))

    def types(self):
        return [m["type"] for m in self.sent]


def bridge(model=GPT, *, bar=500):
    rt = R.RealtimeVoiceSession("x", model=model, voice="", api_key="dummy")
    rt.ws = WireWS()
    if bar is not None:
        rt.voiced_bar = lambda: bar
    return rt


async def send_ms(rt, frame, ms):
    for _ in range(ms // 20):
        await rt.send_audio(frame)


async def next_transcript(rt, timeout=2.0):
    agen = rt.events()
    try:
        while True:
            ev = await asyncio.wait_for(agen.__anext__(), timeout)
            if ev["type"] == "user_transcript":
                return ev
    finally:
        await agen.aclose()


class FakeStore:
    def __init__(self):
        self.events = []
        self.started_at = time.time()

    def event(self, type_, **fields):
        self.events.append(dict(type=type_, **fields))

    def append_user_audio(self, pcm):
        pass

    def append_assistant_audio(self, pcm, agent_id=None):
        pass

    def of(self, type_):
        return [e for e in self.events if e["type"] == type_]


class FakeEngine:
    def __init__(self, agent):
        self.agent = agent

    def _system_prompt(self, branches, note, group=False):
        return f"SYSTEM PROMPT for {self.agent.id}"


class RecordingDirector:
    model = "fake-director"

    def __init__(self):
        self.calls = []

    async def route(self, history, text):
        self.calls.append((list(history), text))
        return []


class FakeSession:
    """Session's surface as the runner uses it; append_user is the real one's
    shape, flags included."""

    def __init__(self, scenario_id):
        self.scenario = load_scenario(scenario_id, "p_test")
        self.is_group = self.scenario.mode == "group"
        self.engines = {a.id: FakeEngine(a) for a in self.scenario.cast}
        self.store = FakeStore()
        self.director = RecordingDirector()
        self.triggered_branches = []
        self.shared_history = []
        self.steering_log = []

    def append_user(self, text, *, low_confidence=False, names_cast=False,
                    to_director=None):
        entry = {"speaker": "user", "text": text}
        if low_confidence:
            entry.update(low_confidence=True, names_cast=bool(names_cast),
                         to_director=(bool(names_cast) if to_director is None
                                      else bool(to_director)))
        self.shared_history.append(entry)

    def append_agent(self, agent_id, text):
        self.shared_history.append({"speaker": agent_id, "text": text})

    async def broadcast(self, payload):
        pass

    async def auto_steer(self, *, delivered=None):
        pass


class PageWS:
    """The browser's end: fixed inbound frames, then a disconnect."""

    def __init__(self, frames=()):
        self._frames = list(frames)
        self.json = []

    async def receive(self):
        if self._frames:
            return {"type": "websocket.receive", "bytes": self._frames.pop(0)}
        return {"type": "websocket.disconnect"}

    async def send_json(self, payload):
        self.json.append(payload)

    async def send_bytes(self, payload):
        pass

    def frames(self, type_):
        return [f for f in self.json if f.get("type") == type_]


def runner_for(scenario_id, frames=()):
    session = FakeSession(scenario_id)
    ws = PageWS(frames)
    return rvs.RealtimeVoiceSessionRunner(session, ws), session, ws


# --------------------------------------------------------------------------
# 1. Probe pads (plan Phase 1 item 3)
# --------------------------------------------------------------------------

@in_a_loop
async def test_a_probe_clears_the_buffer_before_its_pad_and_tags_its_commit():
    rt = bridge()
    await send_ms(rt, QUIET, 2000)            # twelve seconds of nobody, shortened
    info = await rt.commit_probe(b"\x00" * 3200)
    assert info["cleared"] is True
    assert info["buffer_ms"] == 2000 and info["buffer_voiced_ms"] == 0
    types = rt.ws.types()
    clear_at = types.index("input_audio_buffer.clear")
    assert types.count("input_audio_buffer.clear") == 1
    assert types[clear_at + 1:].count("input_audio_buffer.append") >= 1
    assert types[-1] == "input_audio_buffer.commit"
    assert "input_audio_buffer.append" not in types[clear_at + 1:][-1:]

    rt.ws.feed(type="input_audio_buffer.committed", item_id="item_P")
    rt.ws.feed(type="conversation.item.input_audio_transcription.completed",
               item_id="item_P", transcript="Thank you very much.")
    ev = await next_transcript(rt)
    assert ev["probe"] is True and ev["item_id"] == "item_P"
    assert ev["voiced_ms"] == 0


@in_a_loop
async def test_a_probe_never_clears_the_participants_uncommitted_words():
    """A turn whose commit returned early (a reply was in flight) is still in
    the buffer when the watchdog fires; clearing it would lose it. It goes to
    the gateway as the participant's turn instead, untagged."""
    rt = bridge()
    await send_ms(rt, LOUD, 400)
    info = await rt.commit_probe(b"\x00" * 3200)
    assert info["cleared"] is False and info["buffer_voiced_ms"] == 400
    assert "input_audio_buffer.clear" not in rt.ws.types()
    rt.ws.feed(type="input_audio_buffer.committed", item_id="item_T")
    rt.ws.feed(type="conversation.item.input_audio_transcription.completed",
               item_id="item_T", transcript="I did say something.")
    ev = await next_transcript(rt)
    assert ev["probe"] is False and ev["voiced_ms"] == 400


@in_a_loop
async def test_a_probe_that_commit_turn_would_refuse_sends_nothing():
    rt = bridge()
    await send_ms(rt, QUIET, 200)
    rt.ws.sent.clear()
    rt._response_active = True
    rt._response_started_at = time.time()
    assert await rt.commit_probe(b"\x00" * 3200) is None
    assert rt.ws.sent == [], "nothing may be cleared when nothing will be committed"


@in_a_loop
async def test_on_a_route_whose_gateway_commits_a_probe_is_what_it_was():
    rt = bridge(GEMINI)
    await send_ms(rt, QUIET, 200)
    info = await rt.commit_probe(b"\x00" * 3200)
    assert info["cleared"] is False
    assert "input_audio_buffer.clear" not in rt.ws.types()
    assert rt._commit_tags == [], "no tags where the gateway interleaves commits"
    assert not rt.input_restart_due()


@in_a_loop
async def test_the_probe_pads_transcript_never_becomes_a_participant_turn():
    runner, session, ws = runner_for("S2A")
    await runner._record_user_turn("Thank you very much.", item_id="item_P",
                                   probe=True, voiced_ms=0)
    (sup,) = session.store.of("user_turn_suppressed")
    assert sup["reason"] == "probe_pad"
    assert sup["text"] == "Thank you very much." and sup["item_id"] == "item_P"
    assert not session.store.of("user_turn")
    assert not ws.frames("user_transcript"), "no caption"
    assert session.shared_history == [], "no steering or director input"
    assert runner._last_user_text == ""
    assert runner._unrouted_user_texts == []
    assert runner._transcripts_arrived == 0, "a probe is not a participant transcript"


@in_a_loop
async def test_the_watchdog_probe_goes_through_commit_probe(monkeypatch):
    runner, session, _ = runner_for("S2A")
    rt = bridge()
    runner.rt = rt
    await send_ms(rt, QUIET, 1000)
    await runner._probe_commit()
    assert "input_audio_buffer.clear" in rt.ws.types()
    (ev,) = session.store.of("input_buffer_cleared")
    assert ev["reason"] == "probe" and ev["discarded_ms"] == 1000
    assert rt._commit_tags[-1]["probe"] is True


# --------------------------------------------------------------------------
# 2. Transcript <-> commit bookkeeping
# --------------------------------------------------------------------------

@in_a_loop
async def test_transcripts_are_matched_to_their_own_commit_by_item_id():
    rt = bridge()
    await send_ms(rt, LOUD, 200)
    await rt.commit_input()
    await send_ms(rt, LOUD, 1000)
    await rt.commit_input()
    rt.ws.feed(type="input_audio_buffer.committed", item_id="a")
    rt.ws.feed(type="input_audio_buffer.committed", item_id="b")
    rt.ws.feed(type="conversation.item.input_audio_transcription.completed",
               item_id="b", transcript="The long one.")
    rt.ws.feed(type="conversation.item.input_audio_transcription.completed",
               item_id="a", transcript="Short.")
    agen = rt.events()
    got = []
    while len(got) < 2:
        ev = await asyncio.wait_for(agen.__anext__(), 2)
        if ev["type"] == "user_transcript":
            got.append((ev["text"], ev["voiced_ms"]))
    await agen.aclose()
    assert got == [("The long one.", 1000), ("Short.", 200)]


@in_a_loop
async def test_without_committed_frames_transcripts_pair_in_commit_order():
    rt = bridge()
    await send_ms(rt, LOUD, 300)
    await rt.commit_input()
    rt.ws.feed(type="conversation.item.input_audio_transcription.completed",
               item_id="x", transcript="Hello.")
    ev = await next_transcript(rt)
    assert ev["voiced_ms"] == 300


@in_a_loop
async def test_a_refused_commit_does_not_hand_its_tag_to_the_next_one():
    rt = bridge()
    await rt.commit_input()                     # refused below: empty
    await send_ms(rt, LOUD, 400)
    await rt.commit_input()
    rt.ws.feed(type="error", error={"code": "input_audio_buffer_commit_empty",
                                    "message": "buffer too small"})
    rt.ws.feed(type="input_audio_buffer.committed", item_id="real")
    rt.ws.feed(type="conversation.item.input_audio_transcription.completed",
               item_id="real", transcript="Real words.")
    ev = await next_transcript(rt)
    assert ev["voiced_ms"] == 400


@in_a_loop
async def test_a_replayed_line_is_tagged_as_a_replay():
    rt = bridge()
    assert await rt.replay_input(LOUD * 25)
    rt.ws.feed(type="input_audio_buffer.committed", item_id="r")
    rt.ws.feed(type="conversation.item.input_audio_transcription.completed",
               item_id="r", transcript="What I said before.")
    ev = await next_transcript(rt)
    assert ev["replay"] is True and ev["voiced_ms"] == 500


@in_a_loop
async def test_awaiting_transcript_is_true_only_while_a_participant_commit_is_owed():
    rt = bridge()
    assert not rt.awaiting_transcript(6)
    await send_ms(rt, LOUD, 300)
    await rt.commit_input()
    assert rt.awaiting_transcript(6)
    rt.ws.feed(type="input_audio_buffer.committed", item_id="i")
    rt.ws.feed(type="conversation.item.input_audio_transcription.completed",
               item_id="i", transcript="Done.")
    await next_transcript(rt)
    assert not rt.awaiting_transcript(6)
    await rt.commit_input(probe=True)
    assert not rt.awaiting_transcript(6), "a probe pad is nobody's words"


def test_voiced_audio_is_counted_against_the_bar_in_force():
    async def go():
        rt = bridge(bar=10000)                  # LOUD (8192) is under this bar
        await send_ms(rt, LOUD, 400)
        assert rt._voiced_ms == 0
        rt.voiced_bar = lambda: 500
        await send_ms(rt, LOUD, 400)
        assert rt._voiced_ms == 400
    asyncio.run(go())


# --------------------------------------------------------------------------
# 3. The plausibility gate in _record_user_turn (plan Phase 1 item 4)
# --------------------------------------------------------------------------

@pytest.mark.parametrize("text", [".", "...", "Um...", "Mhm.", "Hmm, uh."])
def test_punctuation_or_filler_over_near_silence_is_suppressed_with_its_text(text):
    runner, session, ws = runner_for("S2A")
    asyncio.run(runner._record_user_turn(text, item_id="i1", voiced_ms=40))
    (sup,) = session.store.of("user_turn_suppressed")
    assert sup["reason"] == "no_speech" and sup["text"] == text
    assert sup["voiced_ms"] == 40
    assert not session.store.of("user_turn") and not ws.frames("user_transcript")


def test_words_over_near_silence_are_kept_but_low_confidence():
    """No stock-phrase list: "Thank you very much." is also something a
    participant says. It is kept, captioned and recorded, and held back from
    steering and the director. (Over 550 ms of voice: 7.3 words per voiced
    second, under the rate gate's 8. Over 20 ms, as this test used to say,
    it is the rate gate's since pipeline 2026-09-24a; see
    tests/test_voice_and_rate_gates.py.)"""
    runner, session, ws = runner_for("S2A")
    asyncio.run(runner._record_user_turn("Thank you very much.", voiced_ms=550))
    (turn,) = session.store.of("user_turn")
    assert turn["low_confidence"] is True and turn["voiced_ms"] == 550
    assert ws.frames("user_transcript"), "the caption still shows"
    (entry,) = session.shared_history
    assert entry["low_confidence"] is True and entry["names_cast"] is False
    assert runner._unrouted_user_texts == []
    assert runner._last_user_low_confidence is True
    assert "Thank you" not in steering_mod._format_transcript(session.shared_history, {})
    assert "Thank you" not in director_mod._format_transcript(session.shared_history, {})


def test_a_filler_with_real_voice_under_it_is_a_turn():
    runner, session, _ = runner_for("S2A")
    asyncio.run(runner._record_user_turn("Mhm.", voiced_ms=700))
    (turn,) = session.store.of("user_turn")
    assert turn["low_confidence"] is False
    assert not session.store.of("user_turn_suppressed")


def test_a_short_turn_that_names_a_cast_member_still_reaches_the_director():
    runner, session, _ = runner_for("S4A")
    name = runner._resolve_agents()[1].name
    asyncio.run(runner._record_user_turn(f"{name}?", voiced_ms=400))
    (turn,) = session.store.of("user_turn")
    assert turn["low_confidence"] is True
    (entry,) = session.shared_history
    assert entry["names_cast"] is True
    assert runner._unrouted_user_texts == [f"{name}?"]
    assert f"{name}?" in director_mod._format_transcript(session.shared_history, {})
    assert f"{name}?" not in steering_mod._format_transcript(session.shared_history, {})


def test_a_long_turn_is_an_ordinary_turn():
    runner, session, _ = runner_for("S2A")
    asyncio.run(runner._record_user_turn("I need more context on the deadline.",
                                         voiced_ms=1800))
    (turn,) = session.store.of("user_turn")
    assert turn["low_confidence"] is False
    assert "low_confidence" not in session.shared_history[0]


def test_where_voiced_audio_cannot_be_counted_no_gate_applies():
    """Gemini's own commits carry no tag: voiced_ms is None, and the voice
    gates neither drop nor tag, which is the behaviour before they existed.
    (A line with no letter or digit at all is dropped on every route since
    24c - that rule does not need a voiced count - so this uses a word.)"""
    runner, session, _ = runner_for("S2A")
    asyncio.run(runner._record_user_turn("Okay."))
    (turn,) = session.store.of("user_turn")
    assert turn["low_confidence"] is False and turn["voiced_ms"] is None


def test_the_gate_thresholds_are_knobs(monkeypatch):
    monkeypatch.setenv("PARTICIPANT_MIN_VOICED_MS", "0")
    monkeypatch.setenv("PARTICIPANT_DROP_VOICED_MS", "-1")
    monkeypatch.setenv("PARTICIPANT_DROP_WORDLESS", "0")   # 24c's rule, off for this test
    runner, session, _ = runner_for("S2A")
    asyncio.run(runner._record_user_turn(".", voiced_ms=0))
    (turn,) = session.store.of("user_turn")
    assert turn["low_confidence"] is False
    gate = llm.provenance(GPT)["turn_gate"]
    assert gate["participant_min_voiced_ms"] == 0
    assert gate["participant_drop_voiced_ms"] == -1


def test_the_gate_is_in_provenance_with_its_defaults():
    prov = llm.provenance(GPT)
    # At least this package's stamp; a later package moves it on.
    assert prov["pipeline_version"] >= "2026-09-23c"
    assert prov["room_pacing_version"] >= "2026-09-23b"
    assert prov["turn_gate"] == {
        "participant_min_voiced_ms": 600,
        "participant_drop_voiced_ms": 80,
        "input_preroll_ms": 600,
        "input_buffer_restart": True,
        "participant_dedupe_overlap": 0.9,
        "room_merge_queued_turns": True,
        # Review fixes (pipeline 2026-09-23g): the director rule is a knob,
        # and whether a room on this model runs the near-duplicate filter at
        # all (not on gpt, whose scribe is the only transcriber).
        "participant_low_confidence_director": "named",
        "room_dedupe_second_source": False,
        # Voice and rate gates (pipeline 2026-09-24a).
        "participant_commit_min_voiced_ms": 300,
        "participant_max_words_per_voiced_s": 16.0,
        "participant_rate_gate_max_voiced_ms": 1500,
        # 2026-09-24b (P6 review): over the voiced span, default 16.
        "participant_rate_over": "voiced_span",
    }


def test_low_confidence_reaches_the_record(tmp_path):
    sdir = tmp_path / "s_x"
    sdir.mkdir()
    events = [
        {"t": 0.0, "type": "session_start"},
        {"t": 5.0, "type": "user_turn", "text": "Okay.", "channel": "voice",
         "script_mismatch": False, "low_confidence": True, "voiced_ms": 320},
        {"t": 9.0, "type": "user_turn", "text": "That works for me.",
         "channel": "voice", "script_mismatch": False,
         "low_confidence": False, "voiced_ms": 1400},
    ]
    (sdir / "events.jsonl").write_text(
        "".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")
    record = encounter_record.build(sdir)
    turns = [t for t in record["transcript"] if t["role"] == "participant"]
    assert [t["low_confidence"] for t in turns] == [True, False]
    assert [t["voiced_ms"] for t in turns] == [320, 1400]


# --------------------------------------------------------------------------
# 4. Limit what each commit holds (plan Phase 2)
# --------------------------------------------------------------------------

@in_a_loop
async def test_restart_is_once_per_commit_and_resends_the_preroll():
    rt = bridge()
    assert rt.input_restart_due(), "a fresh socket holds nobody's words"
    await send_ms(rt, QUIET, 3000)
    gone = await rt.restart_input(QUIET * 30)          # 600 ms
    assert gone == {"buffer_ms": 3000, "buffer_voiced_ms": 0,
                    "resent_ms": 600, "resent_voiced_ms": 0}
    types = rt.ws.types()
    at = types.index("input_audio_buffer.clear")
    assert types[at + 1:] == ["input_audio_buffer.append"] * 6   # 100 ms pieces
    assert abs(rt.pending_input - 24000 * 2 * 6 // 10) <= 4
    assert not rt.input_restart_due()
    await rt.commit_input()
    assert rt.input_restart_due()


@in_a_loop
async def test_an_early_returning_commit_leaves_nothing_to_restart():
    """commit_turn returns early while a reply is in flight, so the
    participant's words stay uncommitted; a later speech_started must not
    clear them."""
    rt = bridge()
    await rt.restart_input(b"")
    await send_ms(rt, LOUD, 400)
    rt._response_active = True
    rt._response_started_at = time.time()
    await rt.commit_turn()
    assert rt.commits == 0
    assert not rt.input_restart_due()


def _speech_after(quiet_ms, speech_ms=400):
    return [QUIET] * (quiet_ms // 20) + [LOUD] * (speech_ms // 20)


@in_a_loop
async def test_the_first_speech_started_after_a_commit_clears_and_keeps_the_preroll():
    runner, session, ws = runner_for("S2A", _speech_after(3000))
    rt = bridge()
    runner.rt = rt
    await rt.commit_input()                # the previous participant turn
    rt.ws.sent.clear()
    await runner._client_to_model()
    types = rt.ws.types()
    assert types.count("input_audio_buffer.clear") == 1
    (ev,) = session.store.of("input_buffer_cleared")
    assert ev["reason"] == "speech_started" and ev["channel"] == "voice"
    assert ev["preroll_ms"] == 600
    # The 3 s of quiet plus the 240 ms of speech before the frame that tipped
    # the VAD were on the wire; the last 600 ms of it went straight back in,
    # and so did all of the voiced part.
    assert ev["buffer_ms"] == 3240
    assert ev["discarded_ms"] == 3240 - 600
    assert ev["discarded_voiced_ms"] == 0
    after = rt.ws.sent[types.index("input_audio_buffer.clear") + 1:]
    assert after and all(m["type"] == "input_audio_buffer.append" for m in after)


@in_a_loop
async def test_no_clear_while_a_turn_end_is_being_confirmed():
    """The withdrawn-turn-end case: the first half of the thought is in the
    buffer, uncommitted, and the speech now starting continues it."""
    runner, session, _ = runner_for("S2A")
    rt = bridge()
    runner.rt = rt
    await rt.commit_input()
    runner._turn_end_pending_ms = 500.0
    await runner._restart_input_buffer()
    assert "input_audio_buffer.clear" not in rt.ws.types()
    runner._turn_end_pending_ms = None
    await runner._restart_input_buffer()
    await runner._restart_input_buffer()
    assert rt.ws.types().count("input_audio_buffer.clear") == 1


@in_a_loop
async def test_the_restart_can_be_switched_off(monkeypatch):
    monkeypatch.setenv("INPUT_BUFFER_RESTART", "0")
    runner, session, _ = runner_for("S2A")
    rt = bridge()
    runner.rt = rt
    await rt.commit_input()
    await runner._restart_input_buffer()
    assert "input_audio_buffer.clear" not in rt.ws.types()
    assert not session.store.of("input_buffer_cleared")


def _room(model, runner=None):
    agents = (runner._resolve_agents() if runner is not None
              else load_scenario("S4A", "p_test").cast)
    room = GroupRoom(agents, instructions_for=lambda a: "x",
                     voice_for=lambda a: "", tools=[], model=model)
    room.scribe = bridge(model)
    return room


@in_a_loop
async def test_the_rooms_scribe_restarts_once_per_commit():
    room = _room(GPT)
    await room.hear(QUIET * 100)
    gone = await room.restart_participant_buffer(QUIET * 20 + LOUD * 10)
    assert gone["buffer_ms"] == 2000 and gone["resent_voiced_ms"] == 200
    assert room._fanned_to_scribe == 30 * FRAME * 2
    assert room._scribe_heard_speech is True
    assert await room.restart_participant_buffer(QUIET * 30) is None
    await room.scribe.commit_input()
    assert await room.restart_participant_buffer(QUIET * 30) is not None
    assert room._scribe_heard_speech is False


@in_a_loop
async def test_no_scribe_restart_where_the_gateway_closes_turns():
    room = _room(GEMINI)
    assert await room.restart_participant_buffer(QUIET * 30) is None


@in_a_loop
async def test_no_scribe_restart_while_a_room_turn_awaits_its_transcript():
    runner, session, _ = runner_for("S4A")
    runner.room = _room(GPT, runner)
    runner._group_turn_waiting = True
    await runner._restart_input_buffer()
    assert "input_audio_buffer.clear" not in runner.room.scribe.ws.types()
    runner._group_turn_waiting = False
    await runner._restart_input_buffer()
    assert "input_audio_buffer.clear" in runner.room.scribe.ws.types()
    (ev,) = session.store.of("input_buffer_cleared")
    assert ev["channel"] == "scribe"


# --------------------------------------------------------------------------
# 5. The near-duplicate filter (issue #24)
# --------------------------------------------------------------------------

def _room_runner():
    runner, session, _ = runner_for("S4A")
    runner.room = object()
    runner.rt = type("RT", (), {"model": NATIVE})()
    return runner, session


def test_the_s4a_line_the_filter_deleted_is_kept():
    runner, session = _room_runner()
    asyncio.run(runner._record_user_turn("Okay, I think we have..."))
    asyncio.run(runner._record_user_turn(
        "I have a feeling that we're not on the same page right now."))
    assert not session.store.of("user_transcript_duplicate_dropped")
    assert [e["text"] for e in session.store.of("user_turn")] == [
        "Okay, I think we have...",
        "I have a feeling that we're not on the same page right now.",
    ]


def test_a_second_transcript_of_one_utterance_is_still_one_turn():
    runner, session = _room_runner()
    asyncio.run(runner._record_user_turn("We’re not ready to ship."))
    asyncio.run(runner._record_user_turn("I mean we're not ready to ship it"))
    (drop,) = session.store.of("user_transcript_duplicate_dropped")
    assert drop["matched"] == "We’re not ready to ship."
    assert drop["overlap"] == 1.0, "curly and straight apostrophes are one word"
    assert len(session.store.of("user_turn")) == 1


def test_the_overlap_bar_is_a_knob(monkeypatch):
    monkeypatch.setenv("PARTICIPANT_DEDUPE_OVERLAP", "0.6")
    runner, session = _room_runner()
    asyncio.run(runner._record_user_turn("Okay, I think we have..."))
    asyncio.run(runner._record_user_turn("I think we have a problem here today"))
    assert session.store.of("user_transcript_duplicate_dropped")


# --------------------------------------------------------------------------
# 6. replay_input must not make a second user_turn (plan Phase 2)
# --------------------------------------------------------------------------

def test_a_replays_transcript_after_the_originals_is_suppressed():
    runner, session, _ = runner_for("S2A")
    runner._replay_marker = runner._transcripts_arrived
    asyncio.run(runner._record_user_turn("Good morning.", item_id="orig"))
    asyncio.run(runner._record_user_turn("Good morning", item_id="rep", replay=True))
    assert [e["text"] for e in session.store.of("user_turn")] == ["Good morning."]
    (sup,) = session.store.of("user_turn_suppressed")
    assert sup["reason"] == "replay_duplicate" and sup["item_id"] == "rep"
    assert runner._replay_marker is None


def test_a_replays_transcript_is_the_turn_when_the_original_never_came():
    """The reconnect case: the old socket died with the line, so the replay's
    transcript is the only one there will be."""
    runner, session, _ = runner_for("S2A")
    runner._replay_marker = runner._transcripts_arrived
    asyncio.run(runner._record_user_turn("Good morning.", item_id="rep", replay=True))
    (turn,) = session.store.of("user_turn")
    assert turn["replay"] is True
    assert not session.store.of("user_turn_suppressed")


# --------------------------------------------------------------------------
# 7. Queued participant turns in a room (issue #24)
# --------------------------------------------------------------------------

def test_queued_utterances_are_merged_for_routing_and_each_kept():
    runner, session, _ = runner_for("S4A")
    runner.room = object()
    runner.rt = type("RT", (), {"model": GPT})()
    asyncio.run(runner._record_user_turn("Can I say something?"))
    asyncio.run(runner._record_user_turn("The pilot needs a real gate."))
    assert runner._take_unrouted() == (
        "Can I say something? The pilot needs a real gate.")
    assert runner._unrouted_user_texts == []
    (ev,) = session.store.of("user_turns_merged_for_routing")
    assert ev["count"] == 2 and ev["merged"] is True
    assert len(session.store.of("user_turn")) == 2


def test_merging_can_be_switched_off(monkeypatch):
    monkeypatch.setenv("ROOM_MERGE_QUEUED_TURNS", "0")
    runner, session, _ = runner_for("S4A")
    runner._unrouted_user_texts = ["first", "second"]
    assert runner._take_unrouted() == "second"
    assert session.store.of("user_turns_merged_for_routing")[0]["merged"] is False


class DeadMember:
    """A member whose grant fails at once, so a turn ends right after
    routing (the shape test_runner_blockers uses)."""

    def __init__(self, model):
        self.model = model
        self.ws = object()
        self.voice = ""
        self.send_failures = 0
        self.last_send_error = ""
        self.pending_input = 0
        self.autofire_active = False
        self.responding = False

    async def send_audio(self, pcm):
        raise ConnectionResetError("gone")

    async def update_instructions(self, instructions):
        return True

    async def commit_input(self, **kw):
        raise ConnectionResetError("gone")

    async def request_response(self):
        pass

    def clear_response_state(self):
        pass

    async def close(self):
        pass


def test_a_turn_queued_behind_the_floor_routes_at_once_on_everything_said(monkeypatch):
    """The a42 shape: utterances whose transcripts arrived while the floor
    was held. The old wait snapshotted the text after the floor came free,
    waited the whole ROUTE_TRANSCRIPT_WAIT for a change, and routed on ""."""
    monkeypatch.setenv("ROUTE_TRANSCRIPT_WAIT", "3")

    async def go():
        runner, session, _ = runner_for("S4A")
        room = _room(GPT, runner)
        for a in runner._resolve_agents():
            room.sessions[a.id] = DeadMember(GPT)
        runner.room = room
        runner.director = session.director
        session.append_agent("dan", "The date is locked.")
        runner._turn_end_arrivals = runner._transcripts_arrived
        await runner._record_user_turn("Okay, I think we have...")
        await runner._record_user_turn(
            "I have a feeling that we're not on the same page right now.")
        started = time.time()
        await asyncio.wait_for(runner._run_group_turn(), timeout=10)
        return session, time.time() - started

    session, elapsed = asyncio.run(go())
    assert elapsed < 1.5, "it waited for a transcript that had already come"
    (_, text) = session.director.calls[0]
    assert text == ("Okay, I think we have... I have a feeling that we're "
                    "not on the same page right now.")
    assert len(session.store.of("user_turn")) == 2


def test_the_routing_wait_holds_for_a_transcript_the_scribe_still_owes(monkeypatch):
    monkeypatch.setenv("ROUTE_TRANSCRIPT_WAIT", "3")

    async def go():
        runner, session, _ = runner_for("S4A")
        room = _room(GPT, runner)
        for a in runner._resolve_agents():
            room.sessions[a.id] = DeadMember(GPT)
        runner.room = room
        runner.director = session.director
        session.append_agent("dan", "The date is locked.")
        runner._turn_end_arrivals = runner._transcripts_arrived
        await runner._record_user_turn("First thought.")
        await room.scribe.commit_input()     # a second turn, not transcribed yet

        async def late():
            await asyncio.sleep(0.4)
            room.scribe._commit_tags.clear()
            await runner._record_user_turn("Second thought.")
        asyncio.ensure_future(late())
        await asyncio.wait_for(runner._run_group_turn(), timeout=10)
        return session

    session = asyncio.run(go())
    (_, text) = session.director.calls[0]
    assert text == "First thought. Second thought."


# --------------------------------------------------------------------------
# 8. The input rate follows the session's model (found by the P2 local run)
# --------------------------------------------------------------------------

def test_a_session_built_without_a_model_sends_at_its_models_rate(monkeypatch):
    """The runner and the room build sessions with no `model` argument. The
    rate used to be taken from that argument, so every one of them sent
    16 kHz: the gpt row's 24 kHz never reached a live session."""
    monkeypatch.setattr(R, "MODEL", GPT)
    rt = R.RealtimeVoiceSession("x", voice="", api_key="d")
    assert rt.input_rate == 24000
    rt.model = GEMINI                      # the room settles it afterwards
    assert rt.input_rate == R.CLIENT_RATE
    rt.input_rate = 16000                  # a probe that pins a rate still can
    rt.model = GPT
    assert rt.input_rate == 16000


def test_the_runners_own_session_resamples_on_gpt(monkeypatch):
    monkeypatch.setattr(R, "MODEL", GPT)
    runner, _, _ = runner_for("S2A")
    rt = runner._new_session(instructions="x", voice="")
    rt.ws = WireWS()
    asyncio.run(rt.send_audio(QUIET * 5))                  # 100 ms at 16 kHz
    assert abs(rt.pending_input - 4800) <= 2               # 100 ms at 24 kHz


def test_a_room_members_session_resamples_on_gpt():
    room = GroupRoom(load_scenario("S4A", "p_test").cast,
                     instructions_for=lambda a: "x", voice_for=lambda a: "",
                     tools=[], model=GPT)
    rt = room._new_session(instructions="x", voice="", tools=[])
    assert rt.input_rate == 24000
