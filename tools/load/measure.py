"""Measurements and the fixed load-test decision rules."""
from __future__ import annotations

from collections import Counter, defaultdict
import json
import math
from pathlib import Path
import re

from tools.sim import analyze


ERROR_OUTCOMES = {"http_error", "timeout", "closed", "exception"}
RULE = ("INVALID if achieved concurrency < 90% of N or generator loop-lag p99 > 50 ms, "
        "or required measurements/full encounters are missing. Otherwise FAIL if speech-end-to-first-played "
        "p90 > max(1.5 * N=1 p90, N=1 p90 + 2 s), reply_missing per 100 participant turns "
        "> N=1 + 1, server loop-lag p99 > 250 ms, RSS > 900 MB, or any non-benign "
        "voice_error kind was absent at N=1. Unknown fake routes/protocol errors fail the run. "
        "Capacity is the highest N with every repeat PASS: capacity for the tested configuration.")


def percentiles(values):
    return {"p50": analyze.percentile(values, 50), "p90": analyze.percentile(values, 90),
            "p99": analyze.percentile(values, 99), "n": len(values)}


def first_played(events_by_session):
    """One latency per (session, speech end), taking the earliest member ACK."""
    spoken = set()
    played = {}
    for sid, events in events_by_session.items():
        for event in events:
            if event.get("type") != "turn_timing":
                continue
            start, end = event.get("vad_speech_end"), event.get("first_audio_played")
            if not isinstance(start, (int, float)) or not math.isfinite(start):
                continue
            key = (sid, start)
            spoken.add(key)
            if isinstance(end, (int, float)) and math.isfinite(end) and end >= start:
                played[key] = min(end, played.get(key, end))
    return [played[key] - key[1] for key in sorted(played)], len(spoken - played.keys())


def voice_error_kind(event):
    message = str(event.get("message") or event.get("detail") or "")
    if any(kind in message for kind in analyze.REFUSED_CREATE + analyze.BENIGN_CANCEL):
        return None
    return str(event.get("kind") or event.get("where") or event.get("code") or "unknown")


def upstream_summary(attempts):
    grouped = defaultdict(list)
    for attempt in attempts:
        grouped[attempt["purpose"]].append(attempt)
    result = {}
    for purpose, rows in sorted(grouped.items()):
        durations = [r["duration_ms"] for r in rows if isinstance(r.get("duration_ms"), (int, float))]
        waits = [r["queue_wait_ms"] for r in rows if isinstance(r.get("queue_wait_ms"), (int, float))]
        result[purpose] = {
            "attempts": len(rows), "errors": sum(r.get("outcome") in ERROR_OUTCOMES for r in rows),
            "cancelled": sum(r.get("outcome") == "cancelled" for r in rows),
            "duration_ms": percentiles(durations),
            "queue_wait_ms": {**percentiles(waits), "max": max(waits, default=None)},
            "routes": dict(Counter(r.get("route") for r in rows)),
        }
    return result


def summarize(events_by_session, timelines, *, attempts, resources, concurrency, generator_lag_ms,
              server_lag, measurement_start, measurement_end, close_burst_s, artifact_bytes):
    latency, missing_ack = first_played(events_by_session)
    per_session = [analyze.summarize(events_by_session.get(t["session_id"], []), t) for t in timelines]
    events = [e for rows in events_by_session.values() for e in rows]
    turns = sum(s["participant_turns"] for s in per_session)
    missing = sum(e.get("type") == "reply_missing" for e in events)
    kinds = Counter(kind for event in events if event.get("type") == "voice_error"
                    if (kind := voice_error_kind(event)) is not None)
    steady = [s["active"] for s in concurrency if measurement_start <= s["t"] < measurement_end]
    cpu = [s["cpu_percent"] for s in resources if s.get("cpu_percent") is not None]
    rss = [s["rss_bytes"] for s in resources if s.get("rss_bytes") is not None]
    return {
        "speech_end_to_first_played_s": percentiles(latency), "latency_samples_s": latency,
        "first_played_missing": missing_ack,
        "lines_said": sum(s["lines_said"] for s in per_session),
        "lines_answered": sum(s["lines_answered"] for s in per_session),
        "lines_heard": sum(s["lines_heard"] for s in per_session),
        "participant_turns": turns, "reply_missing": missing,
        "reply_missing_per_100_turns": missing * 100 / turns if turns else None,
        "voice_error": sum(kinds.values()), "voice_error_kinds": dict(kinds),
        "upstream": upstream_summary(attempts),
        "generator_loop_lag_ms": {**percentiles(generator_lag_ms), "max": max(generator_lag_ms, default=None)},
        "achieved_concurrency": sum(steady) / len(steady) if steady else 0.0,
        "concurrency_samples": concurrency, "steady_sample_count": len(steady),
        "measurement_window": {"start": measurement_start, "end": measurement_end},
        "server_loop_lag_ms": {
            "p99": max((s["p99_ms"] for s in server_lag), default=None),
            "max": max((s["max_ms"] for s in server_lag), default=None),
            "windows": server_lag, "aggregation": "maximum window p99; not a percentile of percentiles"},
        "server_cpu_percent": {**percentiles(cpu), "max": max(cpu, default=None)},
        "server_rss_bytes": {**percentiles(rss), "max": max(rss, default=None)},
        "resource_samples": resources,
        "bytes_written_per_encounter": artifact_bytes,
        "bytes_written_definition": "sum of durable encounter file sizes, including the rendered mix; excludes write amplification",
        "websocket_disconnects": sum(t.get("websocket_disconnects", 0) for t in timelines),
        "unexpected_disconnects": sum(bool(t.get("unexpected_disconnect")) for t in timelines),
        "close_burst_s": close_burst_s,
        "encounters": per_session,
    }


def baseline(repeats):
    """Pool raw N=1 latencies; do not average percentile summaries."""
    metrics = [r["metrics"] for r in repeats]
    samples = [x for m in metrics for x in m["latency_samples_s"]]
    turns = sum(m["participant_turns"] for m in metrics)
    return {
        "p90_s": analyze.percentile(samples, 90), "sample_count": len(samples),
        "reply_missing_per_100_turns": sum(m["reply_missing"] for m in metrics) * 100 / turns if turns else None,
        "voice_error_kinds": sorted({kind for m in metrics for kind in m["voice_error_kinds"]}),
    }


def verdict(n, metrics, reference, *, full_length=True, errors=(), fake_errors=(), baseline_valid=True):
    invalid = list(errors)
    if not full_length:
        invalid.append("short diagnostic encounter; not a capacity measurement")
    if metrics["achieved_concurrency"] < 0.9 * n:
        invalid.append(f"achieved concurrency {metrics['achieved_concurrency']:.3f} < {0.9 * n:.3f}")
    lag = metrics["generator_loop_lag_ms"]["p99"]
    if lag is None or lag > 50:
        invalid.append("generator loop-lag p99 missing or > 50 ms")
    for value, label in ((metrics["speech_end_to_first_played_s"]["p90"], "played latency"),
                         (metrics["reply_missing_per_100_turns"], "participant turns"),
                         (metrics["server_loop_lag_ms"]["p99"], "server loop lag"),
                         (metrics["server_rss_bytes"]["max"], "server RSS"),
                         (metrics["close_burst_s"], "session/mix close completion")):
        if value is None:
            invalid.append(label + " missing")
    if not reference or reference.get("p90_s") is None or not baseline_valid:
        invalid.append("valid N=1 baseline missing")
    if invalid:
        return {"verdict": "INVALID", "reasons": invalid, "rule": RULE}
    if fake_errors:
        return {"verdict": "FAIL", "reasons": ["fake upstream unknown route/protocol error"], "rule": RULE}
    failures = []
    limit = max(1.5 * reference["p90_s"], reference["p90_s"] + 2)
    if metrics["speech_end_to_first_played_s"]["p90"] > limit:
        failures.append(f"played p90 exceeds {limit:.3f} s")
    if metrics["reply_missing_per_100_turns"] > reference["reply_missing_per_100_turns"] + 1:
        failures.append("reply_missing per 100 participant turns exceeds N=1 + 1")
    if metrics["server_loop_lag_ms"]["p99"] > 250:
        failures.append("server loop-lag p99 > 250 ms")
    if metrics["server_rss_bytes"]["max"] > 900_000_000:
        failures.append("RSS > 900 MB")
    new = set(metrics["voice_error_kinds"]) - set(reference["voice_error_kinds"])
    if new:
        failures.append("new voice_error kinds: " + ", ".join(sorted(new)))
    return {"verdict": "FAIL" if failures else "PASS", "reasons": failures, "rule": RULE,
            "latency_limit_s": limit}


def capacity(steps, repeat):
    grouped = defaultdict(list)
    for step in steps:
        grouped[step["concurrency"]].append(step["verdict"])
    return max((n for n, results in grouped.items() if len(results) == repeat and all(v == "PASS" for v in results)), default=None)


class LiveGuard:
    def __init__(self):
        self.attempts = 0
        self.errors = 0
        self.stopped = None

    def observe(self, event, concurrency):
        self.attempts += 1
        self.errors += event.get("outcome") in ERROR_OUTCOMES
        reason = None
        if event.get("upstream") == "gateway" and event.get("status") == 429:
            reason = "first gateway 429"
        elif self.errors / self.attempts > 0.05:
            reason = "upstream attempt error rate > 5%"
        if reason and self.stopped is None:
            self.stopped = {"reason": reason, "route": event.get("route"),
                            "purpose": event.get("purpose"), "concurrency": concurrency,
                            "attempts": self.attempts, "errors": self.errors, "t": event.get("t")}
        return self.stopped


class JsonlTail:
    def __init__(self, path):
        self.path = Path(path)
        self.offset = 0
        self.partial = b""

    def read(self):
        if not self.path.exists():
            if self.offset:
                raise FileNotFoundError("telemetry/log file disappeared")
            return []
        with self.path.open(mode="rb") as fh:
            fh.seek(0, 2)
            if fh.tell() < self.offset:
                raise ValueError("telemetry/log file was truncated")
            fh.seek(self.offset)
            data = self.partial + fh.read()
            self.offset = fh.tell()
        lines = data.split(b"\n")
        self.partial = lines.pop()
        return [json.loads(line) for line in lines if line]


def server_lag_windows(log_rows, start, end):
    result = []
    for row in log_rows:
        if not start <= row["t"] <= end:
            continue
        match = re.search(r"loop_lag: n=(\d+) p99=([\d.]+)ms max=([\d.]+)ms", row["line"])
        if match:
            result.append({"t": row["t"], "n": int(match[1]), "p99_ms": float(match[2]), "max_ms": float(match[3])})
    return result
