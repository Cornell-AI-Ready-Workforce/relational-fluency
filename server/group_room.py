"""A group interaction: one realtime session per character.

A single session cannot run a room. Through the LiteLLM bridge a conversation
yields exactly one reply per committed turn, so only the first routed speaker
ever answers, and re-briefing one session mid-conversation does not change who
the model thinks it is. The visible symptom is that whoever spoke first answers
everything: address Priya by name and Dan replies on her behalf.

So each character gets its own session, permanently briefed as that character.
Participant audio is fanned out to all of them, so everyone hears the room. When
the director picks a speaker, only that character's session is asked to reply,
and its audio is fed back into the others' input buffers so they hear what was
said. That is what makes calling on someone by name work.
"""

from __future__ import annotations

import asyncio
from typing import Callable, Dict, List, Optional

from .voice.realtime import RealtimeVoiceSession


class GroupRoom:
    """Holds one live session per character and decides who speaks."""

    def __init__(self, agents: List, instructions_for: Callable[[object], str],
                 voice_for: Callable[[object], str], tools: Optional[list] = None):
        self.agents = list(agents)
        self._instructions_for = instructions_for
        self._voice_for = voice_for
        self._tools = tools or []
        self.sessions: Dict[str, RealtimeVoiceSession] = {}
        self.speaking: Optional[str] = None

    async def open(self) -> None:
        async def start(agent):
            rt = RealtimeVoiceSession(
                instructions=self._instructions_for(agent),
                voice=self._voice_for(agent),
                tools=self._tools,
            )
            await rt.connect()
            self.sessions[agent.id] = rt

        await asyncio.gather(*(start(a) for a in self.agents))

    async def hear(self, pcm: bytes, *, exclude: Optional[str] = None) -> None:
        """Everyone in the room hears this audio."""
        await asyncio.gather(*(
            rt.send_audio(pcm)
            for aid, rt in self.sessions.items() if aid != exclude
        ), return_exceptions=True)

    async def commit_all(self) -> None:
        """Close the participant's turn in every character's session, so each
        has heard it before any of them is asked to respond."""
        await asyncio.gather(*(
            rt.commit_input() for rt in self.sessions.values()
        ), return_exceptions=True)

    async def ask(self, agent_id: str) -> Optional[RealtimeVoiceSession]:
        """Give one character the floor."""
        rt = self.sessions.get(agent_id)
        if rt is None:
            return None
        self.speaking = agent_id
        await rt.request_response()
        return rt

    def session_for(self, agent_id: str) -> Optional[RealtimeVoiceSession]:
        return self.sessions.get(agent_id)

    async def close(self) -> None:
        await asyncio.gather(*(
            rt.close() for rt in self.sessions.values()
        ), return_exceptions=True)
        self.sessions.clear()
