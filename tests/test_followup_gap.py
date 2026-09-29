"""Issues #24 and #48: a second character follows on only after the line
before it has been heard out, plus a second of silence (the researchers'
decision of 2026-09-29, room pacing 2026-09-29a).

The follow-up loop in _run_group_turn granted the next character as soon as
the previous reply's response_done fired. That is generation end, about 8 s
before the line finished PLAYING, so the next line was generated and queued
behind it and started 0.0-0.2 s after it on the page (26 character-to-
character handoffs at 0.0 s in s_1790278989_77ee7e). It read as characters
cutting each other off (#48) and left the participant no opening (#24).

Now the loop waits for the page to finish the line (its play_end ack, the
signal the turn cue waits for) and then FOLLOWUP_GAP_S of silence, and a
participant who speaks in that gap, or whose line is accepted while it runs,
has the floor: the follow-up yields (followup_yielded) and is not kept for
later. Every group form paces the same way; nothing here is per scenario.
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

from server import group_room as gr  # noqa: E402
from server import llm  # noqa: E402
from server import realtime_voice_session as rvs  # noqa: E402
from server.voice.realtime import capabilities_for  # noqa: E402
from test_group_model_passthrough import (  # noqa: E402
    GPT, FakeSession, FakeWS, MemberRT, ScriptedDirector,
)

LINE_S = 0.4      # how long the first character's line plays on the page
ACK_LATE_S = 0.1  # the page's play_end reaches the runner after the model's end
GAP_S = 0.3       # FOLLOWUP_GAP_S here (1.0 in the study; see the provenance test)


@pytest.fixture(autouse=True)
def _short_waits(monkeypatch):
    # The fake members never start a reply, so a commit-only grant would wait
    # the whole ROOM_GRANT_UNANSWERED_S (6 s); that wait is not this one.
    monkeypatch.setenv("ROOM_GRANT_UNANSWERED_S", "0.05")
    monkeypatch.setenv("ROUTE_TRANSCRIPT_WAIT", "0.05")
    monkeypatch.setenv("AUTOFIRE_WAIT", "0.05")
    monkeypatch.setenv("TRANSCRIPT_GRACE_SECONDS", "0.1")
    monkeypatch.setenv("FOLLOWUP_GAP_S", str(GAP_S))


def _paced_room(scenario_id="S4A", while_generating=None):
    """A gpt room whose director routes the scenario's second and third
    characters, and whose page plays each granted line for LINE_S and acks
    its end ACK_LATE_S after the playback clock's own end. `while_generating`
    runs while the first line is still being generated."""
    session = FakeSession(scenario_id, ScriptedDirector([]))
    runner = rvs.RealtimeVoiceSessionRunner(session, FakeWS())
    cast = [a.id for a in runner._resolve_agents()]
    session.director.entries = [{"agent_id": cast[1], "intent": None},
                                {"agent_id": cast[2], "intent": None}]
    room = gr.GroupRoom(runner._resolve_agents(),
                        instructions_for=lambda a: runner._instructions_for(a),
                        voice_for=lambda a: "", tools=[], model=GPT)
    voices = capabilities_for(GPT).voices
    for i, aid in enumerate(cast):
        room.sessions[aid] = MemberRT(voice=voices[i], model=GPT)
    grants, acks, cue_in_gap = [], [], []
    granted = room.give_floor

    async def give_floor(agent_id):
        rt = await granted(agent_id)
        now = time.time()
        grants.append((agent_id, now))
        seq = len(grants)
        # The page is playing this line now: the playback clock runs LINE_S
        # ahead, and the turn cue holds the line until the page acks its end.
        runner._play_cursor = now + LINE_S
        runner._cue_turns[seq] = {"agent_id": agent_id, "audio": True,
                                  "done": True, "at": now, "end": now + LINE_S}

        async def page_acks():
            await asyncio.sleep(LINE_S + ACK_LATE_S)
            acks.append(time.time())
            await runner._handle_client_command(json.dumps(
                {"type": "playback", "phase": "end", "turn": seq,
                 "interrupted": False}))
            await asyncio.sleep(GAP_S / 2)
            cue_in_gap.append(runner._turn_cue_blocker())
        asyncio.ensure_future(page_acks())
        if while_generating is not None and seq == 1:
            await while_generating(runner)
        runner._response_done.set()
        return rt
    room.give_floor = give_floor

    async def nothing():
        return None
    runner._advance_when_spent = nothing
    runner.room = room
    session.append_agent(cast[0], "So that is the plan.")
    runner._last_user_text = "I want to come back to the timeline."
    return runner, session, cast, grants, acks, cue_in_gap


@pytest.mark.parametrize("scenario_id", ["S3A", "S3B", "S4A", "S4B"])
def test_a_follow_up_waits_until_the_line_before_it_has_been_heard(scenario_id):
    runner, session, cast, grants, acks, cue_in_gap = _paced_room(scenario_id)
    asyncio.run(asyncio.wait_for(runner._run_group_turn(), timeout=10))

    assert [aid for aid, _ in grants] == [cast[1], cast[2]]
    assert acks, "the page never finished the first line"
    followed_at = grants[1][1]
    assert followed_at >= acks[0] + GAP_S - 0.01, (
        f"{cast[2]} was given the floor {followed_at - acks[0]:.2f}s after "
        f"the page finished {cast[1]}'s line, inside the {GAP_S}s gap")
    assert followed_at <= acks[0] + GAP_S + 0.25, "the gap overran"
    # The cue stays shut through the gap: a "You can speak now" here would be
    # followed a second later by a character talking, with no change of cue.
    assert cue_in_gap and cue_in_gap[0] is not None
    assert not [f for f in runner.ws.json if f.get("type") == "turn_open"]
    assert not session.store.of("followup_yielded")


@pytest.mark.parametrize("scenario_id", ["S3A", "S4A"])
def test_the_participant_speaking_in_the_gap_has_the_floor(scenario_id):
    runner, session, cast, grants, acks, _ = _paced_room(scenario_id)

    async def go():
        async def speaks_in_the_gap():
            while not acks:
                await asyncio.sleep(0.01)
            await asyncio.sleep(GAP_S / 3)
            runner.vad.speaking = True
        helper = asyncio.ensure_future(speaks_in_the_gap())
        await asyncio.wait_for(runner._run_group_turn(), timeout=10)
        await helper
    asyncio.run(go())

    assert [aid for aid, _ in grants] == [cast[1]], (
        "the follow-up was granted over the participant")
    (ev,) = session.store.of("followup_yielded")
    assert ev["agent_id"] == cast[2] and ev["reason"] == "participant_speaking"


@pytest.mark.parametrize("scenario_id", ["S3A", "S4A"])
def test_a_line_accepted_while_the_previous_one_plays_has_the_floor(scenario_id):
    runner, session, cast, grants, acks, _ = _paced_room(scenario_id)

    async def go():
        async def says_something():
            while not grants:
                await asyncio.sleep(0.01)
            await asyncio.sleep(LINE_S / 2)
            await runner._record_user_turn("Can I come in on that?",
                                           voiced_ms=900)
        helper = asyncio.ensure_future(says_something())
        await asyncio.wait_for(runner._run_group_turn(), timeout=10)
        await helper
    asyncio.run(go())

    assert [aid for aid, _ in grants] == [cast[1]]
    (ev,) = session.store.of("followup_yielded")
    assert ev["agent_id"] == cast[2] and ev["reason"] == "user_turn"


def test_a_line_accepted_before_the_first_reply_ended_has_the_floor():
    """Accepted after this turn was routed, so it waits unrouted as the next
    turn: the follow-up would answer the room instead of it."""
    async def says_something(runner):
        await runner._record_user_turn("Wait, who owns the date?",
                                       voiced_ms=900)
    runner, session, cast, grants, acks, _ = _paced_room(
        while_generating=says_something)
    # Routed on words of its own, so the line above is a new one rather than
    # this turn's transcript arriving late.
    runner._unrouted_user_texts = [runner._last_user_text]
    asyncio.run(asyncio.wait_for(runner._run_group_turn(), timeout=10))
    assert [aid for aid, _ in grants] == [cast[1]]
    (ev,) = session.store.of("followup_yielded")
    assert ev["agent_id"] == cast[2] and ev["reason"] == "user_turn"


def test_the_voice_detector_during_the_line_itself_is_not_a_yield():
    """While the line before plays, the participant's speakers can open the
    VAD on the character's own voice, and a real interjection over a line is
    the barge-in's (it stops that line, which opens the gap at once). So the
    VAD counts in the gap, and an accepted line counts throughout."""
    runner, session, cast, grants, acks, _ = _paced_room()

    async def go():
        async def echo_during_the_line():
            while not grants:
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.05)
            runner.vad.speaking = True
            await asyncio.sleep(LINE_S / 2)
            runner.vad.speaking = False
        helper = asyncio.ensure_future(echo_during_the_line())
        await asyncio.wait_for(runner._run_group_turn(), timeout=10)
        await helper
    asyncio.run(go())

    assert [aid for aid, _ in grants] == [cast[1], cast[2]]
    assert not session.store.of("followup_yielded")


def test_the_old_pacing_is_one_knob_away(monkeypatch):
    monkeypatch.setenv("FOLLOWUP_GAP_S", "-1")
    runner, session, cast, grants, acks, _ = _paced_room()
    asyncio.run(asyncio.wait_for(runner._run_group_turn(), timeout=10))
    assert [aid for aid, _ in grants] == [cast[1], cast[2]]
    assert grants[1][1] - grants[0][1] < LINE_S, (
        "FOLLOWUP_GAP_S below 0 still waited for the line to play")


def test_the_gap_and_its_version_are_on_the_record():
    assert llm.provenance(GPT)["pacing"]["followup_gap_s"] == GAP_S
    assert llm.ROOM_PACING_VERSION == "2026-09-29a"
    src = (ROOT / "server" / "llm.py").read_text(encoding="utf-8")
    assert src.count("#   2026-09-29a") == 1, "a history line for the version"
