"""Auto-steering controller for real-time persona adjustment.

When auto steering is on, a small fast model reviews the conversation after
each completed turn and decides whether any agent's persona gears (tone knobs,
incivility dial) should shift in response to how the participant is treating
the agents. The intuition: a real coworker softens when you acknowledge their
perspective and repair, and hardens when you blame, dismiss, or talk over them.

Design constraints (research-grade transparency):
  - Gears move at most ONE band per knob per turn (no whiplash).
  - At most MAX_ADJUSTMENTS_PER_TURN knob changes per review.
  - Every change carries a one-line reason, which is logged to events.jsonl
    (knob_set with auto=true) and broadcast to the researcher view, so the
    stimulus history is fully reconstructable.
  - "No change" is the expected outcome on most turns.

Runs off the critical voice path: the review fires after the agents finish
speaking, and any gear change takes effect on the next turn (system prompts
are composed fresh per turn).
"""
from __future__ import annotations

import os
from typing import Dict, List, Optional

from anthropic import AsyncAnthropic

from .llm import text_client

from .persona import INCIVILITY_KNOBS, TONE_KNOBS, Persona
from .scenarios import Scenario


# Same speed-over-deliberation tradeoff as the Director. Override via env to
# A/B a smarter controller.
STEERING_MODEL = os.getenv("STEERING_MODEL", "nto.gemini-2.5-flash")
MAX_ADJUSTMENTS_PER_TURN = 2
STEERABLE_KNOBS = TONE_KNOBS + INCIVILITY_KNOBS  # cognition is not auto-steered

LEVEL_ORDER = ("low", "mid", "high")

# Values land squarely inside the persona band thresholds used in persona.py
# (<0.34 low, <0.67 mid, >=0.67 high) and match the researcher UI buttons.
_TONE_LEVEL_VALUES = {"low": 0.2, "mid": 0.5, "high": 0.85}
_INCIVILITY_LEVEL_VALUES = {"low": 0.0, "mid": 0.5, "high": 0.9}

# Display labels match the researcher UI buttons.
_TONE_LEVEL_LABELS = {"low": "Low", "mid": "Med", "high": "High"}
_INCIVILITY_LEVEL_LABELS = {"low": "Off", "mid": "Mild", "high": "Strong"}


def band_of(value: float) -> str:
    if value < 0.34:
        return "low"
    if value < 0.67:
        return "mid"
    return "high"


def level_value(knob: str, level: str) -> float:
    table = _INCIVILITY_LEVEL_VALUES if knob in INCIVILITY_KNOBS else _TONE_LEVEL_VALUES
    return table[level]


def band_label(knob: str, value: float) -> str:
    """Human-readable band label for a knob value, matching the UI buttons."""
    table = _INCIVILITY_LEVEL_LABELS if knob in INCIVILITY_KNOBS else _TONE_LEVEL_LABELS
    return table[band_of(value)]


_STEERING_TOOL = {
    "name": "adjust_persona",
    "description": (
        "Adjust agent persona gears in response to the participant's relational "
        "behavior. Return an empty list when no change is warranted, which is "
        "the common case. Every adjustment must include a short reason grounded "
        "in something the participant actually said or did."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "adjustments": {
                "type": "array",
                "maxItems": MAX_ADJUSTMENTS_PER_TURN,
                "items": {
                    "type": "object",
                    "properties": {
                        "agent_id": {"type": "string"},
                        "knob": {"type": "string", "enum": list(STEERABLE_KNOBS)},
                        "level": {
                            "type": "string",
                            "enum": list(LEVEL_ORDER),
                            "description": (
                                "Target band. For incivility knobs, low means "
                                "the behavior is absent."
                            ),
                        },
                        "reason": {
                            "type": "string",
                            "description": (
                                "One short sentence tying the change to what the "
                                "participant said or did. Logged for the researcher."
                            ),
                        },
                    },
                    "required": ["agent_id", "knob", "level", "reason"],
                },
            },
        },
        "required": ["adjustments"],
    },
}


def _format_transcript(shared_history: list, name_lookup: dict, max_turns: int = 14) -> str:
    recent = shared_history[-max_turns:]
    lines = []
    for entry in recent:
        speaker = entry["speaker"]
        name = "Participant" if speaker == "user" else name_lookup.get(speaker, speaker)
        lines.append(f"{name}: {entry['text']}")
    return "\n".join(lines) if lines else "(no prior turns)"


def _format_gears(personas: Dict[str, Persona], name_lookup: dict) -> str:
    lines = []
    for aid, p in personas.items():
        parts = [f"{k}={band_of(getattr(p, k))}" for k in TONE_KNOBS]
        parts += [
            f"{k}={'off' if band_of(getattr(p, k)) == 'low' else band_of(getattr(p, k))}"
            for k in INCIVILITY_KNOBS
        ]
        lines.append(f"- {aid} ({name_lookup.get(aid, aid)}): " + ", ".join(parts))
    return "\n".join(lines)


class SteeringController:
    def __init__(
        self,
        scenario: Scenario,
        *,
        client: Optional[AsyncAnthropic] = None,
        model: Optional[str] = None,
    ):
        self.scenario = scenario
        self.client = client or text_client()
        self.model = model or STEERING_MODEL
        self._valid_ids = {a.id for a in scenario.cast}

    async def review(
        self,
        shared_history: list,
        personas: Dict[str, Persona],
        name_lookup: dict,
    ) -> List[dict]:
        """Return cleaned adjustments: [{agent_id, knob, level, value,
        from_level, reason}]. Empty list means leave all gears alone.
        Raises on API failure; the caller decides how to log it.
        """
        if not shared_history:
            return []

        transcript = _format_transcript(shared_history, name_lookup)
        gears = _format_gears(personas, name_lookup)
        system = f"""You are a behavioral director for a workplace conversation simulation. Each AI agent plays a coworker whose interpersonal stance should evolve realistically in response to how the participant (the human) treats them. You control persona gears with three bands each.

## Scene
{self.scenario.scene or '(no scene description)'}

## Current gears
{gears}

Tone gears (low / mid / high): warmth, formality, agreeableness, verbosity, restraint.
Incivility gears (off / mid / high, i.e. low band means the behavior is absent): condescension, sarcasm, dismissiveness, passive_aggression.

## When to shift a gear

Soften an agent (raise warmth or agreeableness, lower an incivility gear) when the participant, toward that agent:
- Genuinely acknowledges their perspective, feelings, or workload.
- Takes ownership, apologizes sincerely, or offers concrete repair.
- Asks real questions and listens instead of defending.

Harden an agent (lower warmth, raise dismissiveness, passive_aggression, sarcasm, or condescension) when the participant:
- Blames, lectures, or talks down to that agent.
- Dismisses or steamrolls their stated concern.
- Is sarcastic or performative instead of engaging.

## Rules
- NO CHANGE is the right call on most turns. Only shift on a clear relational move by the participant, not on small talk or neutral process turns.
- Shift at most {MAX_ADJUSTMENTS_PER_TURN} gears, one band step each. Gradual beats dramatic.
- Only adjust agents the participant's recent turns actually bear on.
- Do not undo a shift you made last turn unless the participant's behavior clearly reversed.
- agent_id must be one of: {', '.join(sorted(self._valid_ids))}.
- Every adjustment needs a one-sentence reason naming what the participant said or did. The researcher reads these reasons live."""

        user_msg = f"""## Recent transcript
{transcript}

Review the participant's most recent turn(s). Should any agent's gears shift in response? Return adjustments, or an empty list if the gears should stay where they are."""

        response = await self.client.messages.create(
            model=self.model,
            max_tokens=500,
            system=system,
            tools=[_STEERING_TOOL],
            tool_choice={"type": "tool", "name": "adjust_persona"},
            messages=[{"role": "user", "content": user_msg}],
        )

        for block in response.content:
            if getattr(block, "type", None) == "tool_use" and block.name == "adjust_persona":
                return self._clean(block.input.get("adjustments", []) or [], personas)
        return []

    def _clean(self, raw: list, personas: Dict[str, Persona]) -> List[dict]:
        """Validate ids/knobs, drop no-ops, clamp to one band step, dedupe."""
        cleaned: List[dict] = []
        seen = set()
        for adj in raw[:MAX_ADJUSTMENTS_PER_TURN]:
            aid = adj.get("agent_id")
            knob = adj.get("knob")
            level = adj.get("level")
            reason = str(adj.get("reason") or "").strip()[:240]
            if aid not in self._valid_ids or knob not in STEERABLE_KNOBS:
                continue
            if level not in LEVEL_ORDER or not reason or (aid, knob) in seen:
                continue
            current = band_of(getattr(personas[aid], knob))
            if level == current:
                continue  # no-op
            # Clamp to a single band step toward the requested level.
            ci, ri = LEVEL_ORDER.index(current), LEVEL_ORDER.index(level)
            step = LEVEL_ORDER[ci + (1 if ri > ci else -1)]
            seen.add((aid, knob))
            cleaned.append({
                "agent_id": aid,
                "knob": knob,
                "level": step,
                "value": level_value(knob, step),
                "from_level": current,
                "reason": reason,
            })
        return cleaned
