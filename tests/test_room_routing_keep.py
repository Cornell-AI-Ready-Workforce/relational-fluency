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
