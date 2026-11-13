"""Bounded CloudWatch EMF on stdout. No SDK, network calls or participant data.

Playback samples finalize at the next speech end or after a 30-second hold.
Room records within that window can supply an earlier playback ACK. Runs are
study unless explicitly internal; sessions without a run are internal.
"""
from __future__ import annotations

import asyncio
import importlib
import inspect
import json
import math
import os
import sys
import threading
import time
import weakref
from functools import wraps
from pathlib import Path

NAMESPACE = "RelationalFluency"
DIMENSIONS = {
    "Purpose": frozenset(("preflight", "actor", "stt", "director", "steering", "tts", "other")),
    "Upstream": frozenset(("gateway", "elevenlabs", "other")),
    "Outcome": frozenset(("ok", "http_error", "timeout", "closed", "exception",
                          "cancelled", "connection", "audio", "speech", "no_speech", "failed",
                          "audio_already_sent", "later_commit", "cancel_unsupported", "withdrawn", "other")),
    "Cohort": frozenset(("study", "internal")),
    "ExitCause": frozenset(("auth_rejected", "setup_failed", "completed", "runner_returned",
                            "runner_error", "withdrawn", "client_disconnected", "cancelled", "other")),
    "CloseOutcome": frozenset(("sent_1000", "sent_1011", "sent_other", "none", "close_failed", "client_first", "other")),
    "Kind": frozenset(("participant", "probe", "replay", "unknown", "other")),
    "Reason": frozenset(("no_speech", "implausible_rate", "probe_pad", "not_english",
                         "low_transcriber_confidence", "replay_duplicate", "echo", "duplicate", "other")),
}
ROLLUPS = {"VoiceRouteEnded": (("Cohort", "ExitCause"), ("Cohort", "CloseOutcome"))}
UNITS = {
    **dict.fromkeys(("SpeechEndToFirstPlayed", "UpstreamLatency", "TtsQueueWait",
                     "LoopLagP99", "LoopLagMax"), "Milliseconds"),
    **dict.fromkeys(("FirstPlayedMissing", "ReplyMissing", "VoiceError",
                     "UpstreamRequests", "UpstreamErrors", "UpstreamCancelled", "RoomMemberDropped",
                     "RoomMemberLost", "ActiveSessions", "EncounterStarted",
                     "EncounterCompleted", "EncounterWithdrawn", "ArchiveFailed", "VoiceRouteEnded",
                     "SttResult", "UserTurnSuppressed", "SuppressedTurnReply", "Bargein"), "Count"),
    "EncounterDurationS": "Seconds",
}
# Keep these exclusions equal to tools/sim/analyze.py. Do not import sim code
# into the server image. Outcome is the bounded kind for VoiceError.
BENIGN_VOICE_ERRORS = ("extra response.create was refused", "already_has_active_response",
                       "response_cancel_not_active")
_lock = threading.RLock()
_cohorts: dict[str, str] = {}
_stores: weakref.WeakSet = weakref.WeakSet()
_upstream_module = None
_gauge_task: asyncio.Task | None = None
_sweep_task: asyncio.Task | None = None
# Same answer window as tools/sim/analyze.py; no sim dependency in the server.
ANSWER_GRACE_S = 30.0
SWEEP_INTERVAL_S = 1.0
UPSTREAM_ERRORS = frozenset(("http_error", "timeout", "exception", "closed"))


def _safe(fn):
    @wraps(fn)
    def guarded(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except (Exception, asyncio.CancelledError):
            return None
    return guarded


def cohort(value, *, has_run: bool = True) -> str:
    return "internal" if not has_run or value == "internal" else "study"


def _store_cohort(store) -> str:
    return cohort(store.cohort, has_run=bool(store.run_id))


def _number(value) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and value >= 0)


@_safe
def emit_many(values: dict, *, timestamp_ms: float | None = None, **dimensions) -> None:
    """Emit raw samples in one document, plus global/cohort rollups.

    Unknown dimension names are discarded. Unknown values map to other;
    invalid cohorts use internal. Metric names and units are fixed.
    """
    if os.getenv("METRICS_EMF", "1").strip() == "0":
        return
    values = {name: value for name, value in values.items()
              if name in UNITS and _number(value)}
    if not values or (timestamp_ms is not None and not _number(timestamp_ms)):
        return
    dims = {key: value if isinstance(value, str) and value in DIMENSIONS[key]
            else ("internal" if key == "Cohort" else "other")
            for key, value in dimensions.items() if key in DIMENSIONS}
    sets = [[]]
    for keys in (("Cohort",), ("Upstream",), tuple(sorted(dims))):
        keys = list(keys)
        if keys and all(key in dims for key in keys) and keys not in sets:
            sets.append(keys)
    for name in values:
        for keys in ROLLUPS.get(name, ()):
            if all(key in dims for key in keys) and list(keys) not in sets:
                sets.append(list(keys))
    from .build_info import build_sha
    from .llm import PIPELINE_VERSION
    record = {
        "_aws": {"Timestamp": int(time.time() * 1000 if timestamp_ms is None else timestamp_ms), "CloudWatchMetrics": [{
            "Namespace": NAMESPACE, "Dimensions": sets,
            "Metrics": [{"Name": name, "Unit": UNITS[name]} for name in values],
        }]},
        "Build": build_sha(), "PipelineVersion": PIPELINE_VERSION,
        **dims, **values,
    }
    with _lock:
        sys.stdout.write(json.dumps(record, allow_nan=False, separators=(",", ":")) + "\n")
        sys.stdout.flush()


@_safe
def emit(name: str, value: float = 1, *, timestamp_ms: float | None = None, **dimensions) -> None:
    emit_many({name: value}, timestamp_ms=timestamp_ms, **dimensions)


@_safe
def session_created(store) -> None:
    with _lock:
        if getattr(store, "_metrics_started", False):
            return
        store._metrics_started = True
        store._metrics_turns = {}
        store._metrics_finalized = set()
        store._metrics_speech_end = None
        _stores.add(store)
        _cohorts[store.id] = _store_cohort(store)
        emit("EncounterStarted", Cohort=_store_cohort(store))


def _voice_kind(fields) -> str | None:
    message = str(fields.get("message") or fields.get("detail") or "")
    if any(kind in message for kind in BENIGN_VOICE_ERRORS):
        return None
    text = message.lower()
    if "timeout" in text or "timed out" in text:
        return "timeout"
    if any(word in text for word in ("connect", "socket", "closed")):
        return "connection"
    if any(word in text for word in ("audio", "pcm", "sample rate")):
        return "audio"
    return "other"


@_safe
def session_event(store, type_: str, fields: dict) -> None:
    # Every event can expire an old hold; a shared sweep also handles silence.
    _flush_playback(store)
    dims = {"Cohort": _store_cohort(store)}
    counters = {"reply_missing": "ReplyMissing", "room_member_dropped": "RoomMemberDropped",
                "room_member_lost": "RoomMemberLost"}
    if type_ in counters:
        emit(counters[type_], **dims)
    elif type_ == "user_turn_suppressed":
        emit("UserTurnSuppressed", Reason=fields.get("reason"), **dims)
    elif type_ in ("echo_dropped", "user_transcript_duplicate_dropped"):
        emit("UserTurnSuppressed", Reason="echo" if type_ == "echo_dropped" else "duplicate", **dims)
    elif type_ == "suppressed_turn_reply_cancelled":
        emit("SuppressedTurnReply", Outcome="withdrawn", **dims)
    elif type_ == "reply_to_suppressed_turn":
        outcome = "other"
        if fields.get("kept") == "later_commit":
            outcome = "later_commit"
        elif fields.get("cancel") == "not_on_this_route":
            outcome = "cancel_unsupported"
        elif (not fields.get("kept") and not fields.get("cancel")
              and _number(fields.get("audio_ms")) and fields["audio_ms"] > 0):
            outcome = "audio_already_sent"
        emit("SuppressedTurnReply", Outcome=outcome, **dims)
    elif type_ == "voice_error":
        kind = _voice_kind(fields)
        if kind is not None:
            emit("VoiceError", Outcome=kind, **dims)
    elif type_ == "turn_timing":
        speech_end, played = fields.get("vad_speech_end"), fields.get("first_audio_played")
        if not _number(speech_end) or getattr(store, "_metrics_closed", False):
            return
        with _lock:
            if speech_end in getattr(store, "_metrics_finalized", ()):
                return
            if fields.get("play_end_interrupted") and played is None:
                return
            turns = getattr(store, "_metrics_turns", None)
            if turns is None:
                store._metrics_turns = turns = {}
            previous = turns.setdefault(speech_end, None)
            if _number(played) and played >= speech_end:
                turns[speech_end] = played if previous is None else min(previous, played)
            _stores.add(store)
            _flush_playback(store)


@_safe
def speech_end(store, at: float) -> None:
    """The real TurnTimer's next speech end is a finalization boundary."""
    if not _number(at) or getattr(store, "_metrics_closed", False):
        return
    with _lock:
        previous = getattr(store, "_metrics_speech_end", None)
        store._metrics_speech_end = at if previous is None else max(previous, at)
        _flush_playback(store)


@_safe
def _flush_playback(store, *, final: bool = False) -> None:
    with _lock:
        now = time.time()
        boundary = getattr(store, "_metrics_speech_end", None)
        turns = getattr(store, "_metrics_turns", {})
        for ended, played in list(turns.items()):
            if not (final or (boundary is not None and ended < boundary)
                    or now >= store.started_at + ended + ANSWER_GRACE_S):
                continue
            turns.pop(ended)
            if not hasattr(store, "_metrics_finalized"):
                store._metrics_finalized = set()
            store._metrics_finalized.add(ended)
            if played is None:
                emit("FirstPlayedMissing", Cohort=_store_cohort(store))
            else:
                emit("SpeechEndToFirstPlayed", (played - ended) * 1000,
                     timestamp_ms=(store.started_at + played) * 1000,
                     Cohort=_store_cohort(store))


@_safe
def sweep_playback() -> None:
    with _lock:
        for store in list(_stores):
            _flush_playback(store)


@_safe
def session_closed(store, duration_s: float) -> None:
    with _lock:
        if getattr(store, "_metrics_closed", False):
            return
        _flush_playback(store, final=True)
        store._metrics_closed = True
        store._metrics_turns = {}
        store._metrics_finalized = set()
        _stores.discard(store)
        _cohorts.pop(store.id, None)
        emit("EncounterDurationS", duration_s, Cohort=_store_cohort(store))


@_safe
def upstream_attempt(event: dict) -> None:
    """Listener for the frozen server.upstream_telemetry per-attempt contract."""
    with _lock:
        group = _cohorts.get(event.get("session_id"), "internal")
    dims = {"Purpose": event["purpose"], "Upstream": event["upstream"],
            "Outcome": event["outcome"], "Cohort": group}
    values = {"UpstreamCancelled": int(event["outcome"] == "cancelled")}
    # A cancelled slot wait sent no request. Keep only its queue/cancellation signals.
    if event.get("detail") != "slot_wait":
        values.update(UpstreamRequests=1,
                      UpstreamErrors=int(event["outcome"] in UPSTREAM_ERRORS),
                      UpstreamLatency=event["duration_ms"])
    if event["upstream"] == "elevenlabs" and event.get("queue_wait_ms") is not None:
        values["TtsQueueWait"] = event["queue_wait_ms"]
    emit_many(values, **dims)


@_safe
def install_upstream_listener() -> None:
    global _upstream_module
    module = importlib.import_module("server.upstream_telemetry")
    if module is not _upstream_module:
        module.add_listener(upstream_attempt)
        _upstream_module = module


@_safe
def loop_lag_report(summary: dict) -> None:
    emit("LoopLagP99", summary.get("p99_ms"))
    emit("LoopLagMax", summary.get("max_ms"))


@_safe
def archive_finished(session_dir: Path, result: dict) -> None:
    if result.get("ok"):
        return
    group = "internal"
    try:
        manifest = json.loads((Path(session_dir) / "manifest.json").read_text(encoding="utf-8"))
        group = cohort(manifest.get("cohort"), has_run=bool(manifest.get("run_id")))
    except Exception:
        pass
    emit("ArchiveFailed", Cohort=group)


@_safe
def active_sessions(registry) -> None:
    # Registry sessions, not room members. There is intentionally no TaskId
    # metric dimension; the dashboard's log query splits by @logStream.
    emit("ActiveSessions", len(registry.list_ids()))


async def _gauge_loop(registry) -> None:
    while True:
        active_sessions(registry)
        await asyncio.sleep(60)


async def _sweep_loop() -> None:
    while True:
        sweep_playback()
        await asyncio.sleep(SWEEP_INTERVAL_S)


async def start(registry) -> None:
    global _gauge_task, _sweep_task
    try:
        install_upstream_listener()
        if _gauge_task is None or _gauge_task.done():
            _gauge_task = asyncio.create_task(_gauge_loop(registry))
        if _sweep_task is None or _sweep_task.done():
            _sweep_task = asyncio.create_task(_sweep_loop())
    except Exception:
        pass


async def stop() -> None:
    global _gauge_task, _sweep_task
    tasks = (_gauge_task, _sweep_task)
    _gauge_task = _sweep_task = None
    for task in tasks:
        if task is not None:
            task.cancel()
    for task in tasks:
        if task is None:
            continue
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass


@_safe
def stt_result(session_id, outcome: str, kind: str) -> None:
    with _lock:
        group = _cohorts.get(session_id, "internal")
    emit("SttResult", Outcome=outcome, Kind=kind, Cohort=group)


@_safe
def tag_stt(socket, *, probe: bool, replay: bool) -> None:
    # Adapter-only metadata: never add a field to the upstream wire protocol.
    if hasattr(socket, "_metrics_hearing_kind"):
        socket._metrics_hearing_kind = "replay" if replay else "probe" if probe else "participant"


@_safe
def bargein(store) -> None:
    emit("Bargein", Cohort=_store_cohort(store))


class _VoiceRoute:
    def __init__(self):
        self.cause = "other"
        self.group = "internal"
        self.runner = None
        self.phase = "auth"
        self.close_outcome = "none"
        self.client_first = False

    @_safe
    def sent(self, code):
        self.close_outcome = "sent_1000" if code == 1000 else "sent_1011" if code == 1011 else "sent_other"

    @_safe
    def received(self):
        if self.close_outcome == "none":
            self.client_first = True

    @_safe
    def finish(self):
        cause = self.cause
        if cause == "runner_returned":
            if getattr(self.runner, "completed", False):
                cause = "completed"
            elif self.client_first or (self.close_outcome == "none"
                                       and getattr(self.runner, "client_disconnected", False)):
                cause = "client_disconnected"
        # Base completion is completed + none. After D2's close finalizer,
        # completed + sent_1000, runner_returned + sent_1011 and
        # runner_error + sent_1011 are expected.
        emit("VoiceRouteEnded", ExitCause=cause,
             CloseOutcome="client_first" if self.client_first else self.close_outcome,
             Cohort=self.group)


@_safe
def voice_route_fact(ws, *, cause=None, runner=None, context=None, cleanup=False):
    state = getattr(ws, "_metrics_voice_route", None)
    if state is None:
        return
    if cause is not None and state.cause != "withdrawn":
        state.cause = cause
    if runner is not None:
        state.runner, state.phase = runner, "runner"
        if state.cause != "withdrawn":
            state.cause = "runner_returned"
    if context is not None:
        state.group = cohort(context.get("cohort"), has_run=bool(context.get("run_id")))
        state.phase = "setup"
    if cleanup:
        state.phase = "cleanup"


def observe_voice_route(fn):
    @wraps(fn)
    async def observed(*args, **kwargs):
        ws = kwargs.get("ws") if "ws" in kwargs else args[0] if args else None
        try:
            state = _VoiceRoute()
        except BaseException:
            return await fn(*args, **kwargs)
        originals = {}

        @_safe
        def install():
            ws._metrics_voice_route = state
            # Observe ASGI disconnects even when WebSocket.receive rejects an
            # early disconnect before the initial websocket.connect frame.
            if callable(getattr(ws, "_receive", None)):
                original_raw_receive = ws._receive

                async def raw_receive():
                    message = await original_raw_receive()
                    if message.get("type") == "websocket.disconnect":
                        state.received()
                    return message
                originals["_receive"] = original_raw_receive
                ws._receive = raw_receive
            if callable(getattr(ws, "send", None)):
                original_send = ws.send

                async def send(message):
                    closing = message.get("type") == "websocket.close"
                    try:
                        result = await original_send(message)
                    except BaseException:
                        if closing:
                            state.close_outcome = "close_failed"
                        raise
                    if closing:
                        state.sent(message.get("code", 1000))
                    return result
                originals["send"] = original_send
                ws.send = send
            if callable(getattr(ws, "close", None)):
                original_close = ws.close

                async def close(*a, **k):
                    try:
                        result = await original_close(*a, **k)
                    except BaseException:
                        state.close_outcome = "close_failed"
                        raise
                    state.sent(k.get("code", a[0] if a else 1000))
                    return result
                originals["close"] = original_close
                ws.close = close
            if callable(getattr(ws, "receive", None)):
                original_receive = ws.receive

                async def receive(*a, **k):
                    try:
                        message = await original_receive(*a, **k)
                    except Exception as exc:
                        if type(exc).__name__ == "WebSocketDisconnect":
                            state.received()
                        raise
                    if message.get("type") == "websocket.disconnect":
                        state.received()
                    return message
                originals["receive"] = original_receive
                ws.receive = receive

        @_safe
        def restore():
            for name, method in originals.items():
                setattr(ws, name, method)
            if getattr(ws, "_metrics_voice_route", None) is state:
                del ws._metrics_voice_route

        install()
        try:
            return await fn(*args, **kwargs)
        except asyncio.CancelledError:
            if state.cause != "withdrawn":
                state.cause = "cancelled"
            raise
        except Exception:
            if state.phase == "auth" and state.cause == "setup_failed" and state.client_first:
                state.cause = "client_disconnected"
            elif state.phase == "cleanup" and state.cause == "runner_returned":
                state.cause = "other"
            raise
        finally:
            state.finish()
            restore()

    # Resolve postponed annotations in the route's own module for FastAPI.
    observed.__signature__ = inspect.signature(fn, eval_str=True)
    return observed
