"""The director's own prompt, and the room's participant channel.

Two subjects in one file because they are one defect seen from both ends. The
director routes on what the participant just said; on a family whose own turn
detection is switched off, nothing in the room ever closed the transcription
channel's buffer, so what the participant just said did not exist - and the
director, given a blank, answered it with the most confident routing of any case
measured. One half is fixed in server/group_room.py and the other in
server/director.py, and neither reads as a fix without the other.

WHAT IS ASSERTED HERE, AND WHAT CANNOT BE

No test here asks a model what it would route. The director's decisions were
measured against the real gateway while this was written - twelve calls per
prompt revision, real S4A cast and scene, six fixed synthetic transcripts, two
samples each, every decision then replayed through _run_group_turn's own speaker
pick - and those counts are recorded in server/director.py beside the rules they
produced, because that is where somebody about to change a rule will read them.
A test that re-ran them would spend gateway credit to assert something a model
is free to do differently tomorrow.

What a test CAN hold is the part that is not probabilistic: that the prompt
still says the things the measurement paid for, that the one instruction whose
violation is mechanical rather than stylistic is present, and that the room's
participant channel behaves the way the gateway was measured to behave. The
gateway itself is not opened; tests/conftest.py refuses outbound connections by
design, and the room is driven through the same MeasuredGateway the group tests
already model the wire with.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server import director as D  # noqa: E402
from server import group_room as gr  # noqa: E402
from server.scenarios import load_scenario  # noqa: E402

from test_final_group_live import (  # noqa: E402
    GEMINI, GPT, make_room,
)


# --------------------------------------------------------------------------
# 1. The prompt the director is actually handed.
# --------------------------------------------------------------------------

class _Agent:
    def __init__(self, aid, name, prompt):
        self.id = aid
        self.name = name
        self.system_prompt = prompt


class _Scenario:
    cast = [
        _Agent("dan", "Dan", "# You are Dan\n\nYou want the date agreed today."),
        _Agent("priya", "Priya", "# You are Priya\n\nYou hedge, and you stop "
                                 "yourself."),
    ]
    scene = "Four people planning a rollout."
    director_prompt = "Dan dominates. Priya is excluded."
    opener: list = []


def _system_prompt() -> str:
    """The composed system string, captured without calling the gateway.

    route() builds it inline, so the only way to read exactly what is sent is
    to let route() run and intercept the request. The client is a stub; the
    timeout budget and the tool schema go out unchanged.
    """
    captured = {}

    class _Messages:
        async def create(self, **kw):
            captured.update(kw)
            raise RuntimeError("stop here, the prompt is what we came for")

    class _Client:
        messages = _Messages()

        def with_options(self, **_kw):
            return self

    d = D.Director(_Scenario(), client=_Client())
    # _bounded hands a non-AsyncAnthropic double straight back, so `d.client`
    # is the stub above and route() degrades to its recorded fallback rather
    # than raising. Either way the request was built first.
    asyncio.run(d.route([{"speaker": "user", "text": "So where are we?"}], "So where are we?"))
    return captured["system"]


def test_the_director_is_told_what_to_do_with_a_turn_it_cannot_see():
    """A blank `latest_user_text` is not an edge case on every family.

    Where the gateway's own turn detection is switched off, nothing closes the
    transcription channel's buffer unless the runner does (see
    GroupRoom.close_participant_turn), so the director is handed a blank on
    every single turn. Measured against the real gateway with no rule for it,
    that blank drew the most confident routing of the six cases tried: a new
    subject opened, a decision pushed, two speakers named. The failure is silent
    and it lands on the turn the participant is being scored on.
    """
    system = _system_prompt()
    assert "not transcribed" in system, (
        "the director's prompt no longer says what to do when the participant's "
        "words are missing; on a family with turn detection switched off that is "
        "every turn, and it answers the gap by moving the scene on"
    )


def test_the_director_is_told_that_everyone_it_names_will_speak():
    """The one rule here whose violation is mechanical, not stylistic.

    Asked for the person who would really come in behind the first speaker, the
    director started naming somebody in order to tell them to hold back:
    'Remain silent as you have been talked over', 'Listen, but stay focused'.
    Nothing downstream reads a direction as an instruction not to speak -
    _run_group_turn hands every routed id the floor in order - so that note is
    handed to a character who is at that moment being asked to talk, and it is
    the scenario's deliberately silent character who gets pulled into speaking.
    Leaving them off the list is the only way to say it.
    """
    system = _system_prompt()
    assert "WILL speak" in system, (
        "the prompt no longer tells the director that everyone it names is going "
        "to be given the floor, so 'stay quiet' comes back as a direction and "
        "the character it is handed to speaks anyway"
    )


def test_the_director_is_not_asked_to_perform_the_participants_own_moves():
    """solicits_input and encourages_participation are scored on the person.

    Measured: 'Open by establishing the need for a decision today and call on
    Chris to move the needle.' Two of S4A's six ESCI items, performed by the
    room on the participant's behalf, on a turn the participant had not earned.
    The prompt used to protect only the character written as sidelined; Chris is
    not written as sidelined, so nothing covered this.
    """
    system = _system_prompt()
    assert "Do not do the job the person in the room came to do" in system
    # And the same rule reaches the field the note is actually written into.
    intent = (
        D._DIRECTOR_TOOL["input_schema"]["properties"]["speakers"]
        ["items"]["properties"]["intent"]["description"]
    )
    assert "bring somebody else in" in intent, (
        "the intent field no longer refuses a direction that draws another "
        "character in, which is the participant's move and nobody else's"
    )


def test_scenario_routing_guidance_does_not_get_to_stage_the_planted_beats():
    """The contradiction the director was resolving on every call.

    The text that lands under "How this room behaves" is compiled by
    _director_prompt in server/scenarios_v3.py and ends "Planted triggers fire
    in order. Keep the scene moving toward the next one." This prompt says never
    to stage what has not happened yet. Half the measured directions came back
    on the scenario text's side, which is how Priya came to be directed to give
    up the one fact her brief exists to withhold. The precedence has to be
    written down; the compiled line asking for it is the cleaner fix and is not
    in this file.
    """
    system = _system_prompt()
    assert "does not mean deliver it" in system, (
        "nothing now resolves 'keep the scene moving toward the next trigger' "
        "against 'do not stage what happens next', and the planted beats are "
        "what the encounter is scored on"
    )


def test_the_cast_block_carries_the_characters_and_not_their_titles():
    """_character_sketch skips the markdown heading every v3 brief opens with.

    Without it the director's whole knowledge of the room was '# You are Dan',
    '# You are Priya' - tokens spent to say less than the ids already said.
    """
    block = D._format_cast(_Scenario.cast)
    assert "# You are Dan" not in block
    assert "wants the date agreed today" in block.lower() or \
           "You want the date agreed today" in block


def test_every_group_scenario_composes_a_prompt_the_director_can_route_from():
    """The cast block is built from live scenario files, so it can go empty.

    A sketch that clips to nothing, or a cast whose ids never reach the prompt,
    costs every routing decision in that scenario: route() drops an agent_id it
    does not recognise, and a decision whose speakers are all dropped is a turn
    where nobody answers.
    """
    for sid in ("S4A", "S4B"):
        sc = load_scenario(sid)
        block = D._format_cast(sc.cast)
        for a in sc.cast:
            assert f"({a.id})" in block, f"{sid}: {a.id} missing from the cast block"
            line = next(l for l in block.splitlines() if f"({a.id})" in l)
            assert len(line) > 40, (
                f"{sid}: {a.id}'s sketch clipped to nothing, so the director is "
                "routing this room off the ids alone"
            )


# --------------------------------------------------------------------------
# 2. The participant channel the director routes on.
# --------------------------------------------------------------------------

def _run(model, body):
    mp = pytest.MonkeyPatch()

    async def scenario():
        room, _gw = make_room(mp, model)
        await room.open()
        await body(room)
        return room
    try:
        return asyncio.run(scenario())
    finally:
        mp.undo()


def test_where_turn_detection_is_off_nothing_closed_the_participants_turn():
    """The defect, stated as the room used to behave.

    give_floor commits the member it is granting and nobody else. The scribe is
    the room's ONLY participant channel - _pump_member throws its own
    user_transcript events away on purpose, because a member's input buffer also
    carries the other characters' fanned-out audio and the bridge labels all of
    it "user". So on a family whose own turn detection is switched off, a group
    encounter recorded a perfect participant WAV, a perfect agent transcript,
    and not one word the participant said.
    """
    async def a_turn(room):
        await room.hear(b"\x01\x02" * 12000)
        await room.give_floor("dan")

    room = _run(GPT, a_turn)
    assert room.scribe.commits == 0, (
        "give_floor now commits the scribe; that is the wrong place for it "
        "(see close_participant_turn) and it doubles the replies this family pays for"
    )
    assert len(room.scribe.audio_in) > 0, (
        "the participant's audio never reached the transcription channel at all"
    )


def test_closing_the_participants_turn_is_what_gets_them_transcribed():
    """And the runner is the only caller that knows when the turn ended."""
    async def a_turn(room):
        await room.hear(b"\x01\x02" * 12000)
        room.closed_it = await room.close_participant_turn()
        await room.give_floor("dan")

    room = _run(GPT, a_turn)
    assert room.closed_it is True
    assert room.scribe.commits == 1
    assert room.scribe.empty_commits == 0, (
        "the participant's turn was closed on a buffer holding nothing, which is "
        "input_audio_buffer_commit_empty on this family and a transcript of "
        "silence attributed to the participant"
    )
    assert room.scribe.committed_bytes[-1] > 0
    assert room.scribe_commits == 1


def test_the_family_that_closes_its_own_buffers_is_left_alone():
    """Gemini transcribes the participant unasked - measured, and the reason the
    defect above is invisible until REALTIME_MODEL moves. A second commit here
    would be a commit holding only the silence pad, which produces no turn on
    that family and a transcript of nothing if it did."""
    async def a_turn(room):
        await room.hear(b"\x01\x02" * 12000)
        room.closed_it = await room.close_participant_turn()

    room = _run(GEMINI, a_turn)
    assert room.floor_is_real is False
    assert room.closed_it is False
    assert room.scribe.commits == 0
    assert room.scribe_commits == 0


def test_a_turn_with_nothing_in_it_is_not_closed_twice():
    """Two calls, one participant turn: the second has nothing to close.

    An empty commit is `input_audio_buffer_commit_empty` on this family, and a
    commit carrying only the 300 ms pad would hand the record a transcript of
    silence with the participant's name on it. Both are worse than doing
    nothing, and a runner that calls this on every turn boundary will reach one
    where the participant said nothing at all.
    """
    async def two_calls(room):
        await room.hear(b"\x01\x02" * 12000)
        room.first = await room.close_participant_turn()
        room.second = await room.close_participant_turn()

    room = _run(GPT, two_calls)
    assert room.first is True
    assert room.second is False
    assert room.scribe.commits == 1
    assert room.scribe.empty_commits == 0


def test_agent_audio_never_counts_as_the_participant_having_spoken():
    """hear(exclude=<speaker>) is a character being heard by the others, and the
    scribe is deliberately not one of them. If that audio counted, the room
    would close a participant turn nobody took and the record would carry a
    transcript of one character attributed to the participant."""
    async def only_an_agent_speaks(room):
        await room.hear(b"\x03\x04" * 12000, exclude="dan")
        room.closed_it = await room.close_participant_turn()

    room = _run(GPT, only_an_agent_speaks)
    assert room.closed_it is False
    assert room.scribe.commits == 0
    assert room.scribe.audio_in == b""


def test_a_scribe_whose_socket_is_gone_is_reported_and_not_believed():
    """_send counts a closed socket and drops the frame rather than raising, so
    that one dead channel cannot stop a room. Silence on the way out is
    therefore not success here: unreported, the room goes on believing it has a
    participant channel, and the encounter records agent turns against a
    participant who appears to have stopped talking - which is itself a rateable
    ESCI behaviour."""
    lost = {}

    async def a_turn(room):
        await room.hear(b"\x01\x02" * 12000)
        room.scribe.kill_socket("the gateway closed it")
        room.closed_it = await room.close_participant_turn()

    mp = pytest.MonkeyPatch()

    async def scenario():
        room, _gw = make_room(mp, GPT, on_lost=lambda cid, why: lost.update({cid: why}))
        await room.open()
        await a_turn(room)
        return room
    try:
        room = asyncio.run(scenario())
    finally:
        mp.undo()

    assert room.closed_it is False
    assert gr.SCRIBE_ID in lost
    assert room.scribe is None
    assert room.scribe_commits == 0
