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
    GPT, LOUD, QUIET, DeadMember, _room, bridge, in_a_loop, runner_for,
    send_ms,
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
    assert R.implausible_rate(text, 1499) is not None


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


def test_a_short_line_naming_nobody_gets_no_reply(monkeypatch):
    runner, session, _ = _dead_room_runner(monkeypatch)
    asyncio.run(runner._record_user_turn("Thank you.", voiced_ms=380))
    _run(runner)
    assert session.store.of("user_turn"), "still recorded and captioned"
    (skip,) = session.store.of("group_turn_skipped")
    assert skip["reason"] == "low_confidence" and skip["text"] == "Thank you."
    assert session.director.calls == []


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
    await runner._record_user_turn(CAT, voiced_ms=340, committed_at=time.time() - 1)
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
    await runner._record_user_turn(CAT, voiced_ms=340, committed_at=time.time() - 1)
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
