"""Diagnostic 1:1 gateway latency runs with synthetic, calibrated TTS."""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import random
import shutil
import signal

from tools.load import measure
from tools.load.fake_upstream import load_config
from tools.load.participant import Participant
from tools.load import run
from tools.load.runtime import ROOT, Runtime, command
from tools.load.checkout import validate_checkout
from tools.load.tts_proof import verify as verify_tts
from tools.sim import sequences
from tools.sim.serve import production_environment


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server-checkout", type=Path, required=True)
    parser.add_argument("--report-dir", type=Path, required=True)
    parser.add_argument("--gateway", choices=("live", "fake"), default="live")
    parser.add_argument("--allow-live", action="store_true")
    parser.add_argument("--backend", choices=("local",), default="local")
    parser.add_argument("--participants", type=int, default=1)
    parser.add_argument("--max-concurrency", type=int, default=20)
    parser.add_argument("--turns", type=int, default=24)
    parser.add_argument("--scenarios", default="S1A,S2A")
    parser.add_argument("--seed", type=int, default=20261112)
    parser.add_argument("--timeout", type=run.positive, help="per-encounter deadline; default 60 s per turn + 60 s")
    parser.add_argument("--close-timeout", type=run.positive, default=120)
    parser.add_argument("--live-url")
    parser.add_argument("--fake-config", type=Path)
    parser.add_argument("--fake-tts-profile", choices=("calibrated", "legacy"))
    parser.add_argument("--tts-slots", type=int, help="override server TTS concurrency for contention diagnostics")
    args = parser.parse_args(argv)
    args.server_checkout = args.server_checkout.resolve()
    args.scenarios = args.scenarios.split(",")
    if not args.scenarios or set(args.scenarios) - {"S1A", "S2A"} or len(set(args.scenarios)) != len(args.scenarios):
        parser.error("latency mode requires unique 1:1 scenarios S1A,S2A")
    if not 1 <= args.participants <= args.max_concurrency or args.turns < 1 or (args.tts_slots is not None and args.tts_slots < 1):
        parser.error("positive participants/turns/slots required; participants must not exceed --max-concurrency")
    if args.gateway == "live" and not args.allow_live:
        parser.error("live gateway requires --allow-live")
    try:
        validate_checkout(args.server_checkout, require_traces=True)
    except ValueError as exc:
        parser.error(str(exc))
    if args.report_dir.exists() and (not args.report_dir.is_dir() or any(args.report_dir.iterdir())):
        parser.error("report directory must be fresh and empty (including abandoned runtime data)")
    return args


def sequence(scenario, turns, seed):
    names = list(sequences.lines_used(sequences.DEFAULT_SEQUENCES[scenario]))
    random.Random(seed).shuffle(names)
    return "tone:1," + ",".join("say:" + names[i % len(names)] for i in range(turns)) + ",tone:3"


async def revision(checkout):
    code, sha, _ = await command("git", "rev-parse", "HEAD", cwd=checkout, check=False)
    _, dirty, _ = await command("git", "status", "--porcelain", cwd=checkout, check=False)
    return {"checkout": str(checkout), "commit": sha.strip() if code == 0 else None, "dirty": bool(dirty.strip())}


def archive_events(runtime, directory, timelines):
    sessions = []
    for timeline in timelines:
        sid = timeline.get("session_id")
        if not sid:
            continue
        source = runtime.data / "sessions" / sid / "events.jsonl"
        target = directory / "sessions" / sid / "events.jsonl"
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.exists():
            shutil.copyfile(source, target)
        rows = measure.JsonlTail(target).read()
        counts = Counter(row.get("type") for row in rows)
        valid = all(row.get("schema_version") == 1 and row.get("clock") == "encounter_s"
                    and row.get("units") == "seconds"
                    and row.get("finalized_at") == "encounter_teardown"
                    for row in rows if row.get("type") in {"reply_trace", "commit_trace"})
        sessions.append({"session_id": sid, "scenario": timeline["scenario"],
                         "events": str(target.relative_to(directory)), "event_counts": dict(counts),
                         "replies_started": timeline.get("replies_started", 0),
                         "trace_contract_valid": valid, "lines_sent": len(timeline.get("lines", []))})
    return sessions


async def run_latency(args, *, runtime_factory=Runtime, participant_factory=Participant, key_loader=run.load_key):
    validate_checkout(args.server_checkout, require_traces=True)
    directory = args.report_dir.resolve()
    if directory.exists() and any(directory.iterdir()):
        raise ValueError("report directory must be fresh and empty")
    directory.mkdir(parents=True, exist_ok=True)
    config = load_config(args.fake_config, profile=args.fake_tts_profile, seed=args.seed)
    production = production_environment(args.server_checkout / "infra/terraform")
    server = await revision(args.server_checkout)
    key = None
    if args.gateway == "live":
        key = os.getenv("LITELLM_API_KEY") or os.getenv("ANTHROPIC_API_KEY") or await asyncio.to_thread(key_loader)
    timeout = args.timeout or args.turns * 60 + 60
    overrides = {"BUILD_SHA": server["commit"] or "load-latency"}
    if args.tts_slots is not None:
        overrides["ELEVENLABS_CONCURRENCY"] = str(args.tts_slots)
    runtime = runtime_factory(directory / "runtime", backend="local", gateway=args.gateway,
        fake_config=config, live_key=key, live_url=args.live_url or production.get("LLM_BASE_URL"),
        server_checkout=args.server_checkout, server_environment=overrides, encounter_seconds=timeout + 30,
        record_tts_destinations=True)
    report = {"mode": "latency", "capacity_eligible": False, "capacity": None, "gateway": args.gateway,
              "tts": "fake", "server": server, "harness": await revision(ROOT),
              "configuration": {"participants": args.participants, "turns_per_scenario": args.turns,
                  "scenarios": args.scenarios, "seed": args.seed, "timeout_s": timeout,
                  "fake_tts_profile": config.tts_profile, "fake_configuration": asdict(config),
                  "fake_source_sha256": hashlib.sha256((ROOT / "tools/load/fake_upstream.py").read_bytes()).hexdigest(),
                  "tts_slots_override": args.tts_slots,
                  "live_guard_rule": measure.LIVE_GUARD_RULE, "tts_allowed_destination_journal": True,
                  "live_guard_counted_purposes": list(measure.LIVE_RATE_PURPOSES),
                  "policy": "one encounter per participant per selected scenario; fresh participant state; shared local server; scripted hang-up after turns"},
              "steps": [], "sessions": [], "errors": [], "verdict": "INVALID"}

    def apply_interruption():
        report["interrupted"] = bool(getattr(args, "interrupted", False))
        if report["interrupted"]:
            message = "latency run interrupted; child cleanup requested"
            if message not in report["errors"]:
                report["errors"].append(message)
            report["verdict"] = "INVALID"
        return report["interrupted"]

    def live_guard(rows, phase):
        if args.gateway != "live":
            return None
        guard = measure.LiveGuard()
        for row in rows:
            guard.observe(row, args.participants)
        if guard.stopped:
            report["live_stop"] = {**guard.stopped, "phase": phase}
            return guard.stopped["reason"]
        return None

    try:
        await runtime.start()
        report["host"] = runtime.metadata
        for index, scenario in enumerate(args.scenarios):
            if apply_interruption():
                break
            prior = measure.JsonlTail(runtime.data / "upstream-attempts.jsonl").read()
            if reason := live_guard(prior, "before scenario"):
                await runtime.kill_server()
                report["errors"].append("live safety stop: " + reason)
                break
            step_args = run.parse_args(["--gateway", args.gateway, "--ramp", "1", "--scenarios", scenario,
                "--encounters-per-participant", "1", "--warmup", "0", "--no-fail-fast", "--skip-smoke",
                "--max-concurrency", str(args.max_concurrency)] + (["--allow-live"] if args.gateway == "live" else []))
            step_args.steps = sequence(scenario, args.turns, args.seed + index)
            step_args.duration, step_args.encounter_seconds = timeout, timeout + 30
            step_args.close_timeout = args.close_timeout
            step = await run.run_step(runtime, step_args, args.participants, index + 1, timeout + 30,
                participant_factory=participant_factory, label=f"latency-{scenario}")
            report["steps"].append(step)
            report["sessions"].extend(await asyncio.to_thread(archive_events, runtime, directory, step["timelines"]))
            if step["live_stop"] or apply_interruption():
                break
        reasons = report["errors"]
        for step in report["steps"]:
            reasons.extend(step["errors"])
            reasons.extend(step.get("server_errors", []))
            if step["fake_errors"]:
                reasons.append("fake upstream protocol error")
            if not step["metrics"].get("settled_artifacts"):
                reasons.append("session artifacts did not settle; teardown traces may be missing")
        if len(report["sessions"]) != args.participants * len(args.scenarios):
            reasons.append("requested sessions missing")
        for session in report["sessions"]:
            if session["lines_sent"] < args.turns:
                reasons.append(session["session_id"] + ": requested turns not completed")
            if not session["trace_contract_valid"] or any(not session["event_counts"].get(kind)
                    for kind in ("reply_trace", "commit_trace", "turn_timing")):
                reasons.append(session["session_id"] + ": required teardown traces/turn timing missing or invalid")
            if session["event_counts"].get("reply_trace", 0) < session["replies_started"]:
                reasons.append(session["session_id"] + ": reply_trace records missing for observed replies")
        if not apply_interruption():
            report["verdict"] = "FAIL" if reasons else "PASS"
    except asyncio.CancelledError:
        args.interrupted = True
        apply_interruption()
    except (OSError, RuntimeError, ValueError) as exc:
        report["errors"].append(type(exc).__name__ + ": " + str(exc))
    finally:
        apply_interruption()
        async def finish_runtime():
            try:
                # Retain the fake until server work stops and final counts exist.
                await runtime.kill_server()
                attempts = measure.JsonlTail(runtime.data / "upstream-attempts.jsonl").read()
                if reason := live_guard(attempts, "run end"):
                    report["errors"].append("live safety stop: " + reason)
                    report["verdict"] = "FAIL"
                fake = await runtime.fake_stats()
                destinations = measure.JsonlTail(runtime.data / "tts-destinations.jsonl").read()
                proof = report["tts_proof"] = verify_tts(fake.get("counts", {}).get("tts_ws", 0), attempts, destinations)
                report["errors"].extend(proof["reasons"])
                if not proof["passed"] and report["verdict"] == "PASS":
                    report["verdict"] = "FAIL"
            except (OSError, RuntimeError, ValueError, AttributeError, asyncio.CancelledError) as exc:
                if isinstance(exc, asyncio.CancelledError):
                    args.interrupted = True
                report["errors"].append("TTS proof unavailable: " + type(exc).__name__)
                report["tts_proof"] = {"passed": False}
                if report["verdict"] == "PASS":
                    report["verdict"] = "FAIL"
            finally:
                await runtime.stop()
        cleanup = asyncio.create_task(finish_runtime())
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                args.interrupted = True
                apply_interruption()
        cleanup.result()
        apply_interruption()
        attempts = measure.JsonlTail(runtime.data / "upstream-attempts.jsonl").read()
        gateway = [row for row in attempts if measure.gateway_attempt(row)]
        report["gateway_request_count"] = len(gateway)
        report["gateway_requests_by_purpose"] = dict(Counter(row.get("purpose") for row in gateway))
        report["gateway_errors"] = measure.gateway_errors(attempts)
        (directory / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        (directory / "report.md").write_text("# Gateway latency diagnostic\n\n" +
            f"Verdict: {report['verdict']}. Capacity eligible: false. Gateway requests: {len(gateway)}.\n\n" +
            "Configuration: " + json.dumps(report["configuration"]) + "\n\nServer: " + json.dumps(server) +
            "\n\nSession events: " + json.dumps(report["sessions"]) + "\n\nTTS proof: " + json.dumps(report.get("tts_proof")) +
            "\n\n" + measure.gateway_errors_markdown(report["gateway_errors"]) +
            "\n\nErrors: " + json.dumps(report["errors"]) + "\n",
            encoding="utf-8")
        print("Verdict: " + report["verdict"], flush=True)
        print(f"Gateway requests: {len(gateway)} (includes startup/preflight; excludes TTS and slot-only waits)", flush=True)
    return report, directory


async def cli(args):
    loop, task = asyncio.get_running_loop(), asyncio.current_task()
    previous = signal.getsignal(signal.SIGTERM)
    args.interrupted = False
    def interrupt():
        args.interrupted = True
        task.cancel()
    loop.add_signal_handler(signal.SIGTERM, interrupt)
    try:
        return await run_latency(args)
    finally:
        loop.remove_signal_handler(signal.SIGTERM)
        signal.signal(signal.SIGTERM, previous)


def main(argv=None):
    report, directory = asyncio.run(cli(parse_args(argv)))
    print(directory / "report.json", flush=True)
    return 0 if report["verdict"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
