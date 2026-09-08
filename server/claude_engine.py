"""Claude conversation engine.

Async, streaming, model swappable per session. Composes the system prompt
fresh each turn from scenario + persona + steering state, so live researcher
controls take effect on the next reply.
"""
from __future__ import annotations

from typing import AsyncIterator, List, Optional

from anthropic import AsyncAnthropic

from .llm import setting, text_client
from anthropic.types import MessageParam

from .persona import Persona
from .scenarios import Branch, Scenario, compose_system_prompt


DEFAULT_MODEL = setting("CLAUDE_MODEL", "nto.gemini-3.1-flash-lite")
# Voice replies should be short; cap output tokens so a runaway model doesn't
# block the speaker for 30 seconds.
DEFAULT_MAX_TOKENS = 400


class ConversationEngine:
    """Per-session conversation state and Claude streaming.

    Caller owns the scenario/persona/steering objects and mutates them
    between turns. The engine reads them fresh each turn so live changes apply.
    """

    def __init__(
        self,
        scenario: Scenario,
        persona: Persona,
        *,
        model: Optional[str] = None,
        client: Optional[AsyncAnthropic] = None,
    ):
        self.scenario = scenario
        self.persona = persona
        self.model = model or scenario.model or DEFAULT_MODEL
        self.client = client or text_client()
        self.history: List[MessageParam] = []
        self.live_notes: List[str] = []
        self.triggered_branches: List[Branch] = []

    def add_live_note(self, note: str) -> None:
        note = note.strip()
        if note:
            self.live_notes.append(note)

    def clear_live_notes(self) -> None:
        self.live_notes.clear()

    def trigger_branch(self, branch_id: str) -> Branch:
        for b in self.scenario.branches:
            if b.id == branch_id:
                self.triggered_branches.append(b)
                return b
        raise KeyError(f"No branch {branch_id} in scenario {self.scenario.id}")

    def set_model(self, model: str) -> None:
        self.model = model

    def _system_prompt(self) -> str:
        return compose_system_prompt(
            self.scenario,
            self.persona,
            self.live_notes,
            self.triggered_branches,
        )

    async def stream_reply(self, user_text: str) -> AsyncIterator[str]:
        """Append user turn, stream assistant reply text, append assistant turn."""
        self.history.append({"role": "user", "content": user_text})

        full = []
        try:
            async with self.client.messages.stream(
                model=self.model,
                max_tokens=DEFAULT_MAX_TOKENS,
                system=self._system_prompt(),
                messages=self.history,
            ) as stream:
                async for delta in stream.text_stream:
                    full.append(delta)
                    yield delta
        finally:
            # Append whatever text the participant already saw/heard, even if the
            # stream errored mid-way or the consumer abandoned the generator
            # (client disconnect, barge-in -> GeneratorExit at the yield). Dropping
            # it would leave the user turn unpaired and corrupt later context.
            if full:
                self.history.append(
                    {"role": "assistant", "content": "".join(full)}
                )

        # Branches that were "for your next turn only" are consumed.
        # We keep them in `triggered_branches` for logging, but a transient
        # branch (id starting with "once_") would be popped here if we add that.
