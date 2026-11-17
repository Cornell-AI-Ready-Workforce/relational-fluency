"""#297: a planted beat is not spent on a participant turn that is only a filler.

Ben's two S3A cascade sessions (before #305) spent 6 of their 15 beats on a
turn like "Um", "I think", "Uh, what" or "So": the character performed its next
scripted move over a participant still finding their words. In a cascade room
the beat now waits (trigger_deferred{reason=filler_turn}) for up to
ROOM_BEAT_FILLER_DEFER consecutive such turns, the character still answers the
filler, and the beat fires on the next turn with words in it.
"""
import asyncio

import pytest

from cascade_room_harness import (CASCADE, WORDS, Transcript, wire, speak,
                                  finished)
from test_room_member_reconnect import Room, ScriptedDirector, in_a_loop, until
from test_bridge_correctness import GPT, NATIVE

CONTENT = "We could move the review to Thursday and split the list"
SECOND = "and the second half of that same thought about the rota"


def _fillers(h):
    return [e for e in h.of("trigger_deferred") if e.get("reason") == "filler_turn"]


async def _room_after_first_turn(monkeypatch, lines, *, defer=None):
    """A cascade room that has answered WORDS, with the director then routing
    every turn to the character whose beat is next."""
    if defer is not None:
        monkeypatch.setenv("ROOM_BEAT_FILLER_DEFER", defer)
    scripts = [Transcript()] + [Transcript(t) if isinstance(t, str) else t for t in lines]
    chats, _ = wire(monkeypatch, scripts)
    h = await Room(monkeypatch, model=CASCADE).open()
    await speak(h)
    await until(lambda: len(chats) == 1)
    await finished(h)
    pending = h.runner._next_trigger()
    assert pending
    owner = h.runner._trigger_agent(pending) or "dan"
    h.runner.director = ScriptedDirector([{"agent_id": owner}])
    return h, chats, pending, owner


async def _turn(h, chats, n):
    h.runner._last_group_speaker = None
    await speak(h)
    await until(lambda: len(chats) == n)
    await finished(h)


@in_a_loop
async def test_filler_turn_defers_beat_and_next_line_fires_it(monkeypatch):
    """Fails on origin/main: there the beat fires on 'I think'."""
    from server.verify_record import _net_fired
    h, chats, pending, owner = await _room_after_first_turn(
        monkeypatch, ["I think", CONTENT])
    try:
        before = list(h.of("trigger_fired"))
        index = h.runner._trigger_idx
        net = _net_fired(h.session.store.events)
        await _turn(h, chats, 2)
        assert h.of("trigger_fired") == before
        row = h.of("trigger_deferred")[-1]
        assert row["reason"] == "filler_turn" and row["trigger_id"] == pending["id"]
        assert row["agent_id"] == h.runner._trigger_agent(pending)
        assert row["routed_to"] == owner and row["defers"] == 1 and row["cap"] == 3
        assert row["text"] == "I think" and row["index"] == index
        assert h.runner._trigger_idx == index
        assert pending["cue"] not in chats[-1]["messages"][0]["content"]
        assert _net_fired(h.session.store.events) == net
        await _turn(h, chats, 3)
        fired = h.of("trigger_fired")[-1]
        assert fired["trigger_id"] == pending["id"] and fired["index"] == index
        assert pending["cue"] in chats[-1]["messages"][0]["content"]
        assert h.runner._filler_defer_n == 0
    finally:
        await h.close()


@in_a_loop
async def test_filler_defer_is_capped(monkeypatch):
    """Fails on origin/main. Three fillers in a row hold the beat; the fourth
    spends it, so a participant who never says more still meets it."""
    h, chats, pending, _ = await _room_after_first_turn(
        monkeypatch, ["Um, so", "I think", "Okay, um", "So"], defer="3")
    try:
        index = h.runner._trigger_idx
        for n in range(2, 5):
            await _turn(h, chats, n)
        assert [e["defers"] for e in _fillers(h)] == [1, 2, 3]
        assert h.runner._trigger_idx == index
        await _turn(h, chats, 5)
        assert len(_fillers(h)) == 3
        fired = h.of("trigger_fired")[-1]
        assert fired["trigger_id"] == pending["id"] and fired["index"] == index
    finally:
        await h.close()


@in_a_loop
async def test_filler_guard_off_restores_old_firing(monkeypatch):
    """Passes on origin/main too: the parity guard for ROOM_BEAT_FILLER_DEFER=0."""
    h, chats, pending, _ = await _room_after_first_turn(
        monkeypatch, ["I think"], defer="0")
    try:
        index = h.runner._trigger_idx
        await _turn(h, chats, 2)
        fired = h.of("trigger_fired")[-1]
        assert fired["trigger_id"] == pending["id"] and fired["index"] == index
        assert pending["cue"] in chats[-1]["messages"][0]["content"]
        assert not _fillers(h)
    finally:
        await h.close()


@in_a_loop
async def test_filler_merged_by_resume_hold_rearms_beat(monkeypatch):
    """Fails on origin/main (no re-arm there: the beat was briefed at the fold
    and the intent yielded). "I think", then the rest of the sentence while
    the resume hold keeps the grant: one reply, carrying the beat."""
    monkeypatch.setenv("ROOM_RESUME_HOLD_S", "8")
    owed = asyncio.Event()
    h, chats, pending, owner = await _room_after_first_turn(
        monkeypatch, ["I think", Transcript(SECOND, owed)])
    intent = "Ask them which day suits."
    h.runner.director = ScriptedDirector([{"agent_id": owner, "intent": intent}])
    in_direct, resume = asyncio.Event(), asyncio.Event()
    original = h.runner._direct_member

    async def direct(aid, text):
        in_direct.set()
        await resume.wait()
        return await original(aid, text)

    monkeypatch.setattr(h.runner, "_direct_member", direct)
    try:
        index = h.runner._trigger_idx
        h.runner._last_group_speaker = None
        await speak(h)
        await in_direct.wait()
        assert _fillers(h)[-1]["text"] == "I think"
        await speak(h)                     # the rest of it; its transcript is owed
        resume.set()
        await asyncio.sleep(0.3)
        assert len(chats) == 1             # held, not granted on "I think"
        owed.set()
        await until(lambda: len(chats) == 2)
        await finished(h)
        assert len(chats) == 2             # one reply for the whole turn
        rearmed = h.of("trigger_filler_rearmed")
        assert len(rearmed) == 1 and rearmed[0]["trigger_id"] == pending["id"]
        assert rearmed[0]["index"] == index and rearmed[0]["agent_id"] == owner
        fired = [e for e in h.of("trigger_fired") if e["index"] == index]
        assert [e["trigger_id"] for e in fired] == [pending["id"]]
        assert pending["cue"] in chats[-1]["messages"][0]["content"]
        assert intent not in chats[-1]["messages"][0]["content"]
        dropped = h.of("director_intent_undelivered")
        assert h.room.steering_is_real    # so the intent was delivered at the fold
        assert dropped and dropped[-1]["reason"] == "planted_beat_briefed"
        assert dropped[-1]["intent"] == intent
        assert h.runner._filler_defer_n == 0
    finally:
        owed.set()
        resume.set()
        await h.close()


@in_a_loop
async def test_probe_after_filler_deferral_fires_beat(monkeypatch):
    """Fails on origin/main (no filler_turn deferral to start from). A beat
    held over a filler is still the silence probe's to fire."""
    h, chats, pending, _ = await _room_after_first_turn(monkeypatch, ["I think"])
    try:
        index = h.runner._trigger_idx
        await _turn(h, chats, 2)
        assert h.runner._filler_defer_n == 1
        await h.runner._probe_room()
        await until(lambda: len(chats) == 3)
        fired = h.of("trigger_fired")[-1]
        assert fired["trigger_id"] == pending["id"] and fired["index"] == index
        assert fired["probing"]
        assert "gone quiet" in chats[-1]["messages"][0]["content"]
        assert h.runner._filler_defer_n == 0
    finally:
        await h.close()


def _non_owner(h, pending):
    owner = h.runner._trigger_agent(pending)
    if owner is None:
        pytest.skip("the next beat names nobody, so no turn can belong to another")
    return owner, next(a for a in h.room.sessions if a != owner)


@in_a_loop
async def test_ownership_deferral_takes_precedence_over_a_filler(monkeypatch):
    h, chats, pending, _ = await _room_after_first_turn(monkeypatch, ["I think"])
    try:
        owner, other = _non_owner(h, pending)
        h.runner.director = ScriptedDirector([{"agent_id": other}])
        await _turn(h, chats, 2)
        assert [e["reason"] for e in h.of("trigger_deferred")] == [
            "beat_belongs_to_another_character"]
        assert h.runner._filler_defer_n == 0
    finally:
        await h.close()


@in_a_loop
async def test_the_cap_counts_consecutive_fillers(monkeypatch):
    """Filler, then words routed to another character, then filler: the words
    reset the count, so the beat is held again at 1 and not spent at 2."""
    h, chats, pending, owner = await _room_after_first_turn(
        monkeypatch, ["I think", CONTENT, "So"], defer="2")
    try:
        owner, other = _non_owner(h, pending)
        index = h.runner._trigger_idx
        await _turn(h, chats, 2)
        assert h.runner._filler_defer_n == 1
        h.runner.director = ScriptedDirector([{"agent_id": other}])
        await _turn(h, chats, 3)
        assert h.of("trigger_deferred")[-1]["reason"] == "beat_belongs_to_another_character"
        assert h.runner._filler_defer_n == 0
        h.runner.director = ScriptedDirector([{"agent_id": owner}])
        await _turn(h, chats, 4)
        assert [e["defers"] for e in _fillers(h)] == [1, 1]
        assert h.runner._filler_defer_n == 1
        assert h.runner._trigger_idx == index
    finally:
        await h.close()


@pytest.mark.parametrize("model", [GPT, NATIVE])
def test_gpt_and_native_rooms_still_spend_the_beat_on_a_filler(monkeypatch, model):
    """Cascade only: gpt and native rooms keep today's behaviour exactly."""
    from test_group_model_passthrough import _runner_with_room
    runner, session, room = _runner_with_room(model, [])
    pending = runner._next_trigger()
    owner = runner._trigger_agent(pending) or "dan"
    runner.session.director = runner.director = ScriptedDirector([{"agent_id": owner}])
    runner._unrouted_user_texts = ["I think"]
    index = runner._trigger_idx
    asyncio.run(asyncio.wait_for(runner._run_group_turn(), timeout=10))
    fired = session.store.of("trigger_fired")
    assert fired and fired[-1]["trigger_id"] == pending["id"]
    assert fired[-1]["index"] == index
    assert not [e for e in session.store.of("trigger_deferred")
                if e.get("reason") == "filler_turn"]


@in_a_loop
async def test_rearm_brief_failure_keeps_intent_and_hold(monkeypatch):
    """The resume hold merges words into the filler, but the beat's brief is
    refused (trigger_brief_failed): nothing was re-armed, so no
    trigger_filler_rearmed row, the delivered intent is not retracted and the
    filler hold and its count stand."""
    monkeypatch.setenv("ROOM_RESUME_HOLD_S", "8")
    owed = asyncio.Event()
    h, chats, pending, owner = await _room_after_first_turn(
        monkeypatch, ["I think", Transcript(SECOND, owed)])
    intent = "Ask them which day suits."
    h.runner.director = ScriptedDirector([{"agent_id": owner, "intent": intent}])
    in_direct, resume = asyncio.Event(), asyncio.Event()
    original, deliver_brief = h.runner._direct_member, h.runner._deliver_brief
    directed = []

    async def direct(aid, text):
        in_direct.set()
        await resume.wait()
        r = await original(aid, text)
        directed.append(aid)
        return r

    async def deliver(rt, instructions):
        if directed and pending["cue"] in instructions:
            raise RuntimeError("brief refused")
        return await deliver_brief(rt, instructions)

    monkeypatch.setattr(h.runner, "_direct_member", direct)
    monkeypatch.setattr(h.runner, "_deliver_brief", deliver)
    try:
        index = h.runner._trigger_idx
        h.runner._last_group_speaker = None
        await speak(h)
        await in_direct.wait()
        await speak(h)
        resume.set()
        await asyncio.sleep(0.3)
        owed.set()
        await until(lambda: len(chats) == 2)
        await finished(h)
        assert h.of("trigger_brief_failed")
        assert h.runner._trigger_idx == index
        assert not h.of("trigger_filler_rearmed")
        assert not [e for e in h.of("director_intent_undelivered")
                    if e["reason"] == "planted_beat_briefed"]
        assert h.runner._filler_defer_n == 1
    finally:
        owed.set()
        resume.set()
        await h.close()


@in_a_loop
async def test_unanswered_filler_turn_gives_its_count_back(monkeypatch):
    """A filler turn whose grant fails was never answered, so it does not
    count toward ROOM_BEAT_FILLER_DEFER."""
    h, chats, pending, _ = await _room_after_first_turn(
        monkeypatch, ["I think", "So"])
    try:
        index = h.runner._trigger_idx
        await _turn(h, chats, 2)
        assert h.runner._filler_defer_n == 1

        async def no_grant(*a, **kw):
            return None
        monkeypatch.setattr(h.runner, "_grant", no_grant)
        monkeypatch.setattr(h.runner, "_regrant_after_drop", no_grant)
        h.runner._last_group_speaker = None
        await speak(h)
        await until(lambda: h.of("floor_grant_failed"))
        await finished(h)
        assert [e["defers"] for e in _fillers(h)] == [1, 2]
        assert h.runner._filler_defer_n == 1
        assert h.runner._trigger_idx == index
    finally:
        await h.close()
