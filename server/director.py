"""Director routing for multi-agent group scenarios.

After each user turn, the director decides which agent(s) should respond and
in what order, plus an optional one-line intent for each. Uses Claude with
tool-use to force structured JSON output, and a fast/cheap model since this
runs on the critical path of every turn.

Because it IS the critical path, every gateway call here is bounded (see
DIRECTOR_TIMEOUT_S) and every failure degrades to a fallback speaker rather
than propagating. That fallback is always tagged and logged: for a measurement
instrument, a fallback that looks like a routing decision corrupts the record.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, List, Optional

from anthropic import APITimeoutError, AsyncAnthropic

from .llm import setting, text_client

from .scenarios import Agent, Scenario


log = logging.getLogger(__name__)


# Routing is high-frequency and benefits from speed > deliberation. Haiku is
# fine; can be overridden via env if you want to A/B against Sonnet. Resolved
# via the shared .env-first accessor so an override in .env actually takes
# effect (os.getenv would ignore the .env file).
DIRECTOR_MODEL = setting("DIRECTOR_MODEL", "nto.gemini-3.1-flash-lite")
DIRECTOR_MAX_SPEAKERS = 3


def _timeout_setting() -> float:
    """Total seconds the director gets to answer one turn. Env-tunable."""
    raw = setting("DIRECTOR_TIMEOUT_S", "8")
    try:
        value = float(raw)
    except ValueError:
        value = 0.0
    return value if value > 0 else 8.0


# route() is awaited on the participant's critical path: in a group room the
# floor lock is held and nobody may speak until it returns, and the 45 s
# group-turn watchdog in the runner only starts AFTER it returns, so it cannot
# rescue a wedged call. The SDK's defaults are sized for batch work, not a live
# conversation: read timeout 600 s with two automatic retries, i.e. one hung
# gateway call can hold a recorded encounter silent for the better part of half
# an hour with nothing written to the record. A few seconds is the right budget
# here - a slightly wrong speaker is recoverable, a silent room is not.
DIRECTOR_TIMEOUT_S = _timeout_setting()

# DIRECTOR_TIMEOUT_S is the budget for the whole routing decision, so it has to
# cover a retry as well as the first attempt: half each, plus the SDK's ~0.5 s
# first back-off, comes to ~8.5 s at the default, still under the wait_for
# ceiling in route(). The retry is not optional politeness. On a shared gateway
# the routine failure is a fast 429 or 502, not a hang, and retrying one of
# those costs half a second and usually succeeds; without it every blip
# degrades to the cast[0] fallback, and cast[0] is the dominant character in
# every group spec, so a flaky afternoon would show up in the data as inflated
# dominance rather than as an outage. One retry and not two, because a second
# one does not fit in the budget and the budget is what protects the floor.
DIRECTOR_ATTEMPT_TIMEOUT_S = DIRECTOR_TIMEOUT_S / 2
DIRECTOR_MAX_RETRIES = 1


def _bounded(client: AsyncAnthropic) -> AsyncAnthropic:
    """Copy `client` with the director's request budget applied.

    with_options() returns a copy that re-uses the SAME underlying httpx
    connection pool, so this is cheap and, importantly, leaves the original
    alone: session.py hands the one text client to the actor engines and the
    steering controller too, and those calls are not under the floor lock.

    The timeout is a plain float on purpose. This SDK vendors its transport as
    `httpx2` and explicitly rejects an `httpx.Timeout` object built from the
    top-level `httpx` package (TypeError at construction, which would strand
    every group encounter at startup); a float is accepted and applies to all
    phases - connect, read, write, pool.

    Only a genuine AsyncAnthropic copy is accepted; anything else means the
    caller injected a double, which is handed back untouched. Trusting the
    return value blindly was wrong: MagicMock and friends auto-generate
    `with_options` and hand back a child mock instead of raising, so the except
    below never fired for the commonest kind of double and the Director ended up
    talking to an object the caller had never seen - every route() call then
    died on `await <MagicMock>` and returned the fallback speaker. A double
    loses nothing by being left alone: asyncio.wait_for in route() still bounds
    whatever comes back here.
    """
    try:
        copy = client.with_options(
            timeout=DIRECTOR_ATTEMPT_TIMEOUT_S, max_retries=DIRECTOR_MAX_RETRIES
        )
    except (AttributeError, TypeError):
        # An SDK that will not copy at all: carry on with the client as given.
        return client
    return copy if isinstance(copy, AsyncAnthropic) else client


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
        on_event: Optional[Callable[..., Any]] = None,
    ):
        self.scenario = scenario
        self.client = _bounded(client or text_client())
        self.model = model or DIRECTOR_MODEL
        # Optional session-store hook, same shape as SessionStore.event
        # (type_, **fields). Wired, a director outage lands in the encounter's
        # own record instead of only in the server log; unwired, routing still
        # works exactly as before. See _fallback() for why this matters.
        self._on_event = on_event
        self._cast_block = _format_cast(scenario.cast)
        self._valid_ids = {a.id for a in scenario.cast}
        self._name_lookup = {a.id: a.name for a in scenario.cast}

    def _fallback(self, reason: str, detail: str) -> List[dict]:
        """Liveness fallback to the first cast member, recorded AS a fallback.

        Falling back is right - the encounter must not stop because the gateway
        is unreachable - but returning cast[0] silently was worse than the
        outage: cast[0] is the dominant character in every group spec, so a dead
        or misconfigured director degraded into "the loud one answers every
        turn", exactly the failure the system prompt below warns against, and
        the anti-dominance filter in the runner cannot help when cast[0] is the
        only candidate. Worse for a measurement instrument, the steering trail
        then claimed a routing decision the director never made, and an
        encounter whose director was dead throughout looked identical in the
        record to a steered one. So: log it, hand it to the session store when
        one is wired, and tag the returned entry so a caller can record
        `fallback: true` next to the speaker it played.
        """
        agent_id = self.scenario.cast[0].id
        log.warning(
            "director %s, falling back to %s (model=%s): %s",
            reason, agent_id, self.model, detail,
        )
        if self._on_event is not None:
            try:
                self._on_event(
                    reason, agent_id=agent_id, model=self.model,
                    detail=detail, fallback=True,
                )
            except Exception:  # noqa: BLE001 - recording must never break the room
                log.exception("director fallback could not be recorded")
        return [{
            "agent_id": agent_id,
            "fallback": True,
            "reason": reason,
            "detail": detail,
        }]

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
            # Belt and braces on top of the client's own budget: wait_for bounds
            # the await itself, so a stall anywhere in the SDK (not just the
            # socket) still releases the floor. The ceiling covers both attempts
            # and the back-off between them, plus a second of slack so the
            # transport timeout normally wins and we get the specific error
            # rather than a bare TimeoutError. It is also the only thing bounding
            # a Retry-After header, which the SDK will honor up to 60 s.
            response = await asyncio.wait_for(
                self.client.messages.create(
                    model=self.model,
                    max_tokens=160,
                    system=system,
                    tools=[_DIRECTOR_TOOL],
                    tool_choice={"type": "tool", "name": "set_speakers"},
                    messages=[{"role": "user", "content": user_msg}],
                ),
                timeout=DIRECTOR_TIMEOUT_S + 1.0,
            )
        except Exception as exc:  # noqa: BLE001 - never hang the room on the director
            # Degrade rather than propagate: the participant is mid-encounter and
            # a raised exception would cost them the turn. _fallback() makes the
            # degradation visible instead of silent.
            timed_out = isinstance(exc, (asyncio.TimeoutError, APITimeoutError))
            # wait_for's own TimeoutError stringifies to "", so the naive
            # f-string put a bare "TimeoutError: " in the encounter record.
            # `reason` still says director_timeout, but `detail` is what a
            # reader looks at a month later, and it has to say something. Name
            # the budget that was actually exceeded, as the steering side does.
            detail = str(exc) or (
                f"exceeded its {DIRECTOR_TIMEOUT_S + 1.0:.1f}s budget"
                if timed_out else "no detail"
            )
            return self._fallback(
                "director_timeout" if timed_out else "director_error",
                f"{type(exc).__name__}: {detail}"[:300],
            )

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
                # An empty `cleaned` here is a genuine decision (silence, or a
                # turn routed only to agents no longer in the cast), so it is
                # returned untagged - unlike the fallback below.
                return cleaned
        # No set_speakers block at all: a truncated response (max_tokens=160), a
        # model that ignored tool_choice, or a gateway that answered with plain
        # text. That is a director failure, not a routing decision, so it takes
        # the recorded fallback rather than posing as one.
        return self._fallback(
            "director_no_decision",
            "response contained no set_speakers tool_use block",
        )
