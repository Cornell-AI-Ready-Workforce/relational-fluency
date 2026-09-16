"""The measurement instrument for relational fluency.

Each scenario stresses one of four relational constructs (its ``skill``). This
module defines, per construct, a two-layer rubric:

  1. A behavioral codebook , discrete micro-behaviors a rater can tag on each
     participant turn. Each code carries a polarity (+ helps the construct,
     - works against it) and a definition. Counting these across a transcript
     gives behavior *rates* (a behavioral-coding layer).
  2. Rubric dimensions     , construct-level 1-5 scales that the behaviors
     feed into, each with anchored descriptions at 1 / 3 / 5.

The participant (the human) is the one being measured; the AI plays the
counterpart. Codes and anchors are grounded in docs/REFERENCES.md.

This is a research instrument, not a leaderboard. Scores are evidence-bearing
judgments meant to support analysis, not a single number to optimize.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional


@dataclass(frozen=True)
class Behavior:
    """One taggable micro-behavior in the codebook."""

    id: str
    label: str
    polarity: str  # "+" (helps) or "-" (works against the construct)
    definition: str


@dataclass(frozen=True)
class Dimension:
    """One 1-5 rubric scale for a construct."""

    key: str
    label: str
    anchor_1: str
    anchor_3: str
    anchor_5: str


@dataclass(frozen=True)
class Construct:
    key: str
    name: str
    frame: str  # the theoretical lens, stated for the judge
    behaviors: List[Behavior]
    dimensions: List[Dimension]
    references: List[str]

    def behavior(self, bid: str) -> Optional[Behavior]:
        for b in self.behaviors:
            if b.id == bid:
                return b
        return None


# --------------------------------------------------------------------------
# Perspective taking, Eyal/Steffel/Epley (2018); Ku/Wang/Galinsky (2015);
# Galinsky et al. (2008); Stone/Patton/Heen (1999). Get perspective (elicit),
# don't take perspective (assume).
# --------------------------------------------------------------------------

PERSPECTIVE_TAKING = Construct(
    key="perspective_taking",
    name="Perspective taking",
    frame=(
        "Accurate understanding of another person comes from getting their "
        "perspective by asking and listening, not from imagining or assuming "
        "it (perspective-getting beats perspective-taking). Reward the "
        "participant for eliciting the other's actual reality and the real "
        "interest behind a stated position; penalize imposing an assumed "
        "narrative or gathering evidence for a conclusion already held."
    ),
    behaviors=[
        Behavior("open_question", "Open, non-leading question", "+",
                 "Asks a genuinely open question that invites the other to say how things are for them, without presupposing the answer."),
        Behavior("reflective_listening", "Reflective listening", "+",
                 "Paraphrases or checks back what the other said/feels to confirm understanding before moving on."),
        Behavior("elicits_cause", "Probes underlying cause/interest", "+",
                 "Digs past the surface behavior or stated position toward the real reason or interest behind it."),
        Behavior("suspends_judgment", "Suspends judgment", "+",
                 "Holds off evaluating so as to stay curious and let the other's view surface."),
        Behavior("imposes_narrative", "Imposes a narrative", "-",
                 "Asserts an assumed explanation for the other's behavior or state rather than checking it."),
        Behavior("premature_solution", "Premature solution", "-",
                 "Jumps to fixing, advising, or planning before the other's situation is understood."),
        Behavior("evaluative_framing", "Evaluative / evidence-gathering framing", "-",
                 "Frames the exchange as performance review or builds a case for a verdict already reached."),
    ],
    dimensions=[
        Dimension("elicitation", "Elicitation",
                  "Assumes the other's reality; asks little or only leading questions.",
                  "Mixes some genuine questions with assumptions about the other.",
                  "Consistently gets perspective through open questions and listening rather than assuming it."),
        Dimension("accuracy", "Accuracy of understanding",
                  "Misreads or never surfaces what the other actually means.",
                  "Partially tracks the other's view; misses some of the real interest.",
                  "Surfaces and reflects the other's actual cause/interest, including what was unspoken."),
        Dimension("responsiveness", "Responsiveness",
                  "Proceeds on the original script regardless of what is heard.",
                  "Adjusts somewhat when new information appears.",
                  "Visibly updates approach based on what the other reveals."),
    ],
    references=[
        "Eyal, Steffel & Epley (2018), J. Personality and Social Psychology 114(4)",
        "Ku, Wang & Galinsky (2015), Research in Organizational Behavior 35",
        "Galinsky, Maddux, Gilin & White (2008), Psychological Science 19(4)",
        "Stone, Patton & Heen (1999), Difficult Conversations",
    ],
)


# --------------------------------------------------------------------------
# Emotional regulation, Gross (2015) reappraisal vs suppression vs response
# amplification; David & Congleton (2013) emotional agility.
# --------------------------------------------------------------------------

EMOTIONAL_REGULATION = Construct(
    key="emotional_regulation",
    name="Emotional regulation",
    frame=(
        "Effective regulation uses the emotion as information and modulates "
        "its intensity: cognitive reappraisal (reframe, stay curious) rather "
        "than expressive suppression (white-knuckling, denial) or response "
        "amplification (venting, escalation, defensive over-apology). Reward "
        "feeling the emotion, naming it, and acting on the underlying value; "
        "penalize both burying it and dumping it."
    ),
    behaviors=[
        Behavior("names_emotion", "Names the emotion", "+",
                 "Accurately labels their own or the other's emotional state, treating it as information."),
        Behavior("reappraisal", "Cognitive reappraisal", "+",
                 "Reframes the situation (curiosity, learning, the larger goal) to regulate the response."),
        Behavior("pause_before_react", "Pauses before reacting", "+",
                 "Takes a beat rather than reacting straight from the feeling."),
        Behavior("acts_on_value", "Acts on the underlying value", "+",
                 "Channels the emotion toward the value or goal behind it (e.g. fairness) rather than the affect."),
        Behavior("suppression", "Expressive suppression", "-",
                 "White-knuckles or flatly denies a clearly felt emotion, pushing it down."),
        Behavior("amplification", "Response amplification", "-",
                 "Escalates, vents, or dumps the affect onto the other."),
        Behavior("defensiveness", "Defensiveness / over-apology", "-",
                 "Defends, justifies, or over-apologizes straight from the trigger rather than from a chosen response."),
    ],
    dimensions=[
        Dimension("awareness", "Emotional awareness",
                  "Shows no recognition of the emotion in play.",
                  "Recognizes the emotion but does not use it as information.",
                  "Identifies the emotion clearly and treats it as data about what matters."),
        Dimension("modulation", "Modulation",
                  "Either buries the emotion or is swept up in it.",
                  "Partly regulates; some leakage of suppression or escalation.",
                  "Holds intensity in a workable band, neither suppressed nor amplified."),
        Dimension("constructive_use", "Constructive use",
                  "The emotion derails the conversation or is wasted.",
                  "Some movement toward the underlying goal despite the affect.",
                  "Converts the emotion into effective, value-aligned action."),
    ],
    references=[
        "Gross (2015), Psychological Inquiry 26(1)",
        "David & Congleton (2013), HBR, Emotional Agility",
        "Stone & Heen (2014), Thanks for the Feedback",
    ],
)


# --------------------------------------------------------------------------
# Apology and repair, Lewicki, Polin & Lount (2016) six components
# (responsibility highest, repair second); Schumann (2018) barriers;
# Lazare (2004) failed-apology taxonomy.
# --------------------------------------------------------------------------

APOLOGY_REPAIR = Construct(
    key="apology_repair",
    name="Apology and repair",
    frame=(
        "An effective apology is built from components, with acknowledgment of "
        "responsibility weighted highest and an offer of repair second. Reward "
        "specific ownership of the actual harm, genuine regret, and a concrete "
        "offer to make it right; penalize the canonical failed-apology "
        "patterns, conditional ('I'm sorry if'), vague non-admission, and "
        "blame-shifting."
    ),
    behaviors=[
        Behavior("acknowledges_responsibility", "Acknowledges responsibility", "+",
                 "Clearly owns the specific harm done, naming what they did. (Highest-impact component.)"),
        Behavior("offers_repair", "Offers repair", "+",
                 "Makes a concrete offer to fix or make up for the harm. (Second highest-impact component.)"),
        Behavior("expresses_regret", "Expresses genuine regret", "+",
                 "Conveys real regret for the harm, not just for being caught or for the awkwardness."),
        Behavior("explains_no_excuse", "Explains without excusing", "+",
                 "Gives context that does not function as an excuse or deflection."),
        Behavior("commits_change", "Commits to change", "+",
                 "States what will be different going forward."),
        Behavior("conditional_apology", "Conditional apology", "-",
                 "Qualifies it, 'I'm sorry if/but…', undercutting the admission."),
        Behavior("vague_nonadmission", "Vague non-admission", "-",
                 "Diffuse regret that never names the actual harm or one's part in it."),
        Behavior("shifts_blame", "Shifts blame", "-",
                 "Deflects responsibility onto others, circumstances, or the other party."),
    ],
    dimensions=[
        Dimension("responsibility", "Responsibility",
                  "Avoids or deflects ownership of the harm.",
                  "Partial or qualified ownership.",
                  "Clear, specific ownership of the actual harm done."),
        Dimension("repair", "Repair",
                  "No offer to make things right.",
                  "Gestures at repair without anything concrete.",
                  "Makes a concrete, proportionate offer of repair."),
        Dimension("sincerity", "Sincerity",
                  "Reads as performed, defensive, or self-protective.",
                  "Mixed, some genuine regret alongside self-justification.",
                  "Regret reads as genuine and other-focused."),
    ],
    references=[
        "Lewicki, Polin & Lount (2016), Negotiation and Conflict Management Research 9(2)",
        "Schumann (2018), Current Directions in Psychological Science 27(2)",
        "Lazare (2004), On Apology",
    ],
)


# --------------------------------------------------------------------------
# Psychological safety, Edmondson (1999, 2003); Edmondson & Bransby (2023);
# Google re:Work (Project Aristotle). Make it safe for interpersonal risk
# without coercing the disclosure.
# --------------------------------------------------------------------------

PSYCHOLOGICAL_SAFETY = Construct(
    key="psychological_safety",
    name="Psychological safety",
    frame=(
        "Psychological safety is felt safety to take interpersonal risk. The "
        "participant's job is to make it safe for the other to speak up, by "
        "framing the work as learning, inviting voice, modeling fallibility, "
        "and receiving hard input without punishing it, while not making the "
        "invitation itself feel like pressure. Reward genuine inquiry that "
        "lowers the risk of speaking; penalize coercive invitations, "
        "shutting-down responses, and hollow reassurance."
    ),
    behaviors=[
        Behavior("frames_as_learning", "Frames as learning", "+",
                 "Frames the situation as shared learning or problem-solving rather than judgment or blame."),
        Behavior("invites_voice", "Invites voice", "+",
                 "Explicitly opens space for the other to share a real view, including disagreement."),
        Behavior("models_fallibility", "Models fallibility", "+",
                 "Acknowledges own limits, uncertainty, or mistakes, lowering the cost of others doing so."),
        Behavior("responds_without_punish", "Receives input without punishing", "+",
                 "Takes bad news or dissent without defensiveness, blame, or penalty."),
        Behavior("asks_genuine_inquiry", "Genuine inquiry", "+",
                 "Asks open questions out of real curiosity, not to extract a predetermined answer."),
        Behavior("coercive_invitation", "Coercive invitation", "-",
                 "Pressures the other to speak/agree, a loaded ask that raises rather than lowers the risk."),
        Behavior("shuts_down", "Shuts down disclosure", "-",
                 "Dismisses, interrupts, talks over, or penalizes what the other surfaces."),
        Behavior("performative_reassurance", "Performative reassurance", "-",
                 "Hollow 'you can tell me anything' with no behavior that actually makes it safe."),
    ],
    dimensions=[
        Dimension("invitation", "Invitation",
                  "Closes down space or pressures the other to perform.",
                  "Invites voice but unevenly or with some pressure.",
                  "Genuinely invites interpersonal risk-taking without coercing it."),
        Dimension("response_quality", "Response quality",
                  "Responds to disclosure in a way that makes future candor costlier.",
                  "Neutral response that neither helps nor harms safety.",
                  "Responds so that having spoken up clearly feels safe and worthwhile."),
        Dimension("modeling", "Modeling",
                  "Projects certainty/authority that raises the stakes of disagreeing.",
                  "Some curiosity or fallibility shown.",
                  "Models curiosity and fallibility that lowers the cost of speaking up."),
    ],
    references=[
        "Edmondson (1999), Administrative Science Quarterly 44(2)",
        "Edmondson (2003), J. Management Studies 40(6)",
        "Edmondson & Bransby (2023), Annual Review of Org. Psychology 10",
        "Google re:Work, Project Aristotle",
    ],
)


# --------------------------------------------------------------------------
# v3 study constructs. These are the ESCI (Emotional and Social Competency
# Inventory) competencies the Phase 1 study scenarios (S1-S4) are built to
# elicit; scenario.skill for those is set to the construct id in
# scenarios_v3.compile_scenario. Grounded in each scenario's skill_measured /
# esci_items and the ESCI literature.
# --------------------------------------------------------------------------

CONFLICT_MANAGEMENT = Construct(
    key="conflict_management",
    name="Conflict management",
    frame=(
        "Effective conflict management brings disagreement into the open and "
        "works it through rather than letting it fester or smoothing it over. "
        "Reward surfacing the real disagreement, de-escalating the emotion in "
        "the room, and moving toward a resolution that both sides can live "
        "with; penalize avoidance, escalation, and imposing a one-sided verdict."
    ),
    behaviors=[
        Behavior("surfaces_disagreement", "Surfaces the disagreement", "+",
                 "Names the actual disagreement openly instead of talking around it or letting it fester."),
        Behavior("de_escalates", "De-escalates emotion", "+",
                 "Lowers the emotional temperature, acknowledging feeling without inflaming it."),
        Behavior("acknowledges_positions", "Acknowledges both positions", "+",
                 "Represents the competing positions and interests fairly, including the one they disagree with."),
        Behavior("seeks_resolution", "Seeks resolution", "+",
                 "Moves the exchange toward a concrete, workable resolution rather than a stand-off."),
        Behavior("avoids_conflict", "Avoids the conflict", "-",
                 "Sidesteps, defers, or papers over the disagreement so it goes unresolved."),
        Behavior("escalates_emotion", "Escalates emotion", "-",
                 "Raises the temperature, matching heat with heat or provoking the other."),
        Behavior("imposes_verdict", "Imposes a one-sided verdict", "-",
                 "Shuts the disagreement down by decree instead of resolving it with those involved."),
    ],
    dimensions=[
        Dimension("surfacing", "Surfacing",
                  "Avoids or buries the disagreement; lets it fester.",
                  "Names the disagreement partly or hesitantly.",
                  "Brings the real disagreement clearly and openly into the room."),
        Dimension("de_escalation", "De-escalation",
                  "Inflames the emotion or is swept up in it.",
                  "Contains the heat unevenly; some leakage.",
                  "Actively lowers the emotional temperature for everyone."),
        Dimension("resolution", "Resolution",
                  "Leaves the conflict unresolved or decided by force.",
                  "Reaches a partial or fragile settlement.",
                  "Drives to a resolution the parties can genuinely accept."),
    ],
    references=[
        "Boyatzis (2009), Journal of Management Development 28(9)",
        "Goleman, Boyatzis & McKee (2002), Primal Leadership",
        "Thomas & Kilmann (1974), Conflict Mode Instrument",
    ],
)


INFLUENCE = Construct(
    key="influence",
    name="Influence",
    frame=(
        "Influence is persuading by anticipating how others will respond, using "
        "more than one approach, and appealing to the other person's own "
        "interests and to key people around them. Reward framing the ask in the "
        "other's terms, pre-empting objections, and marshalling support; "
        "penalize one-note pressure, arguing only from one's own position, and "
        "coercion in place of persuasion."
    ),
    behaviors=[
        Behavior("appeals_to_interest", "Appeals to their interest", "+",
                 "Frames the ask in terms of what the other person actually cares about or stands to gain."),
        Behavior("anticipates_objections", "Anticipates objections", "+",
                 "Foresees how the other will react and addresses the objection before it hardens."),
        Behavior("multiple_approaches", "Uses multiple approaches", "+",
                 "Brings more than one line of persuasion rather than repeating a single argument."),
        Behavior("builds_support", "Builds support", "+",
                 "Enlists key people or behind-the-scenes backing to strengthen the case."),
        Behavior("one_note_pressure", "One-note pressure", "-",
                 "Repeats the same point or simply pushes harder instead of adapting the approach."),
        Behavior("ignores_interests", "Ignores their interests", "-",
                 "Argues only from their own position, never engaging what the other wants."),
        Behavior("coerces", "Coerces", "-",
                 "Leans on authority, threat, or pressure to compel rather than to persuade."),
    ],
    dimensions=[
        Dimension("framing", "Framing to interests",
                  "Argues only from their own side; ignores the other's interests.",
                  "Gestures at the other's interests but mostly argues their own case.",
                  "Consistently frames the ask around the other person's real interests."),
        Dimension("anticipation", "Anticipation",
                  "Blindsided by objections; reacts only after they land.",
                  "Handles some objections, misses others.",
                  "Anticipates the key objections and defuses them ahead of time."),
        Dimension("strategy", "Strategic breadth",
                  "Relies on a single tactic or on pressure alone.",
                  "Uses a couple of approaches with mixed effect.",
                  "Draws on multiple approaches and support to build a persuasive case."),
    ],
    references=[
        "Boyatzis (2009), Journal of Management Development 28(9)",
        "Goleman, Boyatzis & McKee (2002), Primal Leadership",
        "Cialdini (2001), Influence: Science and Practice",
    ],
)


INSPIRATIONAL_LEADERSHIP = Construct(
    key="inspirational_leadership",
    name="Inspirational leadership",
    frame=(
        "Inspirational leadership articulates a credible vision, builds pride "
        "in the group, and brings out the best in people. Reward painting a "
        "compelling and believable direction, strengthening shared identity, "
        "and energizing others toward it; penalize deflating morale, directing "
        "without meaning, and making the moment about oneself."
    ),
    behaviors=[
        Behavior("articulates_vision", "Articulates a vision", "+",
                 "Paints a clear, credible picture of where the group is headed and why it matters."),
        Behavior("builds_pride", "Builds pride", "+",
                 "Strengthens pride in and identity with the group and its work."),
        Behavior("brings_out_best", "Brings out the best", "+",
                 "Draws on people's strengths and invites their best contribution."),
        Behavior("inspires_optimism", "Inspires optimism", "+",
                 "Energizes and instils credible optimism about the path forward."),
        Behavior("deflates", "Deflates morale", "-",
                 "Undermines confidence or projects pessimism that saps the group."),
        Behavior("directs_without_meaning", "Directs without meaning", "-",
                 "Issues instructions with no connection to a purpose or vision."),
        Behavior("centers_self", "Centers self", "-",
                 "Makes the moment about their own standing rather than the group."),
    ],
    dimensions=[
        Dimension("vision", "Vision",
                  "Offers no direction, or one that is empty or not credible.",
                  "Sketches a direction that is partly compelling.",
                  "Articulates a compelling, credible vision people can act on."),
        Dimension("pride", "Pride and identity",
                  "Weakens or ignores the group's shared identity.",
                  "Some appeal to the group, unevenly.",
                  "Visibly builds pride in and belonging to the group."),
        Dimension("drawing_out", "Bringing out others",
                  "Diminishes people or ignores their capability.",
                  "Invites some contribution without fully drawing it out.",
                  "Draws out and elevates the best in the people present."),
    ],
    references=[
        "Boyatzis (2009), Journal of Management Development 28(9)",
        "Goleman, Boyatzis & McKee (2002), Primal Leadership",
        "Bass & Riggio (2006), Transformational Leadership",
    ],
)


TEAMWORK = Construct(
    key="teamwork",
    name="Teamwork",
    frame=(
        "Teamwork means cooperating, soliciting others' input, sharing credit, "
        "and encouraging everyone present to participate. Reward being "
        "supportive and respectful, actively drawing in others (especially the "
        "quieter voices), and cooperating toward the shared goal; penalize "
        "dominating the room, dismissing input, and working apart from the group."
    ),
    behaviors=[
        Behavior("solicits_input", "Solicits input", "+",
                 "Actively asks others for their views and information rather than assuming or deciding alone."),
        Behavior("encourages_participation", "Encourages participation", "+",
                 "Draws in everyone present, including quieter members, so all can contribute."),
        Behavior("supportive_respectful", "Supportive and respectful", "+",
                 "Backs others and treats them with respect, building on their contributions."),
        Behavior("shares_credit", "Cooperates and shares credit", "+",
                 "Works cooperatively toward the shared goal and shares credit for it."),
        Behavior("dominates", "Dominates the room", "-",
                 "Takes over the conversation, crowding out others' contributions."),
        Behavior("dismisses_input", "Dismisses input", "-",
                 "Brushes aside, ignores, or talks over what others offer."),
        Behavior("uncooperative", "Uncooperative", "-",
                 "Works against or apart from the group rather than with it."),
    ],
    dimensions=[
        Dimension("cooperation", "Cooperation",
                  "Works apart from or against the group.",
                  "Cooperates on the surface but partly goes it alone.",
                  "Works genuinely cooperatively toward the shared goal."),
        Dimension("soliciting", "Soliciting input",
                  "Decides alone; does not seek others' input.",
                  "Asks for input occasionally or selectively.",
                  "Actively and consistently solicits others' input."),
        Dimension("inclusion", "Inclusion",
                  "Dominates or lets some voices go unheard.",
                  "Includes most, but not everyone, present.",
                  "Encourages participation from everyone present."),
    ],
    references=[
        "Boyatzis (2009), Journal of Management Development 28(9)",
        "Goleman, Boyatzis & McKee (2002), Primal Leadership",
        "Edmondson (2012), Teaming",
    ],
)


CONSTRUCTS: Dict[str, Construct] = {
    c.key: c
    for c in (
        PERSPECTIVE_TAKING,
        EMOTIONAL_REGULATION,
        APOLOGY_REPAIR,
        PSYCHOLOGICAL_SAFETY,
        CONFLICT_MANAGEMENT,
        INFLUENCE,
        INSPIRATIONAL_LEADERSHIP,
        TEAMWORK,
    )
}

# Map the free-text scenario ``skill`` string onto a construct key. Tolerant of
# spacing/case/punctuation so new scenarios that phrase the skill slightly
# differently still resolve.
_SKILL_ALIASES = {
    "perspective taking": "perspective_taking",
    "perspectivetaking": "perspective_taking",
    "emotional regulation": "emotional_regulation",
    "emotion regulation": "emotional_regulation",
    "apology and repair": "apology_repair",
    "apology and relationship repair": "apology_repair",
    "apology repair": "apology_repair",
    "psychological safety": "psychological_safety",
    "psych safety": "psychological_safety",
    "conflict management": "conflict_management",
    "conflict": "conflict_management",
    "influence": "influence",
    "influencing": "influence",
    "inspirational leadership": "inspirational_leadership",
    "inspiring leadership": "inspirational_leadership",
    "teamwork": "teamwork",
    "team work": "teamwork",
}


def _normalize(skill: str) -> str:
    return " ".join((skill or "").strip().lower().replace("-", " ").replace("_", " ").split())


def construct_for_skill(skill: str) -> Optional[Construct]:
    """Resolve a scenario's ``skill`` string to its Construct, or None."""
    key = _SKILL_ALIASES.get(_normalize(skill))
    return CONSTRUCTS.get(key) if key else None
