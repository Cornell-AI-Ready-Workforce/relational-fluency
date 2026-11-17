"""#297 routing keep: an unnamed answer or follow-up stays with the character
who just spoke.

Prod runs no director (DIRECTOR_ROUTE=0), so every unnamed room turn rotated
to the next character in cast order. In Ben's two S3A sessions 0 of 41
unnamed turns went back to the character who had just spoken, and on 11 he was
plainly answering them: "No, you didn't... How many cases are you doing?" to
Alex was answered by Jordan ("That is Alex's workload, not mine").
_keeps_last_speaker keeps such a turn, at most ROOM_KEEP_ADDRESSEE (default 1)
in a row; 0 restores the rotation exactly.

The no-director path is reproduced with ScriptedDirector([]) (entries=[]),
which is what Director.route returns with DIRECTOR_ROUTE=0.
"""
import asyncio
import time

import pytest

from test_bridge_correctness import GPT
from test_group_model_passthrough import _runner_with_room
from test_origin_main_behaviours import _complete_group_replies
from test_room_member_reconnect import Room, ScriptedDirector, in_a_loop, until
from cascade_room_harness import CASCADE, Transcript, wire, speak, finished

ASKED = "Which day works for you?"
ANSWERED = "I already answered that."
FOLLOW_UP = "No, you didn't. How many cases are you doing?"


def _room(monkeypatch, entries=(), *, last="dan", said=ANSWERED):
    monkeypatch.setenv("ROOM_GRANT_UNANSWERED_S", "0.5")
    runner, session, room = _runner_with_room(GPT, list(entries))
    grants = _complete_group_replies(runner, room, monkeypatch)
    session.append_agent(last, said)
    runner._last_group_speaker = last
    return runner, session, room, grants


def _turn(runner, text):
    runner._unrouted_user_texts = [text]
    runner._last_user_text = text
    asyncio.run(asyncio.wait_for(runner._run_group_turn(), timeout=10))


def _route(session):
    return session.store.of("director_route")[-1]


# --------------------------------------------------------------------------
# The no-director path (prod)
# --------------------------------------------------------------------------

def test_unnamed_follow_up_stays_with_last_speaker(monkeypatch):
    """Fails on origin/main: there Priya, next in cast order, answers."""
    runner, session, _, grants = _room(monkeypatch)
    _turn(runner, FOLLOW_UP)
    assert grants == ["dan"]
    route = _route(session)
    assert route["speakers"] == ["dan"]
    assert route["first_by"] == "kept_last_speaker"
    assert route["keep_reason"] == "question_to_them"
    assert route["kept_streak"] == 1 and route["rotated_from"] is None


def test_answer_to_their_question_stays(monkeypatch):
    """Fails on origin/main."""
    runner, session, _, grants = _room(monkeypatch, said=ASKED)
    _turn(runner, "Um, tomorrow.")
    assert grants == ["dan"]
    assert _route(session)["keep_reason"] == "answers_their_question"


@pytest.mark.parametrize("text", [
    "Um", "Okay.", "Who wants to take them?",
    "I need you guys to distribute the queue properly.",
    # A "who" after a leading marker still asks the room (S4A sim, #297).
    "Then, who is going to write up the summary of this meeting?",
    "So who's taking this?",
])
def test_filler_and_group_lines_still_rotate(monkeypatch, text):
    """The speaker passes on origin/main; first_by/rotated_from fail there
    (instrumentation)."""
    runner, session, _, grants = _room(monkeypatch, said=ASKED)
    _turn(runner, text)
    assert grants == ["priya"]
    route = _route(session)
    assert route["first_by"] == "rotation" and route["rotated_from"] == "dan"
    assert route["keep_reason"] is None and route["kept_streak"] is None


def test_a_question_put_to_the_room_is_not_theirs_to_answer(monkeypatch):
    runner, session, _, grants = _room(monkeypatch, said="Why would anyone stay?")
    _turn(runner, "Um, tomorrow.")
    assert grants == ["priya"]


@pytest.mark.parametrize("how", ["interrupted", "cut_after_finalize"])
def test_a_question_cut_off_is_not_one_to_answer(monkeypatch, how):
    """AMEND 4: a '?' the participant never heard to its end does not keep
    the floor; the follow-up rule still can."""
    runner, session, room, grants = _room(monkeypatch, said="So that is that.")
    dan = next(a for a in runner._resolve_agents() if a.id == "dan")
    if how == "interrupted":
        asyncio.run(runner._finalize_member(dan, ASKED, interrupted=True,
                                            audio_bytes=32000))
    else:
        asyncio.run(runner._finalize_member(dan, ASKED, audio_bytes=96000))
        now = time.time()
        runner._last_played = {"agent_id": "dan", "start": now - 1.0,
                               "end": now + 2.0, "text": ASKED}
        runner._cut_last_played(now=now)
    assert runner._last_line_cut == {"agent_id": "dan", "cut": True}
    _turn(runner, "Um, tomorrow.")
    assert grants == ["priya"]


def test_a_whole_question_heard_to_its_end_keeps(monkeypatch):
    runner, session, room, grants = _room(monkeypatch, said="So that is that.")
    dan = next(a for a in runner._resolve_agents() if a.id == "dan")
    asyncio.run(runner._finalize_member(dan, ASKED, audio_bytes=96000))
    assert runner._last_line_cut == {"agent_id": "dan", "cut": False}
    _turn(runner, "Um, tomorrow.")
    assert grants == ["dan"]


@pytest.mark.parametrize("cap,expected,streaks", [
    ("1", ["dan", "priya"], [1, None]),
    ("2", ["dan", "dan", "priya"], [1, 2, None]),
])
def test_keep_is_capped(monkeypatch, cap, expected, streaks):
    """AMEND 2/3: the streak resets wherever the speaker changes
    (_finalize_member_inner), and the default cap is 1."""
    monkeypatch.setenv("ROOM_KEEP_ADDRESSEE", cap)
    runner, session, _, grants = _room(monkeypatch)
    lines = (FOLLOW_UP, "What is in your queue right now?",
             "And how many of those are you taking?")
    for text in lines[:len(expected)]:
        _turn(runner, text)
    assert grants == expected
    assert [r["kept_streak"] for r in session.store.of("director_route")] == streaks
    assert runner._kept_streak == 0       # Priya spoke: the streak is over
    # A named turn to someone else resets the streak.
    _turn(runner, "Chris, what do you think about the date?")
    assert grants[-1] == "chris" and runner._kept_streak == 0
    assert _route(session)["first_by"] is None


def test_knob_zero_is_old_rotation(monkeypatch):
    monkeypatch.setenv("ROOM_KEEP_ADDRESSEE", "0")
    runner, session, _, grants = _room(monkeypatch)
    _turn(runner, FOLLOW_UP)
    assert grants == ["priya"]
    route = _route(session)
    assert route["first_by"] == "rotation" and route["keep_reason"] is None


# --------------------------------------------------------------------------
# With the director on
# --------------------------------------------------------------------------

def test_director_on_keeps_named_last_speaker(monkeypatch):
    """Fails on origin/main (the filter skips Dan there)."""
    runner, session, _, grants = _room(monkeypatch, [
        {"agent_id": "dan"}, {"agent_id": "priya"}])
    _turn(runner, FOLLOW_UP)
    assert grants[0] == "dan"
    route = _route(session)
    assert route["candidates"] == ["dan", "priya"]
    assert route["first_by"] == "kept_last_speaker"
    assert session.store.of("dominance_rotated") == []


def test_director_on_filler_skips_last_speaker_on_the_record(monkeypatch):
    runner, session, _, grants = _room(monkeypatch, [
        {"agent_id": "dan"}, {"agent_id": "priya"}])
    _turn(runner, "Um")
    assert grants[0] == "priya"
    route = _route(session)
    assert route["skipped_last"] == "dan" and route["first_by"] is None


@pytest.mark.parametrize("knob,rotated", [("0", True), ("1", False)])
def test_single_candidate_who_just_spoke(monkeypatch, knob, rotated):
    """AMEND 5: knob 0 is dominance_rotated exactly, on a line that would
    otherwise keep; knob 1 lets the director's pick stand on it."""
    monkeypatch.setenv("ROOM_KEEP_ADDRESSEE", knob)
    runner, session, _, grants = _room(monkeypatch, [{"agent_id": "dan"}])
    _turn(runner, FOLLOW_UP)
    got = session.store.of("dominance_rotated")
    if rotated:
        assert got and got[0]["from_agent"] == "dan" and got[0]["to_agent"] == "priya"
        assert grants[0] == "priya"
        assert _route(session)["rotated_from"] == "dan"
    else:
        assert got == [] and grants == ["dan"]


# --------------------------------------------------------------------------
# A real cascade room
# --------------------------------------------------------------------------

async def _cascade_room(monkeypatch, lines):
    scripts = [Transcript()] + [Transcript(t) if isinstance(t, str) else t
                                for t in lines]
    chats, _ = wire(monkeypatch, scripts)
    h = await Room(monkeypatch, model=CASCADE).open()
    await speak(h)
    await until(lambda: len(chats) == 1)
    await finished(h)
    h.runner.director = ScriptedDirector([])          # DIRECTOR_ROUTE=0
    pending = h.runner._next_trigger()
    owner = h.runner._trigger_agent(pending) if pending else None
    if owner is None:
        pytest.skip("the next beat names nobody")
    order = h.runner.agent_order()
    # The character the rotation would move on from to reach the owner.
    asker = order[(order.index(owner) - 1) % len(order)]
    return h, chats, pending, owner, asker


@in_a_loop
async def test_cascade_answer_stays_and_the_owners_beat_waits(monkeypatch):
    """Fails on origin/main: the rotation hands the turn, and the beat, to
    the owner instead of the character whose question was answered."""
    h, chats, pending, owner, asker = await _cascade_room(
        monkeypatch, ["Thursday works for me, I suppose"])
    try:
        h.session.append_agent(asker, ASKED)
        h.runner._last_group_speaker = asker
        h.runner._last_line_cut = None
        fired = list(h.of("trigger_fired"))
        index = h.runner._trigger_idx
        await speak(h)
        await until(lambda: len(chats) == 2)
        await finished(h)
        route = h.of("director_route")[-1]
        assert route["speakers"][0] == asker
        assert route["first_by"] == "kept_last_speaker"
        assert h.of("assistant_turn")[-1]["agent_id"] == asker
        assert h.of("trigger_fired") == fired and h.runner._trigger_idx == index
        row = h.of("trigger_deferred")[-1]
        assert row["reason"] == "beat_belongs_to_another_character"
        assert row["agent_id"] == owner and row["routed_to"] == asker
    finally:
        await h.close()


@in_a_loop
@pytest.mark.parametrize("defer", ["3", "0"])
async def test_late_keep_reroutes_the_merged_split_turn(monkeypatch, defer):
    """AMEND 1, fails on origin/main and on the design as first written: the
    route is chosen on "Um" (rotation, to the beat's owner), the resume hold
    merges "what's in your queue?" into it, and the turn goes back to the
    character it was put to. With the filler guard on the beat was only held;
    with it off "Um" spent the owner's beat, and it is given back (late_keep)."""
    monkeypatch.setenv("ROOM_RESUME_HOLD_S", "8")
    monkeypatch.setenv("ROOM_BEAT_FILLER_DEFER", defer)
    owed = asyncio.Event()
    h, chats, pending, owner, asker = await _cascade_room(
        monkeypatch, ["Um", Transcript("what's in your queue right now?", owed)])
    in_hold, resume = asyncio.Event(), asyncio.Event()
    original = h.runner._await_participant_resume

    async def hold(room, turn):
        in_hold.set()
        await resume.wait()
        return await original(room, turn)

    monkeypatch.setattr(h.runner, "_await_participant_resume", hold)
    try:
        h.session.append_agent(asker, ANSWERED)
        h.runner._last_group_speaker = asker
        index = h.runner._trigger_idx
        await speak(h)
        await in_hold.wait()
        await speak(h)                     # the rest of it; its transcript is owed
        resume.set()
        await asyncio.sleep(0.3)
        owed.set()
        await until(lambda: len(chats) == 2)
        await finished(h)
        assert len(chats) == 2             # one reply for the whole turn
        route = h.of("director_route")[-1]
        assert route["speakers"][0] == asker
        assert route["first_by"] == "kept_last_speaker"
        assert route["keep_reason"] == "question_to_them"
        assert h.of("assistant_turn")[-1]["agent_id"] == asker
        assert not [e for e in h.of("trigger_fired") if e.get("index") == index]
        assert h.runner._trigger_idx == index
        reasons = [e["reason"] for e in h.of("trigger_deferred")]
        assert reasons[-1] == "beat_belongs_to_another_character"
        if defer == "0":
            assert "late_keep" in reasons
        else:
            assert "filler_turn" in reasons and "late_keep" not in reasons
    finally:
        owed.set()
        resume.set()
        await h.close()


# --------------------------------------------------------------------------
# Review of the keep (#297)
# --------------------------------------------------------------------------

def test_a_kept_turn_never_said_does_not_count_toward_the_cap(monkeypatch):
    """Review 2, fails before it: the kept turn's grant failed, so Dan never
    said it, yet it counted as his one kept turn and the next follow-up to
    him rotated away."""
    runner, session, _, grants = _room(monkeypatch)
    real_grant = runner._grant
    calls = []

    async def failing_grant(agent_id):
        calls.append(agent_id)
        return None if len(calls) == 1 else await real_grant(agent_id)

    async def no_regrant(room, agent_id, why):
        return None

    monkeypatch.setattr(runner, "_grant", failing_grant)
    monkeypatch.setattr(runner, "_regrant_after_drop", no_regrant)
    _turn(runner, FOLLOW_UP)
    assert calls == ["dan"] and grants == []
    assert session.store.of("floor_grant_failed")[-1]["agent_id"] == "dan"
    _turn(runner, "What is in your queue right now?")
    assert grants == ["dan"]
    route = _route(session)
    assert route["first_by"] == "kept_last_speaker" and route["kept_streak"] == 1


def test_another_line_from_the_kept_member_does_not_pay_the_owed_keep(
        monkeypatch):
    """Review 4, fails before it: the kept turn's grant failed, then Dan
    finalized a different line (a silence probe's), which cleared the owed
    marker, so the never-said kept turn still used the keep and the next
    follow-up rotated away from him."""
    runner, session, room, grants = _room(monkeypatch)
    real_grant = runner._grant
    calls = []

    async def failing_grant(agent_id):
        calls.append(agent_id)
        return None if len(calls) == 1 else await real_grant(agent_id)

    async def no_regrant(room, agent_id, why):
        return None

    monkeypatch.setattr(runner, "_grant", failing_grant)
    monkeypatch.setattr(runner, "_regrant_after_drop", no_regrant)
    _turn(runner, FOLLOW_UP)
    assert calls == ["dan"] and grants == []
    dan = next(a for a in runner._resolve_agents() if a.id == "dan")
    asyncio.run(runner._finalize_member(dan, "Are you still there? " + ASKED))
    _turn(runner, "Um, tomorrow.")
    assert grants == ["dan"]
    route = _route(session)
    assert route["first_by"] == "kept_last_speaker" and route["kept_streak"] == 1


async def _held_turn(monkeypatch, knob, mover_at):
    """A turn routed on "Um" (rotation from `a`), held for the rest of the
    line while another member's line is finalized."""
    monkeypatch.setenv("ROOM_KEEP_ADDRESSEE", knob)
    monkeypatch.setenv("ROOM_RESUME_HOLD_S", "8")
    owed = asyncio.Event()
    scripts = [Transcript(), Transcript("Um"),
               Transcript("what's in your queue right now?", owed)]
    chats, _ = wire(monkeypatch, scripts)
    h = await Room(monkeypatch, model=CASCADE).open()
    await speak(h)
    await until(lambda: len(chats) == 1)
    await finished(h)
    h.runner.director = ScriptedDirector([])
    order = h.runner.agent_order()
    if len(order) < 3:
        pytest.skip("needs three characters")
    in_hold, resume = asyncio.Event(), asyncio.Event()
    original = h.runner._await_participant_resume

    async def hold(room, turn):
        in_hold.set()
        await resume.wait()
        return await original(room, turn)

    monkeypatch.setattr(h.runner, "_await_participant_resume", hold)
    try:
        a, b = order[0], order[1]
        h.session.append_agent(a, ANSWERED)
        h.runner._last_group_speaker = a
        await speak(h)
        await in_hold.wait()
        # A line finalized while this turn holds (an earlier reply landing
        # late) moves _last_group_speaker off the route's own.
        mover = next(x for x in h.runner._resolve_agents()
                     if x.id == order[mover_at])
        await h.runner._finalize_member(mover, "Fine, whatever.")
        await speak(h)
        resume.set()
        await asyncio.sleep(0.3)
        owed.set()
        await until(lambda: len(chats) >= 2)
        await finished(h)
        return h, b
    except BaseException:
        owed.set()
        resume.set()
        await h.close()
        raise


@in_a_loop
@pytest.mark.parametrize("knob,mover_at", [("0", 1), ("0", 2), ("1", 2)])
async def test_late_keep_rechecks_against_the_routes_own_last_speaker(
        monkeypatch, knob, mover_at):
    """Review 1, fails before it: the re-check at the grant checkpoint ran
    with ROOM_KEEP_ADDRESSEE=0 and against whoever spoke during the hold, so
    the rotation's pick was re-routed (late_keep). Knob 0 is the old rotation
    exactly; knob 1 re-decides only the keep, against the route's speaker."""
    h, b = await _held_turn(monkeypatch, knob, mover_at)
    try:
        route = h.of("director_route")[-1]
        reasons = [e.get("reason") for e in h.of("trigger_deferred")] + \
                  [e.get("reason") for e in h.of("trigger_undelivered")]
        assert route["speakers"][0] == b
        assert route["first_by"] == "rotation"
        assert "late_keep" not in reasons
    finally:
        await h.close()


@in_a_loop
async def test_late_keep_to_another_non_owner_writes_one_deferral(monkeypatch):
    """Review 3, fails before it: the fold deferred the owner's beat on the
    rotation's pick, and the late keep to a second non-owner wrote a second
    trigger_deferred row for the same turn and index."""
    monkeypatch.setenv("ROOM_RESUME_HOLD_S", "8")
    monkeypatch.setenv("ROOM_BEAT_FILLER_DEFER", "0")
    owed = asyncio.Event()
    h, chats, pending, owner, _ = await _cascade_room(
        monkeypatch, ["Um", Transcript("what's in your queue right now?", owed)])
    order = h.runner.agent_order()
    if len(order) < 3:
        await h.close()
        pytest.skip("needs three characters")
    # Rotation from the asker lands on a non-owner; the keep on another.
    asker = order[(order.index(owner) - 2) % len(order)]
    in_hold, resume = asyncio.Event(), asyncio.Event()
    original = h.runner._await_participant_resume

    async def hold(room, turn):
        in_hold.set()
        await resume.wait()
        return await original(room, turn)

    monkeypatch.setattr(h.runner, "_await_participant_resume", hold)
    try:
        h.session.append_agent(asker, ANSWERED)
        h.runner._last_group_speaker = asker
        index = h.runner._trigger_idx
        before = len(h.of("trigger_deferred"))
        await speak(h)
        await in_hold.wait()
        await speak(h)
        resume.set()
        await asyncio.sleep(0.3)
        owed.set()
        await until(lambda: len(chats) == 2)
        await finished(h)
        route = h.of("director_route")[-1]
        assert route["speakers"][0] == asker
        assert route["first_by"] == "kept_last_speaker"
        rows = [e for e in h.of("trigger_deferred")[before:]
                if e.get("reason") == "beat_belongs_to_another_character"
                and e.get("index") == index]
        assert len(rows) == 1 and rows[0]["agent_id"] == owner
        assert h.runner._trigger_idx == index
    finally:
        owed.set()
        resume.set()
        await h.close()


# A kept turn whose floor came back with nothing said (empty response,
# reply_missing not retried) has not used its keep (#297 review).
@pytest.mark.parametrize("how", ["reply_missing", "empty_response"])
def test_released_without_a_reply_does_not_pay_the_keep(monkeypatch, how):
    """The kept turn's grant went out, but the floor came back with nothing
    said (reply_missing not retried / an empty response: both only set
    _response_done). The keep must be taken back."""
    runner, session, room, grants = _room(monkeypatch)
    real = room.give_floor
    n = []

    async def give_floor(agent_id):
        n.append(agent_id)
        if len(n) == 1:
            rt = await type(room).give_floor(room, agent_id)
            runner._floor_reply_began = True
            runner._response_done.set()   # what reply_missing / empty does
            return rt
        return await real(agent_id)

    monkeypatch.setattr(room, "give_floor", give_floor)
    _turn(runner, FOLLOW_UP)
    assert n == ["dan"]
    assert not [t for t in session.store.of("assistant_turn")
                if t.get("agent_id") == "dan"]
    _turn(runner, "What is in your queue right now?")
    route = _route(session)
    assert route["first_by"] == "kept_last_speaker" and route["kept_streak"] == 1


def test_real_empty_response_does_not_pay_the_keep(monkeypatch):
    runner, session, room, grants = _room(monkeypatch)
    real = room.give_floor
    n = []
    agents = {a.id: a for a in runner._resolve_agents()}

    async def give_floor(agent_id):
        n.append(agent_id)
        if len(n) == 1:
            rt = await type(room).give_floor(room, agent_id)
            await runner._finalize_member_async(agents[agent_id], [], False)
            return rt
        return await real(agent_id)

    monkeypatch.setattr(room, "give_floor", give_floor)
    _turn(runner, FOLLOW_UP)
    assert session.store.of("empty_response")[-1]["agent_id"] == "dan"
    _turn(runner, "What is in your queue right now?")
    route = _route(session)
    assert route["first_by"] == "kept_last_speaker" and route["kept_streak"] == 1
