"""Issue #25: the room's post-turn steering review runs off the floor
(room pacing 2026-09-28a).

_run_group_turn awaited self._steer() while it still held self._floor, so the
next routed participant turn waited for the review (director_route to knob_set
measured 0.8-1.2 s on 2026-09-24c) before its own routing could start. A
room's shifts re-brief nobody at that moment anyway (knob_set delivered=False;
the next _brief_member or the interaction re-brief carries them), so the review
now runs as a tracked task after the floor is released: one at a time and in
turn order, cancelled with the group turns at an interaction change and at
teardown, and its failures written where group-turn failures are.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
for p in (ROOT, ROOT / "tests"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from test_group_model_passthrough import GPT, _runner_with_room  # noqa: E402


@pytest.fixture(autouse=True)
def _quick_grants(monkeypatch):
    # The fake members never start a reply, so a commit-only grant would wait
    # the whole ROOM_GRANT_UNANSWERED_S; the wait is not what this is about.
    monkeypatch.setenv("ROOM_GRANT_UNANSWERED_S", "0.3")
    # test_group_model_passthrough's own short waits (its autouse fixture
    # does not come with the import).
    monkeypatch.setenv("ROUTE_TRANSCRIPT_WAIT", "0.05")
    monkeypatch.setenv("AUTOFIRE_WAIT", "0.05")
    monkeypatch.setenv("TRANSCRIPT_GRACE_SECONDS", "0.1")


def _room_runner():
    return _runner_with_room(GPT, [{"agent_id": "priya", "intent": None}])


def test_the_floor_is_free_while_the_room_is_being_steered():
    runner, session, room = _room_runner()
    seen = {}

    async def go():
        release = asyncio.Event()

        async def slow_review(*, delivered=None):
            seen["floor_locked"] = runner._floor.locked()
            seen["delivered"] = delivered
            await release.wait()
        session.auto_steer = slow_review
        # Before 2026-09-28a this never returned: the review held the floor.
        await asyncio.wait_for(runner._run_group_turn(), timeout=3)
        assert not runner._floor.locked(), "the next turn would wait for steering"
        assert runner._room_steer_tasks, "the review is tracked"
        release.set()
        await asyncio.wait_for(
            asyncio.gather(*runner._room_steer_tasks), timeout=3)
        assert not runner._room_steer_tasks, "a finished review lets go of itself"

    asyncio.run(go())
    assert seen == {"floor_locked": False, "delivered": False}, (
        "a room shift is still written as having reached nobody")


def test_reviews_never_overlap_and_run_in_turn_order():
    runner, session, room = _room_runner()
    log = []

    async def go():
        gates = [asyncio.Event(), asyncio.Event()]

        async def review(*, delivered=None):
            n = len([x for x in log if x[0] == "start"])
            log.append(("start", n))
            await gates[n].wait()
            log.append(("end", n))
        session.auto_steer = review
        runner._spawn_room_steer()
        runner._spawn_room_steer()
        await asyncio.sleep(0.05)
        assert log == [("start", 0)], "a second review started beside the first"
        gates[0].set()
        await asyncio.sleep(0.05)
        gates[1].set()
        await asyncio.gather(*runner._room_steer_tasks)

    asyncio.run(go())
    assert log == [("start", 0), ("end", 0), ("start", 1), ("end", 1)]


def test_an_interaction_change_cancels_a_review_in_flight():
    runner, session, room = _room_runner()
    cancelled = []

    async def go():
        async def review(*, delivered=None):
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                cancelled.append(True)
                raise
        session.auto_steer = review
        runner._spawn_room_steer()
        await asyncio.sleep(0.05)
        await runner._close_room()
        await asyncio.sleep(0.05)
        assert not runner._room_steer_tasks

    asyncio.run(go())
    assert cancelled == [True]
    assert not session.store.of("voice_error"), "a cancel is not a failure"


def test_a_review_that_raises_is_written_down():
    runner, session, room = _room_runner()

    async def go():
        async def review(*, delivered=None):
            raise RuntimeError("steering blew up")
        session.auto_steer = review
        runner._spawn_room_steer()
        await asyncio.sleep(0.05)

    asyncio.run(go())
    (err,) = session.store.of("voice_error")
    assert err["where"] == "room_steer" and "steering blew up" in err["message"]


def test_nothing_is_spawned_once_the_encounter_is_closed():
    runner, session, room = _room_runner()
    calls = []

    async def go():
        async def review(*, delivered=None):
            calls.append(1)
        session.auto_steer = review
        runner._closed = True
        runner._spawn_room_steer()
        await asyncio.sleep(0.05)

    asyncio.run(go())
    assert calls == [] and not runner._room_steer_tasks
