"""Measurements and the fixed load-test decision rules."""
from __future__ import annotations

from collections import Counter, defaultdict
import json
import math
from pathlib import Path
import re

from tools.sim import analyze


ERROR_OUTCOMES = {"http_error", "timeout", "closed", "exception"}
LIVE_RATE_PURPOSES = ("preflight", "stt", "actor")
LIVE_GUARD_RULE = ("Immediate stop on gateway 429, 401 or 403 for every purpose; reply-path error rate > 5% "
                   "only after at least 20 counted attempts, purposes: " + ", ".join(LIVE_RATE_PURPOSES) +
                   "; other purposes and slot-only waits excluded from the rate.")
RULE = ("INVALID if achieved concurrency < 90% of N or generator loop-lag p99 > 50 ms, "
        "or required measurements/full encounters are missing. Otherwise FAIL if speech-end-to-first-played "
        "p90 > max(1.5 * N=1 p90, N=1 p90 + 2 s), reply_missing per 100 participant turns "
        "> N=1 + 1, server loop-lag p99 > 250 ms, RSS > 900 MB, or any non-benign "
        "voice_error kind was absent at N=1. Unknown fake routes/protocol errors fail the run. "
        "Server errors FAIL; generator errors INVALID. Missing first-played ACKs per 100 turns > N=1 + 1 FAIL. Capacity is the largest passing prefix of tested Ns, with every repeat PASS: capacity for the tested configuration on this host.")


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
        sent = [r for r in rows if r.get("detail") != "slot_wait"]
        durations = [r["duration_ms"] for r in sent if isinstance(r.get("duration_ms"), (int, float))]
        waits = [r["queue_wait_ms"] for r in rows if isinstance(r.get("queue_wait_ms"), (int, float))]
        result[purpose] = {
            "attempts": len(sent), "slot_wait_only": len(rows) - len(sent), "errors": sum(r.get("outcome") in ERROR_OUTCOMES for r in sent),
            "cancelled": sum(r.get("outcome") == "cancelled" for r in sent),
            "duration_ms": percentiles(durations),
            "queue_wait_ms": {**percentiles(waits), "max": max(waits, default=None)},
            "routes": dict(Counter(r.get("route") for r in sent)),
        }
    return result


def event_window(events_by_session, start, end):
    result = {}
    for sid, events in events_by_session.items():
        origin = next((e["wall"] - e["t"] for e in events
                       if isinstance(e.get("wall"), (int, float)) and isinstance(e.get("t"), (int, float))), None)
        rows = []
        for e in events:
            epoch = e.get("wall")
            if e.get("type") == "turn_timing" and origin is not None and isinstance(e.get("vad_speech_end"), (int, float)):
                epoch = origin + e["vad_speech_end"]
            if isinstance(epoch, (int, float)) and start <= epoch < end:
                rows.append(e)
        result[sid] = rows
    return result


def event_metrics(events_by_session, timelines, attempts):
    latency, missing_ack = first_played(events_by_session)
    per_session = [analyze.summarize(events_by_session.get(t["session_id"], []), t) for t in timelines]
    events = [e for rows in events_by_session.values() for e in rows]
    turns = sum(e.get("type") == "user_turn" for e in events)
    missing = sum(e.get("type") == "reply_missing" for e in events)
    kinds = Counter(kind for event in events if event.get("type") == "voice_error"
                    if (kind := voice_error_kind(event)) is not None)
    return {
        "speech_end_to_first_played_s": percentiles(latency), "latency_samples_s": latency,
        "first_played_missing": missing_ack,
        "first_played_missing_per_100_turns": missing_ack * 100 / turns if turns else None,
        "lines_said": sum(s["lines_said"] for s in per_session),
        "lines_answered": sum(s["lines_answered"] for s in per_session),
        "lines_heard": sum(s["lines_heard"] for s in per_session),
        "participant_turns": turns, "reply_missing": missing,
        "reply_missing_per_100_turns": missing * 100 / turns if turns else None,
        "voice_error": sum(kinds.values()), "voice_error_kinds": dict(kinds),
        "upstream": upstream_summary(attempts), "encounters": per_session,
    }


def summarize(events_by_session, timelines, *, attempts, resources, concurrency, generator_lag_ms,
              server_lag, measurement_start, measurement_end, close_burst_s, artifact_bytes):
    def inside(row):
        return measurement_start <= row.get("t", -math.inf) < measurement_end
    windowed = event_window(events_by_session, measurement_start, measurement_end)
    window_timelines = [{**t, "lines": [line for line in t.get("lines", [])
                                       if measurement_start <= line["end"] < measurement_end]} for t in timelines]
    result = event_metrics(windowed, window_timelines, [a for a in attempts if inside(a)])
    result["whole_step"] = event_metrics(events_by_session, timelines, attempts)
    result["by_scenario"] = {}
    for scenario in {t["scenario"] for t in timelines if t.get("scenario")}:
        members = [t for t in window_timelines if t.get("scenario") == scenario]
        result["by_scenario"][scenario] = event_metrics(
            {t["session_id"]: windowed.get(t["session_id"], []) for t in members}, members, [])
    steady = [s["active"] for s in concurrency if inside(s)]
    conversing = [s.get("conversing", 0) for s in concurrency if inside(s)]
    window_resources = [s for s in resources if inside(s)]
    cpu = [s["cpu_percent"] for s in window_resources if s.get("cpu_percent") is not None]
    rss = [s["rss_bytes"] for s in window_resources if s.get("rss_bytes") is not None]
    lag = [s["lag_ms"] for s in generator_lag_ms if inside(s)]
    server_lag = [s for s in server_lag if inside(s)]
    throttles = [s["throttled_usec"] for s in resources if s.get("throttled_usec") is not None]
    throttle_count = [s["nr_throttled"] for s in resources if s.get("nr_throttled") is not None]
    result.update({
        "generator_loop_lag_ms": {**percentiles(lag), "max": max(lag, default=None)},
        "generator_lag_samples": generator_lag_ms,
        "achieved_concurrency": sum(steady) / len(steady) if steady else 0.0,
        "conversing_concurrency": sum(conversing) / len(conversing) if conversing else 0.0,
        "concurrency_samples": concurrency, "steady_sample_count": len(steady),
        "measurement_window": {"start": measurement_start, "end": measurement_end},
        "server_loop_lag_ms": {
            "p99": max((s["p99_ms"] for s in server_lag), default=None),
            "max": max((s["max_ms"] for s in server_lag), default=None),
            "windows": server_lag, "aggregation": "maximum window p99; not a percentile of percentiles"},
        "server_cpu_percent": {**percentiles(cpu), "max": max(cpu, default=None)},
        "server_rss_bytes": {**percentiles(rss), "max": max(rss, default=None)},
        "server_throttled_s": (max(throttles) - min(throttles)) / 1e6 if len(throttles) >= 2 else None,
        "server_nr_throttled": max(throttle_count) - min(throttle_count) if len(throttle_count) >= 2 else None,
        "resource_samples": resources,
        "bytes_written_per_encounter": artifact_bytes,
        "bytes_written_definition": "sum of durable encounter file sizes, including the rendered mix; excludes write amplification",
        "websocket_disconnects": sum(t.get("websocket_disconnects", 0) for t in timelines),
        "unexpected_disconnects": sum(bool(t.get("unexpected_disconnect")) for t in timelines),
        "close_burst_s": close_burst_s,
    })
    return result


def baseline(repeats):
    """Pool raw N=1 latencies; do not average percentile summaries."""
    metrics = [r["metrics"] for r in repeats]
    samples = [x for m in metrics for x in m["latency_samples_s"]]
    turns = sum(m["participant_turns"] for m in metrics)
    return {
        "p90_s": analyze.percentile(samples, 90), "sample_count": len(samples),
        "reply_missing_per_100_turns": sum(m["reply_missing"] for m in metrics) * 100 / turns if turns else None,
        "first_played_missing_per_100_turns": sum(m["first_played_missing"] for m in metrics) * 100 / turns if turns else None,
        "voice_error_kinds": sorted({kind for m in metrics for kind in m["voice_error_kinds"]}),
    }


def verdict(n, metrics, reference, *, full_length=True, errors=(), server_errors=(), fake_errors=(), baseline_valid=True,
            early_stop=None):
    invalid = list(errors)
    known_failure = bool(server_errors or fake_errors or early_stop)
    ack_exceeded = (reference is not None and reference.get("first_played_missing_per_100_turns") is not None
                    and metrics.get("first_played_missing_per_100_turns") is not None
                    and metrics["first_played_missing_per_100_turns"] > reference["first_played_missing_per_100_turns"] + 1)
    if not full_length and not known_failure:
        invalid.append("short diagnostic encounter; not a capacity measurement")
    if metrics["achieved_concurrency"] < 0.9 * n and (not known_failure or
            (early_stop and metrics.get("steady_sample_count", 1) > 0)):
        invalid.append(f"achieved concurrency {metrics['achieved_concurrency']:.3f} < {0.9 * n:.3f}")
    lag = metrics["generator_loop_lag_ms"]["p99"]
    if (lag is None and not known_failure) or (lag is not None and lag > 50):
        invalid.append("generator loop-lag p99 missing or > 50 ms")
    for value, label in ((metrics["speech_end_to_first_played_s"]["p90"], "played latency"),
                         (metrics["reply_missing_per_100_turns"], "participant turns"),
                         (metrics["server_loop_lag_ms"]["p99"], "server loop lag"),
                         (metrics["server_rss_bytes"]["max"], "server RSS"),
                         (metrics["close_burst_s"], "session/mix close completion")):
        if value is None and not known_failure and not (label == "played latency" and ack_exceeded):
            invalid.append(label + " missing")
    if (not reference or reference.get("p90_s") is None or not baseline_valid) and not known_failure:
        invalid.append("valid N=1 baseline missing")
    elif early_stop and not baseline_valid:
        invalid.append("valid N=1 baseline missing before deliberate stop")
    if invalid:
        return {"verdict": "INVALID", "reasons": invalid, "rule": RULE}
    if early_stop:
        return {"verdict": "FAIL", "reasons": ["stopped early: " + early_stop["reason"]], "rule": RULE}
    if server_errors:
        count = len(server_errors)
        encounters = metrics.get("encounter_count", 0) or 1
        return {"verdict": "FAIL", "reasons": [f"server errors: {count} ({100 * count / encounters:.3f} per 100 encounters)"] + list(server_errors), "rule": RULE}
    if fake_errors:
        return {"verdict": "FAIL", "reasons": ["fake upstream unknown route/protocol error"], "rule": RULE}
    failures = []
    limit = max(1.5 * reference["p90_s"], reference["p90_s"] + 2)
    if metrics["speech_end_to_first_played_s"]["p90"] is not None and metrics["speech_end_to_first_played_s"]["p90"] > limit:
        failures.append(f"played p90 exceeds {limit:.3f} s")
    if metrics["reply_missing_per_100_turns"] > reference["reply_missing_per_100_turns"] + 1:
        failures.append("reply_missing per 100 participant turns exceeds N=1 + 1")
    ack_rate = metrics.get("first_played_missing_per_100_turns")
    if ack_rate is None:
        return {"verdict": "INVALID", "reasons": ["first-played ACK denominator missing"], "rule": RULE}
    if ack_rate > reference["first_played_missing_per_100_turns"] + 1:
        failures.append("first_played_missing per 100 turns exceeds N=1 + 1")
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
    result = None
    for n, results in sorted(grouped.items()):
        if len(results) != repeat or any(v != "PASS" for v in results):
            break
        result = n
    return result


def non_monotonic(steps):
    blocked = False
    for n in sorted({s["concurrency"] for s in steps}):
        states = [s["verdict"] for s in steps if s["concurrency"] == n]
        if blocked and all(v == "PASS" for v in states):
            return "non-monotonic ramp: a higher N passed after a lower N failed or was INVALID"
        blocked |= any(v != "PASS" for v in states)
    return None


def gateway_attempt(event):
    return event.get("upstream") == "gateway" and event.get("detail") != "slot_wait"


def gateway_error(event):
    status = event.get("status")
    return gateway_attempt(event) and (event.get("outcome") in ERROR_OUTCOMES or
                                       isinstance(status, int) and status >= 400)


def _gateway_row(event):
    return {key: event.get(key) for key in ("t", "session_id", "route", "purpose", "outcome", "detail", "status")}


def gateway_errors(attempts):
    return [_gateway_row(event) for event in attempts if gateway_error(event)]


def gateway_errors_markdown(rows):
    lines = [f"Gateway errors ({len(rows)}):", "", "| Route | Purpose | Outcome | Detail | Status |",
             "| --- | --- | --- | --- | --- |"]
    for row in rows:
        cells = [json.dumps(row.get(key), ensure_ascii=False).replace("|", "\\|")
                 for key in ("route", "purpose", "outcome", "detail", "status")]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


class LiveGuard:
    def __init__(self):
        # attempts/errors are the rate denominator/numerator. Gateway totals
        # retain uncounted purposes for diagnostics and immediate safety stops.
        self.attempts = 0
        self.errors = 0
        self.gateway_attempts = 0
        self.gateway_errors = 0
        self.last_counted_error = None
        self.error_counts_by_purpose = Counter()
        self.stopped = None

    def observe(self, event, concurrency):
        if not gateway_attempt(event):
            return self.stopped
        self.gateway_attempts += 1
        error = gateway_error(event)
        self.gateway_errors += error
        if event.get("purpose") in LIVE_RATE_PURPOSES:
            self.attempts += 1
            if error:
                self.errors += 1
                self.last_counted_error = _gateway_row(event)
                self.error_counts_by_purpose[event["purpose"]] += 1
        reason = None
        if event.get("status") in {429, 401, 403}:
            reason = "first gateway " + str(event["status"])
        elif self.attempts >= 20 and self.errors / self.attempts > 0.05:
            reason = "gateway attempt error rate > 5%"
        if reason and self.stopped is None:
            cause = event if event.get("status") in {429, 401, 403} else self.last_counted_error
            self.stopped = {"reason": reason, "route": cause.get("route"),
                            "purpose": cause.get("purpose"), "concurrency": concurrency,
                            "outcome": cause.get("outcome"), "detail": cause.get("detail"),
                            "status": cause.get("status"), "t": event.get("t"),
                            "attempts": self.attempts, "errors": self.errors,
                            "gateway_attempts": self.gateway_attempts, "gateway_errors": self.gateway_errors,
                            "counted_purposes": list(LIVE_RATE_PURPOSES),
                            "error_counts_by_purpose": dict(self.error_counts_by_purpose),
                            "last_counted_error": self.last_counted_error, "trigger": _gateway_row(event)}
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
        if not start <= row["t"] < end:
            continue
        match = re.search(r"loop_lag: n=(\d+) p99=([\d.]+)ms max=([\d.]+)ms", row["line"])
        if match:
            result.append({"t": row["t"], "n": int(match[1]), "p99_ms": float(match[2]), "max_ms": float(match[3])})
    return result
