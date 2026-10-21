"""The keep-going fallback: a silent participant is still prompted once every
planted beat is spent.

s_1792620472_d091ed (S3A, production 2026-10-21): all seven planted beats fired
by 2:12, the participant answered with agreements ("Sounds good."), Casey said
she should get back to her desk, Alex called end_conversation (held to the
ceiling by the end policy of 2026-09-28), and the next three minutes had no
spoken line. The silence watchdog only ever probed with a planted beat that had
not fired yet, so with none left it had nothing to say.

An interaction can now list `after_beats` ({agent, on_silence}); once the planted
beats are spent they prompt a silent participant, cycle twice, and are NOT
planted beats: they write keep_going_probe (not trigger_fired), spend no
_trigger_idx, carry no ESCI items. Offline, on the room harness the reconnect
tests use.
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

from server import scenarios_v3 as v3  # noqa: E402
from test_room_member_reconnect import (  # noqa: E402,F401
    Room, in_a_loop, quick, reply, until,
)

AFTER = [
    {"agent": "priya", "on_silence": "check one detail with one short question."},
    {"agent": "chris", "on_silence": "say what is still unclear, in one sentence."},
]


def _exhaust(runner):
    """Every planted beat spent, and the interaction listing its after_beats."""
    runner._interaction()["after_beats"] = [dict(b) for b in AFTER]
    runner._trigger_idx = len(runner._triggers())


@in_a_loop
async def test_a_planted_beat_still_comes_first(monkeypatch):
    h = await Room(monkeypatch).open()
    try:
        h.runner._interaction()["after_beats"] = [dict(b) for b in AFTER]
        assert h.runner._trigger_idx == 0
        assert h.runner._keep_going_trigger() is None
        assert h.runner._next_probe_trigger() == h.runner._next_trigger()
    finally:
        await h.close()


@in_a_loop
async def test_once_the_beats_are_spent_the_listed_moves_follow_in_order_then_stop(monkeypatch):
    h = await Room(monkeypatch).open()
    try:
        r = h.runner
        _exhaust(r)
        assert r._next_trigger() is None, "a participant's turn must not be handed one"
        seen = []
        for _ in range(2 * len(AFTER)):
            t = r._next_probe_trigger()
            assert t and t["keep_going"] is True and t["esci"] == []
            seen.append((t["id"], t["agent"], t["on_silence"]))
            r._fire_trigger(t, probing=True)
        assert [s[1] for s in seen] == ["priya", "chris", "priya", "chris"]
        assert [s[0] for s in seen] == [f"keep_going_{i}" for i in (1, 2, 3, 4)]
        assert r._next_probe_trigger() is None, "two cycles, then it stops nagging"
    finally:
        await h.close()


@in_a_loop
async def test_a_keep_going_move_is_not_a_planted_beat(monkeypatch):
    h = await Room(monkeypatch).open()
    try:
        r = h.runner
        _exhaust(r)
        idx, fired = r._trigger_idx, list(r._fired)
        r._fire_trigger(r._next_probe_trigger(), probing=True)
        assert r._trigger_idx == idx and r._fired == fired, "coverage must not move"
        assert not h.of("trigger_fired")
        (kg,) = h.of("keep_going_probe")
        assert kg["trigger_id"] == "keep_going_1" and kg["agent_id"] == "priya"
    finally:
        await h.close()


@in_a_loop
async def test_an_interaction_with_no_after_beats_behaves_as_before(monkeypatch):
    h = await Room(monkeypatch).open()
    try:
        h.runner._trigger_idx = len(h.runner._triggers())
        assert h.runner._keep_going_trigger() is None
        assert h.runner._next_probe_trigger() is None
    finally:
        await h.close()


@in_a_loop
async def test_a_brief_on_a_participants_turn_never_uses_a_keep_going_move(monkeypatch):
    """Only a PROBE may use one: briefing a member on a normal turn with the
    beats spent must do nothing, as it did before."""
    h = await Room(monkeypatch).open()
    try:
        _exhaust(h.runner)
        await h.runner._brief_member("priya", probing=False)
        assert not h.of("keep_going_probe") and not h.of("stage_direction")
    finally:
        await h.close()


@in_a_loop
async def test_the_silence_probe_gives_the_floor_to_the_listed_character(monkeypatch):
    h = await Room(monkeypatch).open()
    try:
        commits = []

        def on_commit(ws, f):
            commits.append(ws)
            from test_bridge_correctness import created
            return [created(f"K{len(commits)}")]
        h.gateway.on_commit = on_commit
        await h.participant("Right. We have a plan for the rollout.")
        h.runner._take_unrouted()
        _exhaust(h.runner)
        probe = asyncio.ensure_future(h.runner._probe_room())
        await until(lambda: (h.room.last_grant or {}).get("via") == "commit")
        priya_ws = h.rt("priya").ws
        assert commits[-1] is priya_ws, "the move is Priya's, so Priya takes the floor"
        priya_ws.feed(reply("K1", "Which section is mine, and by when?"))
        await asyncio.wait_for(probe, 10)
    finally:
        await h.close()
    (kg,) = h.of("keep_going_probe")
    assert kg["trigger_id"] == "keep_going_1"
    (sd,) = h.of("stage_direction")
    assert sd["trigger_id"] == "keep_going_1" and sd["probing"] is True
    assert AFTER[0]["on_silence"] in sd["stage_direction"]
    assert not h.of("trigger_fired") and not h.of("trigger_undelivered")


# --------------------------------------------------------------------------
# The two S3 forms
# --------------------------------------------------------------------------

S3 = {"S3A": ("alex", "jordan", "casey"), "S3B": ("toni", "lee", "ari")}


@pytest.mark.parametrize("sid", sorted(S3))
def test_s3_lists_moves_for_its_three_characters(sid):
    it = v3.compile_scenario(sid).interactions[0]
    beats = it["after_beats"]
    assert len(beats) >= 3
    assert {b["agent"] for b in beats} == set(S3[sid])
    for b in beats:
        assert b["on_silence"].strip()
        assert '"' not in b["on_silence"], "a described move, never a quoted line"


def test_the_two_s3_forms_list_the_same_shape():
    a = v3.compile_scenario("S3A").interactions[0]["after_beats"]
    b = v3.compile_scenario("S3B").interactions[0]["after_beats"]
    roles = dict(zip(S3["S3A"], S3["S3B"]))
    assert [roles[x["agent"]] for x in a] == [x["agent"] for x in b]
