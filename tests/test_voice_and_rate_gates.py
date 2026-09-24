"""Voice and rate gates, the proactive hand-off, the grant wait, and trigger
coverage: issues #21 and #24, pipeline 2026-09-24a, room pacing 2026-09-24a.

Built from the tester's S3A room s_1790217895_4025d8 (production, old image,
2026-09-23). Replaying its user_audio.wav through the branch's SilenceDetector
at its effective bar (500) and counting voiced frames as send_audio does gives
these voiced ms per transcribed line, since the last commit and, second, as
the branch counts them after restart_input's 600 ms pre-roll
(scratchpad p6/calib/calib2.py):

    "I'm not a cat. I'm a cat. I'm a cat. I'm a cat. I'm a cat."   340 / 340
    "Goodbye. Will Lego play more games in the future? ..."         400 / 300
    "Does that sound good?"                                         660 / 660
    "Thank you."                                                    680 / 380
    "Two."                                                          720 / 720
    "Great, how about TC?"                                         1080 / 980

The first two are phantoms (nobody said them) and the director answered
both; "Hmm. Okay. Okay." came in on no VAD turn at all. Everything is offline.
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
for p in (ROOT, ROOT / "tests"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from server import llm  # noqa: E402
from server import verify_record  # noqa: E402
from server.voice import realtime as R  # noqa: E402

from test_participant_turn_integrity import (  # noqa: E402
    GPT, LOUD, QUIET, DeadMember, _room, bridge, in_a_loop, next_transcript,
    runner_for, send_ms,
)
from test_final_record import (  # noqa: E402,F401  (fixture)
    _base_events, _check, _write_session, sessions_root,
)

CAT = "I'm not a cat. I'm a cat. I'm a cat. I'm a cat. I'm a cat."
GOODBYE = ("Goodbye. Will Lego play more games in the future? I'm waiting. "
           "I hope so. Thank you for watching! Bye!")

# (text, voiced_ms) exactly as measured; both counts where they differ.
PHANTOMS = [(CAT, 340), (GOODBYE, 400), (GOODBYE, 300)]
REAL_SHORT = [("Does that sound good?", 660), ("Thank you.", 680),
              ("Thank you.", 380), ("Two.", 720),
              ("Great, how about TC?", 1080), ("Great, how about TC?", 980)]


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    monkeypatch.setattr(R, "RECV_POLL_S", 0.02)
    monkeypatch.setenv("TRANSCRIPT_GRACE_SECONDS", "0.2")
    monkeypatch.delenv("AUTOFIRE_WAIT", raising=False)


# --------------------------------------------------------------------------
# 1. The pre-commit voice floor
# --------------------------------------------------------------------------

def test_the_floor_keeps_every_line_of_the_calibration_session():
    """The floor is for coughs and clicks. Every tester line, phantoms
    included, is at or over it, so the phantoms are the rate gate's."""
    floor = R.commit_min_voiced_ms()
    assert floor == 300
    for _, voiced in PHANTOMS + REAL_SHORT:
        assert not voiced < floor


@in_a_loop
async def test_a_one_to_one_turn_under_the_floor_is_not_committed():
    runner, session, _ = runner_for("S2A")
    rt = bridge()
    runner.rt = rt
    briefed = []

    async def brief(**kw):
        briefed.append(kw)
    runner._brief_next_beat = brief
    await rt.commit_input()               # the previous turn
    await send_ms(rt, QUIET, 400)
    await rt.restart_input(b"")
    await send_ms(rt, LOUD, 200)          # a cough
    await send_ms(rt, QUIET, 900)
    rt.ws.sent.clear()
    await runner._on_turn_ended()

    assert rt.ws.types() == ["input_audio_buffer.clear"]
    (ev,) = session.store.of("participant_turn_discarded")
    assert ev["reason"] == "too_little_voice" and ev["voiced_ms"] == 200
    assert ev["channel"] == "voice" and ev["min_voiced_ms"] == 300
    assert not briefed, "no beat is briefed for a turn that is not one"
    assert not rt.responding, "no reply was asked for"
    # The restart is re-armed: the next speech_started starts clean.
    assert rt.input_restart_due()
    assert rt.voiced_since_commit() == 0


@in_a_loop
async def test_the_phantoms_voice_is_committed_and_left_to_the_rate_gate():
    runner, session, _ = runner_for("S2A")
    rt = bridge()
    runner.rt = rt

    async def brief(**kw):
        pass
    runner._brief_next_beat = brief
    await send_ms(rt, LOUD, 340)
    await send_ms(rt, QUIET, 900)
    await runner._on_turn_ended()
    assert "input_audio_buffer.commit" in rt.ws.types()
    assert not session.store.of("participant_turn_discarded")


@in_a_loop
async def test_the_floor_is_a_knob(monkeypatch):
    monkeypatch.setenv("PARTICIPANT_COMMIT_MIN_VOICED_MS", "0")
    runner, session, _ = runner_for("S2A")
    rt = bridge()
    runner.rt = rt

    async def brief(**kw):
        pass
    runner._brief_next_beat = brief
    await send_ms(rt, LOUD, 100)
    await runner._on_turn_ended()
    assert "input_audio_buffer.commit" in rt.ws.types()
    assert not session.store.of("participant_turn_discarded")


@in_a_loop
async def test_no_floor_where_the_voice_cannot_be_counted():
    """Gemini commits its own turns; a session with no bar counts nothing."""
    runner, session, _ = runner_for("S2A")
    rt = bridge(bar=None)
    runner.rt = rt
    assert rt.voiced_since_commit() is None
    assert await runner._discard_if_too_little_voice() is False
    assert not session.store.of("participant_turn_discarded")


@in_a_loop
async def test_a_room_turn_under_the_floor_never_becomes_a_group_turn():
    runner, session, _ = runner_for("S4A")
    room = _room(GPT, runner)
    runner.room = room
    spawned = []
    runner._spawn_group_turn = lambda coro: (spawned.append(coro), coro.close())
    await room.hear(LOUD * 10)            # 200 ms of voice
    await room.hear(QUIET * 45)
    await runner._on_turn_ended()

    assert "input_audio_buffer.commit" not in room.scribe.ws.types()
    assert "input_audio_buffer.clear" in room.scribe.ws.types()
    (ev,) = session.store.of("participant_turn_discarded")
    assert ev["channel"] == "scribe" and ev["voiced_ms"] == 200
    assert not spawned, "no group turn: nobody is routed or granted"
    assert runner._group_turn_waiting is False
    assert not runner._floor.locked()
    assert room.scribe_commits == 0
    assert room._fanned_to_scribe == 0 and room._scribe_heard_speech is False
    assert room.scribe.input_restart_due()

    # The next real turn is committed and routed as usual.
    await room.hear(LOUD * 40)
    await runner._on_turn_ended()
    assert room.scribe.ws.types().count("input_audio_buffer.commit") == 1
    assert len(spawned) == 1 and runner._group_turn_waiting is True


@in_a_loop
async def test_the_watchdog_still_probes_after_a_discarded_turn(monkeypatch):
    monkeypatch.setenv("PROBE_AFTER_SECONDS", "0.2")
    monkeypatch.setenv("PROBE_TICK_SECONDS", "0.05")
    runner, session, _ = runner_for("S2A")
    rt = bridge()
    runner.rt = rt
    assert runner._next_trigger().get("on_silence"), "S2A i1 t1 probes"
    await send_ms(rt, LOUD, 100)
    await runner._on_turn_ended()
    assert session.store.of("participant_turn_discarded")
    probes = []

    async def probe_commit():
        probes.append(time.time())
        runner._closed = True
        return True

    async def brief(**kw):
        pass
    runner._probe_commit = probe_commit
    runner._brief_next_beat = brief
    await asyncio.wait_for(runner._silence_watchdog(), timeout=3)
    assert probes, "a discarded turn must not stop the silence probe"


# --------------------------------------------------------------------------
# 2. The rate gate
# --------------------------------------------------------------------------

@pytest.mark.parametrize("text,voiced", PHANTOMS)
def test_the_testers_phantoms_are_suppressed_with_their_text(text, voiced):
    runner, session, ws = runner_for("S2A")
    asyncio.run(runner._record_user_turn(text, item_id="i9", voiced_ms=voiced))
    (sup,) = session.store.of("user_turn_suppressed")
    assert sup["reason"] == "implausible_rate" and sup["text"] == text
    assert sup["voiced_ms"] == voiced and sup["item_id"] == "i9"
    assert sup["words"] == R.transcript_words(text)
    assert sup["words_per_voiced_s"] > 30
    assert not session.store.of("user_turn")
    assert not ws.frames("user_transcript"), "no caption"
    assert session.shared_history == [], "no steering or director input"
    assert runner._unrouted_user_texts == []
    assert runner._last_user_text == ""
    # It still arrived: the scribe watchdog must see the scribe working.
    assert runner._transcripts_arrived == 1


@pytest.mark.parametrize("text,voiced", REAL_SHORT)
def test_the_testers_real_short_lines_are_kept(text, voiced):
    runner, session, ws = runner_for("S2A")
    asyncio.run(runner._record_user_turn(text, voiced_ms=voiced))
    (turn,) = session.store.of("user_turn")
    assert turn["text"] == text
    assert not session.store.of("user_turn_suppressed")
    assert ws.frames("user_transcript")


def test_the_measured_rates():
    rates = {t: R.transcript_words(t) / (v / 1000) for t, v in REAL_SHORT}
    assert max(rates.values()) < 6.2 < R.max_words_per_voiced_s()
    assert R.transcript_words(CAT) == 16 and R.transcript_words(GOODBYE) == 19
    assert R.implausible_rate(CAT, 340)["words_per_voiced_s"] == 47.1
    assert R.implausible_rate(GOODBYE, 300)["words_per_voiced_s"] == 63.3


def test_words_over_no_voice_at_all_are_suppressed():
    """What gpt-4o-transcribe says to 20 s of silence ("Sure.", "Sorry.",
    "Ok."; P5 verification, 4 of 7 commits), and what "Hmm. Okay. Okay."
    looks like on a commit with no voiced frame."""
    runner, session, _ = runner_for("S2A")
    for text in ("Sure.", "Hmm. Okay. Okay."):
        asyncio.run(runner._record_user_turn(text, voiced_ms=0))
    reasons = [e["reason"] for e in session.store.of("user_turn_suppressed")]
    assert reasons == ["implausible_rate", "implausible_rate"]
    assert session.store.of("user_turn_suppressed")[0]["words_per_voiced_s"] is None
    assert not session.store.of("user_turn")


def test_a_fast_talker_over_a_long_turn_is_not_rate_gated():
    text = " ".join(["word"] * 20)          # 20 words over 1.6 s: 12.5 w/s
    runner, session, _ = runner_for("S2A")
    asyncio.run(runner._record_user_turn(text, voiced_ms=1600))
    assert session.store.of("user_turn")
    assert R.implausible_rate(text, 1200) is not None   # 16.7 under the ceiling


def test_no_rate_gate_where_the_voice_cannot_be_counted():
    runner, session, _ = runner_for("S2A")
    asyncio.run(runner._record_user_turn(CAT))
    assert session.store.of("user_turn")


def test_the_rate_gate_is_a_set_of_knobs(monkeypatch):
    monkeypatch.setenv("PARTICIPANT_MAX_WORDS_PER_VOICED_S", "0")
    assert R.implausible_rate(CAT, 340) is None
    monkeypatch.setenv("PARTICIPANT_MAX_WORDS_PER_VOICED_S", "50")
    assert R.implausible_rate(CAT, 340) is None
    assert R.implausible_rate(GOODBYE, 300) is not None
    monkeypatch.setenv("PARTICIPANT_RATE_GATE_MAX_VOICED_MS", "300")
    assert R.implausible_rate(GOODBYE, 300) is None
    gate = llm.provenance(GPT)["turn_gate"]
    assert gate["participant_max_words_per_voiced_s"] == 50.0
    assert gate["participant_rate_gate_max_voiced_ms"] == 300


def test_the_versions_move():
    assert llm.PIPELINE_VERSION >= "2026-09-24a"
    assert llm.ROOM_PACING_VERSION >= "2026-09-24a"


# --------------------------------------------------------------------------
# 3a. A room does not answer an unreliable line
# --------------------------------------------------------------------------

def _dead_room_runner(monkeypatch):
    monkeypatch.setenv("ROUTE_TRANSCRIPT_WAIT", "0.6")
    runner, session, _ = runner_for("S4A")
    room = _room(GPT, runner)
    for a in runner._resolve_agents():
        room.sessions[a.id] = DeadMember(GPT)
    runner.room = room
    runner.director = session.director
    session.append_agent("dan", "The date is locked.")
    runner._turn_end_arrivals = runner._transcripts_arrived
    return runner, session, room


def _run(runner):
    asyncio.run(asyncio.wait_for(runner._run_group_turn(), timeout=10))


def test_the_cat_phantom_gets_no_reply_in_a_room(monkeypatch):
    runner, session, _ = _dead_room_runner(monkeypatch)
    runner._group_turn_waiting = True
    runner._turns_without_transcript = 1
    asyncio.run(runner._record_user_turn(CAT, voiced_ms=340))
    _run(runner)
    assert session.director.calls == [], "the director is not asked"
    (skip,) = session.store.of("group_turn_skipped")
    assert skip["reason"] == "implausible_rate" and skip["text"] == CAT
    assert skip["arrivals"][0]["voiced_ms"] == 340
    assert not session.store.of("floor_grant_failed"), "nobody was granted"
    assert not session.store.of("trigger_fired")
    assert runner._group_turn_waiting is False
    assert not runner._floor.locked()
    assert runner._turns_without_transcript == 0, "the scribe is working"
    assert runner._unreliable_arrivals == []


@pytest.mark.parametrize("text,voiced", [
    ("Thank you.", 380), ("Two.", 520), ("Yes.", 450), ("Great, how about TC?", 560)])
def test_a_short_line_naming_nobody_is_still_answered(monkeypatch, text, voiced):
    """P6 review: 24a skipped a room turn whose only line was low_confidence
    and named nobody, so a real one-word answer (or "Casey?" transcribed
    "TC?") got no reply and, with no on_silence beat next, no probe either.
    It routes as it did before 24a: the director is called, on context (the
    short line is not its input unless the knob says "all"), and the floor
    is offered."""
    runner, session, _ = _dead_room_runner(monkeypatch)
    asyncio.run(runner._record_user_turn(text, voiced_ms=voiced))
    (turn,) = session.store.of("user_turn")
    assert turn["low_confidence"] is True, "still flagged in the record"
    _run(runner)
    assert not session.store.of("group_turn_skipped")
    assert session.director.calls, "the director is asked"
    assert session.store.of("floor_grant_failed"), "the floor was offered"
    assert runner._unreliable_arrivals == []


def test_a_short_line_naming_someone_is_still_routed(monkeypatch):
    runner, session, _ = _dead_room_runner(monkeypatch)
    name = runner._resolve_agents()[1].name
    asyncio.run(runner._record_user_turn(f"{name}?", voiced_ms=400))
    _run(runner)
    assert not session.store.of("group_turn_skipped")
    assert session.store.of("floor_grant_failed"), "the floor was offered"


def test_a_short_line_goes_to_the_director_when_the_knob_says_all(monkeypatch):
    monkeypatch.setenv("PARTICIPANT_LOW_CONFIDENCE_DIRECTOR", "all")
    runner, session, _ = _dead_room_runner(monkeypatch)
    asyncio.run(runner._record_user_turn("Thank you.", voiced_ms=380))
    _run(runner)
    assert not session.store.of("group_turn_skipped")
    assert session.director.calls[0][1] == "Thank you."


def test_a_phantom_beside_a_real_line_does_not_stop_the_real_one(monkeypatch):
    runner, session, _ = _dead_room_runner(monkeypatch)
    asyncio.run(runner._record_user_turn(CAT, voiced_ms=340))
    asyncio.run(runner._record_user_turn(
        "So how about we focus on those two urgent items?", voiced_ms=2680))
    _run(runner)
    assert not session.store.of("group_turn_skipped")
    assert session.director.calls[0][1] == (
        "So how about we focus on those two urgent items?")


def test_a_stray_from_before_this_turn_does_not_skip_it(monkeypatch):
    """"Hmm. Okay. Okay." came in on no VAD turn of its own. Suppressed, it
    must not decide the NEXT turn, whose own transcript is what counts."""
    runner, session, _ = _dead_room_runner(monkeypatch)
    asyncio.run(runner._record_user_turn("Hmm. Okay. Okay.", voiced_ms=0))
    runner._turn_end_speech_began = time.time() + 0.01
    time.sleep(0.02)
    runner._turn_end_arrivals = runner._transcripts_arrived
    _run(runner)
    assert not session.store.of("group_turn_skipped")
    assert session.director.calls, "routed as a turn with nothing heard"


def test_a_turn_with_nothing_at_all_still_goes_to_the_director(monkeypatch):
    """The scribe-failure case the director is prompted to handle."""
    runner, session, _ = _dead_room_runner(monkeypatch)
    _run(runner)
    assert not session.store.of("group_turn_skipped")
    assert session.director.calls and session.director.calls[0][1] == ""
    assert runner._turns_without_transcript == 1


# --------------------------------------------------------------------------
# 3b. 1:1: the reply to a suppressed turn
# --------------------------------------------------------------------------

def _one_to_one():
    runner, session, ws = runner_for("S2A")
    rt = bridge()
    runner.rt = rt
    return runner, session, ws, rt


@in_a_loop
async def test_a_reply_that_has_played_nothing_is_cancelled_and_its_tail_dropped():
    runner, session, ws, rt = _one_to_one()
    await send_ms(rt, LOUD, 340)
    assert await rt.commit_turn() is True          # the commit starts the reply
    committed_at = time.time()
    rt.ws.sent.clear()
    await runner._record_user_turn(CAT, voiced_ms=340, committed_at=committed_at)

    assert rt.ws.types() == ["response.cancel"]
    (ev,) = session.store.of("suppressed_turn_reply_cancelled")
    assert ev["text"] == CAT and ev["reason"] == "implausible_rate"
    assert ev["response_id"] is None, "not named yet when the transcript came"
    assert not rt.responding

    # The gateway then names the reply and sends it; none of it is played.
    from test_bridge_correctness import adelta, created, done, tdelta, types
    for frame in (created("resp_P"), tdelta("resp_P", "it_1", "Okay, let's "),
                  adelta("resp_P", "it_1"), done("resp_P", "cancelled")):
        rt.ws.feed(**frame)
    agen = rt.events()
    got = []
    try:
        while True:
            e = await asyncio.wait_for(agen.__anext__(), 1)
            got.append(e)
            if e["type"] in ("cancelled_output", "response_done"):
                break
    except asyncio.TimeoutError:
        pass
    finally:
        await agen.aclose()
    assert "agent_audio" not in types(got)
    assert "cancelled_output" in types(got)


@in_a_loop
async def test_a_named_reply_is_cancelled_by_name():
    runner, session, ws, rt = _one_to_one()
    await send_ms(rt, LOUD, 340)
    await rt.commit_turn()
    rt._response_created_id = "resp_N"
    await runner._record_user_turn(CAT, voiced_ms=340, committed_at=rt.last_commit_at)
    (ev,) = session.store.of("suppressed_turn_reply_cancelled")
    assert ev["response_id"] == "resp_N"
    assert "resp_N" in rt._discard_ids
    assert runner._barged_response_id == "resp_N"


@in_a_loop
async def test_a_reply_opened_by_text_alone_is_withdrawn_from_the_page():
    runner, session, ws, rt = _one_to_one()
    await rt.commit_turn()
    runner._speaking = True
    runner._agent_text = ["Okay, ", "so "]
    await runner._record_user_turn(CAT, voiced_ms=340, committed_at=rt.last_commit_at)
    (ev,) = session.store.of("suppressed_turn_reply_cancelled")
    assert ev["generated_text"] == "Okay, so"
    assert ws.frames("assistant_interrupted")
    assert runner._speaking is False and runner._agent_text == []
    assert not session.store.of("assistant_turn"), "nothing of it was heard"


@in_a_loop
async def test_a_reply_already_playing_is_left_and_written_down():
    runner, session, ws, rt = _one_to_one()
    await rt.commit_turn()
    runner._speaking = True
    runner._turn_audio_bytes = 48000               # 1.5 s at 16 kHz
    rt.ws.sent.clear()
    await runner._record_user_turn(GOODBYE, voiced_ms=400,
                                   committed_at=time.time() - 3)
    assert "response.cancel" not in rt.ws.types()
    (ev,) = session.store.of("reply_to_suppressed_turn")
    assert ev["audio_ms"] == 1500 and ev["playing"] is True
    assert ev["text"] == GOODBYE
    assert not session.store.of("suppressed_turn_reply_cancelled")


@in_a_loop
async def test_a_reply_that_already_finished_is_written_down():
    runner, session, ws, rt = _one_to_one()
    committed_at = time.time() - 4
    runner._agent_turn_began_at = committed_at + 2
    await runner._record_user_turn(CAT, voiced_ms=340, committed_at=committed_at)
    (ev,) = session.store.of("reply_to_suppressed_turn")
    assert ev["playing"] is False
    assert "response.cancel" not in rt.ws.types()


@in_a_loop
async def test_nothing_is_cancelled_when_nothing_answers_the_turn():
    runner, session, ws, rt = _one_to_one()
    await runner._record_user_turn(CAT, voiced_ms=340, committed_at=time.time())
    assert "response.cancel" not in rt.ws.types()
    assert not session.store.of("reply_to_suppressed_turn")
    assert not session.store.of("suppressed_turn_reply_cancelled")
    assert session.store.of("user_turn_suppressed")


# --------------------------------------------------------------------------
# 5. The commit-only grant waits 6 s and holds the bridge's own bar off
# --------------------------------------------------------------------------

def test_the_grant_wait_defaults_to_six_seconds(monkeypatch):
    assert R.room_grant_unanswered_s() == 6.0
    assert llm.provenance(GPT)["pacing"]["room_grant_unanswered_s"] == 6.0
    monkeypatch.setenv("ROOM_GRANT_UNANSWERED_S", "4.5")
    assert llm.provenance(GPT)["pacing"]["room_grant_unanswered_s"] == 4.5


def test_the_bridge_does_not_ask_again_inside_the_grants_window():
    rt = bridge()
    rt.expect_commit_reply(hold_s=6.0)
    rt._response_started_at = time.time() - (R.REQUEST_UNANSWERED_S + 1)
    assert rt._request_unanswered() is False, "the grant is still waiting"
    rt._response_started_at = time.time() - (R.REQUEST_UNANSWERED_S + 6.5)
    assert rt._request_unanswered() is True
    # A plain commit (1:1) keeps the bridge's own bar.
    rt2 = bridge()
    rt2.expect_commit_reply()
    rt2._response_started_at = time.time() - (R.REQUEST_UNANSWERED_S + 1)
    assert rt2._request_unanswered() is True


@in_a_loop
async def test_give_floor_hands_the_bridge_its_wait(monkeypatch):
    monkeypatch.setenv("ROOM_GRANT_UNANSWERED_S", "0.2")
    from test_room_reply_lifecycle import gpt_room
    room, rt = gpt_room()
    holds = []
    real = rt.expect_commit_reply

    def mark(hold_s=0.0):
        holds.append(hold_s)
        real(hold_s=hold_s)
    rt.expect_commit_reply = mark
    await room.give_floor("dan")
    assert holds == [0.2]
    assert rt._request_hold_s == 0.0, "the fallback create is on its own bar"
    assert room.grant_fallback_creates == 1


# --------------------------------------------------------------------------
# 4. The proactive S1 hand-off is in tests/test_s1_timebox.py.
# 6. Trigger coverage counts only what was performed
# --------------------------------------------------------------------------

def test_the_ledger_nets_unperformed_and_undelivered_beats():
    events = [
        {"type": "trigger_fired", "trigger_id": "t1", "index": 0,
         "interaction": "i1"},
        {"type": "stage_direction_unperformed", "trigger_id": "t1",
         "interaction": "i1", "reason": "empty_response"},
        {"type": "trigger_fired", "trigger_id": "t2", "index": 1,
         "interaction": "i1"},
        {"type": "trigger_fired", "trigger_id": "t3", "index": 2,
         "interaction": "i1"},
        {"type": "trigger_undelivered", "trigger_id": "t3", "index": 2},
        # A handoff or director note names no beat and cancels nothing.
        {"type": "stage_direction_unperformed", "trigger_id": None},
    ]
    ledger = verify_record._trigger_ledger(events)
    assert [e["trigger_id"] for e in ledger["performed"]] == ["t2"]
    assert [e["trigger_id"] for e in ledger["unperformed"]] == ["t1"]
    assert [e["trigger_id"] for e in ledger["undelivered"]] == ["t3"]
    # The dashboard's rule (was the brief delivered?) is unchanged.
    assert [e["trigger_id"] for e in verify_record._net_fired(events)] == [
        "t1", "t2"]


def test_a_beat_refired_and_performed_later_is_reached():
    events = [
        {"type": "trigger_fired", "trigger_id": "t1", "index": 0},
        {"type": "stage_direction_unperformed", "trigger_id": "t1"},
        {"type": "trigger_fired", "trigger_id": "t1", "index": 0},
    ]
    ledger = verify_record._trigger_ledger(events)
    assert [e["trigger_id"] for e in ledger["performed"]] == ["t1"]


def test_verify_reports_an_unperformed_beat_as_not_reached(sessions_root):
    """The P5 S4A sim: Dan answered t1_priya_interrupted with a near-silent
    fragment, stage_direction_unperformed was written, and verify_record
    still counted t1 as fired."""
    expected = verify_record._expected_triggers("S4A")
    assert len(expected) >= 2
    t1, t2 = expected[0], expected[1]
    events = _base_events()
    events[0]["scenario"] = "S4A"
    events += [
        {"t": 5.0, "type": "trigger_fired", "trigger_id": t1, "index": 0,
         "interaction": "i1", "probing": False, "esci": ["x"]},
        {"t": 9.0, "type": "stage_direction_unperformed", "trigger_id": t1,
         "interaction": "i1", "reason": "empty_response"},
        {"t": 20.0, "type": "trigger_fired", "trigger_id": t2, "index": 1,
         "interaction": "i1", "probing": True, "esci": ["y"]},
    ]
    sdir = _write_session(sessions_root, events)
    ok, detail = _check(verify_record.verify(sdir)[1], "planted triggers fired")
    assert not ok
    assert detail.startswith(f"1/{len(expected)} (0 volunteered, 1 probed)")
    assert f"unperformed: {t1}" in detail
    assert t1 in detail.split("missed: ")[1]


# --------------------------------------------------------------------------
# 7. P6 review (pipeline 2026-09-24b)
# --------------------------------------------------------------------------
#
# The rate is taken over the voiced SPAN. Replaying the tester's WAV quieter
# (scratchpad p6/review_fixes/gate_sweep.py), the voiced count of a real line
# shrinks while its words do not; the span stays put. (voiced ms / span ms):
#
#   "Does that sound good?"                      0 dB 660/820  -2.5 dB 460/680
#                                                -4 dB 400/640
#   "Right. Sounds good. Casey, are you there?"  -7 dB 360/440
#   phantoms                                     CAT 340/340, GOODBYE 300/340

QUIETER = [("Does that sound good?", 460, 680), ("Does that sound good?", 400, 640),
           ("Right. Sounds good. Casey, are you there?", 360, 440)]


@pytest.mark.parametrize("text,voiced,span", QUIETER)
def test_a_quieter_real_line_is_kept(text, voiced, span):
    assert R.implausible_rate(text, voiced, span) is None
    runner, session, _ = runner_for("S2A")
    asyncio.run(runner._record_user_turn(text, voiced_ms=voiced,
                                         voiced_span_ms=span))
    (turn,) = session.store.of("user_turn")
    assert turn["voiced_span_ms"] == span
    assert not session.store.of("user_turn_suppressed")


def test_the_measure_of_24a_would_have_dropped_them(monkeypatch):
    """At 24a's bar of 8 over the voiced count, the first two were
    suppressed (8.7 and 10.0 w/s); over the span at 16 nothing is. The knobs
    put 24a's measure back."""
    monkeypatch.setenv("PARTICIPANT_MAX_WORDS_PER_VOICED_S", "8")
    monkeypatch.setenv("PARTICIPANT_RATE_OVER", "voiced_count")
    assert R.implausible_rate("Does that sound good?", 460, 680) is not None
    assert R.implausible_rate("Does that sound good?", 400, 640) is not None
    assert llm.provenance(GPT)["turn_gate"]["participant_rate_over"] == "voiced_count"
    monkeypatch.setenv("PARTICIPANT_RATE_OVER", "voiced_span")
    assert R.implausible_rate("Does that sound good?", 460, 680) is None   # 5.9


def test_without_a_span_the_count_is_used_and_the_quieter_line_still_kept():
    assert R.implausible_rate("Does that sound good?", 460) is None   # 8.7


@pytest.mark.parametrize("text,voiced,span", [(CAT, 340, 340), (GOODBYE, 300, 340)])
def test_the_phantoms_are_still_suppressed_over_their_span(text, voiced, span):
    runner, session, _ = runner_for("S2A")
    asyncio.run(runner._record_user_turn(text, voiced_ms=voiced,
                                         voiced_span_ms=span))
    (sup,) = session.store.of("user_turn_suppressed")
    assert sup["reason"] == "implausible_rate"
    assert sup["voiced_span_ms"] == span and sup["words_per_voiced_s"] > 40
    assert not session.store.of("user_turn")


@in_a_loop
async def test_the_bridge_measures_the_span_and_hands_it_on():
    rt = bridge()
    await send_ms(rt, QUIET, 200)
    await send_ms(rt, LOUD, 100)
    await send_ms(rt, QUIET, 300)
    await send_ms(rt, LOUD, 60)
    await send_ms(rt, QUIET, 400)
    await rt.commit_input()
    rt.ws.feed(type="conversation.item.input_audio_transcription.completed",
               item_id="x", transcript="Yes, I can.")
    ev = await next_transcript(rt)
    assert ev["voiced_ms"] == 160 and ev["voiced_span_ms"] == 460
    # A clear or a commit starts the next count from nothing.
    await send_ms(rt, LOUD, 40)
    await rt.clear_input()
    await send_ms(rt, QUIET, 100)
    await rt.commit_input()
    assert rt._commit_tags[-1]["voiced_span_ms"] == 0
    assert rt._commit_tags[-1]["voiced_ms"] == 0


def test_the_rate_measure_is_in_provenance():
    gate = llm.provenance(GPT)["turn_gate"]
    assert gate["participant_rate_over"] == "voiced_span"
    assert gate["participant_max_words_per_voiced_s"] == 16.0
    assert llm.PIPELINE_VERSION >= "2026-09-24b"
    assert llm.ROOM_PACING_VERSION >= "2026-09-24b"


@in_a_loop
async def test_a_late_transcript_does_not_cancel_the_reply_to_a_later_commit():
    """C1 (a phantom) is answered and done; the participant's real C2 goes
    out and its reply is in flight, unheard; only then does C1's transcript
    come back and fail the rate gate. The reply is C2's and stays."""
    runner, session, ws, rt = _one_to_one()
    await send_ms(rt, LOUD, 340)
    await rt.commit_turn()
    c1 = rt.last_commit_at
    rt._end_response()                       # C1's reply is over
    await asyncio.sleep(0.01)
    await send_ms(rt, LOUD, 1200)
    assert await rt.commit_turn() is True    # C2, its reply in flight
    assert rt.last_commit_at > c1 and rt.responding
    rt.ws.sent.clear()
    await runner._record_user_turn(CAT, voiced_ms=340, committed_at=c1)
    assert "response.cancel" not in rt.ws.types()
    assert rt.responding, "C2's reply is untouched"
    (ev,) = session.store.of("reply_to_suppressed_turn")
    assert ev["kept"] == "later_commit" and ev["text"] == CAT
    assert not session.store.of("suppressed_turn_reply_cancelled")
    assert session.store.of("user_turn_suppressed"), "the phantom is still on record"


async def _fired_then_withdrawn(runner, rt):
    async def deliver(rt_, instructions):
        return True
    runner._deliver_brief = deliver
    await runner._brief_next_beat(probing=False)    # the turn end spends t1
    assert runner._trigger_idx == 1 and runner._pending_direction["trigger_id"]
    await send_ms(rt, LOUD, 340)
    await rt.commit_turn()
    await runner._record_user_turn(CAT, voiced_ms=340,
                                   committed_at=rt.last_commit_at)


@in_a_loop
async def test_a_withdrawn_reply_gives_its_beat_back():
    """S2A: the phantom's turn end fired t1, and its reply was cancelled
    before any of it played. t1 is retracted and re-offered, so the next
    turn's brief fires it again rather than moving on to t2."""
    runner, session, ws, rt = _one_to_one()
    t1 = runner._next_trigger()["id"]
    await _fired_then_withdrawn(runner, rt)
    (cancel,) = session.store.of("suppressed_turn_reply_cancelled")
    assert cancel["pending_trigger_id"] == t1
    (und,) = session.store.of("trigger_undelivered")
    assert und["trigger_id"] == t1 and und["index"] == 0
    assert und["reason"] == "reply_withdrawn"
    assert runner._trigger_idx == 0 and runner._fired == []
    assert runner._pending_direction is None
    # The participant's next real turn: the same beat, at the same index.
    await runner._brief_next_beat(probing=False)
    fired = session.store.of("trigger_fired")
    assert [(e["trigger_id"], e["index"]) for e in fired] == [(t1, 0), (t1, 0)]
    ledger = verify_record._trigger_ledger(session.store.events)
    assert [e["trigger_id"] for e in ledger["performed"]] == [t1]
    assert [e["trigger_id"] for e in ledger["undelivered"]] == [t1]


@in_a_loop
async def test_a_withdrawn_beat_that_cannot_go_back_is_written_unperformed():
    runner, session, ws, rt = _one_to_one()
    t1 = runner._next_trigger()["id"]

    async def deliver(rt_, instructions):
        return True
    runner._deliver_brief = deliver
    await runner._brief_next_beat(probing=False)
    await send_ms(rt, LOUD, 340)
    await rt.commit_turn()
    async with runner._brief_lock:            # a brief is going out right now
        await runner._record_user_turn(CAT, voiced_ms=340,
                                       committed_at=rt.last_commit_at)
    (un,) = session.store.of("stage_direction_unperformed")
    assert un["trigger_id"] == t1 and un["reason"] == "reply_withdrawn"
    assert not session.store.of("trigger_undelivered")
    assert runner._trigger_idx == 1 and runner._pending_direction is None
    ledger = verify_record._trigger_ledger(session.store.events)
    assert ledger["performed"] == [] and len(ledger["unperformed"]) == 1


@in_a_loop
async def test_a_hand_off_note_survives_a_withdrawn_reply():
    runner, session, ws, rt = _one_to_one()
    runner._pending_direction = {"trigger_id": None, "source": "handoff",
                                 "agent_id": runner.agent_id,
                                 "interaction": runner._interaction_id()}
    await send_ms(rt, LOUD, 340)
    await rt.commit_turn()
    await runner._record_user_turn(CAT, voiced_ms=340,
                                   committed_at=rt.last_commit_at)
    assert session.store.of("suppressed_turn_reply_cancelled")
    assert runner._pending_direction["source"] == "handoff"
    assert not session.store.of("trigger_undelivered")
    assert not session.store.of("stage_direction_unperformed")


async def _drain(rt, until=("cancelled_output",), n=12):
    agen = rt.events()
    got = []
    try:
        while len(got) < n:
            e = await asyncio.wait_for(agen.__anext__(), 1)
            got.append(e)
            if e["type"] in until:
                break
    except asyncio.TimeoutError:
        pass
    finally:
        await agen.aclose()
    return got


@in_a_loop
async def test_an_unnamed_cancel_that_missed_is_sent_again_once_named():
    """cancel_unheard_reply's cancel reached the gateway before the reply
    existed (response_cancel_not_active); the reply it then creates is
    cancelled once named, and nothing of the miss reaches the page."""
    from test_bridge_correctness import adelta, created, done, tdelta, types
    rt = bridge()
    await send_ms(rt, LOUD, 340)
    await rt.commit_turn()
    assert await rt.cancel_unheard_reply() == ""
    rt.ws.sent.clear()
    rt.ws.feed(type="error", error={"type": "invalid_request_error",
                                    "code": "response_cancel_not_active",
                                    "message": "no active response"})
    for frame in (created("resp_L"), tdelta("resp_L", "it_1", "Sure, "),
                  adelta("resp_L", "it_1"), done("resp_L", "cancelled")):
        rt.ws.feed(**frame)
    got = await _drain(rt)
    assert rt.ws.types() == ["response.cancel"], "cancelled again, once"
    assert "error" not in types(got) and "agent_audio" not in types(got)
    (co,) = [e for e in got if e["type"] == "cancelled_output"]
    assert co["response_id"] == "resp_L" and co["recancelled"] is True


@in_a_loop
async def test_an_unnamed_cancel_that_landed_is_not_sent_twice():
    from test_bridge_correctness import adelta, created, done, types
    rt = bridge()
    await send_ms(rt, LOUD, 340)
    await rt.commit_turn()
    await rt.cancel_unheard_reply()
    rt.ws.sent.clear()
    for frame in (created("resp_H"), adelta("resp_H", "it_1"),
                  done("resp_H", "cancelled")):
        rt.ws.feed(**frame)
    got = await _drain(rt)
    assert "response.cancel" not in rt.ws.types()
    (co,) = [e for e in got if e["type"] == "cancelled_output"]
    assert "recancelled" not in co
    assert "agent_audio" not in types(got)
