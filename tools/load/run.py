"""Run full simulated encounters against one CPU-capped server."""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict, replace
import datetime as dt
import json
import math
import os
from pathlib import Path
import secrets
import time

import httpx

from tools.load import measure
from tools.load.fake_upstream import FakeConfig
from tools.load.participant import Participant, ServerError, error_side, full_sequence, open_identity
from tools.load.runtime import ROOT, Runtime, RuntimeUnavailable, read_encounters
from tools.load.settle import SettlePollError, SettlePoller
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
    parser.add_argument("--profile-image", help="local image containing py-spy; sidecar only")
    parser.add_argument("--continue-after-fail", action="store_true")
    parser.add_argument("--cpu-scale", type=positive, default=1.0, help="multiply the 0.5 host CPU quota")
    parser.add_argument("--fargate-reference", type=Path, help="stored calibration JSON; never contacts AWS")
    parser.add_argument("--tts-factors", help="separate ramps, for example 1.0,3.0; default: fake config factor")
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
    if args.tts_factors is not None:
        try:
            args.tts_factors = [positive(value) for value in args.tts_factors.split(",")]
            if len(set(args.tts_factors)) != len(args.tts_factors):
                raise ValueError("duplicate factors")
        except (ValueError, argparse.ArgumentTypeError):
            parser.error("--tts-factors requires unique finite positive numbers")
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
             "| TTS factor / N | Repeat | Verdict | Played p90 (s) | Achieved N | Generator p99 (ms) | RSS max (MB) | Reasons |",
             "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    for step in report.get("steps", []):
        m = step["metrics"]
        rss = m["server_rss_bytes"]["max"]
        lines.append(f"| {step.get('tts_realtime_factor')} / {step['concurrency']} | {step['repeat']} | {step['verdict']} | "
                     f"{m['speech_end_to_first_played_s']['p90']} | {m['achieved_concurrency']:.2f} | "
                     f"{m['generator_loop_lag_ms']['p99']} | {rss / 1e6 if rss is not None else None} | "
                     + "; ".join(step["reasons"]) + " |")
    lines.extend(["", f"Capacity for the tested configuration on this host: {report.get('capacity_range', report.get('capacity'))}", ""])
    for ramp in report.get("ramps", []):
        if ramp.get("non_monotonic_warning"):
            lines.extend([ramp["non_monotonic_warning"], ""])
    if report.get("fatal_error"):
        lines.extend(["Execution error: " + report["fatal_error"], ""])
    if report.get("live_stop"):
        lines.extend(["Live stop: " + json.dumps(report["live_stop"]), ""])
    if report.get("first_failure_profile"):
        lines.extend(["First failing step profile:", "", "```json",
                      json.dumps(report["first_failure_profile"], indent=2), "```", ""])
    (directory / "report.md").write_text("\n".join(lines), encoding="utf-8")


async def run_step(runtime, args, n, repeat, ceiling, *, participant_factory=Participant, profile_only=False):
    start = time.time()
    directory = runtime.directory / (f"N{n}-profile-only" if profile_only else f"N{n}-repeat{repeat}")
    await asyncio.to_thread(directory.mkdir, exist_ok=True)
    participants, timelines, finished, errors, server_errors, measurement_errors = [], [], [], [], [], []
    resources, concurrency, attempts, log_rows, generator_lag = [], [], [], [], []
    guard, stopped, done, attempts_done = measure.LiveGuard(), asyncio.Event(), asyncio.Event(), asyncio.Event()
    attempt_tail = measure.JsonlTail(runtime.data / "upstream-attempts.jsonl")
    log_tail = measure.JsonlTail(runtime.directory / "server-log.jsonl")
    await asyncio.to_thread(attempt_tail.read)
    await asyncio.to_thread(log_tail.read)
    fake_before = await runtime.fake_stats()
    default_mix = set(args.scenarios) == set(SCENARIOS)
    full_length = args.duration is None or args.duration >= ceiling
    settle_poller = SettlePoller(runtime)

    async def worker(index):
        await asyncio.sleep(index * args.arrival)
        client = await asyncio.to_thread(httpx.AsyncClient, timeout=30, follow_redirects=False, trust_env=False)
        async with client as http:
            identity = await open_identity(http, runtime.base, runtime.key, f"N{n}_{repeat}_{index}")
            run_id, view = identity["run"], None
            if default_mix:
                response = await http.get(runtime.base + f"/api/run/{run_id}", params={"key": runtime.key})
                response.raise_for_status()
                view = response.json()
            for slot in range(len(args.scenarios)):
                scenario = view["current"]["id"] if default_mix else args.scenarios[(slot + index + repeat - 1) % len(args.scenarios)]
                if scenario not in args.scenarios:
                    raise RuntimeError("server Williams run selected an unexpected scenario")
                participant = participant_factory(
                    runtime.base, scenario, args.steps or full_sequence(scenario, ceiling),
                    index=f"{n}_{repeat}_{index}_{slot}", key=runtime.key,
                    duration_s=args.duration or ceiling + 30, webcam=args.webcam, http=http, identity=identity)
                participants.append(participant)
                try:
                    timeline = await participant.run()
                finally:
                    if participant.tl.get("session_id"):
                        timelines.append(participant.tl)
                if not timeline.get("completed"):
                    if full_length:
                        server_errors.append("encounter did not reach encounter_complete")
                    break
                if default_mix:
                    if not await settle_poller.wait(timeline["session_id"], args.close_timeout):
                        raise ServerError("session/mix close timeout before run advancement")
                    response = await http.post(runtime.base + f"/api/run/{run_id}/advance",
                                               params={"session_id": timeline["session_id"], "key": runtime.key})
                    response.raise_for_status()
                    view = response.json()
        finished.append(time.time())

    async def kill_and_stop():
        # Stop billed work before cancellation or storage settling can wait.
        await runtime.kill_server()
        stopped.set()

    async def check_observer():
        if args.gateway != "live":
            return False
        status = runtime.observer_telemetry()
        if not status["failure"]:
            return False
        if guard.stopped is None:
            guard.stopped = {"reason": "attempt telemetry unavailable", "detail": status["failure"],
                             "observer": status, "concurrency": sum(p.active for p in participants),
                             "t": time.time()}
        if not stopped.is_set():
            await kill_and_stop()
        return True

    async def watch_attempts():
        while not attempts_done.is_set():
            if await check_observer():
                return
            try:
                rows = await asyncio.to_thread(attempt_tail.read)
            except (OSError, ValueError) as exc:
                errors.append("attempt telemetry unavailable: " + type(exc).__name__)
                if args.gateway == "live":
                    guard.stopped = {"reason": "attempt telemetry unavailable", "concurrency": n}
                    await kill_and_stop()
                else:
                    stopped.set()
                return
            attempts.extend(rows)
            if args.gateway == "live":
                for row in rows:
                    if guard.observe(row, sum(p.active for p in participants)):
                        await kill_and_stop()
                        return
            await asyncio.sleep(0.1)

    async def sample_concurrency():
        due = time.monotonic()
        while not done.is_set():
            concurrency.append({"t": time.time(), "active": sum(p.active for p in participants),
                                "conversing": sum(p.active and p.tl.get("conversation_started") is not None for p in participants)})
            due += 1.0
            await asyncio.sleep(max(0, due - time.monotonic()))

    async def sample_resources():
        due = time.monotonic()
        while not done.is_set():
            try:
                resources.append(await runtime.resources())
                log_rows.extend(await asyncio.to_thread(log_tail.read))
            except (RuntimeError, OSError, ValueError) as exc:
                if not stopped.is_set():
                    measurement_errors.append("resource sampling: " + type(exc).__name__)
            due += 1.0
            await asyncio.sleep(max(0, due - time.monotonic()))

    async def sample_lag():
        while True:
            before = time.perf_counter()
            await asyncio.sleep(0.05)
            generator_lag.append({"t": time.time(), "lag_ms": max(0, (time.perf_counter() - before - 0.05) * 1000)})

    workers = [asyncio.create_task(worker(index)) for index in range(n)]
    async def workers_done():
        for result in await asyncio.gather(*workers, return_exceptions=True):
            if isinstance(result, SettlePollError):
                measurement_errors.append("settle polling: " + str(result))
            elif isinstance(result, Exception):
                (server_errors if error_side(result) == "server_errors" else errors).append(
                    "participant worker: " + type(result).__name__ + ": " + str(result)[:100])
    all_workers = asyncio.create_task(workers_done())
    stop_waiter = asyncio.create_task(stopped.wait())
    watchers = [asyncio.create_task(watch_attempts()), asyncio.create_task(sample_concurrency()),
                asyncio.create_task(sample_resources()), asyncio.create_task(sample_lag())]
    profile = (asyncio.create_task(runtime.profile(directory, delay=args.warmup, seconds=args.profile_seconds))
               if profile_only else None)
    try:
        await asyncio.wait({all_workers, stop_waiter}, return_when=asyncio.FIRST_COMPLETED)
        if stopped.is_set():
            for task in workers:
                task.cancel()
        await all_workers
    finally:
        done.set()
        stop_waiter.cancel()
        for task in watchers[1:]:
            task.cancel()
        await asyncio.gather(*watchers[1:], stop_waiter, return_exceptions=True)
    measurement_end = min(finished, default=time.time())
    last_hangup = max((t.get("hangup_requested") or measurement_end for t in timelines), default=measurement_end)
    settled, close_burst = False, None
    close_waiter = close_stop = None
    try:
        await check_observer()
        if timelines and not stopped.is_set():
            close_waiter = asyncio.gather(*(settle_poller.wait(sid, args.close_timeout)
                for sid in dict.fromkeys(t["session_id"] for t in timelines)), return_exceptions=True)
            close_stop = asyncio.create_task(stopped.wait())
            await asyncio.wait({close_waiter, close_stop}, return_when=asyncio.FIRST_COMPLETED)
            if close_waiter.done():
                results = close_waiter.result()
                poll_errors = [str(result) for result in results if isinstance(result, Exception)]
                measurement_errors.extend("settle polling: " + error for error in poll_errors)
                settled = all(result is True for result in results)
                if settled:
                    close_burst = max(0, time.time() - last_hangup)
                elif not poll_errors and not stopped.is_set():
                    server_errors.append("session/mix close timeout")
            await check_observer()
    finally:
        for task in (close_waiter, close_stop):
            if task is not None:
                task.cancel()
        await asyncio.gather(*(task for task in (close_waiter, close_stop) if task is not None), return_exceptions=True)
        await settle_poller.close()
        attempts_done.set()
        watchers[0].cancel()
        await asyncio.gather(watchers[0], return_exceptions=True)
    final_attempts = await asyncio.to_thread(attempt_tail.read)
    attempts.extend(final_attempts)
    if args.gateway == "live":
        for row in final_attempts:
            if guard.observe(row, 0) and not stopped.is_set():
                await kill_and_stop()
    log_rows.extend(await asyncio.to_thread(log_tail.read))
    if profile:
        if not profile.done():
            profile.cancel()
        try:
            profile_result = await profile
        except asyncio.CancelledError:
            profile_result = {"status": "unavailable", "reason": "profile-only repeat too short"}
    else:
        profile_result = None
    await runtime.export(timelines)
    # Parse all events once, after close polling and with the lag probe stopped.
    events, sizes, _ = await asyncio.to_thread(read_encounters, runtime.data, timelines)
    fake = await runtime.fake_stats()
    for participant in participants:
        errors.extend(participant.tl.get("generator_errors", []))
        server_errors.extend(participant.tl.get("server_errors", []))
    if guard.stopped:
        server_errors.append("live safety stop: " + guard.stopped["reason"])
    if not server_errors:
        errors.extend(measurement_errors)
    end = time.time()
    metrics = measure.summarize(
        events, timelines, attempts=attempts, resources=resources, concurrency=concurrency,
        generator_lag_ms=generator_lag, server_lag=measure.server_lag_windows(log_rows, start, end),
        measurement_start=start + (n - 1) * args.arrival + args.warmup,
        measurement_end=measurement_end, close_burst_s=close_burst, artifact_bytes=sizes)
    metrics.update(encounter_count=max(len(participants), n), server_error_count=len(server_errors),
                   server_errors_per_100_encounters=100 * len(server_errors) / max(len(participants), n),
                   unmatched_fragment=fake.get("unmatched_fragment", 0) - fake_before.get("unmatched_fragment", 0))
    result = {"concurrency": n, "repeat": repeat, "started": start, "ended": end,
              "metrics": metrics, "errors": errors, "server_errors": server_errors,
              "measurement_errors": measurement_errors,
              "fake_errors": fake["errors"][len(fake_before["errors"]):], "full_length": full_length,
              "live_stop": guard.stopped, "profile": profile_result, "profile_only": profile_only}
    result["observer_telemetry"] = runtime.observer_telemetry()
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
    factors = args.tts_factors or [config.tts_realtime_factor]
    report = {"rule": measure.RULE, "generated_at": stamp, "steps": [], "ramps": [], "capacity": None,
              "gateway": args.gateway, "tts": "fake", "fake_configuration": asdict(config),
              "capacity_scope": "this host; fake timing and speaker/tool probabilities are uncalibrated assumptions",
              "configuration": {"ramp": args.ramp, "repeat": args.repeat, "scenarios": args.scenarios,
                                "arrival_spacing_s": args.arrival, "duration_s": args.duration,
                                "warmup_s": args.warmup, "webcam": args.webcam, "custom_steps": args.steps,
                                "cpu_scale": args.cpu_scale, "tts_factors": factors,
                                "continue_after_fail": args.continue_after_fail,
                                "default_encounter_ceiling_s": ceiling, "backend": args.backend,
                                "scenario_order": "server Williams run" if set(args.scenarios) == set(SCENARIOS) else "rotating selected scenarios"}}
    for factor in factors:
        runtime = runtime_factory(directory / f"tts-{factor:g}", backend=args.backend, gateway=args.gateway, image=args.image,
                                  fake_config=replace(config, tts_realtime_factor=factor), live_key=live_key,
                                  live_url=args.live_url or env.get("LLM_BASE_URL"), cpu_scale=args.cpu_scale,
                                  fargate_reference=args.fargate_reference, profile_image=args.profile_image)
        ramp = {"tts_realtime_factor": factor, "steps": [], "capacity": None}
        report["ramps"].append(ramp)
        try:
            await runtime.start()
            ramp.update(isolation=runtime.isolation, health=runtime.health, image=runtime.image, host=runtime.metadata)
            report.update(isolation=runtime.isolation, host=runtime.metadata)
            startup = await asyncio.to_thread(measure.JsonlTail(runtime.data / "upstream-attempts.jsonl").read)
            ramp["startup_upstream_attempts"] = startup
            if args.gateway == "live":
                guard = measure.LiveGuard()
                for row in startup:
                    guard.observe(row, 0)
                if guard.stopped:
                    report["live_stop"] = guard.stopped
                    await runtime.kill_server()
                    raise RuntimeError("live safety guard stopped during startup")
            if args.isolation_only:
                report["isolation_only"] = True
                report["verdict"] = "PASS" if runtime.isolation["passed"] else "FAIL"
                break
            reference, baseline_valid = None, True
            for n in args.ramp:
                repeats = []
                for repeat in range(1, args.repeat + 1):
                    step = await run_step(runtime, args, n, repeat, ceiling, participant_factory=participant_factory)
                    step["tts_realtime_factor"] = factor
                    repeats.append(step)
                    if step["live_stop"]:
                        report["live_stop"] = step["live_stop"]
                        break
                    preliminary = measure.verdict(n, step["metrics"], measure.baseline(repeats) if n == 1 else reference,
                        full_length=step["full_length"], errors=step["errors"], server_errors=step.get("server_errors", []),
                        fake_errors=step["fake_errors"], baseline_valid=baseline_valid)
                    if step["fake_errors"] or (preliminary["verdict"] == "FAIL" and not args.continue_after_fail):
                        break
                if n == 1:
                    reference = measure.baseline(repeats)
                    ramp["baseline"] = reference
                for step in repeats:
                    step.update(measure.verdict(n, step["metrics"], reference,
                        full_length=step["full_length"], errors=step["errors"], server_errors=step.get("server_errors", []),
                        fake_errors=step["fake_errors"], baseline_valid=baseline_valid))
                    if step["live_stop"] and not step["errors"]:
                        step.update(verdict="FAIL", reasons=["live safety stop: " + step["live_stop"]["reason"]])
                    report["steps"].append(step)
                    ramp["steps"].append(step)
                    print(f"TTS={factor:g} N={n} repeat={step['repeat']}: {step['verdict']} " + "; ".join(step["reasons"]), flush=True)
                if n == 1:
                    baseline_valid = all(step["verdict"] == "PASS" for step in repeats)
                if any(step["verdict"] == "FAIL" for step in repeats) and "first_failure_profile" not in report:
                    profile = {"concurrency": n, "tts_realtime_factor": factor, "excluded_from_verdict": True}
                    report["first_failure_profile"] = profile
                    if report.get("live_stop"):
                        profile.update(status="unavailable", reason="live guard killed the server")
                    elif await runtime.profile_available():
                        extra = await run_step(runtime, args, n, 0, ceiling,
                                               participant_factory=participant_factory, profile_only=True)
                        profile.update(extra["profile"] or {}, repeat=extra)
                        if extra["live_stop"]:
                            report["live_stop"] = extra["live_stop"]
                    else:
                        profile.update(status="unavailable", reason="no py-spy image (--profile-image) or local binary")
                await asyncio.to_thread(_write_report, directory, report)
                if (report.get("live_stop") or any(step["fake_errors"] for step in repeats)
                        or (not args.continue_after_fail and any(step["verdict"] == "FAIL" for step in repeats))):
                    break
            ramp["capacity"] = measure.capacity(ramp["steps"], args.repeat)
            ramp["non_monotonic_warning"] = measure.non_monotonic(ramp["steps"])
        except (RuntimeError, OSError, ValueError) as exc:
            report.update(verdict="INVALID", fatal_error=type(exc).__name__ + ": " + str(exc))
            if args.gateway == "live":
                guard = measure.LiveGuard()
                for row in await asyncio.to_thread(measure.JsonlTail(runtime.data / "upstream-attempts.jsonl").read):
                    guard.observe(row, 0)
                report["live_stop"] = report.get("live_stop") or guard.stopped
                if report.get("live_stop"):
                    await runtime.kill_server()
                    report["verdict"] = "FAIL"
            break
        finally:
            await runtime.stop()
            await asyncio.to_thread(_write_report, directory, report)
        if report.get("live_stop"):
            break
    if not report.get("fatal_error") and not args.isolation_only:
        values = [r["capacity"] for r in report["ramps"]]
        report["capacity"] = values[0] if len(values) == 1 else None
        report["capacity_by_tts_factor"] = {str(r["tts_realtime_factor"]): r["capacity"] for r in report["ramps"]}
        report["capacity_range"] = [min(values), max(values)] if values and all(v is not None for v in values) else None
        report["capacity_range_complete"] = len(values) == len(factors) and all(v is not None for v in values)
        states = {step["verdict"] for step in report["steps"]}
        report["verdict"] = ("FAIL" if "FAIL" in states or report.get("live_stop")
                             else "INVALID" if "INVALID" in states else "PASS")
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
