"""Conflict Management's three forms must concede on the same condition.

WHAT WAS MEASURED, and why these assertions and not others.

The reported defect was that the half-concession "lands 6 runs in 10 on S1C
against 10 in 10 on S1A and S1B", i.e. that S1C was the broken form. Driven
three ways against the shipped prompt stack -- 360 encounters, 60 a form, three
synthetic participant scripts sharing turns 1, 2 and 5 verbatim and differing
only at turns 3 and 4 -- that is not what is happening:

    t4 half-concession occurred, brief-only condition, 20 encounters a cell

      script     S1A      S1B      S1C     Fisher (two-sided)
      FIRM      20/20    20/20    20/20    all ns
      HOSTILE   20/20    20/20    20/20    all ns
      CAVE       6/20     3/20    13/20    B/C p=0.003

S1C is never the low form. The real defect is the HOSTILE row, and it belongs
to all three equally: every brief's concession precondition excludes a hostile
turn in the same words ("they do not attack you, they do not demand an
apology"), every brief's next bullet says "harden and stop giving ground" --
and all three conceded on 20 hostile encounters out of 20. A concession that
arrives whether the participant de-escalated or attacked is not measuring
de_escalate, which is one of the two ESCI items t4 carries. That is the gift
the anchors cannot survive, and it was bank-wide rather than S1C's.

WHICH FORM WAS JUDGED CORRECT: on the concession, none of them. S1C was
acquitted -- it is the most responsive of the three, not the least -- and the
repair was applied to all three identically. The cause was structural: the
exclusions were a mid-sentence parenthetical hung off "hold their ground
without heat", a STATE the actor re-decides every turn, while "harden and stop
giving ground" was attached to the FULL-resolution bullet that comes after, so
nothing in any brief said that heat blocks the HALF-concession. The same
STATE-into-EVENT lesson is already written into S1C's own t1 needle. The two
excluded turns are now their own bullet, named as recognisable events, placed
before the concession bullet, and word-for-word identical in all three briefs.

At t2 the outlier is real and it is S1B: its opening raised the grievance
itself on 12 encounters of 60 against 0 and 0 (p<0.001 against both siblings),
because Drew was told to own the message "flat and without apology -- it is a
thing already said and you are not taking it back", a defensive posture neither
sibling carries. Sam is pleased with himself and Wes is impatient; both name
their subject without bracing for an argument about it. A counterpart who
raises the grievance himself removes the participant's t2 choice, and
talk_openly / bring_into_open / fester_r is scored on exactly that choice.

No network and no credentials here. The live measurement is quoted above; this
is what holds its shape still afterwards.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server import scenarios_v3 as v3  # noqa: E402

FORMS = ("S1A", "S1B")
V3DIR = ROOT / "scenarios" / "v3"


def _counterpart_brief(sid: str) -> str:
    """The i2 counterpart's system_prompt -- the one that carries the beat.

    Found through the interaction rather than by agent name, so this keeps
    working if a form is ever recast.
    """
    spec = v3.load_spec(sid)
    i2 = [i for i in spec["interactions"] if i.get("kind") == "counterpart"]
    assert len(i2) == 1, f"{sid}: expected exactly one counterpart interaction"
    return spec["agents"][i2[0]["agent"]]["system_prompt"]


def _norm(text: str) -> str:
    """Collapse the YAML block scalar's own wrapping, which is not content."""
    return re.sub(r"\s+", " ", text).strip()


# --- the shared concession gate -------------------------------------------
# These two sentences are the whole of what makes the three forms score the
# same beat at the same difficulty. They are asserted as shared TEXT rather
# than as a behaviour, because the behaviour costs 360 gateway calls to see and
# the text is what a future edit will touch.

EXCLUSION_BULLET = _norm("""
    THAT TURN HAS THREE THINGS IN IT AT ONCE, and you check for all three
    before you part with anything. One: they say the thing still stands for
    them. Two: there is no heat in how they say it — they have not come at
    you, told you what you are, or asked you to apologise or admit it.
    Three: they have left you your own half, rather than taking it all back
    or telling you to drop it. Two of the three is not the turn. A turn
    with heat in it buys nothing however many times it arrives, and a turn
    that folds buys nothing either, because there is nothing to trade with
    someone who has stopped asking. IF THAT TURN NEVER COMES, YOU NEVER
    GIVE GROUND AT ALL, and this ends with you not having given it; that is
    a real way for it to go and not a failure to avoid.
""")

GATE_CLAUSE = _norm("""
    You give ground ONCE, and it happens on a turn you can recognise: the
    first time they hold their ground without heat, after you have already
    defended yourself at least once. Not before the defence, and not twice.
""")

# The second half of the repair. Each counterpart is handed one cheap word for
# the concession, and before this binding existed that word was also the thing
# he reached for when asked to apologise -- so a demand for an apology BOUGHT
# the concession it was supposed to block. S1A conceded on 20 hostile
# encounters of 20 with the exclusion bullet already in place, and 2 of every 3
# of those landed on the turn the script demanded an apology.
CHEAP_WORD_BINDING = _norm("""
    and it belongs to the single turn you give
    ground on and to no other. Asked to apologise before that turn comes,
    you decline and put nothing in its place: reaching for the word is
    giving the ground, so a demand for an apology cannot be what buys it.
""")


@pytest.mark.parametrize("sid", FORMS)
def test_concession_names_the_two_excluded_turns(sid):
    """Heat must block the HALF-concession, not merely full resolution.

    Before this bullet existed all three forms conceded on 20 hostile
    encounters out of 20, because the only place hostility was mentioned was
    the full-resolution bullet that comes after this beat.
    """
    assert EXCLUSION_BULLET in _norm(_counterpart_brief(sid)), (
        f"{sid}: the counterpart brief has lost the two-excluded-turns bullet. "
        "Without it the concession fires on a hostile turn as readily as on a "
        "firm one and t4's de_escalate item stops discriminating."
    )


@pytest.mark.parametrize("sid", FORMS)
def test_concession_gate_is_the_shared_event(sid):
    assert GATE_CLAUSE in _norm(_counterpart_brief(sid)), (
        f"{sid}: the concession gate no longer matches its siblings. All three "
        "forms score this beat against the same ESCI anchors, so the condition "
        "the concession fires on must be the same sentence in all three."
    )


@pytest.mark.parametrize("sid", FORMS)
def test_the_cheap_word_is_bound_to_the_concession_turn(sid):
    """The concession word must not double as the answer to an apology demand.

    Each counterpart is given one cheap word so the beat has a move to make
    rather than only an absence to observe -- the sibling with a ready-made
    word conceded 0.90 where the one with a bare negation conceded 0.20. But
    the same bullet also told him what to say instead of sorry, so a hostile
    participant demanding an apology was handed the concession by the very
    rule meant to withhold it.
    """
    assert CHEAP_WORD_BINDING in _norm(_counterpart_brief(sid)), (
        f"{sid}: the concession word is no longer bound to the concession "
        "turn. Unbound, it becomes the counterpart's reflex answer to 'say "
        "sorry', and the beat fires on hostility as readily as on firmness."
    )


def test_the_gate_is_identical_across_all_three_forms():
    """A pairwise check is not enough once there are three forms.

    B-vs-C is not implied by A-vs-B, and this beat has already been broken
    once in each direction: S1A conceded 0.20 against S1B's 0.90 before the
    gate was introduced, and S1C was accused of the reverse afterwards.
    """
    for shared in (EXCLUSION_BULLET, GATE_CLAUSE, CHEAP_WORD_BINDING):
        holders = {sid for sid in FORMS if shared in _norm(_counterpart_brief(sid))}
        assert holders == set(FORMS), (
            f"forms missing a shared concession sentence: {sorted(set(FORMS) - holders)}"
        )


# --- t2: the counterpart must not raise the grievance himself --------------

@pytest.mark.parametrize("sid", FORMS)
def test_counterpart_is_told_which_half_is_not_his_to_raise(sid):
    """t2 scores whether the PARTICIPANT brings it into the open.

    If the counterpart raises it first there is no choice left to score, and
    the low anchor (dodge a direct ask) becomes unreachable.
    """
    brief = _norm(_counterpart_brief(sid)).lower()
    assert "you do not raise" in brief, (
        f"{sid}: the counterpart brief no longer says which half of the "
        "grievance is not his to raise."
    )


def test_s1b_opening_carries_no_defensive_preemption():
    """S1B's measured t2 defect, asserted so it cannot come back.

    Drew raised the grievance himself on 12 encounters of 60 against 0 and 0
    for both siblings while his brief told him to own the message "flat and
    without apology -- it is a thing already said and you are not taking it
    back". Bracing against an argument nobody has started yet IS raising it.
    Neither sibling's opening defends its subject, and Drew's must not either.
    """
    brief = _norm(_counterpart_brief("S1B")).lower()
    for banned in ("without apology",
                   "not taking it back",
                   "refusing to take it back",
                   "braced for an argument",
                   "already said and you are not"):
        assert banned not in brief, (
            f"S1B: the opening has regained the defensive framing ({banned!r}) "
            "that made Drew put the grievance on the table himself."
        )


# --- structural parity the beat depends on ---------------------------------

def test_all_three_share_trigger_ids_and_esci_map():
    ref = None
    for sid in FORMS:
        spec = v3.load_spec(sid)
        shape = [[(t["id"], tuple(t.get("esci", [])))
                  for t in i.get("triggers", [])]
                 for i in spec["interactions"]]
        if ref is None:
            ref = shape
        assert shape == ref, f"{sid}: trigger/ESCI shape diverged from its siblings"


@pytest.mark.parametrize("sid", FORMS)
def test_spec_is_lf_only(sid):
    path = next(p for p in V3DIR.glob(f"{sid}_*.yaml"))
    assert b"\r\n" not in path.read_bytes(), f"{path.name} has CRLF line endings"
