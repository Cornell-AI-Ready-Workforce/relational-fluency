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

from tools.load import measure
from tools.load.fake_upstream import load_config
from tools.load.participant import Participant
from tools.load import run
from tools.load.runtime import ROOT, Runtime, command
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
    if any(not (args.server_checkout / path).is_file() for path in
           ("server/app.py", "server/reply_trace.py", "infra/terraform/ecs.tf")):
        parser.error("server checkout must contain app.py, reply_trace.py and infra/terraform/ecs.tf")
    if (args.report_dir / "report.json").exists():
        parser.error("report directory already contains a run")
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
                    and row.get("finalized_at") == "encounter_teardown"
                    for row in rows if row.get("type") in {"reply_trace", "commit_trace"})
        sessions.append({"session_id": sid, "scenario": timeline["scenario"],
                         "events": str(target.relative_to(directory)), "event_counts": dict(counts),
                         "trace_contract_valid": valid, "lines_sent": len(timeline.get("lines", []))})
    return sessions


async def run_latency(args, *, runtime_factory=Runtime, participant_factory=Participant, key_loader=run.load_key):
    directory = args.report_dir.resolve()
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
        server_checkout=args.server_checkout, server_environment=overrides, encounter_seconds=timeout + 30)
    report = {"mode": "latency", "capacity_eligible": False, "capacity": None, "gateway": args.gateway,
              "tts": "fake", "server": server, "harness": await revision(ROOT),
              "configuration": {"participants": args.participants, "turns_per_scenario": args.turns,
                  "scenarios": args.scenarios, "seed": args.seed, "timeout_s": timeout,
                  "fake_tts_profile": config.tts_profile, "fake_configuration": asdict(config),
                  "fake_source_sha256": hashlib.sha256((ROOT / "tools/load/fake_upstream.py").read_bytes()).hexdigest(),
                  "tts_slots_override": args.tts_slots,
                  "policy": "one encounter per participant per selected scenario; fresh participant state; shared local server; scripted hang-up after turns"},
              "steps": [], "sessions": [], "errors": [], "verdict": "INVALID"}
    try:
        await runtime.start()
        report["host"] = runtime.metadata
        for index, scenario in enumerate(args.scenarios):
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
            if step["live_stop"]:
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
        report["verdict"] = "FAIL" if reasons else "PASS"
    except (OSError, RuntimeError, ValueError) as exc:
        report["errors"].append(type(exc).__name__ + ": " + str(exc))
    finally:
        await runtime.stop()
        attempts = measure.JsonlTail(runtime.data / "upstream-attempts.jsonl").read()
        gateway = [row for row in attempts if row.get("upstream") == "gateway" and row.get("detail") != "slot_wait"]
        report["gateway_request_count"] = len(gateway)
        report["gateway_requests_by_purpose"] = dict(Counter(row.get("purpose") for row in gateway))
        (directory / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        (directory / "report.md").write_text("# Gateway latency diagnostic\n\n" +
            f"Verdict: {report['verdict']}. Capacity eligible: false. Gateway requests: {len(gateway)}.\n\n" +
            "Configuration: " + json.dumps(report["configuration"]) + "\n\nServer: " + json.dumps(server) +
            "\n\nSession events: " + json.dumps(report["sessions"]) + "\n\nErrors: " + json.dumps(report["errors"]) + "\n",
            encoding="utf-8")
        print(f"Gateway requests: {len(gateway)} (includes startup/preflight; excludes TTS and slot-only waits)", flush=True)
    return report, directory


def main(argv=None):
    report, directory = asyncio.run(run_latency(parse_args(argv)))
    print(directory / "report.json", flush=True)
    return 0 if report["verdict"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
