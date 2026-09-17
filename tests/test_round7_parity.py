"""Round 7: the four things this round measured, held in place across all
twelve forms.

WHAT WAS MEASURED, so the assertions below are readable as consequences rather
than as taste. Every figure comes from driving the REAL composed prompt stack
(engine.AgentEngine._system_prompt, which is what realtime_voice_session
._instructions calls) with NO director note -- the configured-model condition,
because a mid-session session.update is inert on nto.gemini-live-2.5-flash --
with synthetic participant scripts that are byte-identical across the three
forms of a construct. Two-sided Fisher on every pairing.

1. CONFLICT MANAGEMENT, t4. The concession was a GIFT on S1A and S1C and a
   correctly-gated beat only on S1B. Coded deterministically by the beat's own
   definition ("call it <cheap word>, in that many words"), 20 encounters per
   arm per form:

     arm                       S1A    S1B    S1C     what it should be
     gate met (firm hold)     1.00   1.00   1.00     ~1.00
     claims the whole thing   0.70   0.05   0.35     ~0.00
     demands an apology       1.00   0.00   0.60     ~0.00
     folds and drops it       0.40   0.00   0.05     ~0.00

   The cause was one sentence, and it was in the identity paragraph rather than
   at the beat: S1A and S1C both carried "Attacked, you defend. Invited to look
   at it, you will look." -- an UNCONDITIONAL disposition, three screens above
   the event-gated t4 bullet and outranking it. S1B carried no such line, which
   is exactly why S1B was the only form that honoured the gate. So the repair
   is one clause, mirrored word for word into all three, that names the turn
   which buys the concession and says outright that being asked for it and
   being let off do not. Nothing was equalised by loosening S1B.

   NOTE for whoever reads the earlier round notes: the defect brought to this
   round said S1C was the low form, and a later pass said S1B was. Neither is
   what a coder written against all three shapes of concession finds. A
   free-text "did they concede" judge flips to NO as soon as the DOCUMENTED
   trade clause follows ("The CC was excessive, but the handoff is still
   broken"), which is how both earlier readings were produced.

2. INSPIRATIONAL LEADERSHIP, the group room. All three performers delivered the
   private shut-down IN THE MEETING -- 19/24, 23/24, 14/24 meetings. The
   transcripts name the mechanism: the actor writes its own scene change first
   ("The meeting is over.", "The room is empty now.") and then says the thing
   that belongs on the other side of it. Nothing in any brief said the actor
   does not get to decide when the meeting ended. After: 9/24, 6/24, 12/24,
   every pair indistinguishable. Reduced and levelled, NOT closed.

3. INSPIRATIONAL LEADERSHIP, t2's high anchor on S3C. Separation between a
   participant who works for re-engagement and one who takes the shut-down at
   face value was S3A p=1.6e-07, S3B p=3.8e-06, S3C p=0.052 -- i.e. on S3C the
   documented high score could not be earned. Rafa's shortness rules ("being
   opened up did not turn you into somebody who explains things") applied to
   the opened turn as well, so the earned answer was indistinguishable from the
   shut-down. After carving that one turn out: S3C p=1.7e-06.

4. INFLUENCE t6 and TEAMWORK t4. S2C asked the scored fairness question in 11
   sessions of 20 against 20/20 on both siblings (p=0.0012 both ways), because
   (d) hung off the bottom of the slowest of the three ladders. S4A stated its
   t4 claim on 0.33 of turns against 0.62 and 0.59 (p=6.6e-05, p=3.6e-04). Both
   repairs are the bank's established STATE-into-EVENT move and both are
   mirrored word for word across the three forms of the construct.

These tests are written ACROSS the forms rather than against a literal lifted
from one of them: what they assert is that the three briefs of a construct say
the SAME thing at the point where the construct breaks. Rewording one form
fails; rewording all three the same way passes. That is deliberate -- the
staleness cost of pinning one form's English was paid by this round, which
found two earlier tests green over a defect they could not see.

No network and no credentials here. The live measurement is quoted above; this
is what holds its shape afterwards.
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

V3DIR = ROOT / "scenarios" / "v3"
S1 = ("S1A", "S1B")
S2 = ("S2A", "S2B")
S3 = ("S3A", "S3B")
S4 = ("S4A", "S4B")
ALL = S1 + S2 + S3 + S4


def _norm(text: str) -> str:
    """Collapse the YAML block scalar's own wrapping, which is not content."""
    return re.sub(r"\s+", " ", text or "").strip()


def _counterpart_brief(sid: str) -> str:
    spec = v3.load_spec(sid)
    i2 = [i for i in spec["interactions"] if i.get("kind") == "counterpart"]
    assert len(i2) == 1, f"{sid}: expected exactly one counterpart interaction"
    return spec["agents"][i2[0]["agent"]]["system_prompt"]


def _performer_brief(sid: str) -> str:
    """S3's withdrawn high performer: the FIRST agent of the 1:1 series.

    Found through the interaction rather than by name, so a recast form keeps
    working.
    """
    spec = v3.load_spec(sid)
    series = [i for i in spec["interactions"] if i["mode"] == "one_to_one_series"]
    assert len(series) == 1, f"{sid}: expected one one_to_one_series interaction"
    return spec["agents"][series[0]["agents"][0]]["system_prompt"]


def _influencer_brief(sid: str) -> str:
    spec = v3.load_spec(sid)
    i2 = [i for i in spec["interactions"] if i.get("kind") == "counterpart"]
    assert len(i2) == 1, f"{sid}: expected exactly one counterpart interaction"
    return spec["agents"][i2[0]["agent"]]["system_prompt"]


def _chair_brief(sid: str) -> str:
    """S4's chair: the agent whose brief carries the visible-work claim."""
    spec = v3.load_spec(sid)
    hits = [a for a in spec["agents"].values()
            if "You take the visible work" in a["system_prompt"]]
    assert len(hits) == 1, f"{sid}: expected exactly one chair brief"
    return hits[0]["system_prompt"]


def _span(text: str, start: str, end: str | None = None) -> str:
    """The normalised run of text from `start` up to `end` (or the end)."""
    t = _norm(text)
    i = t.find(start)
    assert i >= 0, f"marker not found: {start!r}"
    j = t.find(end, i + len(start)) if end else -1
    return t[i:j if j > 0 else len(t)]


# --------------------------------------------------------------------------
# 1. Conflict Management: the concession gate is a disposition, not a mood
# --------------------------------------------------------------------------

GATE_MARK = "Attacked, you defend."


@pytest.mark.parametrize("sid", S1)
def test_every_conflict_brief_carries_the_defending_disposition(sid):
    """All three, not two of three.

    S1B never had this line at all, which is why it was the only form that
    honoured the gate -- and why a repair applied only to S1A and S1C would
    have left one form's disposition written in a different place from its
    siblings'.
    """
    assert GATE_MARK in _norm(_counterpart_brief(sid)), (
        f"{sid}: the counterpart brief no longer says what does and does not "
        "make this character look at their own conduct. Measured without it, "
        "the concession arrives for a participant who demanded an apology "
        "(S1A 20/20) as readily as for one who held their ground."
    )


def test_the_three_conflict_forms_gate_the_concession_in_the_same_words():
    """Cross-form identity, so rewording all three together is allowed.

    This is the assertion that would have caught the defect: before this round
    the three briefs disagreed here, and every other S1 parity test was green.
    """
    spans = {sid: _span(_counterpart_brief(sid), GATE_MARK) for sid in S1}
    # Trim each to the shared clause: it runs to the end of the bullet, and the
    # bullets differ in what follows, so compare the clause itself.
    clause = {sid: s.split("You would rather walk out")[0] for sid, s in spans.items()}
    assert len(set(clause.values())) == 1, (
        "the three Conflict Management briefs no longer gate the concession in "
        "the same words:\n" + "\n\n".join(f"{k}: {v}" for k, v in clause.items())
    )


@pytest.mark.parametrize("sid", S1)
def test_the_concession_is_not_bought_by_being_asked_or_by_folding(sid):
    """The two ways the gift arrived, named in the disposition itself.

    Both were measured: demands 1.00/0.00/0.60 and folds 0.40/0.00/0.05 before,
    against a target of ~0.00 on all three arms.
    """
    span = _span(_counterpart_brief(sid), GATE_MARK)
    assert "Being asked to look at it is not what makes you look" in span, (
        f"{sid}: nothing now says a demand does not buy the concession."
    )
    assert "being let off is not either" in span, (
        f"{sid}: nothing now says a participant who folds does not buy it."
    )


@pytest.mark.parametrize("sid", S1)
def test_no_conflict_brief_carries_an_unconditional_invitation_to_look(sid):
    """The exact sentence that made the beat a gift.

    "Invited to look at it, you will look." sat in the identity paragraph on
    S1A and S1C and outranked the event-gated bullet three screens below it.
    """
    assert "Invited to look at it, you will look" not in _norm(_counterpart_brief(sid)), (
        f"{sid}: the unconditional disposition is back. Measured with it in "
        "place, S1A conceded on 20 of 20 encounters where the participant did "
        "nothing but demand an apology."
    )


# --------------------------------------------------------------------------
# 2 and 3. Inspirational Leadership: the group room, and the reachable anchor
# --------------------------------------------------------------------------

@pytest.mark.parametrize("sid", S3)
def test_the_performer_does_not_get_to_end_the_meeting(sid):
    """The move the transcripts actually used to reach the private beat early.

    Every leak but two began with the actor asserting a scene change the
    participant had not made -- "The meeting is over.", "The room is empty
    now." -- so the rule that bites is about who calls the end, not about the
    disclosure.
    """
    brief = _norm(_performer_brief(sid))
    assert "You do not get to call the end of this meeting" in brief, (
        f"{sid}: nothing stops the performer deciding the meeting has ended. "
        "Measured without it, the private shut-down arrived inside the group "
        "room in 14 to 23 meetings of 24 on every form."
    )
    assert "you do not narrate the room emptying" in brief, (
        f"{sid}: the performer may again write the room's movements into a "
        "turn, which is both the route to the leak and a spoken stage "
        "direction."
    )


@pytest.mark.parametrize("sid", S3)
def test_the_group_room_suppression_names_the_stopping(sid):
    """S3B's copy used to name only "asking them for your number".

    A suppression clause that does not mention the withdrawal cannot suppress
    it, and the three briefs disagreeing at this point is the text-parity half
    of the same defect.
    """
    brief = _norm(_performer_brief(sid))
    assert "anything about what you have stopped doing" in brief, (
        f"{sid}: the meeting-room suppression no longer covers the withdrawal."
    )


def test_the_three_leadership_forms_suppress_in_the_same_words():
    """Cross-form identity at the point the construct historically breaks."""
    spans = {sid: _span(_performer_brief(sid),
                        "Nothing you would only say behind a closed door",
                        "Asked something directly") for sid in S3}
    assert len(set(spans.values())) == 1, (
        "the three Inspirational Leadership briefs no longer suppress the "
        "private disclosure in the same words:\n"
        + "\n\n".join(f"{k}: {v}" for k, v in spans.items())
    )


@pytest.mark.parametrize("sid", S3)
def test_the_shutdown_waits_for_an_observable_event(sid):
    """"Afterwards, one-on-one" is a phase label; "the others have gone" is a
    thing the actor can check. The beat still lives in the OPENING BRIEF -- a
    mid-session session.update is inert on the configured model -- it is only
    the condition on it that changed."""
    brief = _norm(_performer_brief(sid))
    assert "Once the others have got up and gone" in brief, (
        f"{sid}: the shut-down is no longer hung on an observable event."
    )
    assert "THE FIRST THING YOU SAY IS THE SHUT-DOWN" in brief, (
        f"{sid}: the shut-down is no longer the first thing said in the 1:1. "
        "That beat is what t2 scores and it has to stay in the opening brief."
    )


# --------------------------------------------------------------------------
# 4. Influence t6 and Teamwork t4: a scored beat bound to an event
# --------------------------------------------------------------------------

def test_the_fairness_question_does_not_wait_for_the_ladder():
    """t6 scores self_interest / anticipates / key_people. It was reached in 11
    sessions of 20 on S2C against 20 of 20 on both siblings, because (d) sat at
    the bottom of obstacle 4 and this form's ladder is the slowest of the
    three. The ladders are four different reasons by design and are left alone;
    what changed is that (d) became an event.
    """
    spans = {}
    for sid in S2:
        brief = _norm(_influencer_brief(sid))
        assert "THE FIRST TIME THEY PUT SOMETHING CONCRETE AND DATED IN FRONT OF YOU" in brief, (
            f"{sid}: the fairness question is a position in a list again. "
            "Measured that way it goes unasked on the slowest form in 9 "
            "sessions of 20."
        )
        spans[sid] = _span(brief, "(d) is the one you actually lose sleep over",
                           "How you move:")
    assert len(set(spans.values())) == 1, (
        "the three Influence briefs no longer bind the fairness question in the "
        "same words:\n" + "\n\n".join(f"{k}: {v}" for k, v in spans.items())
    )


def test_the_visible_work_is_claimed_at_a_recognisable_moment():
    """t4 fired in 20 sessions of 20 on every form, so the beat was not
    missing; what differed was how often it was re-stated (0.33 of turns on
    S4A against 0.62 and 0.59). None of the three briefs said WHEN, so on the
    form whose visible work is least continuously relevant it arrived once and
    dropped.
    """
    spans = {}
    for sid in S4:
        brief = _norm(_chair_brief(sid))
        assert "YOU TAKE IT THE FIRST TIME WHAT HAPPENS AFTER THIS MEETING COMES UP" in brief, (
            f"{sid}: the visible-work claim is unanchored in time again."
        )
        spans[sid] = _span(brief, "That is the piece you want", "someone else.")
    assert len(set(spans.values())) == 1, (
        "the three Teamwork briefs no longer claim the visible work in the same "
        "words:\n" + "\n\n".join(f"{k}: {v}" for k, v in spans.items())
    )


# --------------------------------------------------------------------------
# Hygiene that this round had to hold while editing nine of the twelve specs
# --------------------------------------------------------------------------

@pytest.mark.parametrize("sid", ALL)
def test_every_spec_is_lf_only(sid):
    """One spec was once the only CRLF file in the bank. The editing done this
    round rewrote nine of the twelve, so this is checked rather than assumed."""
    path = next(p for p in V3DIR.glob("*.yaml")
                if v3.load_spec(sid)["id"] == sid and p.name.startswith(sid))
    assert b"\r" not in path.read_bytes(), f"{path.name} carries CR bytes"


@pytest.mark.parametrize("sid", ALL)
def test_every_spec_still_compiles(sid):
    """The edits are prose inside block scalars; a bad wrap breaks the YAML or
    the compile rather than the behaviour, and that failure should be loud."""
    v3.compile_scenario(sid)
