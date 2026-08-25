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
        # The scribe hears ONLY the participant. Member sessions cannot supply
        # the participant transcript: their input buffers also carry the other
        # characters' fanned-out audio, so the bridge's input transcription
        # mixes agent speech into what it labels the user. A dedicated session
        # that never hears an agent gives a clean participant channel.
        self.scribe: Optional[RealtimeVoiceSession] = None
        self.speaking: Optional[str] = None

    async def open(self) -> None:
        async def start(agent):
            rt = RealtimeVoiceSession(
                instructions=self._instructions_for(agent),
                voice=self._voice_for(agent),
                tools=self._tools,
            )
            await rt.connect(open_conversation=False)
            self.sessions[agent.id] = rt

        async def start_scribe():
            rt = RealtimeVoiceSession(
                instructions=(
                    "You are a silent transcription channel. Never speak. "
                    "If you must respond, reply with a single space."
                ),
                voice="Puck",
                tools=[],
            )
            await rt.connect(open_conversation=False)
            self.scribe = rt

        await asyncio.gather(start_scribe(), *(start(a) for a in self.agents))

    async def hear(self, pcm: bytes, *, exclude: Optional[str] = None) -> None:
        """Everyone in the room hears this audio.

        Participant audio (exclude=None) also reaches the scribe; agent audio
        (exclude=<speaker>) deliberately does not, keeping the scribe's input
        transcription a pure participant channel.
        """
        targets = [
            rt for aid, rt in self.sessions.items() if aid != exclude
        ]
        if exclude is None and self.scribe is not None:
            targets.append(self.scribe)
        await asyncio.gather(*(
            rt.send_audio(pcm) for rt in targets
        ), return_exceptions=True)

    async def give_floor(self, agent_id: str) -> Optional[RealtimeVoiceSession]:
        """Give one character the floor by committing their input buffer.

        On this bridge, committing IS the response trigger: any session whose
        buffer is committed replies on its own, and an explicit response.create
        is neither needed nor reliable. So turn-taking is: fan the audio to
        every session, but commit only the character who should speak. The
        others keep the turn in their (uncommitted) buffer and will hear the
        speaker's reply fanned in afterwards, so context stays shared.
        """
        rt = self.sessions.get(agent_id)
        if rt is None:
            return None
        self.speaking = agent_id
        try:
            # The bridge auto-fires a response on every session after speech +
            # silence, consuming its input buffer; cancelling that auto-fire
            # (the pump does) leaves the buffer EMPTY, and committing an empty
            # buffer kills the session on this bridge. Pad with 300 ms of
            # silence so the commit is always safe; the real audio is already
            # in the conversation history from the auto-commit.
            if rt.pending_input < 3200:
                await rt.send_audio(b"\x00" * 9600)
            await rt.commit_input()
            await rt.request_response()
        except Exception:  # noqa: BLE001, a dead session must not kill the turn
            self.sessions.pop(agent_id, None)
            return None
        return rt

    def session_for(self, agent_id: str) -> Optional[RealtimeVoiceSession]:
        return self.sessions.get(agent_id)

    async def close(self) -> None:
        closers = list(self.sessions.values())
        if self.scribe is not None:
            closers.append(self.scribe)
        await asyncio.gather(*(
            rt.close() for rt in closers
        ), return_exceptions=True)
        self.sessions.clear()
        self.scribe = None
