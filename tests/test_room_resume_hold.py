"""#299: a participant who pauses mid-sentence gets one reply, not two.

Ben, S3A 2026-11-15: he paused about a second, the 900 ms end of turn
committed the first half, a character was granted it, and the second half,
still being transcribed at the grant, became its own turn whose reply queued
right behind the first. The resume hold keeps the first grant back while that
transcript is owed, so merge_accepted folds the second half into the turn.
"""
import asyncio

import pytest

from cascade_room_harness import (CASCADE, WORDS, Transcript, wire, speak,
                                  finished, assert_heard)
from test_room_member_reconnect import Room, in_a_loop, until

SECOND = "and the second half of that same thought about the rota"


async def _two_halves(monkeypatch, hold):
    monkeypatch.setenv("ROOM_RESUME_HOLD_S", hold)
    owed = asyncio.Event()
    chats, _ = wire(monkeypatch, [Transcript(WORDS), Transcript(SECOND, owed)])
    h = await Room(monkeypatch, model=CASCADE).open()
    in_brief, resume = asyncio.Event(), asyncio.Event()
    original = h.runner._brief_member
    paused = False

    async def brief(aid, **kw):
        nonlocal paused
        if not paused:
            paused = True
            in_brief.set()
            await resume.wait()
        await original(aid, **kw)

    monkeypatch.setattr(h.runner, "_brief_member", brief)
    return h, chats, owed, in_brief, resume


@in_a_loop
async def test_the_second_half_joins_the_turn_while_its_transcript_is_owed(monkeypatch):
    h, chats, owed, in_brief, resume = await _two_halves(monkeypatch, "8")
    try:
        await speak(h)
        await in_brief.wait()
        await speak(h)                      # he goes on; its transcript is owed
        resume.set()
        await asyncio.sleep(0.4)
        assert chats == []                  # held, not granted on the first half
        owed.set()
        await until(lambda: len(chats) == 1)
        await finished(h)
        assert len(chats) == 1              # one character answers the whole turn
        assert_heard(chats[0], [WORDS, SECOND])
        held = h.of("participant_resume_held")
        assert held and held[0]["reason"] == "transcript_owed"
        assert held[0]["merged"] is True and held[0]["capped"] is False
        assert h.room.hearing.active_turn.commits == [1, 2]
    finally:
        owed.set()
        resume.set()
        await h.close()


@in_a_loop
async def test_hold_off_keeps_the_old_two_replies(monkeypatch):
    """The control: with ROOM_RESUME_HOLD_S=0 the first half is granted at
    once and the second half is answered as its own turn (Ben's experience)."""
    h, chats, owed, in_brief, resume = await _two_halves(monkeypatch, "0")
    try:
        await speak(h)
        await in_brief.wait()
        await speak(h)
        resume.set()
        await until(lambda: len(chats) == 1)
        owed.set()
        await until(lambda: len(chats) == 2, 10)
        await finished(h)
        assert len(chats) == 2
        assert h.of("participant_resume_held") == []
    finally:
        owed.set()
        resume.set()
        await h.close()


@in_a_loop
async def test_a_turn_with_nothing_after_it_is_granted_without_a_hold(monkeypatch):
    monkeypatch.setenv("ROOM_RESUME_HOLD_S", "8")
    chats, _ = wire(monkeypatch, [Transcript(WORDS)])
    h = await Room(monkeypatch, model=CASCADE).open()
    try:
        await speak(h)
        await until(lambda: len(chats) == 1)
        await finished(h)
        assert h.of("participant_resume_held") == []
    finally:
        await h.close()


@in_a_loop
async def test_the_hold_is_bounded(monkeypatch):
    """A transcript that never comes holds the grant only ROOM_RESUME_HOLD_S."""
    h, chats, owed, in_brief, resume = await _two_halves(monkeypatch, "0.5")
    try:
        await speak(h)
        await in_brief.wait()
        await speak(h)
        resume.set()
        await until(lambda: len(chats) == 1, 5)
        held = h.of("participant_resume_held")
        assert held and held[0]["capped"] is True and held[0]["merged"] is False
        assert held[0]["waited_s"] < 1.5
    finally:
        owed.set()
        resume.set()
        await h.close()


async def _feed(h, frames):
    h.page._frames = frames
    await h.runner._client_to_model()


@in_a_loop
async def test_still_speaking_at_the_grant_holds_until_they_finish(monkeypatch):
    """Ben's usual case (12 of 14): he is already talking again, with no
    commit yet, when the first character would be granted."""
    from cascade_room_harness import QUIET
    from test_bridge_correctness import LOUD
    monkeypatch.setenv("ROOM_RESUME_HOLD_S", "8")
    chats, _ = wire(monkeypatch, [Transcript(WORDS), Transcript(SECOND)])
    h = await Room(monkeypatch, model=CASCADE).open()
    in_brief, resume = asyncio.Event(), asyncio.Event()
    original = h.runner._brief_member
    paused = False

    async def brief(aid, **kw):
        nonlocal paused
        if not paused:
            paused = True
            in_brief.set()
            await resume.wait()
        await original(aid, **kw)

    monkeypatch.setattr(h.runner, "_brief_member", brief)
    try:
        await speak(h)
        await in_brief.wait()
        await _feed(h, [LOUD] * 60)          # talking again, no turn end yet
        assert h.runner.vad.speaking and h.runner.vad.barge_in
        resume.set()
        await asyncio.sleep(0.4)
        assert chats == []
        await _feed(h, [QUIET] * 120)        # they finish: turn end, commit
        await until(lambda: len(chats) == 1, 10)
        await finished(h)
        assert len(chats) == 1
        assert_heard(chats[0], [WORDS, SECOND])
        held = h.of("participant_resume_held")
        assert held[0]["reason"] == "participant_speaking" and held[0]["merged"] is True
    finally:
        resume.set()
        await h.close()


@in_a_loop
async def test_a_short_sound_does_not_start_a_hold(monkeypatch):
    """Under the barge-in bar (a click, a breath, a short "mm") the plain VAD
    may open, but the first reply is granted without waiting on it."""
    from test_bridge_correctness import LOUD
    monkeypatch.setenv("ROOM_RESUME_HOLD_S", "8")
    chats, _ = wire(monkeypatch, [Transcript(WORDS), Transcript("")])
    h = await Room(monkeypatch, model=CASCADE).open()
    in_brief, resume = asyncio.Event(), asyncio.Event()
    original = h.runner._brief_member
    paused = False

    async def brief(aid, **kw):
        nonlocal paused
        if not paused:
            paused = True
            in_brief.set()
            await resume.wait()
        await original(aid, **kw)

    monkeypatch.setattr(h.runner, "_brief_member", brief)
    try:
        await speak(h)
        await in_brief.wait()
        h.runner.vad.barge_ms = 10_000       # this sound never reaches the bar
        await _feed(h, [LOUD] * 20)
        assert h.runner.vad.speaking and not h.runner.vad.barge_in
        resume.set()
        await until(lambda: len(chats) == 1, 3)
        assert h.of("participant_resume_held") == []
    finally:
        resume.set()
        await h.close()
