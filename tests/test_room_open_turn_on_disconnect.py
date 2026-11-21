"""#224: the participant's last line must reach `user_turn` when the page goes.

Clicking End closes the socket at once and sends no end message. The server
used to cancel the room with whatever the participant had said still open:
before the 900 ms of quiet that closes a turn nothing was committed, and once
committed, `_close_room` expired the pending slot and the cascade scribe
cancelled its STT call. The audio stayed in user_audio.wav; the line was
missing from `user_turn`, which is what raters read.

Teardown below is what run()'s finally does: `_closed` set, then `_close_room`.
"""
import asyncio
import time

from cascade_room_harness import (CASCADE, WORDS, Transcript, wire, speak, QUIET,
                                  LOUD)
from test_room_member_reconnect import Room, in_a_loop, until
from test_room_resume_hold import SECOND, _two_halves


def texts(h):
    return [e["text"] for e in h.of("user_turn")]


async def teardown(h, *, release_after=None, gate=None):
    """The page is gone: run()'s teardown, timed. A slow STT call answers
    `release_after` seconds into it."""
    if gate is not None and release_after is not None:
        asyncio.get_running_loop().call_later(release_after, gate.set)
    h.runner._closed = True
    started = time.monotonic()
    await h.runner._close_room()
    return time.monotonic() - started


@in_a_loop
async def test_disconnect_before_the_turn_closes_keeps_the_line(monkeypatch):
    """(a) End inside the 900 ms of quiet: the runner's VAD never closed the
    turn, so nothing was committed to the scribe."""
    gate = asyncio.Event()
    transcript = Transcript(WORDS, gate)
    chats, _ = wire(monkeypatch, [transcript])
    h = await Room(monkeypatch, model=CASCADE).open()
    try:
        h.page._frames = [LOUD] * 100 + [QUIET] * 20       # 400 ms of quiet
        await h.runner._client_to_model()
        assert h.of("user_turn") == []                     # turn still open
        await teardown(h, release_after=0.3, gate=gate)
        assert texts(h) == [WORDS]
        assert chats == []                                 # saved, not answered
    finally:
        gate.set()


@in_a_loop
async def test_disconnect_during_the_stt_call_keeps_the_line(monkeypatch):
    """(b) The turn closed and was committed; the transcript is still on its
    way when the socket goes."""
    gate = asyncio.Event()
    transcript = Transcript(WORDS, gate)
    chats, _ = wire(monkeypatch, [transcript])
    h = await Room(monkeypatch, model=CASCADE).open()
    try:
        await speak(h)                                     # 100 loud, 120 quiet
        await until(transcript.started.is_set)
        assert h.of("user_turn") == []                     # STT in flight
        await teardown(h, release_after=0.3, gate=gate)
        assert texts(h) == [WORDS]
        assert chats == []                                 # saved, not answered
    finally:
        gate.set()


@in_a_loop
async def test_end_during_the_resume_hold_keeps_the_second_half_and_does_not_wait_it_out(monkeypatch):
    """(c) #305's hold is waiting (8 s) for the second half's transcript when
    End is clicked. The line is saved, and teardown waits for the scribe, not
    for ROOM_RESUME_HOLD_S."""
    monkeypatch.setenv("CLOSE_TRANSCRIPT_WAIT_S", "3")
    h, chats, owed, in_brief, resume = await _two_halves(monkeypatch, "8")
    try:
        await speak(h)
        await in_brief.wait()
        await speak(h)                      # second half; its transcript is owed
        resume.set()
        await asyncio.sleep(0.4)
        assert chats == []                  # the hold is waiting
        waited = await teardown(h, release_after=0.3, gate=owed)
        assert SECOND in texts(h)
        assert waited < 3.0                 # not the 8 s hold
        assert chats == []                  # nobody answers it
    finally:
        owed.set()
        resume.set()


@in_a_loop
async def test_the_wait_for_the_scribe_is_bounded(monkeypatch):
    """A transcript that never comes costs the teardown CLOSE_TRANSCRIPT_WAIT_S
    and no more. (Passes before the fix too: it guards the fix's bound.)"""
    monkeypatch.setenv("CLOSE_TRANSCRIPT_WAIT_S", "0.5")
    gate = asyncio.Event()                  # never released
    transcript = Transcript(WORDS, gate)
    wire(monkeypatch, [transcript])
    h = await Room(monkeypatch, model=CASCADE).open()
    try:
        await speak(h)
        await until(transcript.started.is_set)
        waited = await teardown(h)
        assert waited < 2.0
        assert h.of("user_turn") == []
    finally:
        gate.set()


@in_a_loop
async def test_a_scribe_that_blocks_closing_the_turn_costs_only_the_bound(monkeypatch):
    """close_participant_turn sends to the scribe and can block on a stalled
    socket: it is under the same deadline, and teardown still finishes."""
    monkeypatch.setenv("CLOSE_TRANSCRIPT_WAIT_S", "0.5")
    wire(monkeypatch, [Transcript(WORDS)])
    h = await Room(monkeypatch, model=CASCADE).open()
    blocked = asyncio.Event()                # never set

    async def hangs():
        await blocked.wait()
    monkeypatch.setattr(h.room, "close_participant_turn", hangs)
    h.page._frames = [LOUD] * 100 + [QUIET] * 20
    await h.runner._client_to_model()
    waited = await teardown(h)
    assert waited < 2.0
    saved = h.of("open_turn_saved_on_close")
    assert saved and saved[0]["close_timed_out"] is True
    assert h.of("user_turn") == []
