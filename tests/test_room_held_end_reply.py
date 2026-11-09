"""A room member whose whole reply is a held end_conversation call is asked
to speak (issue #181; s_1794253712_581349, S4A, 2026-11-09).

The record: at 294.8 s Chris answered the participant with end_conversation
and nothing else. The call was held (the encounter does not end before its
ceiling), the floor was released, and nobody was asked to say anything. The
participant's "Did you hear me?" drew the same from Dan, and the room was then
silent for 87 s until the participant pressed End.

The floor holder is now given the floor again, once, after its call has been
answered. A member that spoke before calling is not asked again, and a second
call in the asked-for reply is not asked after.
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for p in (ROOT, ROOT / "tests"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from test_room_member_reconnect import (  # noqa: E402
    Room, created, done_out, in_a_loop, reply, until,
)

LINE = "Dan, are you willing to bet the rollout on everyone going live the same day?"


def end_call(rid, call_id):
    """A reply that is the end_conversation call and nothing else."""
    return [{"type": "response.function_call_arguments.done",
             "response_id": rid, "name": "end_conversation",
             "call_id": call_id, "arguments": "{}"},
            done_out(rid, [])]


def _last_interaction(h, *, ago):
    r = h.runner
    r.segment = len(r.interactions) - 1
    r._encounter_started_at = r._first_line_at = time.time() - ago


async def _turn_until_asked(h, commits):
    await h.participant(LINE)
    turn = asyncio.ensure_future(h.runner._run_group_turn())
    await until(lambda: commits)
    # The grant itself is over (the gateway began the reply) before the reply
    # turns out to be the call alone, as it was on the day.
    await until(lambda: (h.room.last_grant or {}).get("via") == "commit")
    return turn


def _asks(ws):
    return [m.get("type") for m in ws.sent].count("response.create")


@in_a_loop
async def test_a_member_whose_reply_was_only_a_held_end_call_is_asked_to_speak(monkeypatch):
    h = await Room(monkeypatch).open()
    try:
        _last_interaction(h, ago=250)
        commits = []

        def on_commit(ws, f):
            commits.append(ws)
            return [created(f"R{len(commits)}")]
        h.gateway.on_commit = on_commit
        turn = await _turn_until_asked(h, commits)
        dan_ws = h.rt("dan").ws
        assert commits == [dan_ws]
        before = _asks(dan_ws) + len(commits)
        dan_ws.feed(end_call("R1", "call_E1"))
        # Asked again: a fresh reply on Dan's session, by whichever route.
        await until(lambda: _asks(dan_ws) + len(commits) > before)
        await until(lambda: h.of("held_call_reply"))
        dan_ws.feed(reply("R2", "No. Split it, Sales first."))
        await asyncio.wait_for(turn, 10)
    finally:
        await h.close()
    (held,) = h.of("auto_end_held")
    assert held["reason"] == "end_conversation"
    (ans,) = h.of("tool_call_answered")
    assert ans["agent_id"] == "dan" and ans["reply_requested"] is False
    (asked,) = h.of("held_call_reply")
    assert asked["agent_id"] == "dan" and asked["requested"] is True and asked["room"] is True
    (said,) = h.of("assistant_turn")
    assert said["agent_id"] == "dan" and said["text"] == "No. Split it, Sales first."
    assert not h.of("group_turn_timeout")
    assert not [f for f in h.page.json if f.get("type") == "encounter_complete"]


@in_a_loop
async def test_a_second_call_in_the_asked_for_reply_is_not_asked_after(monkeypatch):
    h = await Room(monkeypatch).open()
    try:
        _last_interaction(h, ago=250)
        commits = []

        def on_commit(ws, f):
            commits.append(ws)
            return [created(f"R{len(commits)}")]
        h.gateway.on_commit = on_commit
        turn = await _turn_until_asked(h, commits)
        dan_ws = h.rt("dan").ws
        before = _asks(dan_ws) + len(commits)
        dan_ws.feed(end_call("R1", "call_E1"))
        await until(lambda: _asks(dan_ws) + len(commits) > before)
        await until(lambda: h.of("held_call_reply"))
        dan_ws.feed(end_call("R2", "call_E2"))
        await asyncio.wait_for(turn, 10)
    finally:
        await h.close()
    # Asked once: the second call is answered and the turn ends there.
    assert len(h.of("held_call_reply")) == 1
    assert len(commits) == 2
    assert len(h.of("tool_call_answered")) == 2
    assert not h.of("assistant_turn")


@in_a_loop
async def test_a_member_that_spoke_before_calling_is_not_asked_again(monkeypatch):
    h = await Room(monkeypatch).open()
    try:
        _last_interaction(h, ago=250)
        commits = []

        def on_commit(ws, f):
            commits.append(ws)
            return [created(f"R{len(commits)}")]
        h.gateway.on_commit = on_commit
        turn = await _turn_until_asked(h, commits)
        dan_ws = h.rt("dan").ws
        before = _asks(dan_ws) + len(commits)
        spoken = reply("R1", "That is the plan. We are done here.")
        call = {"type": "response.function_call_arguments.done",
                "response_id": "R1", "name": "end_conversation",
                "call_id": "call_E1", "arguments": "{}"}
        # The goodbye, the call, then the reply's own done.
        dan_ws.feed(spoken[:-1] + [call, spoken[-1]])
        await asyncio.wait_for(turn, 10)
        assert _asks(dan_ws) + len(commits) == before
    finally:
        await h.close()
    assert not h.of("held_call_reply")
    (said,) = h.of("assistant_turn")
    assert said["text"] == "That is the plan. We are done here."
