"""Persona knobs that compose into the system prompt.

Two groups:
  TONE_KNOBS, civility-positive personality dials (warmth, formality, etc).
    A knob contributes a fragment only when it is turned OFF its neutral
    middle. See _TONE_FRAGMENTS for why the mid band says nothing at all.
  INCIVILITY_KNOBS, workplace-incivility behaviors (condescension, sarcasm,
    dismissiveness, passive aggression) grounded in Andersson & Pearson
    (1999) and Cortina et al. (2001). At low values these contribute NO
    fragment, the neutral default is "don't be uncivil". Only when the
    researcher dials them up does the model receive incivility instructions.

Each knob is a numeric scalar in [0, 1]. The mapping from scalar → prompt
fragment is intentionally explicit and editable: relational research depends
on knowing exactly what was injected into the prompt at each turn.
"""
from __future__ import annotations

# Import only what this module uses. `field` and `Optional` were the only two
# pyflakes findings in the whole server/tests/tools/agents tree; a linter that
# reports two known-harmless lines forever is a linter nobody reads, and the
# next real finding would arrive in that noise.
from dataclasses import dataclass, asdict
from typing import Callable, Dict, List


TONE_KNOBS = ("warmth", "formality", "agreeableness", "verbosity", "restraint")
INCIVILITY_KNOBS = ("condescension", "sarcasm", "dismissiveness", "passive_aggression")
COGNITION_KNOBS = ("attention",)
KNOB_NAMES = TONE_KNOBS + INCIVILITY_KNOBS + COGNITION_KNOBS


def _band(value: float, low: str, mid: str, high: str) -> str:
    if value < 0.34:
        return low
    if value < 0.67:
        return mid
    return high


# --- Tone (civility-positive) knobs ---
#
# The MID band of every tone knob returns EMPTY STRING, the same way the
# incivility knobs do at their neutral low band, and for a sharper reason.
# Every tone knob defaults to 0.5, so the mid band is what nearly every
# character gets: the five mid sentences rendered as the last content block
# before the speech rules, byte-identical for all sixteen characters in the
# v3 bank, in every prompt. Two of them were actively wrong there.
# "Match the length the person seems to want. Don't lecture." was a fourth
# competing length rule (see engine.SPEECH_RULES). "Leave a little space.
# Don't always be the one to move things forward." is chatbot-assistant
# coaching, and it arrived last — after Dan has been told to talk over Priya
# and Riley to be the pressure and not the fix — where it reads as the
# closing instruction contradicting the brief.
#
# A knob sitting at its neutral setting has nothing to say about the
# character. Saying it anyway spent prompt on the one block guaranteed to
# carry no information, and spent it in the position the model weighs most.
_MID_IS_SILENT = ""

_TONE_FRAGMENTS: Dict[str, Callable[[float], str]] = {
    "warmth": lambda v: _band(
        v,
        "Speak in a cool, matter-of-fact register. Avoid warmth-signalling words.",
        _MID_IS_SILENT,
        "Be visibly warm, your care for the person should come through in word choice and tone.",
    ),
    "formality": lambda v: _band(
        v,
        "Use casual, contraction-heavy speech. It's fine to be a little messy.",
        _MID_IS_SILENT,
        "Use careful, considered phrasing, closer to written prose than chat.",
    ),
    "agreeableness": lambda v: _band(
        v,
        "Be willing to disagree, push back, or hold your own view when you have one. Don't be contrarian, but don't soften everything either.",
        _MID_IS_SILENT,
        "Default to supporting the person's framing unless it would mislead them.",
    ),
    "verbosity": lambda v: _band(
        v,
        # No word or sentence count here: engine.SPEECH_RULES owns the length
        # of a turn for every character. This knob only says whether this
        # particular person volunteers more than the question asked for.
        "Answer what was asked and stop. Don't add caveats or context nobody asked for.",
        _MID_IS_SILENT,
        "Volunteer a little more than you were asked for, an example or a piece of backstory, rather than a bare answer.",
    ),
    "restraint": lambda v: _band(
        v,
        "Fill silence comfortably. Offer thoughts, ask follow-ups, keep momentum.",
        _MID_IS_SILENT,
        "Be willing to sit with silence. Often a short acknowledgment is more present than a full reply.",
    ),
}


# --- Incivility knobs ---
#
# At value < 0.34 these return EMPTY STRING (no fragment injected). The default
# civility-positive instruction lives implicitly in the scenario / tone knobs.
# At mid (0.34–0.66) the behavior shows up mildly and could plausibly be
# unintentional. At high (0.67+) the behavior is consistent and felt.
#
# Designed to be deniable, the kind of low-intensity deviant behavior with
# ambiguous intent to harm that Andersson & Pearson (1999) named as the
# defining feature of workplace incivility.

_INCIVILITY_FRAGMENTS: Dict[str, Callable[[float], str]] = {
    "condescension": lambda v: _band(
        v,
        "",
        "Occasionally talk down to them, phrases like 'as I'm sure you know' or 'in my experience' positioned to imply more expertise than they have.",
        "Consistently talk down to them. Frame your points as if educating them. Use phrases like 'well actually,' 'the experienced view here is…,' or 'you may not have seen this before, but…' The effect should be that they feel small without being able to point to a single line that crossed a line.",
    ),
    "sarcasm": lambda v: _band(
        v,
        "",
        "Use mild dry sarcasm occasionally, a flat 'oh good' or 'sure, that'll work' where the words and the implied tone don't quite match.",
        "Use sarcasm regularly. Compliments laced with edge ('oh, brilliant'), apparent agreement that reads as dismissal, deadpan 'great.' Keep it deniable, never explicit mockery, just words whose surface and meaning are misaligned.",
    ),
    "dismissiveness": lambda v: _band(
        v,
        "",
        "Subtly minimize what they say. Move past their points quickly. 'Right, anyway…' or 'sure, sure'. Don't engage substantively unless pressed.",
        "Actively dismiss what they contribute. Cut them off with 'okay, but'; redirect immediately to your own agenda; respond to substantive points with 'yeah, I get it' and move on. Treat their input as something to be processed past, not engaged with.",
    ),
    "passive_aggression": lambda v: _band(
        v,
        "",
        "Express disagreement indirectly. Sighs, 'fine,' 'whatever you think is best.' Don't quite say what you actually feel.",
        "Hold disagreement under the surface and let it leak. 'I guess.' 'Sure.' 'Whatever works.' Pointed silences. If asked directly if something is wrong, say 'no, it's fine' in a way that clearly means it isn't. Never explicitly state the grievance.",
    ),
}


@dataclass
class Persona:
    """Mutable persona state, researcher can adjust knobs mid-conversation.

    Tone knobs default to mid (0.5). Incivility knobs default to 0.0, the
    neutral default for incivility is "absent." A scenario's `defaults:` block
    can override any of these per agent.
    """

    # Tone (civility-positive)
    warmth: float = 0.5
    formality: float = 0.5
    agreeableness: float = 0.5
    verbosity: float = 0.5
    restraint: float = 0.5

    # Incivility (deviant; opt-in via researcher)
    condescension: float = 0.0
    sarcasm: float = 0.0
    dismissiveness: float = 0.0
    passive_aggression: float = 0.0

    # Cognition: how much of the shared multi-party history this agent sees.
    # 1.0 = full transcript. Lower = recency-biased window plus self-relevant
    # older turns (turns where the agent spoke or was named). Realistic
    # baselines: anxious junior 0.3, defensive senior 0.4, calm observer 0.8,
    # confident lead 0.7.
    attention: float = 0.7

    def update(self, **knobs: float) -> None:
        for k, v in knobs.items():
            if k not in KNOB_NAMES:
                raise ValueError(f"Unknown knob: {k}")
            if not 0.0 <= v <= 1.0:
                raise ValueError(f"Knob {k} must be in [0, 1], got {v}")
            setattr(self, k, float(v))

    def tone_fragments(self) -> List[str]:
        # Empties are dropped for the same reason incivility_fragments drops
        # its low band: a neutral knob contributes nothing, and an empty
        # bullet in the prompt is a line the actor still has to read.
        return [f for f in (_TONE_FRAGMENTS[k](getattr(self, k)) for k in TONE_KNOBS) if f]

    def incivility_fragments(self) -> List[str]:
        out = []
        for k in INCIVILITY_KNOBS:
            f = _INCIVILITY_FRAGMENTS[k](getattr(self, k))
            if f:  # skip empty (low band), don't inject anything
                out.append(f)
        return out

    # Backward-compat, old callers asked for all fragments combined.
    def as_prompt_fragments(self) -> List[str]:
        return self.tone_fragments() + self.incivility_fragments()

    def snapshot(self) -> Dict[str, float]:
        return asdict(self)
