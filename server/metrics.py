"""Bounded CloudWatch EMF on stdout. No SDK, network calls or participant data.

Playback samples are held until session close: a later room member's timing
record can contain an earlier playback ACK. Only explicit study cohorts are
study; internal, unattributed and standalone sessions use internal.
"""
from __future__ import annotations

import asyncio
import importlib
import json
import math
import os
import threading
import time
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
                     "UpstreamRequests", "UpstreamErrors", "RoomMemberDropped",
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
_upstream_module = None
_gauge_task: asyncio.Task | None = None


def _safe(fn):
    @wraps(fn)
    def guarded(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception:  # instrumentation must never change encounter behaviour
            return None
    return guarded


def cohort(value) -> str:
    return "study" if value == "study" else "internal"


def _number(value) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and value >= 0)


@_safe
def emit(name: str, value: float = 1, **dimensions) -> None:
    """Emit a raw sample, plus global/cohort rollups. Reject unbounded values.

    Unknown dimension names are discarded before serialization. Known names
    with unknown values reject the sample. Metric names and units are fixed.
    """
    if os.getenv("METRICS_EMF", "1").strip() == "0":
        return
    if name not in UNITS or not _number(value):
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
        "_aws": {"Timestamp": int(time.time() * 1000), "CloudWatchMetrics": [{
            "Namespace": NAMESPACE, "Dimensions": sets,
            "Metrics": [{"Name": name, "Unit": UNITS[name]}],
        }]},
        **dims, name: value,
    }
    with _lock:
        print(json.dumps(record, allow_nan=False, separators=(",", ":")), flush=True)


@_safe
def session_created(store) -> None:
    with _lock:
        if getattr(store, "_metrics_started", False):
            return
        store._metrics_started = True
        store._metrics_turns = {}
        _cohorts[store.id] = cohort(store.cohort)
        emit("EncounterStarted", Cohort=cohort(store.cohort))


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
    dims = {"Cohort": cohort(store.cohort)}
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
            turns = getattr(store, "_metrics_turns", None)
            if turns is None:
                store._metrics_turns = turns = {}
            previous = turns.setdefault(speech_end, None)
            if _number(played) and played >= speech_end:
                turns[speech_end] = played if previous is None else min(previous, played)


@_safe
def session_closed(store, duration_s: float) -> None:
    with _lock:
        if getattr(store, "_metrics_closed", False):
            return
        store._metrics_closed = True
        dims = {"Cohort": cohort(store.cohort)}
        for speech_end, played in getattr(store, "_metrics_turns", {}).items():
            if played is None:
                emit("FirstPlayedMissing", **dims)
            else:
                emit("SpeechEndToFirstPlayed", (played - speech_end) * 1000, **dims)
        store._metrics_turns = {}
        _cohorts.pop(store.id, None)
        emit("EncounterDurationS", duration_s, **dims)


@_safe
def upstream_attempt(event: dict) -> None:
    """Listener for the frozen server.upstream_telemetry per-attempt contract."""
    with _lock:
        group = _cohorts.get(event.get("session_id"), "internal")
    dims = {"Purpose": event["purpose"], "Upstream": event["upstream"],
            "Outcome": event["outcome"], "Cohort": group}
    emit("UpstreamRequests", **dims)
    emit("UpstreamErrors", int(event["outcome"] != "ok"), **dims)
    emit("UpstreamLatency", event["duration_ms"], **dims)
    if event["upstream"] == "elevenlabs" and event.get("queue_wait_ms") is not None:
        emit("TtsQueueWait", event["queue_wait_ms"], **dims)


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
        group = cohort(json.loads((Path(session_dir) / "manifest.json").read_text(
            encoding="utf-8")).get("cohort"))
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


async def start(registry) -> None:
    global _gauge_task
    try:
        install_upstream_listener()
        if _gauge_task is None or _gauge_task.done():
            _gauge_task = asyncio.create_task(_gauge_loop(registry))
    except Exception:
        pass


async def stop() -> None:
    global _gauge_task
    task, _gauge_task = _gauge_task, None
    if task is not None:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
