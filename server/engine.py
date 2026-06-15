"""Per-agent reasoning engine for multi-party scenarios.

Each AgentEngine owns one Agent in a scenario's cast + that agent's Persona.
It reads the shared multi-party history at turn time and builds its own
Claude messages view (own turns as 'assistant', everyone else's turns
collapsed into 'user' messages with [Speaker]: prefixes).

Single-agent scenarios use a 1-element cast — the same AgentEngine works,
with the shared log simply containing only user + this agent's turns.
"""
from __future__ import annotations

import os
from typing import AsyncIterator, Dict, List, Optional

from anthropic import AsyncAnthropic
from anthropic.types import MessageParam

from .persona import Persona
from .scenarios import Agent, Branch, Scenario


DEFAULT_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6")
DEFAULT_MAX_TOKENS = 130


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
        self.client = client or AsyncAnthropic()
        # Per-agent steering — the researcher can add notes targeting one agent
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
    ) -> str:
        parts: List[str] = []
        if self.scenario.scene:
            parts.append("## Scene")
            parts.append(self.scenario.scene)
            parts.append("")
        parts.append(f"You are **{self.agent.name}** in this conversation.")
        parts.append("")
        parts.append(self.agent.system_prompt)
        parts.append("")
        parts.append("## Tone and manner")
        parts.extend(f"- {f}" for f in self.persona.tone_fragments())
        incivility = self.persona.incivility_fragments()
        if incivility:
            parts.append("")
            parts.append("## Incivility behaviors (active — research dial)")
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
        if director_intent:
            parts.append("")
            parts.append("## Director note for this turn only")
            parts.append(f"- {director_intent}")
        parts.append("")
        if self.scenario.mode == "group":
            parts.append(
                "You are in a multi-party voice conversation. Keep it SHORT — usually "
                "one sentence, two at most. This is real speech, so be brief and to the "
                "point; do not give little speeches. Use plain, everyday language — no "
                "jargon, no buzzwords, no elaborate metaphors. Others may speak after "
                "you. Do not narrate, summarize, or restate what others said.\n"
                "Output ONLY the words you say out loud. Do NOT begin with your own name, "
                "initials, or a 'Name:' speaker label — your name is shown separately. "
                "No stage directions, no quotation marks around your line."
            )
        else:
            parts.append(
                "Speak as if in a real-time voice conversation. Keep replies natural-length "
                "for speech — not chat-text bullets. Do not narrate or describe what you are doing."
            )
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
            # No prior context — synthesize a kickoff prompt.
            messages = [{"role": "user", "content": "(meeting begins — please open)"}]

        async with self.client.messages.stream(
            model=self.model,
            max_tokens=DEFAULT_MAX_TOKENS,
            system=self._system_prompt(triggered_branches, director_intent),
            messages=messages,
        ) as stream:
            async for delta in stream.text_stream:
                yield delta
