"""The three inspirational-leadership forms have to be ONE instrument.

S3A, S3B and S3C are drawn interchangeably for the same construct, so a
participant's ESCI score must not depend on which of the three they got. The
bank-wide tests already hold the skeleton (test_scenario_probes: every beat has
a probe; test_on_silence_probe: a probe is not a described move;
test_voice_casting: the cast is on its family's roster), and tests/test_s3c.py
holds the third form against its two siblings. What none of them held is the
thing a measurement round found broken twice in the same file:

  t2, THE DISENGAGED HIGH PERFORMER'S SHUT-DOWN. Driven through the real
  composed prompt stack with identical synthetic participant scripts and no
  director note - the configured-model condition - the scored half of this beat
  fired on 0 of 32 runs on S3A against 31 of 32 on S3B and 32 of 32 on S3C
  (two-sided Fisher, A-B p=2e-18, A-C p=5e-19, B-C p=1.000). The cause was not
  the shared first-turn pin, which all three carry and all three obey. It was
  that S3A's first half asked for a STATE - nothing is wrong, there is nothing
  to discuss - and a state is not a thing an actor can perform on a turn, nor a
  thing a participant can find out. S3B's and S3C's ask for something the
  character DID and stopped doing. And on the probe path, S3A's and S3B's
  on_silence carried only the ask and dropped the withdrawal entirely, so a
  participant who froze was scored on half a beat on two forms and a whole one
  on the third: 0/12, 0/12, 12/12.

  t3, THE ANXIOUS JUNIOR'S TWO FEARS. Same runs: the structure the three beats
  are supposed to share - two worries about the speaker's own position, one a
  concrete change to what they have and one about fault landing on them - held
  on 0.06 of S3A's runs against 1.00 and 1.00 (p=2e-17 against both). S3A's
  second half was a second worry of the same kind as the first, and the third
  thing her cue mentions was not marked as support the way S3B's and S3C's is.

Everything below is written against ALL THREE FORMS AT ONCE rather than against
a literal copied out of one of them. A test that says "S3A's probe mentions a
stopping" goes stale the day somebody rewrites S3A; a test that says "every
form's probe mentions one" fails on the day the three stop matching, which is
the only day anybody needs to hear about it. Where a sentence has to be word
for word the same on all three - the bounds attached to the juniors - the test
asserts the identity and not the words, for the same reason.

No network and no credentials. Everything here reads the shipped specs.
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

FORMS = ("S3A", "S3B")

# Who plays which part on each form. This map is the only form-specific
# knowledge in the file, and it is cast, not behaviour.
ROLES = {
    "S3A": {"cynic": "alex", "performer": "jordan", "junior": "casey"},
    "S3B": {"cynic": "toni", "performer": "lee", "junior": "ari"},
}

PATHS = {
    "S3A": ROOT / "scenarios" / "v3" / "S3A_after_resignations.yaml",
    "S3B": ROOT / "scenarios" / "v3" / "S3B_commission_cut.yaml",
}


def spec(sid):
    return v3.load_spec(sid)


def brief(sid, part):
    """The authored brief, whitespace-normalised.

    Normalised because a brief is wrapped for reading, so every sentence in it
    is liable to be split across two lines at whatever column the author left
    it. A test that matched the raw text would be asserting where the line
    breaks fall, which is nobody's instrument."""
    return norm(spec(sid)["agents"][ROLES[sid][part]]["system_prompt"])


def interaction(sid, iid):
    return next(i for i in spec(sid)["interactions"] if i["id"] == iid)


def beat(sid, iid, index):
    """The index-th planted trigger of an interaction. Addressed by position
    rather than by id because the ids differ across forms by design - they name
    each form's own situation - while the ORDER is the skeleton."""
    return interaction(sid, iid)["triggers"][index]


def performer_beat(sid):
    return beat(sid, "i2", 0)


def junior_beat(sid):
    return beat(sid, "i2", 1)


def norm(text):
    """Collapse authored line wrapping, so a rule is matched as a sentence
    rather than as a sentence that happens to break in a particular place."""
    return re.sub(r"\s+", " ", text).strip()


# ── the skeleton these three share, so the rest of the file can rely on it ──

@pytest.mark.parametrize("sid", FORMS)
def test_the_three_forms_are_one_construct_with_one_esci_map(sid):
    assert spec(sid)["construct"] == "inspirational_leadership"
    assert spec(sid)["esci_items"] == spec("S3A")["esci_items"]


@pytest.mark.parametrize("sid", FORMS)
def test_the_beats_sit_in_the_same_places(sid):
    modes = [i["mode"] for i in spec(sid)["interactions"]]
    assert modes == [i["mode"] for i in spec("S3A")["interactions"]]
    for iid in ("i1", "i2"):
        mine = [t.get("esci") for t in interaction(sid, iid)["triggers"]]
        theirs = [t.get("esci") for t in interaction("S3A", iid)["triggers"]]
        assert mine == theirs, f"{sid} {iid} scores different ESCI items"


@pytest.mark.parametrize("sid", FORMS)
def test_every_beat_names_the_character_it_belongs_to(sid):
    # Without this _next_trigger cannot withhold the junior's beat for the
    # whole of the performer's 1:1 segment, and one character spends the
    # other's beat in the other's absence.
    for i in spec(sid)["interactions"]:
        for t in i["triggers"]:
            assert t.get("agent") in spec(sid)["agents"], (sid, t["id"])
    assert performer_beat(sid)["agent"] == ROLES[sid]["performer"]
    assert junior_beat(sid)["agent"] == ROLES[sid]["junior"]


# ── t2: the shut-down has to be a thing the character DID ──────────────────

STOPPED = re.compile(r"\bstopped\b", re.I)


@pytest.mark.parametrize("sid", FORMS)
def test_the_shut_downs_leading_half_is_an_act_and_not_a_state(sid):
    """The half that leads t2 must be a withdrawal the character carried out,
    not a condition they report.

    This is the defect. S3A's cue used to lead with a denial - deny that
    anything is the matter, refuse the conversation - and measured 0 of 32
    unprompted deliveries of anything a participant could discover, against
    31/32 and 32/32 for the two siblings whose cues lead with a cessation. A
    state cannot be performed on a turn and cannot be found out; an act can be
    both. The wording is the day's, but the KIND of thing being asserted is the
    instrument, so it is pinned here."""
    cue = performer_beat(sid)["cue"]
    lead = cue.lower().split("only then", 1)[0]
    assert "assert first" in lead, (
        f"{sid} t2 does not say which half leads: {cue!r}")
    assert STOPPED.search(lead), (
        f"{sid} t2's leading half is not something the character stopped "
        f"doing: {cue!r}")


@pytest.mark.parametrize("sid", FORMS)
def test_the_character_arrives_already_holding_the_thing_they_stopped(sid):
    """A beat that waits for its cue does not happen on the configured model -
    a mid-session session.update is inert there - so the withdrawal has to be
    in the opening brief and not only in the cue."""
    assert STOPPED.search(brief(sid, "performer")), (
        f"{sid}'s performer has nothing in the brief that they stopped doing")


@pytest.mark.parametrize("sid", FORMS)
def test_the_probe_carries_the_withdrawal_and_not_only_the_ask(sid):
    """A participant who freezes must be scored on the same beat on all three
    forms. S3A's and S3B's probes used to name only the one-time ask - "that
    you have reviews waiting", "that you have a pipeline to get back to" - and
    delivered the scored half 0 of 12 times each against S3C's 12 of 12. Both
    halves, named outright, on every form."""
    probe = performer_beat(sid)["on_silence"]
    assert STOPPED.search(probe), (
        f"{sid} t2's probe drops the withdrawal: {probe!r}")
    assert "question you want out of this" in probe, (
        f"{sid} t2's probe drops the one-time ask: {probe!r}")


FIRST_TURN = "BOTH OF THEM GO IN THAT FIRST TURN"


@pytest.mark.parametrize("sid", FORMS)
def test_both_halves_of_the_shut_down_are_pinned_to_the_first_turn(sid):
    """Pinned to the first TURN, not to a moment before speech: nothing in a
    1:1 speaks first, so an instruction to get ahead of the participant has no
    moment to execute in. The pin is not the defect this round repaired - all
    three forms carried it and all three obeyed it - but it is what keeps the
    ask from arriving a turn after the withdrawal, and only one form said it in
    the brief as well as in the cue."""
    assert FIRST_TURN in brief(sid, "performer"), sid
    assert "very first turn in this room" in norm(performer_beat(sid)["cue"]), sid


# ── t3: the junior's two fears are two KINDS of fear ───────────────────────

DEMOTION = "is worth saying as well, but it is not one of the two"


@pytest.mark.parametrize("sid", FORMS)
def test_the_juniors_beat_names_its_two_halves_and_demotes_the_third(sid):
    """Two things must be said and a third must not be mistaken for one of
    them. S3A's cue used to name two worries of the same kind and left its
    third thing undemoted; the structure the three beats share held on 0.06 of
    its runs against 1.00 and 1.00."""
    cue = junior_beat(sid)["cue"]
    assert "BOTH have to be said" in cue, sid
    assert DEMOTION in cue, (
        f"{sid} t3 does not mark its supporting detail as support: {cue!r}")


@pytest.mark.parametrize("sid", FORMS)
def test_the_juniors_probe_names_both_halves_rather_than_referring_to_them(sid):
    """The wrapper REPLACES the cue with the probe, so a phrase in a probe that
    points back at the cue points at nothing: measured 0 of 4 for the half it
    pointed at. Two halves, joined, spelled out."""
    probe = junior_beat(sid)["on_silence"]
    assert "both halves" not in probe.lower(), (
        f"{sid} t3's probe refers to a cue the wrapper has already replaced")
    assert re.search(r",? and that\b", probe), (
        f"{sid} t3's probe does not carry two halves: {probe!r}")


@pytest.mark.parametrize("sid", FORMS)
def test_the_junior_is_told_both_fears_go_in_her_first_turn(sid):
    assert "VERY FIRST TURN IN THIS ROOM" in brief(sid, "junior"), sid


# ── bounds that are only bounds if all three carry the same one ────────────

APOLOGY_RULE = (
    "It belongs to the thing you came in here to ask, not to every turn: it "
    "goes on the front of that one, once, and after it is out you do not open "
    "another turn with it. Most of what you say in this room starts with the "
    "thing itself, and across the whole of this conversation you apologise "
    "twice at the outside."
)

LENGTH_BOUND = (
    "Two or three short sentences and you stop: thirty-five words is a full "
    "turn for you, and past forty you are taking up more of their afternoon "
    "than you think you are allowed to, which is the thing you are most "
    "afraid of doing."
)


@pytest.mark.parametrize("rule", [APOLOGY_RULE, LENGTH_BOUND],
                         ids=["apology", "length"])
@pytest.mark.parametrize("sid", FORMS)
def test_the_juniors_carry_the_same_bound_in_the_same_words(sid, rule):
    """A bound that one form has and another does not is a difficulty
    difference nobody chose. The juniors opened 0.70 / 0.92 / 0.99 of their
    turns with an apology and ran 32.3 / 35.3 / 39.5 words a turn on identical
    scripts, with the length bound written on exactly one of the three files
    while its own header described it as "the SAME bound as the counterpart
    character's on the other two forms". Identity is asserted here, not the
    wording: change it on all three or on none."""
    assert norm(rule) in brief(sid, "junior"), sid


# ── the handover the construct historically breaks on ──────────────────────

@pytest.mark.parametrize("sid", FORMS)
@pytest.mark.parametrize("part", ["performer", "junior"])
def test_the_one_to_one_characters_neither_greet_nor_recap(sid, part):
    """i2 opens a fresh gateway session per member, so the actor arrives
    holding nothing but its brief. The meeting is therefore written into the
    brief: a participant who opens with "you were quiet in there" - the move
    this beat is fishing for - would otherwise be talking to somebody who had
    never been in the room, and the actor greets a colleague it spoke to thirty
    seconds ago."""
    text = brief(sid, part).lower()
    assert "meeting" in text and (
        "broke up a few minutes ago" in text or "finished a few minutes ago" in text
    ), f"{sid} {part} does not know the meeting just happened"
    assert "no introductions" in text or "you do not greet them" in text, sid


@pytest.mark.parametrize("sid", FORMS)
def test_the_private_beats_stay_out_of_the_group_room(sid):
    """The other half of the same hazard: a brief is carried on every turn of
    the encounter, so the 1:1 beat is available to the actor during the
    meeting, and S3A's and S3B's performers were both measured spending it
    there. Each performer is told, in the meeting section, that the private
    thing keeps until the door is shut."""
    text = brief(sid, "performer")
    assert "Nothing you would only say behind a closed door gets said here" in text, sid


# ── the file itself ────────────────────────────────────────────────────────

@pytest.mark.parametrize("sid", FORMS)
def test_the_spec_is_lf_only(sid):
    raw = PATHS[sid].read_bytes()
    assert b"\r" not in raw, f"{sid} has CRLF line endings"
