"""Per-agent reasoning engine for multi-party scenarios.

Each AgentEngine owns one Agent in a scenario's cast + that agent's Persona.
It reads the shared multi-party history at turn time and builds its own
Claude messages view (own turns as 'assistant', everyone else's turns
collapsed into 'user' messages with [Speaker]: prefixes).

Single-agent scenarios use a 1-element cast, the same AgentEngine works,
with the shared log simply containing only user + this agent's turns.
"""
from __future__ import annotations

from typing import AsyncIterator, Dict, List, Optional

from anthropic import AsyncAnthropic

from .llm import setting, text_client
from anthropic.types import MessageParam

from .persona import Persona
from .scenarios import Agent, Branch, Scenario


# Resolved via the shared .env-first accessor so the model actually used matches
# the one recorded in provenance() (os.getenv would ignore the .env file).
DEFAULT_MODEL = setting("CLAUDE_MODEL", "nto.gemini-3.1-flash-lite")
# Headroom for natural-length single-mode (voice/text-chat) replies. The group
# one-sentence mode is constrained by the system prompt, not this cap.
DEFAULT_MAX_TOKENS = 400


# ── The one place the universal speech rules live ─────────────────────────────
#
# What is true of EVERY character in EVERY scenario is said here, once, and
# nowhere else in the assembly. It used to be said up to six times in a single
# prompt with a different number each time: twice here (one rule in each mode
# branch), again in the realtime session's VOICE block, again in the persona's
# verbosity fragment, and twice more from the scenario layer.
#
# Measured on the gateway, same S1A/Sam brief, same three participant turns:
# the full stack produced 27/37/26-word turns; the tight rule ALONE produced
# 11/24/25; no rule at all produced 29/51/53. Six rules bought what one rule
# would have bought, and the tightest of the six was violated on every
# substantive turn. The cost of the duplication was not clutter, it was the
# cap not being obeyed.
#
# Stated tight, and stated LAST: on nto.gemini-live-2.5-flash a mid-session
# session.update is never acknowledged — three frames sent, zero acked, probed
# on the real gateway — so the opening prompt is the whole of the instruction
# and position in it is the only emphasis available.
#
# Deliberately a SUBSET of what the scenario briefs still say ("one to three
# sentences per turn"), not a rival count: a turn of one or two sentences
# satisfies both, so the assembly does not fight briefs it does not own while
# the tighter bound is the one stated last.
SPEECH_RULES = (
    "You are speaking out loud, not writing. Keep every turn short: one or two "
    "sentences, about twenty-five words. Make one point, then stop and let the "
    "other person talk. Use plain, everyday words, no jargon and no elaborate "
    "metaphors. Say only the words you speak: no name or speaker label in front "
    "of them, no quotation marks around them, and nothing that describes what "
    "you are doing or thinking. Everyone here is speaking English."
)

# Group mode adds only who-holds-the-floor. It says nothing about how long a
# turn is — that is SPEECH_RULES's job in both modes, and a second length rule
# here is exactly what the measurement above cost.
#
# The floor rules read like clutter and are not: without "let them answer" a
# four-person room answers every participant turn four times over, and without
# "do not repeat what someone else just said" the quiet character's one fact
# gets restated by a louder one before the participant can retrieve it.
MEETING_RULES = (
    "Several people are in this room and others will speak after you. Do not "
    "repeat, rephrase, or summarise what someone else has just said. Do not "
    "answer every turn: when someone else is addressed by name, stay quiet and "
    "let them answer, and leave room for the people who have said less."
)


class AgentEngine:
    """One agent's view of an ongoing multi-party conversation."""

    def __init__(
        self,
        agent: Agent,
        scenario: Scenario,
        persona: Persona,
        *,
        client: Optional[AsyncAnthropic] = None,
        model: Optional[str] = None,
    ):
        self.agent = agent
        self.scenario = scenario
        self.persona = persona
        self.model = model or scenario.model or DEFAULT_MODEL
        self.client = client or text_client()
        # Per-agent steering, the researcher can add notes targeting one agent
        # specifically, separate from global scenario-level state.
        self.live_notes: List[str] = []

    # --- Steering ---

    def add_live_note(self, note: str) -> None:
        note = note.strip()
        if note:
            self.live_notes.append(note)

    def clear_live_notes(self) -> None:
        self.live_notes.clear()

    def set_model(self, model: str) -> None:
        self.model = model

    # --- System prompt composition ---

    def _system_prompt(
        self,
        triggered_branches: List[Branch],
        director_intent: Optional[str] = None,
        *,
        group: Optional[bool] = None,
    ) -> str:
        # The layers, in the order they are laid down, each one said ONCE:
        #   1. the scene            — the situation, from the scenario
        #   2. the character brief  — who this person is, from the scenario file
        #   3. the persona knobs    — how this person comes across, when a knob
        #                             is off its neutral setting (see persona.py)
        #   4. what has changed     — branches and the researcher's live notes
        #   5. how anyone speaks    — SPEECH_RULES, the universal block
        #   6. this moment only     — the director note, last, so it wins
        # Nothing belongs in two of them. A rule that is true of every
        # character goes in 5; a rule that is true of THIS character goes in
        # the scenario file, which this module does not own and does not fight.
        parts: List[str] = []
        if self.scenario.scene:
            parts.append("## Scene")
            parts.append(self.scenario.scene)
            parts.append("")
        parts.append(f"You are **{self.agent.name}** in this conversation.")
        parts.append("")
        parts.append(self.agent.system_prompt)
        tone = self.persona.tone_fragments()
        if tone:
            # Emitted only when a knob is actually off neutral. The heading used
            # to stand over five neutral sentences in every prompt ever built.
            parts.append("")
            parts.append("## Tone and manner")
            parts.extend(f"- {f}" for f in tone)
        incivility = self.persona.incivility_fragments()
        if incivility:
            parts.append("")
            # NOT "Incivility behaviors (active, research dial)". That heading
            # told the actor, in its own brief, that it was an experimental
            # manipulation — and it rendered only when a knob was up, i.e. only
            # in the incivility arm, the one arm where an actor stepping outside
            # the fiction costs the most.
            parts.append("## How you come across in this conversation")
            parts.extend(f"- {f}" for f in incivility)
        if triggered_branches:
            parts.append("")
            parts.append("## Situational updates")
            for b in triggered_branches:
                parts.append(f"- {b.inject}")
        if self.live_notes:
            parts.append("")
            parts.append("## Live direction from the researcher")
            for note in self.live_notes:
                parts.append(f"- {note}")
        parts.append("")
        # Group vs. single guidance must match the interaction actually in
        # progress, not the whole-scenario flag: a scenario is stamped
        # mode="group" if ANY interaction is group, so a one-to-one segment of a
        # mixed scenario (e.g. S3's 1:1 series) would otherwise wrongly be told
        # "others may speak after you". The runner passes the current
        # interaction's mode via `group`; fall back to scenario.mode only when it
        # is not supplied (e.g. the text-chat path).
        is_group_mode = (self.scenario.mode == "group") if group is None else group
        parts.append(SPEECH_RULES)
        if is_group_mode:
            parts.append(MEETING_RULES)
        if director_intent:
            # Last, deliberately. A note about THIS moment has to arrive late
            # enough to win against the standing rules above it, and on the
            # configured realtime model the opening prompt is the only place it
            # can arrive at all. The voice path appends its own note after this
            # whole string for the same reason; see
            # realtime_voice_session._director_note.
            parts.append("")
            parts.append(f"RIGHT NOW: {director_intent}")
        return "\n".join(parts)

    # --- Message-view construction ---

    def _agent_name_for(self, speaker_id: str, name_lookup: Dict[str, str]) -> str:
        if speaker_id == "user":
            return "User"
        return name_lookup.get(speaker_id, speaker_id)

    def _filter_history_by_attention(self, shared_history: List[dict]) -> List[dict]:
        """Slice the shared history based on this agent's attention level.

        High attention (>= 0.95): full history.
        Otherwise: take a recency window (depth grows with attention) and
        fold in any older turns where this agent spoke or was named.
        """
        a = self.persona.attention
        n = len(shared_history)
        if a >= 0.95 or n <= 3:
            return shared_history

        if a >= 0.7:
            depth = max(10, int(n * 0.7))
        elif a >= 0.4:
            depth = max(5, int(n * 0.4))
        elif a >= 0.2:
            depth = max(3, int(n * 0.25))
        else:
            depth = 2

        recent = shared_history[-depth:]
        older = shared_history[:-depth]
        if not older:
            return recent

        # Self-relevant older turns: this agent spoke, or this agent's name
        # appears in the text (case-insensitive). Preserves continuity for
        # things directed at the agent earlier in the conversation.
        name_lower = (self.agent.name or "").lower()
        self_relevant = [
            e for e in older
            if e["speaker"] == self.agent.id
               or (name_lower and name_lower in (e.get("text", "") or "").lower())
        ]
        return self_relevant + recent

    def _build_messages(
        self,
        shared_history: List[dict],
        name_lookup: Dict[str, str],
    ) -> List[MessageParam]:
        """Convert shared multi-party log into this agent's user/assistant alternation.

        Anthropic requires strict user/assistant alternation. Consecutive non-self
        turns are grouped into a single 'user' message with [Speaker]: prefixes.
        """
        filtered = self._filter_history_by_attention(shared_history)
        out: List[MessageParam] = []
        buffer: List[str] = []

        def flush_buffer():
            if buffer:
                out.append({"role": "user", "content": "\n\n".join(buffer)})
                buffer.clear()

        for entry in filtered:
            speaker = entry["speaker"]
            text = entry["text"]
            if not text:
                continue
            if speaker == self.agent.id:
                # Attention filtering can drop the turns that sat between two of
                # this agent's own turns, leaving them adjacent. Merging them into
                # one assistant message preserves the strict user/assistant
                # alternation Anthropic requires (back-to-back assistant messages
                # otherwise 400 on backends that enforce it). Only merge when no
                # other-speaker text is buffered between them.
                if not buffer and out and out[-1]["role"] == "assistant":
                    out[-1] = {
                        "role": "assistant",
                        "content": out[-1]["content"] + "\n\n" + text,
                    }
                else:
                    flush_buffer()
                    out.append({"role": "assistant", "content": text})
            else:
                label = self._agent_name_for(speaker, name_lookup)
                buffer.append(f"[{label}]: {text}")
        flush_buffer()
        # Anthropic requires the conversation to start with a user message.
        if out and out[0]["role"] == "assistant":
            out.insert(0, {"role": "user", "content": "(meeting begins)"})
        return out

    # --- Streaming reply ---

    async def stream_reply(
        self,
        shared_history: List[dict],
        triggered_branches: List[Branch],
        name_lookup: Dict[str, str],
        director_intent: Optional[str] = None,
    ) -> AsyncIterator[str]:
        """Stream a reply token by token. Caller appends the final text to the
        shared history when done."""
        messages = self._build_messages(shared_history, name_lookup)
        if not messages:
            # No prior context, synthesize a kickoff prompt.
            messages = [{"role": "user", "content": "(meeting begins, please open)"}]

        async with self.client.messages.stream(
            model=self.model,
            max_tokens=DEFAULT_MAX_TOKENS,
            system=self._system_prompt(triggered_branches, director_intent),
            messages=messages,
        ) as stream:
            async for delta in stream.text_stream:
                yield delta
