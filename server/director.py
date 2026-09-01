"""Director routing for multi-agent group scenarios.

After each user turn, the director decides which agent(s) should respond and
in what order, plus an optional one-line intent for each. Uses Claude with
tool-use to force structured JSON output, and a fast/cheap model since this
runs on the critical path of every turn.
"""
from __future__ import annotations

from typing import List, Optional

from anthropic import AsyncAnthropic

from .llm import setting, text_client

from .scenarios import Agent, Scenario


# Routing is high-frequency and benefits from speed > deliberation. Haiku is
# fine; can be overridden via env if you want to A/B against Sonnet. Resolved
# via the shared .env-first accessor so an override in .env actually takes
# effect (os.getenv would ignore the .env file).
DIRECTOR_MODEL = setting("DIRECTOR_MODEL", "nto.gemini-3.1-flash-lite")
DIRECTOR_MAX_SPEAKERS = 3


_DIRECTOR_TOOL = {
    "name": "set_speakers",
    "description": (
        "Decide which agents respond next and in what order. "
        "Return an empty list if no agent should speak (silence is a valid choice). "
        "How many speakers depends entirely on the scenario routing guidance: it "
        "may call for a single responder, or a multi-speaker sequence where agents "
        "argue with EACH OTHER (e.g. [A, B, A]). Follow that guidance; do not "
        "default to one."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "rationale": {
                "type": "string",
                "description": "One short sentence on why these speakers, in this order.",
            },
            "speakers": {
                "type": "array",
                "maxItems": DIRECTOR_MAX_SPEAKERS,
                "items": {
                    "type": "object",
                    "properties": {
                        "agent_id": {"type": "string"},
                        "intent": {
                            "type": "string",
                            "description": (
                                "Short note describing what this speaker's turn should "
                                "do (e.g., 'concede the technical point', 'stay silent "
                                "but visibly tense', 'ask a clarifying question'). "
                                "Optional, omit if a vanilla reply is fine."
                            ),
                        },
                    },
                    "required": ["agent_id"],
                },
            },
        },
        "required": ["speakers"],
    },
}


def _format_transcript(shared_history: list, name_lookup: dict, max_turns: int = 6) -> str:
    """Render the most recent turns as a transcript string for the director."""
    recent = shared_history[-max_turns:]
    lines = []
    for entry in recent:
        speaker = entry["speaker"]
        name = "User" if speaker == "user" else name_lookup.get(speaker, speaker)
        lines.append(f"{name}: {entry['text']}")
    return "\n".join(lines) if lines else "(no prior turns)"


def _format_cast(cast: List[Agent]) -> str:
    lines = []
    for a in cast:
        # First line of the agent's system prompt is usually the strongest
        # character signal, keep the description compact for the director.
        first = a.system_prompt.strip().split("\n", 1)[0][:200]
        lines.append(f"- {a.id} ({a.name}): {first}")
    return "\n".join(lines)


class Director:
    def __init__(
        self,
        scenario: Scenario,
        *,
        client: Optional[AsyncAnthropic] = None,
        model: Optional[str] = None,
    ):
        self.scenario = scenario
        self.client = client or text_client()
        self.model = model or DIRECTOR_MODEL
        self._cast_block = _format_cast(scenario.cast)
        self._valid_ids = {a.id for a in scenario.cast}
        self._name_lookup = {a.id: a.name for a in scenario.cast}

    async def route(
        self,
        shared_history: list,
        latest_user_text: str,
    ) -> List[dict]:
        """Return [{agent_id, intent?}], possibly empty."""
        # Fast path: the very first reaction (no agent has spoken yet) is the
        # configured opener. Skip the director LLM call so the meeting opens fast.
        opener = getattr(self.scenario, "opener", None)
        if opener and not any(e.get("speaker") != "user" for e in shared_history):
            served = [{"agent_id": aid} for aid in opener if aid in self._valid_ids]
            if served:
                return served

        transcript = _format_transcript(shared_history, self._name_lookup)
        system = f"""You are a meeting director routing turns in a multi-party voice conversation.

## Cast
{self._cast_block}

## Decision procedure (apply in order, stop at the first that matches)

1. **Did the user address an agent by name?** If the user's latest turn names
   a specific agent (e.g. "Marcus, what do you think?", "Theo, your read?"),
   route ONLY to that named agent. Do not also include others. Ignore all
   other heuristics. This rule has highest priority.

2. **Otherwise, FOLLOW THE SCENARIO ROUTING GUIDANCE BELOW.** It decides who
   speaks and HOW MANY. If it asks for a multi-speaker sequence where agents
   argue with each other (e.g. [arjun, claire, arjun]), return exactly that,
   do NOT trim it down to one speaker. The number of speakers is whatever the
   guidance says, up to the max.

3. **Did the user say something purely transitional?** ("ok", "thanks") with
   nothing substantive, a single brief responder or empty is fine.

## Scene
{self.scenario.scene or '(no scene description)'}

## Scenario-specific routing guidance
{self.scenario.director_prompt or 'Default: one realistic speaker per turn based on who would naturally respond given the cast and recent flow.'}

## Hard rules
- Use ONLY agent_ids from the cast above. Never invent new ones.
- Return at most {DIRECTOR_MAX_SPEAKERS} speakers per turn.
- Order matters, first speaker in the list speaks first.
- Empty list = nobody speaks. Use for genuine silence or pure transitions.
- If the user's turn explicitly names an agent, that agent MUST be the only
  speaker, regardless of personality defaults like "X tends to answer first."
- Spread the floor. Unless the user names them or the guidance demands it,
  do NOT route to the agent who spoke most recently; pick someone who has
  spoken less. A meeting where one voice answers everything is a failure.
"""
        user_msg = f"""## Recent transcript
{transcript}

## Latest user turn
{latest_user_text}

Decide who speaks next."""
        try:
            response = await self.client.messages.create(
                model=self.model,
                max_tokens=160,
                system=system,
                tools=[_DIRECTOR_TOOL],
                tool_choice={"type": "tool", "name": "set_speakers"},
                messages=[{"role": "user", "content": user_msg}],
            )
        except Exception:
            # If routing fails, fall back to the first agent, preserves
            # liveness rather than hanging the conversation.
            return [{"agent_id": self.scenario.cast[0].id}]

        for block in response.content:
            if getattr(block, "type", None) == "tool_use" and block.name == "set_speakers":
                raw = block.input.get("speakers", []) or []
                # Validate: drop unknown agent_ids; cap length; dedupe consecutive.
                cleaned: List[dict] = []
                seen_last = None
                for s in raw[:DIRECTOR_MAX_SPEAKERS]:
                    aid = s.get("agent_id")
                    if aid in self._valid_ids and aid != seen_last:
                        entry = {"agent_id": aid}
                        if s.get("intent"):
                            entry["intent"] = str(s["intent"])[:300]
                        cleaned.append(entry)
                        seen_last = aid
                return cleaned
        return [{"agent_id": self.scenario.cast[0].id}]
