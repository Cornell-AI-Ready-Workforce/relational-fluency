"""1:1 cascade reply-path observations, separate from playback/behaviour state.

Schema v1: one ``reply_trace`` per response id and one ``commit_trace`` per
commit item id. Events are written once at encounter teardown, after socket
close attempts, so an early play_end or a late transcription cannot discard stamps.
``stages`` values are seconds on ``encounter_s``: wall elapsed at attachment
plus monotonic elapsed thereafter. Wall clock adjustments after attachment do
not move this clock. Commit/participant/page identities are explicit, nullable
for openers or replies that never reached the page. No text or PCM is retained.
Event t/wall date teardown, not observations. Task-local call context carries
actual commit identities; no create inherits the latest socket commit.
Legacy turn_timing and cascade millisecond diagnostics remain independent.
"""
from __future__ import annotations

import asyncio
import time
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy


_call_context = ContextVar("reply_trace_call_context", default=None)


@contextmanager
def context(owner, origin):
    """Trace-only call provenance; never add it to a gateway frame.

    Nested calls keep their caller's origin. Task identity prevents a spawned
    task from inheriting a completed call's intent through ContextVar copying.
    """
    token = None
    try:
        trace = getattr(owner, "_reply_trace", None)
        if trace is not None:
            key = (trace, id(owner.ws), asyncio.current_task())
            previous = _call_context.get()
            if previous is None or previous[:3] != key:
                token = _call_context.set((*key, origin, {}))
    except Exception:  # noqa: BLE001 - telemetry is optional
        pass
    try:
        yield
    finally:
        if token is not None:
            try:
                _call_context.reset(token)
            except Exception:  # noqa: BLE001 - telemetry is optional
                pass


def observe(owner, method, *args, **kwargs):
    """Observation must never cost a frame, commit, cancellation or reply."""
    try:
        trace = getattr(owner, "_reply_trace", None)
        if trace is not None:
            return getattr(trace, method)(*args, **kwargs)
    except Exception:  # noqa: BLE001 - telemetry is optional
        pass
    return None


def attach(rt):
    """Carry the runner's clock onto a newly connected cascade socket."""
    try:
        from .voice.cascade import CascadeSocket
        if isinstance(rt.ws, CascadeSocket):
            rt.ws._reply_trace = getattr(rt, "_reply_trace", None)
    except Exception:  # noqa: BLE001 - telemetry is optional
        pass


def bind(runner, rt):
    """Attach before connect as well as at pump entry; 1:1 cascade only."""
    try:
        from .voice.cascade import CascadeSocket
        from .voice.realtime import family_of
        socket = getattr(rt, "ws", None)
        if (getattr(runner, "room", None) is not None
                or family_of(rt.model) != "cascade"
                or (socket is not None and not isinstance(socket, CascadeSocket))):
            return
        if getattr(runner, "_reply_trace", None) is None:
            runner._reply_trace = ReplyTrace(runner.session.store)
        rt._reply_trace = runner._reply_trace
        attach(rt)
    except Exception:  # noqa: BLE001 - telemetry is optional
        pass


class ReplyTrace:
    def __init__(self, store):
        self.store = store
        self.origin = time.monotonic() - (time.time() - store.started_at)
        self.replies = {}
        self.commits = {}
        self.participant_turn = 0
        self.participant = {}
        self._participant_id = None
        self.latest_commit = None
        self.flushed = False
        self.sockets = {}

    def now(self, mono=None):
        return round((time.monotonic() if mono is None else mono) - self.origin, 6)

    def stamp(self, rid, name, **fields):
        rec = self.replies.get(rid)
        if rec is not None:
            if name not in rec["stages"]:
                rec["stages"][name] = self.now()
                rec.update(fields)

    def vad(self, end):
        self.participant_turn += 1
        self.participant = {
            "vad_detected": self.now(end["detected_mono"]),
            "last_voiced_estimate": self.now(end["detected_mono"]) - end["trailing_silence_samples"] / end["sample_rate_hz"],
            "vad_sample_position": {k: v for k, v in end.items() if k != "detected_mono"},
        }
        self._participant_id = self.participant_turn

    def briefing(self, phase):
        if phase == "entered":
            self.participant.pop("briefing_completed", None)
        self.participant["briefing_" + phase] = self.now()

    def _call(self, socket_key):
        call = _call_context.get()
        if (call is not None and call[:2] == (self, socket_key)
                and call[2] is asyncio.current_task()):
            return call
        return None

    def commit_entered(self, socket_key):
        call = self._call(socket_key)
        if call is not None:
            call[4].pop("commit_item_id", None)

    def commit(self, iid, socket_key=None):
        socket = self.sockets.setdefault(socket_key, {"replies": set(), "commits": set()})
        socket["commits"].add(iid)
        self.latest_commit = iid
        call = self._call(socket_key)
        origin = call[3] if call is not None else "uncommitted"
        if call is not None:
            call[4]["commit_item_id"] = iid
        participant = origin == "participant_commit"
        self.commits[iid] = {"commit_item_id": iid, "origin": origin,
                             "participant_turn_id": self._participant_id if participant else None,
                             "stages": {"commit_dispatched": self.now()},
                             "participant": dict(self.participant) if participant else {},
                             "stt_outcome": None, "decision": None}
        self.participant = {}
        self._participant_id = None

    def stt(self, iid, phase, outcome=None):
        rec = self.commits.get(iid)
        if rec is not None and rec["stt_outcome"] != "socket_closed":
            rec["stages"].setdefault("stt_" + phase, self.now())
            if outcome is not None and rec["stt_outcome"] is None:
                rec["stt_outcome"] = outcome

    def decision(self, iid, outcome, reason=None):
        rec = self.commits.get(iid)
        if rec is not None and rec["decision"] is None:
            rec["stages"]["transcript_decided"] = self.now()
            rec["decision"] = {"outcome": outcome, "reason": reason}

    def create(self, rid, socket_key=None):
        socket = self.sockets.setdefault(socket_key, {"replies": set(), "commits": set()})
        socket["replies"].add(rid)
        call = self._call(socket_key)
        origin = call[3] if call is not None else "uncommitted"
        # The actual item id belongs to this call, never the latest socket commit.
        iid = (call[4].pop("commit_item_id", None)
               if call is not None and origin in ("participant_commit", "silence_probe", "replay") else None)
        self.replies[rid] = {"response_id": rid, "origin": origin,
                             "commit_item_ids": [iid] if iid else [],
                             "participant_turn_id": self.commits[iid]["participant_turn_id"] if iid else None,
                             "page_turn": None, "status": "creating",
                             "stages": {"create_entered": self.now()}}

    def page(self, rid, turn):
        if rid in self.replies and isinstance(turn, int) and self.replies[rid]["page_turn"] is None:
            self.replies[rid]["page_turn"] = turn

    def done(self, rid, status):
        rec = self.replies.get(rid)
        if rec is not None and "reply_done" not in rec["stages"]:
            self.stamp(rid, "reply_done")
            rec["status"] = status

    def socket_closed(self, socket_key=None):
        records = self.sockets.get(socket_key, {"replies": (), "commits": ()})
        for rid in records["replies"]:
            rec = self.replies.get(rid)
            if rec is not None and "reply_done" not in rec["stages"]:
                self.done(rid, "socket_closed")
        for iid in records["commits"]:
            rec = self.commits.get(iid)
            if rec is not None and rec["stt_outcome"] is None:
                rec["stt_outcome"] = "socket_closed"
                rec["stages"].setdefault("socket_closed", self.now())

    def flush(self):
        if self.flushed:
            return
        self.flushed = True
        for kind, records in (("reply_trace", self.replies), ("commit_trace", self.commits)):
            for rec in records.values():
                try:
                    self.store.event(kind, schema_version=1, clock="encounter_s",
                                     units="seconds", finalized_at="encounter_teardown", **deepcopy(rec))
                except Exception:  # noqa: BLE001 - one bad record cannot stop teardown
                    pass
