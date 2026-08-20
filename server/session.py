"""Session orchestration — unified across single-agent and multi-agent scenarios.

A Session owns:
  - The Scenario (loaded from YAML)
  - One Persona + one AgentEngine per agent in the cast (1 for single, N for group)
  - A shared multi-party history (the source of truth for what's been said)
  - The list of triggered branches (additive, persists for the rest of the session)
  - A SessionStore (events.jsonl + audio WAVs + manifest + SQLite row)
  - Optional Director for group mode
  - Connected participant + researcher websockets
"""
from __future__ import annotations

import asyncio
import os
import secrets
import time
from typing import Dict, List, Optional, Set, TYPE_CHECKING

from anthropic import AsyncAnthropic

from .llm import text_client

from .director import Director
from .engine import AgentEngine, DEFAULT_MODEL
from .persona import Persona
from .scenarios import Branch, Scenario, load_scenario
from .steering import SteeringController, band_label
from .storage import SessionStore

if TYPE_CHECKING:
    from fastapi import WebSocket


def new_session_id() -> str:
    return f"s_{int(time.time())}_{secrets.token_hex(3)}"


# Auto steering is ON by default so participant-initiated sessions (landing
# page -> "Start a conversation") get adaptive personas without any researcher
# setup. Set AUTO_STEERING_DEFAULT=0 to start sessions with it off instead.
AUTO_STEERING_DEFAULT = os.getenv("AUTO_STEERING_DEFAULT", "1").lower() not in (
    "0", "false", "off",
)


class Session:
    def __init__(
        self,
        scenario_id: str,
        *,
        model: Optional[str] = None,
        participant_id: Optional[str] = None,
        capture_audio: bool = False,
    ):
        self.id = new_session_id()
        # The participant plays an assigned character; passing their key means
        # the same name is used in the brief, by the actors, and across all four
        # of their encounters.
        self.scenario: Scenario = load_scenario(scenario_id, participant_id or "")
        self.personas: Dict[str, Persona] = self.scenario.initial_personas()
        self.model: str = model or self.scenario.model or DEFAULT_MODEL

        client = text_client()
        self.engines: Dict[str, AgentEngine] = {
            a.id: AgentEngine(
                a, self.scenario, self.personas[a.id], client=client, model=self.model
            )
            for a in self.scenario.cast
        }
        self.director: Optional[Director] = (
            Director(self.scenario, client=client) if self.is_group else None
        )
        # Auto steering: on by default (see AUTO_STEERING_DEFAULT); researcher
        # can toggle it live. When on, the controller reviews each completed
        # turn and may shift persona gears, every shift logged with a reason.
        self.steering = SteeringController(self.scenario, client=client)
        self.auto_steering: bool = AUTO_STEERING_DEFAULT

        self.shared_history: List[dict] = []  # [{speaker: 'user'|agent_id, text, t}]
        self.triggered_branches: List[Branch] = []
        # Gear-switch history (presets, manual, auto) replayed to researchers
        # who connect after the switches happened.
        self.steering_log: List[dict] = []

        self.store = SessionStore(
            self.id,
            scenario=self.scenario.id,
            model=self.model,
            participant_id=participant_id,
            capture_audio=capture_audio,
            agent_ids=[a.id for a in self.scenario.cast],
        )

        self.participant_ws: Optional["WebSocket"] = None
        self.researcher_wss: Set["WebSocket"] = set()
        self.lock = asyncio.Lock()

        self.store.event(
            "session_start",
            scenario=self.scenario.id,
            mode=self.scenario.mode,
            model=self.model,
            participant_id=participant_id,
            capture_audio=capture_audio,
            cast=[{"id": a.id, "name": a.name} for a in self.scenario.cast],
            personas={aid: p.snapshot() for aid, p in self.personas.items()},
            auto_steering=self.auto_steering,
        )

    # --- shorthand ---

    @property
    def is_group(self) -> bool:
        return self.scenario.mode == "group"

    @property
    def primary_engine(self) -> AgentEngine:
        """Single-agent paths use this."""
        return self.engines[self.scenario.cast[0].id]

    @property
    def name_lookup(self) -> Dict[str, str]:
        return {a.id: a.name for a in self.scenario.cast}

    # Back-compat alias so legacy callers' `session.log.event(...)` still works.
    @property
    def log(self) -> SessionStore:
        return self.store

    # --- shared history helpers ---

    def append_user(self, text: str) -> None:
        self.shared_history.append({
            "speaker": "user",
            "text": text,
            "t": round(time.time() - self.store.started_at, 3),
        })

    def append_agent(self, agent_id: str, text: str) -> None:
        self.shared_history.append({
            "speaker": agent_id,
            "text": text,
            "t": round(time.time() - self.store.started_at, 3),
        })

    # --- snapshot for researcher view ---

    def snapshot(self) -> dict:
        return {
            "session_id": self.id,
            "scenario": {
                "id": self.scenario.id,
                "title": self.scenario.title,
                "mode": self.scenario.mode,
                "skill": self.scenario.skill,
                "branches": [
                    {"id": b.id, "label": b.label} for b in self.scenario.branches
                ],
            },
            "model": self.model,
            "auto_steering": self.auto_steering,
            "cast": [
                {"id": a.id, "name": a.name, "photo": a.photo}
                for a in self.scenario.cast
            ],
            "personas": {aid: p.snapshot() for aid, p in self.personas.items()},
            "live_notes": {
                aid: list(e.live_notes) for aid, e in self.engines.items()
            },
            "triggered_branches": [b.id for b in self.triggered_branches],
            "turn_count": sum(1 for h in self.shared_history if h["speaker"] == "user"),
        }

    # --- researcher controls (agent_id-aware) ---

    def _resolve_agent(self, agent_id: Optional[str]) -> str:
        if agent_id:
            if agent_id not in self.engines:
                raise KeyError(f"Unknown agent: {agent_id}")
            return agent_id
        # Single-agent default
        return self.scenario.cast[0].id

    async def set_knob(
        self,
        knob: str,
        value: float,
        agent_id: Optional[str] = None,
        *,
        auto: bool = False,
        reason: Optional[str] = None,
    ) -> None:
        aid = self._resolve_agent(agent_id)
        from_label = band_label(knob, getattr(self.personas[aid], knob))
        self.personas[aid].update(**{knob: value})
        to_label = band_label(knob, value)
        # Every gear switch, manual or auto, lands in events.jsonl with the
        # band transition (and the controller's reason when auto) so the
        # stimulus history is reconstructable.
        self.store.event(
            "knob_set",
            agent_id=aid,
            knob=knob,
            value=value,
            auto=auto,
            from_level=from_label,
            to_level=to_label,
            reason=reason,
        )
        payload = {
            "type": "steering",
            "auto": auto,
            "agent_id": aid,
            "agent_name": self.name_lookup.get(aid, aid),
            "knob": knob,
            "from_level": from_label,
            "to_level": to_label,
            "reason": reason,
        }
        self.steering_log.append(payload)
        await self.broadcast(payload)

    async def set_auto_steering(self, enabled: bool) -> None:
        self.auto_steering = bool(enabled)
        self.store.event("auto_steering_set", enabled=self.auto_steering)

    async def auto_steer(self) -> None:
        """Run one steering review and apply any gear shifts. Called by the
        session runners after each completed turn; a no-op unless the
        researcher has turned auto steering on. Never raises."""
        if not self.auto_steering:
            return
        try:
            adjustments = await self.steering.review(
                self.shared_history, self.personas, self.name_lookup
            )
        except Exception as e:
            self.store.event("auto_steer_error", message=str(e))
            return
        for adj in adjustments:
            if not self.auto_steering:
                break  # researcher flipped it off mid-review
            try:
                await self.set_knob(
                    adj["knob"], adj["value"], agent_id=adj["agent_id"],
                    auto=True, reason=adj["reason"],
                )
            except (KeyError, ValueError) as e:
                self.store.event("auto_steer_error", message=str(e))
        if adjustments:
            await self.broadcast({"type": "state", **self.snapshot()})

    async def add_note(self, note: str, agent_id: Optional[str] = None) -> None:
        aid = self._resolve_agent(agent_id)
        self.engines[aid].add_live_note(note)
        self.store.event("live_note", agent_id=aid, note=note)

    async def clear_notes(self, agent_id: Optional[str] = None) -> None:
        if agent_id:
            self.engines[agent_id].clear_live_notes()
            self.store.event("live_notes_cleared", agent_id=agent_id)
        else:
            for e in self.engines.values():
                e.clear_live_notes()
            self.store.event("live_notes_cleared")

    async def trigger_branch(self, branch_id: str) -> Branch:
        b = next((br for br in self.scenario.branches if br.id == branch_id), None)
        if b is None:
            raise KeyError(f"No branch {branch_id} in scenario {self.scenario.id}")
        self.triggered_branches.append(b)
        self.store.event("branch_triggered", branch=b.id, inject=b.inject)
        return b

    async def set_model(self, model: str) -> None:
        self.model = model
        for e in self.engines.values():
            e.set_model(model)
        self.store.event("model_set", model=model)

    # --- broadcasting to researcher subscribers ---

    async def broadcast(self, message: dict) -> None:
        dead = []
        for ws in self.researcher_wss:
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.researcher_wss.discard(ws)


class SessionRegistry:
    def __init__(self):
        self._sessions: Dict[str, Session] = {}

    def create(
        self,
        scenario_id: str,
        *,
        model: Optional[str] = None,
        participant_id: Optional[str] = None,
        capture_audio: bool = False,
    ) -> Session:
        s = Session(
            scenario_id,
            model=model,
            participant_id=participant_id,
            capture_audio=capture_audio,
        )
        self._sessions[s.id] = s
        return s

    def get(self, session_id: str) -> Session:
        s = self._sessions.get(session_id)
        if s is None:
            raise KeyError(f"Unknown session: {session_id}")
        return s

    def list_ids(self) -> List[str]:
        return list(self._sessions.keys())

    def drop(self, session_id: str) -> None:
        s = self._sessions.pop(session_id, None)
        if s:
            n_turns = sum(1 for h in s.shared_history if h["speaker"] == "user")
            s.store.event("session_end", n_turns=n_turns)
            s.store.close(n_turns=n_turns)


registry = SessionRegistry()
