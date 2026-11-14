"""Bounded CloudWatch EMF on stdout. No SDK, network calls or participant data.

Playback samples finalize at the next speech end or after a 30-second hold.
Room records within that window can supply an earlier playback ACK. Runs are
study unless explicitly internal; sessions without a run are internal.
"""
from __future__ import annotations

import asyncio
import importlib
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
    "Purpose": frozenset(("preflight", "actor", "stt", "director", "steering", "tts")),
    "Upstream": frozenset(("gateway", "elevenlabs")),
    "Outcome": frozenset(("ok", "http_error", "timeout", "closed", "exception",
                          "cancelled", "connection", "audio", "other")),
    "Cohort": frozenset(("study", "internal")),
}
UNITS = {
    **dict.fromkeys(("SpeechEndToFirstPlayed", "UpstreamLatency", "TtsQueueWait",
                     "LoopLagP99", "LoopLagMax"), "Milliseconds"),
    **dict.fromkeys(("FirstPlayedMissing", "ReplyMissing", "VoiceError",
                     "UpstreamRequests", "UpstreamErrors", "UpstreamCancelled", "RoomMemberDropped",
                     "RoomMemberLost", "ActiveSessions", "EncounterStarted",
                     "EncounterCompleted", "EncounterWithdrawn", "ArchiveFailed"), "Count"),
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
        except Exception:  # instrumentation must never change encounter behaviour
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

    Unknown dimension names are discarded before serialization. Known names
    with unknown values reject the sample. Metric names and units are fixed.
    """
    if os.getenv("METRICS_EMF", "1").strip() == "0":
        return
    values = {name: value for name, value in values.items()
              if name in UNITS and _number(value)}
    if not values or (timestamp_ms is not None and not _number(timestamp_ms)):
        return
    dims = {key: value for key, value in dimensions.items() if key in DIMENSIONS}
    if any(value not in DIMENSIONS[key] for key, value in dims.items()):
        return
    sets = [[]]
    for keys in (("Cohort",), ("Upstream",), tuple(sorted(dims))):
        keys = list(keys)
        if keys and all(key in dims for key in keys) and keys not in sets:
            sets.append(keys)
    record = {
        "_aws": {"Timestamp": int(time.time() * 1000 if timestamp_ms is None else timestamp_ms), "CloudWatchMetrics": [{
            "Namespace": NAMESPACE, "Dimensions": sets,
            "Metrics": [{"Name": name, "Unit": UNITS[name]} for name in values],
        }]},
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
