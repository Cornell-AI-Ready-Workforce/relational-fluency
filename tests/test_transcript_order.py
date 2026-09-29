"""Issue #50: the transcript in the order things were said and heard.

Both surfaces ordered lines by when they were LOGGED. A participant line is
logged when its transcript arrives, a second or more after they stop; a
character line is logged when it is closed (generation end plus transcript
settle), and put on the page when its audio starts. So on the S4A room
s_1790278989_77ee7e (pipeline 2026-09-24c):

* the record put the participant's line, said while Priya was speaking
  (transcript 203.39), under Dan's follow-up, closed at 202.23 and first
  heard at 208.27: a rater reads it as the answer to a line they had not
  heard;
* the page drew three participant lines under a character line granted for
  their previous turn whose audio began while the newest one was still being
  transcribed (speech end 153.38, the character's audio 154.35, transcript
  154.56).

Now the bridge dates each commit by the runner VAD's speech start for the
turn it closes (else by its first voiced frame), the runner writes that on
user_turn (`spoken_at`), steering_pair names the page turn its line played as
(`page_turn`), and encounter_record sorts by `spoken_at` / `heard_at`,
keeping `t`. The timings below are the 77ee7e and 09bcbb ones; the words are
made up.
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
for p in (ROOT, ROOT / "tests"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from server import encounter_record  # noqa: E402
from server import realtime_voice_session as rvs  # noqa: E402
from server.turn_timing import TurnTimer  # noqa: E402
from server.voice import realtime as R  # noqa: E402

from test_participant_turn_integrity import (  # noqa: E402
    GPT, LOUD, QUIET, WireWS, bridge, in_a_loop, next_transcript, runner_for,
    send_ms,
)
from test_turn_instrumentation import (  # noqa: E402
    CL_AUDIO, FakeRoom, FakeRT, FakeSession, FakeWS, Store, one_to_one, settle,
)


# --------------------------------------------------------------------------
# 1. The bridge: when the participant began what a commit holds
# --------------------------------------------------------------------------

@in_a_loop
async def test_a_commit_is_dated_by_the_vads_speech_start_for_the_turn_it_closes():
    """S2A 09bcbb at 60-89 s: voice again 0.4 s after one commit, Morgan's
    14 s reply, then the participant's next line, all in one 27.7 s buffer.
    Its first voiced frame is before the reply they were answering; the VAD's
    speech start for the turn the commit closes is after it."""
    rt = bridge()
    await send_ms(rt, LOUD, 300)                    # soon after the last commit
    await send_ms(rt, QUIET, 14000)                 # Morgan's reply plays
    vad_began = time.time()
    rt.speech_began = lambda: vad_began
    await send_ms(rt, LOUD, 3000)
    await send_ms(rt, QUIET, 900)
    await rt.commit_input()
    tag = rt._commit_tags[-1]
    assert tag["spoken_at"] == vad_began
    rt.ws.feed(type="conversation.item.input_audio_transcription.completed",
               item_id="x", transcript="I would like a promotion.")
    ev = await next_transcript(rt)
    assert ev["spoken_at"] == vad_began
    assert rvs._turn_meta(ev)["spoken_at"] == vad_began


@in_a_loop
async def test_without_a_speech_start_since_the_last_commit_the_first_voiced_frame():
    """Counted back from the commit on the buffer's own audio: 400 ms of voice
    and 900 ms of quiet followed the first voiced frame."""
    rt = bridge()
    await rt.commit_input()
    stale = time.time() - 60.0                      # before that commit
    rt.speech_began = lambda: stale
    await send_ms(rt, QUIET, 300)
    await send_ms(rt, LOUD, 400)
    await send_ms(rt, QUIET, 900)
    await rt.commit_input()
    tag = rt._commit_tags[-1]
    assert tag["spoken_at"] == pytest.approx(tag["committed_at"] - 1.3, abs=1e-6)


@in_a_loop
async def test_the_preroll_a_restart_resends_is_dated_where_it_was_said():
    """restart_input appends the pre-roll in one instant; its voiced frames
    were said before the VAD confirmed the speech, and the count-back puts
    them there, not at the moment they were re-sent."""
    rt = bridge()
    await rt.restart_input(QUIET * 15 + LOUD * 15)     # 300 ms quiet, 300 ms voice
    await send_ms(rt, LOUD, 500)
    await send_ms(rt, QUIET, 900)
    await rt.commit_input()
    tag = rt._commit_tags[-1]
    assert tag["spoken_at"] == pytest.approx(tag["committed_at"] - 1.7, abs=1e-6)


@in_a_loop
async def test_nothing_voiced_or_nothing_counted_is_not_dated():
    rt = bridge()
    await send_ms(rt, QUIET, 600)
    await rt.commit_input()
    assert rt._commit_tags[-1]["spoken_at"] is None
    rt = bridge(bar=None)
    await send_ms(rt, LOUD, 600)
    await rt.commit_input()
    assert rt._commit_tags[-1]["spoken_at"] is None


@in_a_loop
async def test_the_runners_own_bridge_is_dated_by_its_vad(monkeypatch):
    """The whole 1:1 path on the gpt route: the page's frames through the
    runner's VAD, its turn end, and the commit it sends."""
    monkeypatch.setattr(R, "MODEL", GPT)
    frames = [QUIET] * 50 + [LOUD] * 40 + [QUIET] * 120
    runner, session, ws = runner_for("S2A", frames)
    rt = runner._new_session(instructions="x", voice="")
    rt.ws = WireWS()
    runner.rt = rt
    await runner._client_to_model()
    assert rt.commits == 1
    assert runner._speech_started_at > 0
    assert rt._commit_tags[-1]["spoken_at"] == runner._speech_started_at


# --------------------------------------------------------------------------
# 2. The runner: user_turn.spoken_at, page_turn
# --------------------------------------------------------------------------

def test_the_participant_line_carries_when_it_was_begun():
    runner, session, ws = runner_for("S2A")
    session.store.started_at = time.time() - 20.0
    began = session.store.started_at + 12.5             # 7.5 s ago
    asyncio.run(runner._record_user_turn(
        "Well, I think it does.", voiced_ms=1500, voiced_span_ms=1600,
        spoken_at=began))
    (turn,) = session.store.of("user_turn")
    assert turn["spoken_at"] == 12.5


def test_a_line_the_bridge_could_not_date_keeps_the_old_shape():
    runner, session, ws = runner_for("S2A")
    asyncio.run(runner._record_user_turn("Well, I think it does.",
                                         voiced_ms=1500, voiced_span_ms=1600))
    assert session.store.of("user_turn")[0]["spoken_at"] is None


def test_turn_of_names_the_turn_the_next_done_closes():
    tt = TurnTimer(Store())
    assert tt.turn_of("dan") is None
    a = tt.started("dan")
    assert tt.turn_of("dan") == a
    b = tt.started("dan")              # a held reply announced a second time
    assert tt.turn_of("dan") == b
    tt.done("dan")
    assert tt.turn_of("dan") is None, "a line closed with nothing announced"


@in_a_loop
async def test_the_1to1_line_names_its_page_turn(monkeypatch):
    runner, session, ws, rt = one_to_one(monkeypatch)
    rt.feed({"type": "agent_transcript_delta", "text": "Okay."})
    rt.feed({"type": "agent_audio", "pcm": CL_AUDIO})
    rt.feed({"type": "response_done", "audio_unterminated": False,
             "retried": False, "retry_reason": None, "retryable": False})
    rt.end()
    await runner._pump_events(rt)
    await settle(runner)
    (started,) = ws.frames("assistant_started")
    (pair,) = session.store.of("steering_pair")
    assert pair["page_turn"] == started["turn"]


@in_a_loop
async def test_a_1to1_line_that_never_reached_the_page_names_none(monkeypatch):
    runner, session, ws, rt = one_to_one(monkeypatch)
    await runner._finalize_turn(runner.agent_id, runner.agent, rt, ["Well I"],
                                None, interrupted=True)
    assert session.store.of("steering_pair")[0]["page_turn"] is None


@in_a_loop
async def test_the_room_line_names_its_page_turn(monkeypatch):
    monkeypatch.setenv("TRANSCRIPT_GRACE_SECONDS", "0.2")
    session = FakeSession("S4A")
    ws = FakeWS()
    runner = rvs.RealtimeVoiceSessionRunner(session, ws)
    agent = runner._resolve_agents()[0]
    runner.room = FakeRoom(speaking=agent.id)
    rt = FakeRT()
    rt.feed({"type": "agent_transcript_delta", "text": "We ship Friday."})
    for _ in range(3):
        rt.feed({"type": "agent_audio", "pcm": CL_AUDIO})
    rt.feed({"type": "response_done", "audio_unterminated": False,
             "retried": False, "retry_reason": None, "retryable": False})
    rt.end()
    await runner._pump_member(agent, rt)
    await settle(runner)
    (started,) = ws.frames("assistant_started")
    (pair,) = session.store.of("steering_pair")
    assert pair["page_turn"] == started["turn"]


# --------------------------------------------------------------------------
# 3. The record: sorted by spoken_at / heard_at, `t` kept
# --------------------------------------------------------------------------

def _write(root: Path, events) -> Path:
    sdir = root / "s_1790000000_abcdef"
    sdir.mkdir(parents=True)
    with (sdir / "events.jsonl").open("w", encoding="utf-8") as fh:
        for e in events:
            fh.write(json.dumps(e) + "\n")
    return sdir


def _pair(t, agent_id, text, **extra):
    return {"t": t, "type": "steering_pair", "direction": None,
            "actor": {"agent_id": agent_id, "text": text}, **extra}


def _user(t, text, **extra):
    return {"t": t, "type": "user_turn", "text": text, **extra}


def _order(rec):
    return [(t["agent_id"] if t["role"] == "agent" else "you")
            for t in rec["transcript"]]


def test_a_line_begun_under_a_colleague_goes_above_the_follow_up_heard_after_it(tmp_path):
    """77ee7e at 190-214 s: Priya's line plays from 194.07; Dan's follow-up
    is closed at 202.23, held behind her and first heard at 208.27; the
    participant's line lands at 203.39. Here they began it under Priya, at
    199.5. Chris's line was queued behind Dan's and cut before it played."""
    sdir = _write(tmp_path, [
        {"t": 0.0, "type": "session_start"},
        {"t": 194.07, "type": "play_start", "turn": 21, "agent_id": "priya", "at": 194.07},
        _pair(197.15, "priya", "No. Last year it all went at once.", page_turn=21),
        _pair(202.226, "dan", "That is the model, not the date.", page_turn=22),
        _user(203.388, "I wonder if you could...", spoken_at=199.5),
        {"t": 208.272, "type": "play_start", "turn": 22, "agent_id": "dan", "at": 208.272},
        _pair(212.402, "chris", "Let us keep this tight.", page_turn=23),
        {"t": 214.49, "type": "turn_timing", "turn": 23, "agent_id": "chris",
         "first_audio_played": None, "first_audio_to_client": 208.268,
         "play_end": 214.49, "play_end_interrupted": True},
        _user(214.514, "I want to do.", spoken_at=213.4),
    ])
    rec = encounter_record.build(sdir)
    assert _order(rec) == ["priya", "you", "dan", "chris", "you"], rec["transcript"]
    by_text = {t["text"]: t for t in rec["transcript"]}
    # The logged time stays on every turn; the new fields sit beside it.
    assert by_text["I wonder if you could..."]["t"] == 203.388
    assert by_text["I wonder if you could..."]["spoken_at"] == 199.5
    assert by_text["That is the model, not the date."]["t"] == 202.226
    assert by_text["That is the model, not the date."]["heard_at"] == 208.272
    # Cut before it played: never heard, so it keeps its logged place.
    assert by_text["Let us keep this tight."]["heard_at"] is None


def test_a_line_the_page_played_after_they_began_goes_under_theirs(tmp_path):
    """77ee7e at 150-157 s: Dan's line, granted for their previous turn, starts
    playing at 154.35 while their next line (begun 151.5, turn end 153.38) is
    still being transcribed; it lands at 154.56."""
    sdir = _write(tmp_path, [
        {"t": 0.0, "type": "session_start"},
        _user(151.218, "That went well, I thought.", spoken_at=142.3),
        {"t": 154.351, "type": "play_start", "turn": 16, "agent_id": "dan", "at": 154.35},
        _user(154.558, "Very well deserved.", spoken_at=151.5),
        _pair(156.733, "dan", "Hold that. Last year does not help.", page_turn=16),
    ])
    assert _order(encounter_record.build(sdir)) == ["you", "you", "dan"]


def test_heard_at_is_the_ack_then_turn_timing_then_the_first_audio_sent(tmp_path):
    sdir = _write(tmp_path, [
        {"t": 0.0, "type": "session_start"},
        {"t": 4.0, "type": "play_start", "turn": 1, "at": 4.0},
        {"t": 9.0, "type": "turn_timing", "turn": 1, "first_audio_played": 4.0,
         "first_audio_to_client": 3.9, "play_end": 9.0},
        # The page's start ack lost, its turn_timing kept.
        {"t": 19.0, "type": "turn_timing", "turn": 2, "first_audio_played": 14.0,
         "first_audio_to_client": 13.9, "play_end": 19.0},
        # The page said nothing: written at encounter end.
        {"t": 90.0, "type": "turn_timing", "turn": 3, "first_audio_played": None,
         "first_audio_to_client": 23.9, "play_end": None},
        # Acked as ended with no start: cut before it played.
        {"t": 34.0, "type": "turn_timing", "turn": 4, "first_audio_played": None,
         "first_audio_to_client": 33.9, "play_end": 34.0},
        _pair(6.0, "a", "one", page_turn=1),
        _pair(16.0, "a", "two", page_turn=2),
        _pair(26.0, "a", "three", page_turn=3),
        _pair(36.0, "a", "four", page_turn=4),
        _pair(46.0, "a", "nothing on record", page_turn=5),
        _pair(56.0, "a", "never announced", page_turn=None),
    ])
    heard = [t["heard_at"] for t in encounter_record.build(sdir)["transcript"]]
    assert heard == [4.0, 14.0, 23.9, None, None, None]


def test_an_older_record_keeps_the_order_it_always_had(tmp_path):
    """No spoken_at, no page_turn: sorted by `t`, as before, even with the
    acks present to join on."""
    events = [
        {"t": 0.0, "type": "session_start"},
        {"t": 194.07, "type": "play_start", "turn": 21, "at": 194.07},
        _pair(197.15, "priya", "No. Last year it all went at once."),
        _pair(202.226, "dan", "That is the model, not the date."),
        _user(203.388, "I wonder if you could...", voiced_span_ms=11000),
        {"t": 208.272, "type": "play_start", "turn": 22, "at": 208.272},
    ]
    rec = encounter_record.build(_write(tmp_path, events))
    assert _order(rec) == ["priya", "dan", "you"]
    assert [t["t"] for t in rec["transcript"]] == [197.15, 202.226, 203.388]
    assert all(t.get("spoken_at") is None and t.get("heard_at") is None
               for t in rec["transcript"])
