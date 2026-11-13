"""Run full simulated encounters against one CPU-capped server."""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict
import datetime as dt
import json
import math
import os
from pathlib import Path
import secrets
import time

import httpx

from server.loop_lag import LoopLagProbe
from tools.load import measure
from tools.load.fake_upstream import FakeConfig
from tools.load.participant import Participant, full_sequence, open_identity
from tools.load.runtime import ROOT, Runtime, RuntimeUnavailable, read_encounters
from tools.sim import sequences, stim
from tools.sim.serve import production_environment


SCENARIOS = tuple(sequences.DEFAULT_SEQUENCES)


def positive(value):
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise argparse.ArgumentTypeError("must be finite and positive")
    return result


def arrival(value):
    if value == "burst":
        return 0.0
    if value.startswith("stagger:"):
        try:
            seconds = float(value.split(":", 1)[1])
            if math.isfinite(seconds) and seconds >= 0:
                return seconds
        except ValueError:
            pass
    raise argparse.ArgumentTypeError("expected burst or stagger:<nonnegative seconds>")


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ramp", default="1,5,10,20,30")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--scenarios", default=",".join(SCENARIOS))
    parser.add_argument("--arrival", type=arrival, default="burst", metavar="burst|stagger:<s>")
    parser.add_argument("--duration", type=positive, help="diagnostic time cap per encounter; shorter runs are INVALID for capacity")
    parser.add_argument("--steps", help="custom sim sequence; requires one scenario")
    parser.add_argument("--warmup", type=float, default=30)
    parser.add_argument("--webcam", action="store_true")
    parser.add_argument("--gateway", choices=("fake", "live"), default="fake")
    parser.add_argument("--allow-live", action="store_true")
    parser.add_argument("--max-concurrency", type=int, default=20)
    parser.add_argument("--backend", choices=("docker", "local"), default="docker")
    parser.add_argument("--image")
    parser.add_argument("--fake-config", type=Path)
    parser.add_argument("--report-dir", type=Path)
    parser.add_argument("--close-timeout", type=positive, default=120)
    parser.add_argument("--profile-seconds", type=positive, default=10)
    parser.add_argument("--live-url")
    parser.add_argument("--isolation-only", action="store_true")
    args = parser.parse_args(argv)
    if args.report_dir and (args.report_dir / "report.json").exists():
        parser.error("--report-dir already contains a run; choose a fresh directory")
    try:
        args.ramp = [int(n) for n in args.ramp.split(",")]
    except ValueError:
        parser.error("--ramp requires comma-separated positive integers")
    if (not args.ramp or args.ramp[0] != 1 or args.ramp != sorted(set(args.ramp))
            or min(args.ramp) < 1):
        parser.error("--ramp must increase without duplicates and start at N=1")
    args.scenarios = args.scenarios.split(",")
    if not args.scenarios or len(set(args.scenarios)) != len(args.scenarios) or set(args.scenarios) - set(SCENARIOS):
        parser.error("--scenarios requires unique S1A,S2A,S3A,S4A IDs")
    if args.repeat < 1 or args.max_concurrency < 1 or not math.isfinite(args.warmup) or args.warmup < 0:
        parser.error("positive repeat/max-concurrency and finite nonnegative warmup required")
    if args.steps:
        if len(args.scenarios) != 1:
            parser.error("--steps requires one scenario")
        try:
            sequences.parse(args.steps)
        except sequences.StepError as exc:
            parser.error(str(exc))
    if args.gateway == "live":
        if not args.allow_live:
            parser.error("live gateway requires --allow-live")
        if max(args.ramp) > args.max_concurrency:
            parser.error("live ramp exceeds --max-concurrency")
        if args.isolation_only:
            parser.error("isolation proof is available only with --gateway fake")
    if args.isolation_only and args.backend != "docker":
        parser.error("isolation proof requires Docker")
    return args


def load_key():
    from dotenv import dotenv_values
    values = dotenv_values(ROOT / ".env")
    value = (values.get("LITELLM_API_KEY") or values.get("ANTHROPIC_API_KEY")
             or os.getenv("LITELLM_API_KEY") or os.getenv("ANTHROPIC_API_KEY"))
    if not value:
        raise ValueError("live gateway key is absent")
    return value


def _write_report(directory, report):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    lines = ["# Load run", "", report["rule"], "", f"Configuration: {report.get('isolation', {}).get('label', 'not started')}", "",
             "| N | Repeat | Verdict | Played p90 (s) | Achieved N | Generator p99 (ms) | RSS max (MB) | Reasons |",
             "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    for step in report.get("steps", []):
        m = step["metrics"]
        rss = m["server_rss_bytes"]["max"]
        lines.append(f"| {step['concurrency']} | {step['repeat']} | {step['verdict']} | "
                     f"{m['speech_end_to_first_played_s']['p90']} | {m['achieved_concurrency']:.2f} | "
                     f"{m['generator_loop_lag_ms']['p99']} | {rss / 1e6 if rss is not None else None} | "
                     + "; ".join(step["reasons"]) + " |")
    lines.extend(["", f"Capacity for the tested configuration: {report.get('capacity')}", ""])
    if report.get("fatal_error"):
        lines.extend(["Execution error: " + report["fatal_error"], ""])
    if report.get("live_stop"):
        lines.extend(["Live stop: " + json.dumps(report["live_stop"]), ""])
    if report.get("first_failure_profile"):
        lines.extend(["First failing step profile:", "", "```json",
                      json.dumps(report["first_failure_profile"], indent=2), "```", ""])
    (directory / "report.md").write_text("\n".join(lines), encoding="utf-8")


async def _closed(data, sid, timeout):
    deadline = time.monotonic() + timeout
    path = data / "sessions" / sid / "manifest.json"
    while time.monotonic() < deadline:
        def check():
            try:
                return json.loads(path.read_text(encoding="utf-8")).get("status") == "closed"
            except (FileNotFoundError, ValueError):
                return False
        if await asyncio.to_thread(check):
            return True
        await asyncio.sleep(0.1)
    return False


async def run_step(runtime, args, n, repeat, ceiling, *, participant_factory=Participant):
    start = time.time()
    directory = runtime.directory / f"N{n}-repeat{repeat}"
    await asyncio.to_thread(directory.mkdir, exist_ok=True)
    participants, timelines, finished, errors = [], [], [], []
    resources, concurrency, attempts, log_rows = [], [], [], []
    guard = measure.LiveGuard()
    stopped = asyncio.Event()
    done = asyncio.Event()
    attempt_tail = measure.JsonlTail(runtime.data / "upstream-attempts.jsonl")
    log_tail = measure.JsonlTail(runtime.directory / "server-log.jsonl")
    # Consume startup/earlier-step records; they are kept separately in the run report.
    await asyncio.to_thread(attempt_tail.read)
    await asyncio.to_thread(log_tail.read)
    default_mix = set(args.scenarios) == set(SCENARIOS)

    async def worker(index):
        await asyncio.sleep(index * args.arrival)
        client = await asyncio.to_thread(httpx.AsyncClient, timeout=30, follow_redirects=False, trust_env=False)
        async with client as http:
            identity = await open_identity(http, runtime.base, runtime.key, f"N{n}_{repeat}_{index}")
            run_id = identity["run"]
            view = None
            if default_mix:
                response = await http.get(runtime.base + f"/api/run/{run_id}", params={"key": runtime.key})
                response.raise_for_status()
                view = response.json()
            for slot in range(len(args.scenarios)):
                # The server owns the Williams assignment and its advancement.
                scenario = view["current"]["id"] if default_mix else args.scenarios[(slot + index + repeat - 1) % len(args.scenarios)]
                if scenario not in args.scenarios:
                    raise RuntimeError("server Williams run selected an unexpected scenario")
                steps = args.steps or full_sequence(scenario, ceiling)
                participant = participant_factory(
                    runtime.base, scenario, steps, index=f"{n}_{repeat}_{index}_{slot}", key=runtime.key,
                    duration_s=args.duration or ceiling + 30, webcam=args.webcam, http=http, identity=identity)
                participants.append(participant)
                try:
                    timeline = await participant.run()
                finally:
                    if participant.tl.get("session_id"):
                        # Also preserve partial timelines when the live guard cancels work.
                        timelines.append(participant.tl)
                if not timeline.get("completed"):
                    errors.append("encounter did not reach encounter_complete")
                    break
                if default_mix:
                    if not await _closed(runtime.data, timeline["session_id"], args.close_timeout):
                        raise RuntimeError("session did not close before run advancement")
                    response = await http.post(runtime.base + f"/api/run/{run_id}/advance",
                                               params={"session_id": timeline["session_id"], "key": runtime.key})
                    response.raise_for_status()
                    view = response.json()
            finished.append(time.time())

    async def watch_attempts():
        while not done.is_set():
            try:
                rows = await asyncio.to_thread(attempt_tail.read)
            except (OSError, ValueError) as exc:
                errors.append("attempt telemetry unavailable: " + type(exc).__name__)
                if args.gateway == "live":
                    guard.stopped = {"reason": "attempt telemetry unavailable", "route": None, "purpose": None,
                                     "concurrency": sum(p.active for p in participants), "t": time.time()}
                stopped.set()
                return
            attempts.extend(rows)
            if args.gateway == "live":
                for row in rows:
                    if guard.observe(row, sum(p.active for p in participants)):
                        stopped.set()
                        return
            await asyncio.sleep(0.1)

    async def sample_concurrency():
        due = time.monotonic()
        while not done.is_set():
            concurrency.append({"t": time.time(), "active": sum(p.active for p in participants)})
            due += 1.0
            await asyncio.sleep(max(0, due - time.monotonic()))

    async def sample_resources():
        due = time.monotonic()
        while not done.is_set():
            try:
                resources.append(await runtime.resources())
            except (RuntimeError, OSError, ValueError) as exc:
                errors.append("resource sampling: " + type(exc).__name__)
            log_rows.extend(await asyncio.to_thread(log_tail.read))
            due += 1.0
            await asyncio.sleep(max(0, due - time.monotonic()))

    workers = [asyncio.create_task(worker(index)) for index in range(n)]
    async def workers_done():
        results = await asyncio.gather(*workers, return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException):
                errors.append("participant worker: " + type(result).__name__)
    all_workers = asyncio.create_task(workers_done())
    stop_waiter = asyncio.create_task(stopped.wait())
    watchers = [asyncio.create_task(watch_attempts()), asyncio.create_task(sample_concurrency()),
                asyncio.create_task(sample_resources())]
    profile = asyncio.create_task(runtime.profile(directory, delay=args.warmup, seconds=args.profile_seconds))
    async with LoopLagProbe() as generator_probe:
        try:
            await asyncio.wait({all_workers, stop_waiter}, return_when=asyncio.FIRST_COMPLETED)
            if stopped.is_set():
                for task in workers:
                    task.cancel()
            await all_workers
            last_hangup = max((t["hangup_requested"] for t in timelines), default=time.time())
            close_deadline = time.monotonic() + args.close_timeout
            events, sizes, settled = {}, {}, False
            while timelines and time.monotonic() < close_deadline:
                events, sizes, settled = await asyncio.to_thread(read_encounters, runtime.data, timelines)
                if timelines and settled:
                    break
                await asyncio.sleep(0.25)
            close_burst = max(0.0, time.time() - last_hangup) if settled and timelines else None
        finally:
            done.set()
            stop_waiter.cancel()
            for task in watchers:
                task.cancel()
            await asyncio.gather(*watchers, stop_waiter, return_exceptions=True)
    final_attempts = await asyncio.to_thread(attempt_tail.read)
    attempts.extend(final_attempts)
    if args.gateway == "live":
        for row in final_attempts:
            guard.observe(row, sum(p.active for p in participants))
    log_rows.extend(await asyncio.to_thread(log_tail.read))
    if not profile.done():
        profile.cancel()
    try:
        profile_result = await profile
    except asyncio.CancelledError:
        profile_result = {"status": "unavailable", "reason": "step ended before scheduled profile completed"}
    fake = await runtime.fake_stats()
    for timeline in timelines:
        if timeline.get("unexpected_disconnect") or timeline.get("video_error") or timeline.get("driver_error"):
            errors.append("participant transport/video error")
    if guard.stopped:
        errors.append("live safety stop: " + guard.stopped["reason"])
    end = time.time()
    metrics = measure.summarize(
        events, timelines, attempts=attempts, resources=resources, concurrency=concurrency,
        generator_lag_ms=generator_probe.samples, server_lag=measure.server_lag_windows(log_rows, start, end),
        measurement_start=start + (n - 1) * args.arrival + args.warmup,
        measurement_end=min(finished, default=end), close_burst_s=close_burst, artifact_bytes=sizes)
    result = {"concurrency": n, "repeat": repeat, "started": start, "ended": end,
              "metrics": metrics, "errors": errors, "fake_errors": fake["errors"],
              "full_length": args.duration is None or args.duration >= ceiling,
              "live_stop": guard.stopped, "profile": profile_result}
    await asyncio.to_thread((directory / "timelines.json").write_text, json.dumps(timelines, indent=2), encoding="utf-8")
    return result


async def run_experiment(args, *, runtime_factory=Runtime, participant_factory=Participant):
    if args.gateway == "live" and (not args.allow_live or max(args.ramp) > args.max_concurrency):
        raise ValueError("live gateway was not explicitly authorized within its concurrency cap")
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    directory = (args.report_dir or ROOT / "tools" / "load" / "reports" / (stamp + "-" + secrets.token_hex(3))).resolve()
    config = await asyncio.to_thread(lambda: FakeConfig.from_dict(json.loads(args.fake_config.read_text(encoding="utf-8")))
                                     if args.fake_config else FakeConfig())
    env = await asyncio.to_thread(production_environment)
    def preload():
        for scenario in args.scenarios:
            for name in sequences.lines_used(args.steps or sequences.DEFAULT_SEQUENCES[scenario]):
                stim.load(name)
        stim.room_tone(0)
    await asyncio.to_thread(preload)
    ceiling = float(env.get("ENCOUNTER_MAX_SECONDS", "600"))
    live_key = await asyncio.to_thread(load_key) if args.gateway == "live" else None
    runtime = runtime_factory(directory, backend=args.backend, gateway=args.gateway, image=args.image,
                              fake_config=config, live_key=live_key, live_url=args.live_url or env.get("LLM_BASE_URL"))
    report = {"rule": measure.RULE, "generated_at": stamp, "steps": [], "capacity": None,
              "gateway": args.gateway, "tts": "fake", "fake_configuration": asdict(config),
              "configuration": {"ramp": args.ramp, "repeat": args.repeat, "scenarios": args.scenarios,
                                "arrival_spacing_s": args.arrival, "duration_s": args.duration,
                                "warmup_s": args.warmup, "webcam": args.webcam, "custom_steps": args.steps,
                                "default_encounter_ceiling_s": ceiling, "backend": args.backend,
                                "scenario_order": "server Williams run" if set(args.scenarios) == set(SCENARIOS) else "rotating selected scenarios"}}
    try:
        await runtime.start()
        report.update(isolation=runtime.isolation, health=runtime.health, image=runtime.image)
        startup = await asyncio.to_thread(measure.JsonlTail(runtime.data / "upstream-attempts.jsonl").read)
        report["startup_upstream_attempts"] = startup
        if args.gateway == "live":
            guard = measure.LiveGuard()
            for row in startup:
                guard.observe(row, 0)
            if guard.stopped:
                report["live_stop"] = guard.stopped
                raise RuntimeError("live safety guard stopped during startup")
        if args.isolation_only:
            report["isolation_only"] = True
            report["verdict"] = "PASS" if runtime.isolation["passed"] else "FAIL"
        else:
            reference = None
            baseline_valid = True
            for n in args.ramp:
                repeats = []
                for repeat in range(1, args.repeat + 1):
                    step = await run_step(runtime, args, n, repeat, ceiling, participant_factory=participant_factory)
                    repeats.append(step)
                    if step["live_stop"]:
                        report["live_stop"] = step["live_stop"]
                        break
                if n == 1:
                    reference = measure.baseline(repeats)
                    report["baseline"] = reference
                for step in repeats:
                    step.update(measure.verdict(n, step["metrics"], reference,
                        full_length=step["full_length"], errors=step["errors"], fake_errors=step["fake_errors"],
                        baseline_valid=baseline_valid))
                    if step["live_stop"]:
                        if step["verdict"] != "INVALID":
                            step.update(verdict="FAIL", reasons=["live safety stop: " + step["live_stop"]["reason"]])
                    report["steps"].append(step)
                    if step["verdict"] == "FAIL" and "first_failure_profile" not in report:
                        report["first_failure_profile"] = {"concurrency": n, "repeat": step["repeat"], **step["profile"]}
                    print(f"N={n} repeat={step['repeat']}: {step['verdict']} " + "; ".join(step["reasons"]), flush=True)
                if n == 1:
                    baseline_valid = all(step["verdict"] == "PASS" for step in repeats)
                await asyncio.to_thread(_write_report, directory, report)
                if report.get("live_stop") or any(step["fake_errors"] for step in repeats):
                    break
            report["capacity"] = measure.capacity(report["steps"], args.repeat) if baseline_valid else None
            states = {step["verdict"] for step in report["steps"]}
            report["verdict"] = ("FAIL" if "FAIL" in states or report.get("live_stop")
                                 or any(step["fake_errors"] for step in report["steps"])
                                 else "INVALID" if "INVALID" in states else "PASS")
    except (RuntimeError, OSError, ValueError) as exc:
        report.update(verdict="INVALID", fatal_error=type(exc).__name__ + ": " + str(exc))
        if args.gateway == "live":
            guard = measure.LiveGuard()
            for row in await asyncio.to_thread(measure.JsonlTail(runtime.data / "upstream-attempts.jsonl").read):
                guard.observe(row, 0)
            report["live_stop"] = report.get("live_stop") or guard.stopped
        fake_logs = await asyncio.to_thread(measure.JsonlTail(runtime.directory / "fake-log.jsonl").read)
        protocol_errors = []
        for row in fake_logs:
            try:
                event = json.loads(row["line"])
                if event.get("kind") == "protocol_error":
                    protocol_errors.append(event)
            except (ValueError, KeyError):
                pass
        if protocol_errors or report.get("live_stop"):
            report["verdict"] = "FAIL"
            report["fake_protocol_errors"] = protocol_errors
    finally:
        await runtime.stop()
        await asyncio.to_thread(_write_report, directory, report)
    return report, directory


def main(argv=None):
    args = parse_args(argv)
    print(measure.RULE, flush=True)
    report, directory = asyncio.run(run_experiment(args))
    print(str(directory / "report.json"), flush=True)
    return 0 if report["verdict"] == "PASS" else 1 if report["verdict"] == "FAIL" else 2


if __name__ == "__main__":
    raise SystemExit(main())
