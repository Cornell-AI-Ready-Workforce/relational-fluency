"""Session orchestration, unified across single-agent and multi-agent scenarios.

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
from typing import Any, Dict, List, Optional, Set, TYPE_CHECKING

from .llm import redact_key, text_client

from .director import Director
from .engine import AgentEngine, DEFAULT_MODEL
from .persona import KNOB_NAMES, Persona
from .scenarios import Branch, Scenario, load_scenario
from .steering import SteeringController, band_label
from .storage import SessionStore

if TYPE_CHECKING:
    from fastapi import WebSocket


def new_session_id() -> str:
    return f"s_{int(time.time())}_{secrets.token_hex(3)}"


def _realtime_model_name() -> str:
    """The realtime voice model this process opens sessions on, read at call
    time off the bridge module (the way the runner reads it), so a model
    resolved after import is still the one the manifest names. Empty when the
    bridge cannot be imported, which a text-only deployment is allowed to be."""
    try:
        from .voice import realtime as _realtime
    except Exception:  # noqa: BLE001 - a text encounter needs no bridge
        return ""
    return str(getattr(_realtime, "MODEL", "") or "")


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
        run_context: Optional[dict] = None,
    ):
        self.id = new_session_id()
        # Which run (and therefore which cohort) this encounter belongs to,
        # resolved server-side by the caller. Optional: sessions started outside
        # a run (landing page, researcher launch) have no run context, and the
        # encounter then records nulls rather than pretending to be study data.
        rc = run_context or {}
        self.run_id: Optional[str] = rc.get("run_id")
        self.cohort: Optional[str] = rc.get("cohort")
        self.participant_key: Optional[str] = rc.get("participant_key")
        self.encounter_index: Optional[int] = rc.get("encounter_index")
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
            # Hand the director this session's event writer. Routing degrades to
            # cast[0] whenever the gateway stalls or answers without a decision,
            # and unwired that degradation was visible only in the server log, so
            # an encounter whose director was dead throughout read in the record
            # exactly like a steered one. With the hook in place the fallback
            # lands in the encounter's own events.jsonl, where a later analysis
            # pass can find it. _store_event defers the lookup because the store
            # is built a few lines below this.
            Director(self.scenario, client=client, on_event=self._store_event)
            if self.is_group else None
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

        # What the record names as THE model. `self.model` is the text model
        # the engines, director and steering run on, and that is what the
        # manifest's `model` used to say for every encounter — including a
        # voice encounter, where the character the participant actually
        # talked to was played by REALTIME_MODEL and the text model never
        # produced a spoken word. A manifest reading "nto.gemini-2.5-pro" on
        # an encounter run on nto.gemini-live-2.5-flash misleads every later
        # reader (rater packet, dashboards, an analyst asking which family a
        # wave was collected on). A voice encounter is one that captures
        # audio (the voice socket is the only caller that asks for it), and
        # its manifest now names the realtime model; the text model stays
        # on session_start as `text_model`, and realtime_session_started
        # still records the voice model per encounter as before.
        self.realtime_model: str = _realtime_model_name() if capture_audio else ""
        self.store = SessionStore(
            self.id,
            scenario=self.scenario.id,
            model=self.realtime_model or self.model,
            participant_id=participant_id,
            capture_audio=capture_audio,
            agent_ids=[a.id for a in self.scenario.cast],
            run_id=self.run_id,
            cohort=self.cohort,
            participant_key=self.participant_key,
            encounter_index=self.encounter_index,
        )

        self.participant_ws: Optional["WebSocket"] = None
        self.researcher_wss: Set["WebSocket"] = set()
        self.lock = asyncio.Lock()
        # Set once the session is being torn down; guards store writes from
        # racing background tasks (auto_steer) after the store is closed.
        self._closed: bool = False
        # Outstanding auto_steer tasks, retained so they are not GC-cancelled
        # and can be cancelled on teardown.
        self._auto_steer_tasks: Set[asyncio.Task] = set()

        self.store.event(
            "session_start",
            scenario=self.scenario.id,
            mode=self.scenario.mode,
            model=self.model,
            # Both named, so nobody has to infer which one spoke. `model` is
            # the text model (what the engines, director and steering use);
            # `realtime_model` is what a voice encounter's characters are
            # played on, empty for a text encounter.
            text_model=self.model,
            realtime_model=self.realtime_model,
            participant_id=participant_id,
            capture_audio=capture_audio,
            # Study context on the first event too, so record.json (built from
            # events.jsonl alone) can inherit it without reading the manifest.
            # encounter_record.build() reads exactly these four keys off this
            # event; if you rename one, rename it there.
            run_id=self.run_id,
            cohort=self.cohort,
            participant_key=self.participant_key,
            encounter_index=self.encounter_index,
            # And the planted-trigger plan this encounter ran against, for the
            # same reason: the record has to carry its own denominator, because
            # the spec file it came from can be edited afterwards.
            spec_fingerprint=self.store.spec_fingerprint,
            cast=[{"id": a.id, "name": a.name} for a in self.scenario.cast],
            personas={aid: p.snapshot() for aid, p in self.personas.items()},
            auto_steering=self.auto_steering,
        )

    def _store_event(self, type_: str, **fields: Any) -> None:
        """Write an event on behalf of a collaborator built before the store.

        The director is constructed above the SessionStore (it needs the same
        text client), so it cannot be handed `self.store.event` directly. This
        forwards at call time instead. Silent when the store does not exist yet
        or the session is closing: recording a routing fallback must never be
        the thing that takes down a live encounter.
        """
        store = getattr(self, "store", None)
        if store is None or getattr(self, "_closed", False):
            return
        store.event(type_, **fields)

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
        delivered: Optional[bool] = None,
    ) -> None:
        """Move one persona gear and record the move.

        `delivered` says whether the caller has ALSO put this shift in front of
        the actor, and it is the difference between a stimulus history and a
        list of intentions. A gear only reaches a speech-to-speech actor through
        a re-brief, and no mode re-briefs on every shift:

        * True  — the caller re-briefs the actor as part of this same step.
        * False — the caller knows it does not, and the actor is still playing
          the persona it had. This is the group-room case (S3/S4, half the
          study): realtime_voice_session._steer cannot re-brief a room, because
          a mid-stream session.update mutes this bridge, so a shift made on a
          turn with no beat pending reaches nobody until the next planted beat
          briefs that member — and a shift made after the interaction's beats
          are spent never reaches anyone at all.
        * None  — undetermined here; read the shift together with the delivery
          events around it (`steer_deferred` / `steer_delivered` on the 1:1
          path, `stage_direction` in a room).

        It was absent, and every group shift was therefore written exactly like
        one that had landed. An analyst reading knob_set, auto=true had no way
        to tell a stimulus the participant actually met from one the record only
        intended — which is the difference between a manipulation check that
        means something and one that does not.
        """
        if self._closed:
            return
        # Validate the knob name before reading it off the persona: an unknown
        # name (or one that collides with a method) would otherwise raise
        # AttributeError/TypeError instead of the clean ValueError callers
        # expect, and crash the researcher websocket.
        if knob not in KNOB_NAMES:
            raise ValueError(f"Unknown knob: {knob}")
        aid = self._resolve_agent(agent_id)
        from_label = band_label(knob, getattr(self.personas[aid], knob))
        self.personas[aid].update(**{knob: value})
        to_label = band_label(knob, value)
        # Every gear switch, manual or auto, lands in events.jsonl with the
        # band transition (and the controller's reason when auto) so the
        # stimulus history is reconstructable — which it is only if the record
        # also says whether the actor was told, hence `delivered`.
        self.store.event(
            "knob_set",
            agent_id=aid,
            knob=knob,
            value=value,
            auto=auto,
            from_level=from_label,
            to_level=to_label,
            reason=reason,
            delivered=delivered,
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
            "delivered": delivered,
        }
        self.steering_log.append(payload)
        await self.broadcast(payload)

    async def set_auto_steering(self, enabled: bool) -> None:
        self.auto_steering = bool(enabled)
        self.store.event("auto_steering_set", enabled=self.auto_steering)

    def spawn_auto_steer(self) -> None:
        """Kick off one auto_steer review as a tracked background task.

        Retained in a set (and self-removing on completion) so the task is not
        garbage-collected mid-run, and so teardown can cancel it."""
        task = asyncio.create_task(self.auto_steer())
        self._auto_steer_tasks.add(task)
        task.add_done_callback(self._auto_steer_tasks.discard)

    def cancel_auto_steer(self) -> None:
        """Cancel any outstanding auto_steer tasks (called on teardown)."""
        for task in list(self._auto_steer_tasks):
            task.cancel()
        self._auto_steer_tasks.clear()

    async def auto_steer(self, *, delivered: Optional[bool] = None) -> None:
        """Run one steering review and apply any gear shifts. Called by the
        session runners after each completed turn; a no-op unless the
        researcher has turned auto steering on. Never raises.

        `delivered` is passed straight to set_knob for every shift this review
        makes; see set_knob for what the three values mean. The caller is the
        only thing that knows whether it is about to re-brief the actor, so the
        answer has to come in from there — this method cannot work it out."""
        if not self.auto_steering or self._closed:
            return
        try:
            adjustments = await self.steering.review(
                self.shared_history, self.personas, self.name_lookup
            )
        except Exception as e:
            if not self._closed:
                # str(e) here is whatever the gateway said, and anthropic's
                # AuthenticationError stringifies to the response body verbatim.
                # A gateway that quotes the credential it was sent would put a
                # live key into events.jsonl, which is archived per encounter
                # and shipped whole in the per-session download.zip - permanent
                # in a way a log line is not. The type and the wording survive
                # redaction; only key-shaped material does not.
                self.store.event("auto_steer_error", message=redact_key(str(e)))
            return
        for adj in adjustments:
            if not self.auto_steering or self._closed:
                break  # researcher flipped it off, or session torn down
            try:
                await self.set_knob(
                    adj["knob"], adj["value"], agent_id=adj["agent_id"],
                    auto=True, reason=adj["reason"], delivered=delivered,
                )
            except (KeyError, ValueError) as e:
                if not self._closed:
                    # Same sink, same treatment. This branch is local
                    # validation rather than gateway text, but the adjustment
                    # it is rejecting came from the model, so a key echoed into
                    # a knob or agent_id would land here and nowhere else.
                    self.store.event("auto_steer_error", message=redact_key(str(e)))
        if adjustments and not self._closed:
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

    # --- teardown ---

    async def close_participant_socket(self, code: int = 1000) -> bool:
        """Shut the participant's capture socket. Says whether one was open.

        THE HALF A WITHDRAWAL WAS MISSING. SessionRegistry.drop marks the
        session closed and closes the store, and that is where the teardown
        stopped: audio stopped being SAVED and did not stop being SENT. The
        participant's microphone socket stayed open, every frame on it was still
        read, still forwarded to the realtime gateway and still billed, until
        they closed the tab themselves. config/consent.yaml promises the person
        that pressing stop stops the recording, and a recording nobody keeps is
        still a recording being taken.

        Closing the socket is what actually ends it: the runner's
        _client_to_model is blocked on ws.receive(), the close frame turns that
        into a websocket.disconnect, run() is a FIRST_COMPLETED wait on it, and
        run()'s finally closes the gateway sessions. One frame here tears the
        whole chain down.

        Never raises, and clears the reference before closing so a second call —
        the handler's own finally, a second tab's withdrawal arriving behind the
        first — is a no-op rather than a RuntimeError. The withdrawal is already
        on disk by the time this runs, and a socket that has already gone must
        not turn a participant's stop into a 500.
        """
        ws = self.participant_ws
        if ws is None:
            return False
        self.participant_ws = None
        try:
            await ws.close(code=code)
            return True
        except Exception as e:  # noqa: BLE001, see the docstring
            print(f"  WARNING: could not close the capture socket of session "
                  f"{self.id}: {type(e).__name__}: {e}")
            return False

    # --- broadcasting to researcher subscribers ---

    async def broadcast(self, message: dict) -> None:
        dead = []
        # Snapshot the set: a researcher connecting/disconnecting during an
        # await would otherwise mutate it mid-iteration and raise RuntimeError,
        # which propagates into the participant turn and kills the session.
        for ws in list(self.researcher_wss):
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
        run_context: Optional[dict] = None,
    ) -> Session:
        s = Session(
            scenario_id,
            model=model,
            participant_id=participant_id,
            capture_audio=capture_audio,
            run_context=run_context,
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
            # Mark closed first so a racing auto_steer task no-ops its store
            # writes, then cancel outstanding tasks before closing the store.
            s._closed = True
            s.cancel_auto_steer()
            n_turns = sum(1 for h in s.shared_history if h["speaker"] == "user")
            s.store.event("session_end", n_turns=n_turns)
            s.store.close(n_turns=n_turns)


registry = SessionRegistry()
