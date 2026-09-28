"""Issue #21 leftovers (pipeline 2026-09-28a).

1. A participant transcript made of nothing but the transcriber's own sound
   tags ("(laughter)", "[background noise]", "[Music]", "(inaudible)") is not
   something anybody said. gpt-4o-transcribe writes them over bleed, breath
   and room noise, and on 2026-09-24c they passed as participant turns: in
   S4A s_1790278989_77ee7e "(laughter)" at 319.3 s (1000 ms voiced) became the
   steering pair's participant line for Dan, and "[background noise]" at
   411.6 s (2600 ms voiced) was routed by the director to Chris. They are now
   suppressed as no_speech, the word-less rule's path, in 1:1 and in rooms.

2. In 1:1 a no_speech suppression withdraws the reply its commit already
   started, as implausible_rate does. S2A s_1790278762_09bcbb: a commit at
   245.84 s transcribed "。" was suppressed, and Morgan's reply to it ("Take a
   minute if you need it...") still played 248.1-258.3 s, so the character
   spoke twice in a row to nobody, and the beat the commit fired
   (t3_rung2_everyone_stretched) was spent on it.

No network: the fakes the participant-turn tests already use.
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
from server.voice import realtime as R  # noqa: E402

from test_participant_turn_integrity import (  # noqa: E402
    GPT, LOUD, DeadMember, _room, bridge, in_a_loop, runner_for, send_ms,
)

ANNOTATIONS = [
    "(laughter)", "[background noise]", "[Music]", "(inaudible)",
    "[Music] (laughter)", "(laughs).", " [BLANK_AUDIO] ", "♪ [Music] ♪",
    "(Laughter) (Applause)", "((coughs))", "...(sighs)...",
]
SPEECH_WITH_A_TAG = [
    "Yeah (laughs)", "(laughs) okay, fine", "Priya [inaudible] the date",
    "Fine. (sighs) Go on.",
]


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    monkeypatch.setattr(R, "RECV_POLL_S", 0.02)
    monkeypatch.setenv("TRANSCRIPT_GRACE_SECONDS", "0.2")
    monkeypatch.delenv("PARTICIPANT_DROP_ANNOTATIONS", raising=False)


# --------------------------------------------------------------------------
# 1. The rule
# --------------------------------------------------------------------------

@pytest.mark.parametrize("text", ANNOTATIONS)
def test_a_transcript_of_tags_alone_is_an_annotation(text):
    assert R.is_annotation_only(text)


@pytest.mark.parametrize("text", SPEECH_WITH_A_TAG + [
    "", "   ", "...", "Hmm.", "Okay", "(", "Casey (the new one)?"])
def test_a_line_with_a_word_outside_the_tags_is_not(text):
    assert not R.is_annotation_only(text)


@pytest.mark.parametrize("text", ANNOTATIONS)
@pytest.mark.parametrize("voiced", [0, 400, 1000, 2600, None])
def test_an_annotation_is_never_a_turn(text, voiced):
    runner, session, ws = runner_for("S4A")
    asyncio.run(runner._record_user_turn(text, item_id="i9", voiced_ms=voiced))
    (sup,) = session.store.of("user_turn_suppressed")
    assert sup["reason"] == "no_speech" and sup["text"] == text
    assert sup["annotation_only"] is True, "the record says which rule ran"
    assert not session.store.of("user_turn")
    assert not ws.frames("user_transcript"), "no caption"
    assert session.shared_history == [], "no steering or director input"
    # An arrival, and an unreliable one: a room turn for it is skipped.
    assert runner._transcripts_arrived == 1
    assert [a["reason"] for a in runner._unreliable_arrivals] == ["no_speech"]


@pytest.mark.parametrize("text", SPEECH_WITH_A_TAG)
def test_speech_beside_a_tag_is_kept_whole(text):
    runner, session, _ws = runner_for("S4A")
    asyncio.run(runner._record_user_turn(text, voiced_ms=900))
    (turn,) = session.store.of("user_turn")
    assert turn["text"] == text


def test_the_annotation_rule_is_a_knob(monkeypatch):
    monkeypatch.setenv("PARTICIPANT_DROP_ANNOTATIONS", "0")
    assert R.drop_annotations() is False
    runner, session, _ws = runner_for("S4A")
    asyncio.run(runner._record_user_turn("(laughter)", voiced_ms=1000))
    assert session.store.of("user_turn"), "0 restores the 24c behaviour"
    assert not session.store.of("user_turn_suppressed")


def test_the_knob_is_in_provenance(monkeypatch):
    assert llm.provenance(GPT)["turn_gate"]["participant_drop_annotations"] is True
    monkeypatch.setenv("PARTICIPANT_DROP_ANNOTATIONS", "0")
    assert llm.provenance(GPT)["turn_gate"]["participant_drop_annotations"] is False


def test_a_room_does_not_route_background_noise(monkeypatch):
    """s_1790278989: "[background noise]" over 2600 ms of voice was routed to
    Chris. Now the group turn is skipped and the director is not asked."""
    monkeypatch.setenv("ROUTE_TRANSCRIPT_WAIT", "0.6")
    runner, session, _ = runner_for("S4A")
    room = _room(GPT, runner)
    for a in runner._resolve_agents():
        room.sessions[a.id] = DeadMember(GPT)
    runner.room = room
    runner.director = session.director
    session.append_agent("dan", "The date is locked.")
    runner._turn_end_arrivals = runner._transcripts_arrived
    runner._group_turn_waiting = True
    runner._turns_without_transcript = 1
    asyncio.run(runner._record_user_turn("[background noise]", voiced_ms=2600))
    asyncio.run(asyncio.wait_for(runner._run_group_turn(), timeout=10))
    assert session.director.calls == [], "the director is not asked"
    (skip,) = session.store.of("group_turn_skipped")
    assert skip["reason"] == "no_speech" and skip["text"] == "[background noise]"
    assert not session.store.of("floor_grant_failed"), "nobody was granted"
    assert not runner._floor.locked()


# --------------------------------------------------------------------------
# 2. 1:1: the reply a no_speech commit started is withdrawn
# --------------------------------------------------------------------------

def _one_to_one():
    runner, session, ws = runner_for("S2A")
    rt = bridge()
    runner.rt = rt
    return runner, session, ws, rt


@pytest.mark.parametrize("text", ["。", "(laughter)", "[background noise]"])
@in_a_loop
async def test_the_reply_to_a_no_speech_commit_is_cancelled_before_it_plays(text):
    runner, session, ws, rt = _one_to_one()
    await send_ms(rt, LOUD, 400)
    assert await rt.commit_turn() is True          # the commit starts the reply
    committed_at = time.time()
    rt.ws.sent.clear()
    await runner._record_user_turn(text, voiced_ms=400, committed_at=committed_at)
    assert rt.ws.types() == ["response.cancel"]
    (ev,) = session.store.of("suppressed_turn_reply_cancelled")
    assert ev["reason"] == "no_speech" and ev["text"] == text
    assert not rt.responding
    assert session.store.of("user_turn_suppressed")[0]["reason"] == "no_speech"


@in_a_loop
async def test_a_filler_under_no_voice_withdraws_its_reply_too():
    runner, session, ws, rt = _one_to_one()
    await send_ms(rt, LOUD, 400)
    await rt.commit_turn()
    rt.ws.sent.clear()
    await runner._record_user_turn("Um...", voiced_ms=40,
                                   committed_at=rt.last_commit_at)
    (ev,) = session.store.of("suppressed_turn_reply_cancelled")
    assert ev["reason"] == "no_speech"


@in_a_loop
async def test_a_no_speech_reply_already_playing_is_left_and_written_down():
    runner, session, ws, rt = _one_to_one()
    await rt.commit_turn()
    runner._speaking = True
    runner._turn_audio_bytes = 32000               # 1 s at 16 kHz
    rt.ws.sent.clear()
    await runner._record_user_turn("。", voiced_ms=400,
                                   committed_at=time.time() - 3)
    assert "response.cancel" not in rt.ws.types()
    (ev,) = session.store.of("reply_to_suppressed_turn")
    assert ev["reason"] == "no_speech" and ev["playing"] is True


@in_a_loop
async def test_the_beat_the_no_speech_commit_fired_is_given_back():
    """The 09bcbb case: the beat the commit fired goes back, so the next real
    turn performs it rather than the ladder moving on over a non-turn."""
    runner, session, ws, rt = _one_to_one()
    await send_ms(rt, LOUD, 400)
    await rt.commit_turn()
    triggers = runner._triggers()
    assert triggers, "S2A has planted beats"
    tid = triggers[0]["id"]
    runner._trigger_idx = 1
    runner._fired = [tid]
    runner._pending_direction = {"trigger_id": tid, "agent_id": runner.agent_id,
                                 "interaction": runner._interaction_id()}
    await runner._record_user_turn("。", voiced_ms=400,
                                   committed_at=rt.last_commit_at)
    (back,) = session.store.of("trigger_undelivered")
    assert back["trigger_id"] == tid and back["reason"] == "reply_withdrawn"
    assert runner._trigger_idx == 0 and runner._fired == []


@in_a_loop
async def test_nothing_is_withdrawn_in_a_room():
    runner, session, ws = runner_for("S4A")
    rt = bridge()
    runner.rt = rt
    await send_ms(rt, LOUD, 400)
    await rt.commit_turn()
    rt.ws.sent.clear()
    await runner._record_user_turn("(laughter)", voiced_ms=400,
                                   committed_at=rt.last_commit_at)
    assert "response.cancel" not in rt.ws.types()
    assert not session.store.of("suppressed_turn_reply_cancelled")
