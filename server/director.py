"""Director routing for multi-agent group scenarios.

After each user turn, the director decides which agent(s) should respond and
in what order, plus an optional one-line intent for each. Uses Claude with
tool-use to force structured JSON output, and a fast/cheap model since this
runs on the critical path of every turn.

Because it IS the critical path, every gateway call here is bounded (see
DIRECTOR_TIMEOUT_S) and every failure degrades to a fallback speaker rather
than propagating. That fallback is always tagged and logged: for a measurement
instrument, a fallback that looks like a routing decision corrupts the record.

WHERE THE `intent` ACTUALLY GOES, TODAY
---------------------------------------
Worth saying plainly, because the rest of this module implies more than is
true. The per-speaker `intent` composed here has exactly one consumer in the
voice path, `_speak_as` in server/realtime_voice_session.py, and nothing calls
`_speak_as`. The live group path (`_run_group_turn`) reads `agent_id` out of
each routed entry and drops every other key, and the `director_route` event it
writes records the speaker list and nothing else. So as shipped, a direction is
composed, validated, truncated - and then discarded unread.

Two things follow, and both are in this file.

First, `route()` records its own decision through the session-store hook (see
`_record_decision`), so the direction reaches events.jsonl even while no runner
delivers it. That is the difference between an encounter where an analyst can
ask "what was this character being asked to do when they said that" and one
where the answer does not exist anywhere.

Second, the directions are written to be playable anyway, because the day a
runner does deliver them the register is what decides whether they help. On
gpt-realtime a mid-session session.update is acked in ~38 ms and obeyed on the
very next reply; on nto.gemini-live-2.5-flash - the model this study is
configured for - it is inert, three frames sent and zero acknowledged, so a
direction would not reach the actor even if the runner sent it. None of that is
a reason to write labels instead of directions: a note an actor could act on
costs exactly what a note they could not act on costs.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, List, Optional

from anthropic import APITimeoutError, AsyncAnthropic

from .llm import redact_key, setting, text_client

from .scenarios import Agent, Scenario


log = logging.getLogger(__name__)


# Routing is high-frequency and benefits from speed > deliberation. Haiku is
# fine; can be overridden via env if you want to A/B against Sonnet. Resolved
# via the shared .env-first accessor so an override in .env actually takes
# effect (os.getenv would ignore the .env file).
DIRECTOR_MODEL = setting("DIRECTOR_MODEL", "nto.gemini-3.1-flash-lite")
DIRECTOR_MAX_SPEAKERS = 3

# Output budget for one routing decision. 160 was enough for three bare
# agent_ids and not much else, and running out is not a soft failure here: a
# truncated tool call arrives with no `set_speakers` block at all, which
# route() correctly refuses to read as a decision and degrades to cast[0] - the
# dominant character in every group spec. So the budget has to fit the worst
# honest answer, which is DIRECTOR_MAX_SPEAKERS speakers each carrying a
# twenty-word direction, plus the rationale and the JSON around them: call it
# 300. It stays small for the other reason too - this is generated while the
# floor lock is held and nobody in the room may speak until it returns, so
# tokens here are silence the participant is sitting through.
DIRECTOR_MAX_TOKENS = 300


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
    # This text is unchanged, and that is a finding rather than an oversight.
    # Three rewrites of it were measured against the shipped wording - twelve
    # live routing calls each on the configured director model, real S4A cast and
    # scene, six fixed transcripts, replayed afterwards through the runner's own
    # anti-dominance pick to see who would actually have spoken - and every one
    # of them made the ROOM worse:
    #
    #   * "One person answers most turns": Dan took 22 of 24 turns instead of 18,
    #     Chris none instead of 12, and Dan followed Dan on all 8 turns he had
    #     just finished. The list is not only who speaks - _run_group_turn reads
    #     it as an ordered preference and plays the NEXT name when the first is
    #     whoever just spoke, so rationing names removes the only alternative the
    #     room has to its loudest character.
    #   * "Name a second only when they would take the floor on their own
    #     account": Chris stayed at zero. Chris's own brief is "you agree in a
    #     way that does not quite endorse" and four or five words will do it -
    #     the bland second is not a footnote bolted onto somebody's answer, it is
    #     the entire character, and a rule against it writes him out of the room.
    #   * Adding "never somebody the room has written as sidelined" to keep the
    #     quiet one quiet did not keep her quiet: Priya was still named second on
    #     4 of the 8 calls the runner would not have short-circuited, and the
    #     same prompt started producing third-person notes ("attempt to voice HER
    #     experience") where the shipped wording produced none in twenty.
    #
    # The room's own description is already the better authority on how many
    # people answer, and the rules that DID hold up went into the system prompt
    # instead, where the failures actually were. Do not re-ration the names here
    # without replaying the result through the runner's pick; the counts above
    # are what that costs.
    "description": (
        "Decide who speaks next, in what order, and what each of them is going "
        "for. An empty list is a real answer: it means nobody speaks. "
        "How many speakers depends on how the room behaves, not on a default. "
        "A room may want one responder, or a short exchange between two people "
        "who are arguing with EACH OTHER ([A, B, A]). Return what the room "
        "wants."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            # First in the schema on purpose. Tool arguments are generated in
            # order, so a short reason written before the names gives the model
            # somewhere to notice that the participant asked Priya a question
            # before it picks Dan out of habit. Nothing downstream requires it;
            # it is thinking room that happens to be worth recording.
            "rationale": {
                "type": "string",
                "description": (
                    "Up to fifteen words on why these people, in this order. "
                    "Notes to yourself, not prose."
                ),
            },
            "speakers": {
                "type": "array",
                "maxItems": DIRECTOR_MAX_SPEAKERS,
                "items": {
                    "type": "object",
                    "properties": {
                        "agent_id": {"type": "string"},
                        # The register of this string is the whole point, and
                        # the old description taught the wrong one. It asked for
                        # labels ("concede the technical point") and for stage
                        # descriptions of the actor in the third person ("stay
                        # silent but visibly tense") - and this text is pasted
                        # verbatim into that character's own brief, under a
                        # heading telling them to follow it and never mention
                        # it. A brief that has just forbidden them to refer to
                        # themselves in the third person or read out stage
                        # directions then hands them one of each. Ask instead
                        # for the thing a director actually says to an actor
                        # stepping on, and the contradiction goes away.
                        "intent": {
                            "type": "string",
                            "description": (
                                "What this person is going for on this turn, "
                                "written TO them. Second person, at most about "
                                "twenty words: one concrete thing to do and why "
                                "it is in them to do it. Good: 'Press him on "
                                "the timeline, he keeps sliding off the date.' "
                                "'Let her finish this time; you have already "
                                "made your point.' 'Take the number and nothing "
                                "else.' "
                                "Not a label ('deflect', 'concede the technical "
                                "point'), not a description of them from "
                                "outside ('stays silent but visibly tense'), and "
                                "never about them in the third person - they are "
                                "reading it as themselves. "
                                "Never a line for them to say: a quoted line "
                                "comes back word for word and sounds written. "
                                "Never another character's words or actions - "
                                "they can only play themselves. "
                                "Never a new fact, decision or revelation; you "
                                "direct how they meet what was just said, not "
                                "what happens next in the scene. "
                                # Measured, same twelve calls: 'Try to voice the
                                # concern about last year's collapse', 'Explain
                                # the collapse of last year's rollout to the
                                # participant', 'Propose a single-team pilot',
                                # 'call on Chris to move the needle'. Every one
                                # of those is a scene's own turning point, or
                                # the other person's move, handed out by the
                                # director on a turn that had not earned it. The
                                # rule above already forbade the first kind and
                                # did not name the second at all.
                                "Never an instruction to bring somebody else in "
                                "- asking the quiet one what they think, handing "
                                "credit back, making room for whoever was talked "
                                "over. Those belong to the other person in the "
                                "room and to nobody here. "
                                "Leave it out when you have nothing specific. An "
                                "absent note is better than a generic one."
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


# How much of the room's history the director gets to see. Six turns sounded
# like a window and was not one: a group turn is a participant turn plus up to
# DIRECTOR_MAX_SPEAKERS replies, so six entries can be a single exchange, and
# the director was routing a four-person meeting from one question and its
# answers. Twelve covers two to three full exchanges, which is where "she asked
# Priya something two turns ago and Dan answered it" becomes visible at all.
# The cost is a few hundred tokens on a flash-lite call that is already waiting
# on a network round trip.
_TRANSCRIPT_TURNS = 12
# Per-entry ceiling, so one long answer cannot eat the window it is part of.
_TRANSCRIPT_CHARS = 400
# Per-character ceiling on the sketch below.
_SKETCH_CHARS = 600


def _clip(text: str, limit: int) -> str:
    """Collapse whitespace and cut at a sentence end inside `limit`."""
    flat = " ".join(str(text or "").split())
    if len(flat) <= limit:
        return flat
    cut = flat[:limit]
    # Prefer the last sentence boundary; a sketch that stops mid-clause reads
    # as a different claim than the one the brief made.
    stop = max(cut.rfind(". "), cut.rfind("? "), cut.rfind("! "))
    if stop > limit // 2:
        return cut[: stop + 1]
    return cut.rsplit(" ", 1)[0] + "..."


def _format_transcript(shared_history: list, name_lookup: dict,
                       max_turns: int = _TRANSCRIPT_TURNS) -> str:
    """Render the most recent turns as a transcript string for the director.

    Each speaker carries their agent_id as well as their name, because the name
    is what the transcript says and the id is what the tool call has to return.
    Without it the director had to infer "Priya" -> "priya" from the cast block,
    and an id it gets wrong is dropped by the validator in route() - silently,
    and possibly down to an empty list, which the room plays as nobody speaking.

    `.get()` rather than `[...]`: a turn the transcriber never returned reaches
    here with an empty `text`, and one malformed entry raising KeyError out of
    here costs the participant the turn (the runner's handler records a
    director_error and rotates). An untranscribed turn is shown as such, because
    "Dan: " with nothing after it reads as Dan having said nothing.
    """
    recent = shared_history[-max_turns:]
    lines = []
    for entry in recent:
        speaker = entry.get("speaker", "")
        if speaker == "user":
            who = "Participant"
        else:
            name = name_lookup.get(speaker, speaker or "?")
            who = f"{name} ({speaker})" if speaker else name
        text = _clip(entry.get("text", ""), _TRANSCRIPT_CHARS)
        lines.append(f"{who}: {text}" if text else f"{who}: (not transcribed)")
    return "\n".join(lines) if lines else "(nobody has spoken yet)"


def _character_sketch(agent: Agent) -> str:
    """The opening of a character's own brief, as the director should read it.

    This used to be `system_prompt.split("\\n")[0][:200]`, on the theory that a
    brief opens with its strongest character signal. Every v3 brief opens with a
    markdown title, so what the director was actually handed, for all eight of
    them, was:

        - dan (Dan): # You are Dan
        - priya (Priya): # You are Priya
        - chris (Chris): # You are Chris

    A "## Cast" block that names three people it knows nothing about, costing
    tokens to say less than the ids already said. Everything the director knew
    about who these people were came from the scenario's own routing guidance,
    and that is a role line each ("dominates, relabels others' ideas").

    So: skip the heading, take the brief's own opening paragraphs up to a
    budget. For Priya that is the difference between "# You are Priya" and the
    fact that she is the only person in the room who knows why last year's
    rollout failed and that she stops herself mid-sentence - which is precisely
    what a director needs in order not to hand her the floor uninvited.
    """
    body = []
    for line in agent.system_prompt.strip().splitlines():
        stripped = line.strip()
        if not body and (not stripped or stripped.startswith("#")):
            continue  # the title, and the blank line under it
        body.append(line)
    return _clip("\n".join(body) or agent.system_prompt, _SKETCH_CHARS)


def _format_cast(cast: List[Agent]) -> str:
    return "\n".join(
        f"- {a.name} ({a.id}): {_character_sketch(a)}" for a in cast
    )


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
        # `detail` is the gateway's own words on every path that matters here,
        # and this method has three public sinks: the warning line below (boot
        # log, then CloudWatch at 90-day retention), the encounter's own
        # events.jsonl (archived per encounter and shipped whole in the
        # per-session download.zip), and the entry handed back to the runner to
        # record beside the speaker it played. A gateway that quotes the
        # credential it was sent - which is what a LiteLLM 401 body does - would
        # otherwise put a live key in all three at once, so it is scrubbed once,
        # here, rather than at each sink where the next one added would forget.
        detail = redact_key(detail)
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

    def _record_decision(self, speakers: List[dict], rationale: str) -> None:
        """Put the director's own decision in the encounter record.

        The runner writes a `director_route` event, and it writes the speaker
        list: [first] + followups, as ids. That is who spoke. What each of them
        was being asked to do is composed here, handed back in the routed
        entries, and then dropped on the floor - `_run_group_turn` reads
        `agent_id` and nothing else, and `_speak_as`, the one function that ever
        delivered an intent, has no callers. So the direction existed in exactly
        one place, a local variable, for the length of one turn.

        For a study whose record is meant to answer "what was this character
        being asked to do when they said that", that is the answer going
        missing. This costs one jsonl line per routing call and makes it
        answerable, without the runner having to change first.

        Deliberately a different event type from the runner's `director_route`:
        the two say different things (what was decided, versus who actually got
        the floor after the runner's own anti-dominance filter and the
        availability checks moved it), and collapsing them would let a decision
        that was never played read as one that was.
        """
        if self._on_event is None:
            return
        try:
            self._on_event(
                "director_decision",
                model=self.model,
                rationale=rationale or None,
                speakers=[s.get("agent_id") for s in speakers],
                # The direction verbatim, per speaker, because a paraphrase of a
                # stage direction is not evidence of what the actor was told.
                directions=[
                    {"agent_id": s.get("agent_id"), "intent": s.get("intent")}
                    for s in speakers if s.get("intent")
                ],
                # Named here rather than left for the reader to infer: on the
                # configured model nothing carries this to the actor, so an
                # analyst must not read a recorded direction as a performed one.
                delivered=False,
            )
        except Exception:  # noqa: BLE001 - recording must never break the room
            log.exception("director decision could not be recorded")

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
        # WHAT THIS PROMPT WAS MEASURED DOING, AND WHY IT READS THE WAY IT DOES
        #
        # Twelve live routing calls on the configured director model, real S4A
        # cast and scene, six fixed synthetic transcripts, two samples each, then
        # each decision replayed through _run_group_turn's own speaker pick to
        # see who would have actually spoken. The register was already fine - the
        # second-person direction had landed and every note came back playable,
        # none of the twenty in the third person. What came back wrong was SCOPE,
        # in three shapes, and only those three are answered here:
        #
        #   * Priya was directed to give up the one fact her brief exists to
        #     withhold, on three of the twelve ("Try to voice the concern about
        #     last year's collapse"). Her staying quiet unless somebody retrieves
        #     her is the control case of the whole teamwork measurement, and the
        #     director volunteering it removes the low end of the scale.
        #   * Dan was directed to "call on Chris to move the needle" - which is
        #     solicits_input and encourages_participation, two of the six ESCI
        #     items this scenario scores, performed by the room on the
        #     participant's behalf. There was no rule against that at all: the
        #     old text protected only the character written as sidelined, and
        #     Chris is not written as sidelined.
        #   * A blank `latest_user_text` drew the MOST confident routing of the
        #     six cases - a new subject, a decision pushed, two names. Nothing
        #     told it what to do with a turn it could not see, and on a family
        #     whose turn detection is switched off that blank is EVERY turn until
        #     the runner closes the participant's turn on the scribe (see
        #     GroupRoom.close_participant_turn). Rule 5 exists for that.
        #
        # The first two share a cause worth naming, because it is not in this
        # file. The scenario text that lands in "How this room behaves" is
        # compiled by _director_prompt in server/scenarios_v3.py and ends
        # "Planted triggers fire in order. Keep the scene moving toward the next
        # one", while this prompt says never to stage what has not happened yet.
        # The director was resolving that contradiction on every call, and losing
        # about half the time. Rule 2 and the second bottom rule now say which
        # wins; the clean fix is for that compiled line to stop asking.
        #
        # What was tried and REVERTED is recorded on the tool description above,
        # with the counts. The short version: every attempt to tell the director
        # how many people should answer made the room worse, because the speaker
        # list is also the only pool the runner's anti-dominance pick has to draw
        # from. The empty list is the same story from the other side - it is
        # still offered and still correct, but the old "often the right one"
        # push produced zero empty lists in twelve calls, and an empty list is
        # not silence in the runner anyway: _run_group_turn reads it as "no
        # signal at all" and rotates the floor to the next character in order,
        # with no direction attached. Nothing here leans on it until that
        # changes.
        system = f"""You are directing a live conversation between several people and one
other person who is really there, speaking out loud, in the room. You never
write dialogue. Each turn you decide who speaks next, in what order, and what
each of them is going for.

## Who is in the room
{self._cast_block}

## What the situation is
{self.scenario.scene or '(no situation given)'}

## How this room behaves
{self.scenario.director_prompt or 'Nothing specific. Route to whoever would really answer, given who these people are and what was just said.'}

## Choosing who speaks

1. If they named someone - "Marcus, what do you think?", "Theo, your read?" -
   that person answers, and only that person: one speaker, no second name on
   that turn. This beats everything below, including anything under "How this
   room behaves". Somebody who is asked a question directly and hears a
   colleague answer instead has been ignored, and that is not the moment this
   room is trying to create.

2. Otherwise, "How this room behaves" decides WHO. Read it as a description of
   these people, not as a problem to manage. If it says one of them dominates,
   let them dominate. If it says one of them is sidelined or holds back, let
   them stay back. If it asks for a short exchange where two of them go at each
   other ([a, b, a]), return that whole sequence rather than trimming it to one
   speaker. What it never decides is what any of them gets to reveal, settle or
   give away on this turn - see the two rules at the bottom, which it does not
   override.

3. Where it does not settle it, pick whoever a real person would answer: the
   one who was spoken to, the one who was contradicted, the one whose work was
   just named. If two of them would both plausibly answer and neither is
   written as holding back, take the one who has said less lately. A room where
   one voice answers everything stops being a room.

4. "Ok", "thanks", "mhm" - a transition with nothing in it. One short answer,
   or none. Real rooms have gaps, and something said into every single pause is
   what makes a conversation feel machined.

5. If what they just said is missing - blank, or not transcribed - you are
   routing blind. Route it anyway, but do not move the scene while you cannot
   see it: nothing new opened, nothing decided, nothing given away. They may
   have just said the thing this whole encounter turns on, and the recording
   will show the room talking straight past it.

## Two things that are not yours to do

- Do not do the job the person in the room came to do. Drawing back in whoever
  has been talked over, asking the one who has not spoken what they think,
  handing credit back to whoever earned it: those are their moves to make or
  fail to make, and which one they do is the entire reason this is being
  recorded. Somebody here written as reluctant does not find their voice on
  their own, and a room that goes and fetches the quiet one has answered the
  question for them. "Ask the one who has gone quiet what they think" is a
  direction to delete, not to shorten.
- Do not stage what happens next. The scene's own turning points are delivered
  elsewhere, on their own schedule, with their own wording. You never tell
  anyone to reveal something, decide something, concede something or announce
  something that has not already happened in the transcript. Where "How this
  room behaves" asks you to keep the scene moving toward what is coming, that
  means do not let it resolve early; it does not mean deliver it. The easiest
  way to get this wrong is to read the sketches above, find the thing one of
  them is holding back, and direct them to say it. That is on a schedule you
  cannot see, and a turning point played early, or played twice, is one the
  encounter can no longer use.

## Hard rules
- Use ONLY the agent_ids in brackets above. An id you invent is dropped, and a
  turn whose speakers are all dropped is a turn where nobody speaks at all.
- At most {DIRECTOR_MAX_SPEAKERS} speakers, and every one of them has to want
  the floor for their own reasons. First in the list speaks first.
- Everyone you name WILL speak out loud. So never name somebody in order to
  tell them to stay quiet, to listen, or to hold back: leaving them off the
  list is how you say that, and it is the only way that works. A person told to
  be silent still gets handed the floor and still fills it.
- Never the same person twice in a row.
"""
        user_msg = f"""## The last few turns
{transcript}

## What they just said
{latest_user_text or '(nothing yet - or it has not been transcribed)'}

Who speaks, and what are they going for?"""
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
                    max_tokens=DIRECTOR_MAX_TOKENS,
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
            # Redacted before the truncation, not after: a chatty 401 body can
            # put the credential either side of the 300-character cut, and
            # slicing first leaves a prefix of the key that no longer matches
            # any needle. _fallback redacts again on the way out, which is free
            # (the redactor is idempotent) and covers its other callers.
            return self._fallback(
                "director_timeout" if timed_out else "director_error",
                redact_key(f"{type(exc).__name__}: {detail}")[:300],
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
                # returned untagged - unlike the fallback below. Recorded all
                # the same: a director that chose silence and a director that
                # named three people nobody could find are the same empty list
                # from the runner's side, and only this line can tell them
                # apart.
                self._record_decision(
                    cleaned, str(block.input.get("rationale") or "")[:300]
                )
                return cleaned
        # No set_speakers block at all: a response truncated at
        # DIRECTOR_MAX_TOKENS, a model that ignored tool_choice, or a gateway
        # that answered with plain text. That is a director failure, not a
        # routing decision, so it takes the recorded fallback rather than
        # posing as one.
        return self._fallback(
            "director_no_decision",
            "response contained no set_speakers tool_use block",
        )
