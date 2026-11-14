"""Commit order and delivery state for rooms with one text scribe.

The runner decides acceptance. This module never interprets a transcript.
Commit sequence and conversation sequence are separate: colleagues also add
conversation items, but only the participant makes scribe commits.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field


@dataclass
class HearingTurn:
    commits: list[int] = field(default_factory=list)
    granted: bool = False
    route_text: str = ""
    clarification: bool | None = None
    consumed: bool = False
    trigger_fired: dict | None = None


@dataclass
class HearingCommit:
    turn: HearingTurn
    event: dict | None = None
    outcome: str = "pending"
    text: str = ""
    route: bool = False
    seen: bool = False
    scribe: int | None = None


@dataclass
class MemberDelivery:
    cursor: int = 0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class RoomHearing:
    def __init__(self):
        self.commits: dict[int, HearingCommit] = {}
        self.released = 0
        self.pending_turn: HearingTurn | None = None
        self.active_turn: HearingTurn | None = None
        self.release_lock = asyncio.Lock()
        self.members: dict[object, MemberDelivery] = {}
        self.entries: list[dict] = []
        self.tasks: set[asyncio.Task] = set()
        self.closing = False
        self.on_error = None

    def commit(self, *, scribe=None) -> int:
        if self.pending_turn is None:
            self.pending_turn = HearingTurn()
        seq = len(self.commits) + 1
        self.commits[seq] = HearingCommit(self.pending_turn,
                                        scribe=id(scribe) if scribe is not None else None)
        self.pending_turn.commits.append(seq)
        return seq

    def text(self, turn: HearingTurn) -> str:
        return " ".join(self.commits[s].text for s in turn.commits
                        if self.commits[s].route)

    def accepted(self, turn: HearingTurn) -> bool:
        return any(self.commits[s].text for s in turn.commits)

    def missing(self, turn: HearingTurn) -> bool:
        # An older timed-out slot cannot make a later accepted turn blind.
        last = next((self.commits[s] for s in reversed(turn.commits)
                     if self.commits[s].outcome != "empty"), None)
        return last is None or last.outcome in ("pending", "missing", "failed")

    def expire_pending(self, *, upto=None, scribe=None) -> None:
        for seq, slot in self.commits.items():
            if (upto is None or seq <= upto) and (scribe is None or slot.scribe == id(scribe)):
                if slot.outcome == "pending" and slot.event is None:
                    slot.outcome = "missing"

    def merge_accepted(self, turn: HearingTurn) -> bool:
        """Consume newer ungranted turns whose words enter this request."""
        merged = False
        for seq, slot in self.commits.items():
            other = slot.turn
            if (other is turn or other.granted or other.consumed or not slot.text
                    or (turn.commits and seq <= max(turn.commits))):
                continue
            last_accepted = max(s for s in other.commits if self.commits[s].text)
            prefix = [s for s in other.commits if s <= last_accepted]
            tail = [s for s in other.commits if s > last_accepted]
            turn.commits.extend(prefix)
            turn.commits.sort()
            for member in prefix:
                self.commits[member].turn = turn
            other.commits = tail
            other.consumed = not tail
            if other.consumed and self.pending_turn is other:
                self.pending_turn = None
            merged = True
        return merged

    def delivery(self, rt) -> MemberDelivery:
        return self.members.setdefault(rt, MemberDelivery())

    def spawn(self, coro) -> None:
        task = asyncio.ensure_future(coro)
        self.tasks.add(task)
        task.add_done_callback(self._done)

    def _done(self, task: asyncio.Task) -> None:
        self.tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            if self.on_error is not None:
                self.on_error(exc)
            else:
                logging.getLogger(__name__).error(
                    "Room hearing task failed", exc_info=(type(exc), exc, exc.__traceback__))

    async def close(self) -> None:
        for task in list(self.tasks):
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.members.clear()
