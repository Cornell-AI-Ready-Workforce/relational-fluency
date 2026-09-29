"""Pre-deploy sim check: drive this build through the four default encounters.

    python -m tools.sim.check                       # all four, local server
    python -m tools.sim.check --scenarios S2A,S4A
    python -m tools.sim.check --server http://127.0.0.1:8797 --build 4798e64

Run by hand before a deploy (docs/OPERATIONS.md, "Releasing a build"). It needs
the model gateway, so it cannot run in CI. By default it starts its own server
from this checkout (tools/sim/serve.py: production's configuration, archiving
off, sessions in a temporary directory), plays each scenario's default sequence
(tools/sim/sequences.py) through the participant protocol (tools/sim/driver.py),
reads each session's events back through the researcher API, summarizes them
(tools/sim/analyze.py) and compares them with tools/sim/baseline.json. The
report is written to tools/sim/reports/<build>.json; tools/deploy.sh warns
when the build it is about to plan has no passing report there.

Which build a report is for. With --build, that. Otherwise, the tag pinned in
infra/terraform/terraform.tfvars when this checkout's image contents (what the
Dockerfile copies: server/, static/, scenarios/, requirements.txt, and the
Dockerfile) are identical to that commit's, which is the case on main right
after a pin PR; otherwise this checkout's HEAD. A checkout with uncommitted
changes to those paths is not any build, and its report is named <sha>-dirty so
it can never pass for one.

Exit code 0 only when every scenario ran and passed.
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import hashlib
import http.client
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional, Tuple

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools import pinned_image  # noqa: E402
from tools.sim import analyze, driver, sequences, stim  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]
SIM_DIR = REPO_ROOT / "tools" / "sim"
BASELINE = SIM_DIR / "baseline.json"
REPORTS = SIM_DIR / "reports"

# What the image is made of (the Dockerfile's COPY lines, plus the Dockerfile).
# tests/test_sim_analysis.py holds this to the Dockerfile.
IMAGE_PATHS = ("Dockerfile", "requirements.txt", "server", "static", "scenarios")

_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=REPO_ROOT, capture_output=True,
                          text=True, encoding="utf-8", check=True).stdout.strip()


def image_dirty() -> bool:
    return bool(_git("status", "--porcelain", "--", *IMAGE_PATHS))


def default_build() -> Tuple[str, str]:
    """(build, why) for a report run from this checkout."""
    head = _git("rev-parse", "--short", "HEAD")
    if image_dirty():
        return f"{head}-dirty", "the checkout has uncommitted changes to the image's files"
    try:
        tag = pinned_image.read().tag
        same = subprocess.run(["git", "diff", "--quiet", tag, "HEAD", "--", *IMAGE_PATHS],
                              cwd=REPO_ROOT, capture_output=True).returncode == 0
    except (OSError, pinned_image.PinError, subprocess.CalledProcessError):
        same, tag = False, ""
    if same:
        return tag, f"HEAD's image contents are identical to the pinned tag {tag}"
    return head, "HEAD (its image contents differ from the pinned tag)"


def sequence_fingerprint(steps: str) -> str:
    """What a baseline was recorded against: the steps and every byte of the
    stimulus they say. A changed line or a re-generated stimulus file makes
    the old numbers a comparison with a different experiment."""
    h = hashlib.sha256(steps.encode("utf-8"))
    for name in sequences.lines_used(steps):
        h.update(name.encode("utf-8"))
        h.update(stim.load(name))
    return h.hexdigest()[:16]


def _read_key() -> str:
    """SESSION_KEY from the repository's .env. Returned, never printed."""
    from dotenv import dotenv_values

    return (dotenv_values(REPO_ROOT / ".env").get("SESSION_KEY") or
            os.environ.get("SESSION_KEY") or "").strip()


def _get_json(url: str, timeout: float = 15.0):
    req = urllib.request.Request(url, headers={"User-Agent": driver.USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def wait_healthy(server: str, proc: Optional[subprocess.Popen], limit: float = 90.0) -> dict:
    dl = time.time() + limit
    while time.time() < dl:
        if proc is not None and proc.poll() is not None:
            raise RuntimeError(f"the sim server exited with code {proc.returncode} "
                               f"before answering /health")
        try:
            return _get_json(f"{server}/health", timeout=5)
        except (urllib.error.URLError, OSError, ValueError):
            time.sleep(1.0)
    raise RuntimeError(f"{server}/health did not answer within {limit:.0f} s")


def fetch_events(server: str, session_id: str, key: str, limit: float = 60.0) -> List[dict]:
    """The session's events.jsonl through the researcher download route,
    waiting for session_end: the runner writes the last events after the
    socket closes."""
    q = urllib.parse.urlencode({"key": key}) if key else ""
    url = f"{server}/api/sessions/{session_id}/download/events.jsonl" + (f"?{q}" if q else "")
    dl, events = time.time() + limit, []
    while time.time() < dl:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": driver.USER_AGENT})
            with urllib.request.urlopen(req, timeout=15) as resp:
                text = resp.read().decode("utf-8")
            events = [json.loads(ln) for ln in text.splitlines() if ln.strip()]
            if any(e.get("type") == "session_end" for e in events):
                return events
        # http.client.HTTPException too: the route is a FileResponse, and a
        # file the runner is still appending to outgrows the Content-Length
        # it was served with, so the server aborts the body and the read ends
        # in IncompleteRead. That is not an OSError, and on 2026-09-28 it
        # reported a finished S3A as "did not run"; it is the same "not yet"
        # as a missing session_end, so ask again.
        except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError):
            pass
        time.sleep(2.0)
    if events:
        return events
    raise RuntimeError(f"could not read events for {session_id} from the server")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def load_baseline(path: Path = BASELINE) -> dict:
    if not path.is_file():
        return {"scenarios": {}}
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data: dict) -> None:
    """Committed artefacts: UTF-8, LF, sorted keys, so a re-run diffs cleanly
    on every platform."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=1, sort_keys=True, ensure_ascii=False) + "\n",
                    encoding="utf-8", newline="")


def build_report(build: str, why: str, server: str, health: dict,
                 results: Dict[str, dict], baseline: dict) -> dict:
    """The report: every scenario's metrics and checks, and one verdict."""
    failures = []
    model = ((health.get("gateway") or {}).get("realtime_model"))
    want_model = baseline.get("realtime_model")
    model_ok = not want_model or model == want_model
    if not model_ok:
        failures.append(f"server runs {model}, the baseline was recorded on {want_model}: "
                        f"the comparison means nothing")
    # The pipeline and room pacing the server runs, beside those each baseline
    # was recorded on (the entry's own `recorded`, else the file's). A new
    # version is a different experiment: 28a runs S2A to the 720 s ceiling
    # where 24c ended it about 451 s in, and the absolute-count limits below
    # (phantom_turns <= base+1, voice_error <= base, ...) were calibrated on
    # the shorter run. The sequence fingerprint cannot see that, so this is
    # the model check's twin, per scenario (tools/sim/README.md: a new
    # PIPELINE_VERSION is re-recorded in the same PR).
    gw = health.get("gateway") or {}
    running = {k: gw.get(k) for k in ("pipeline_version", "room_pacing_version")}
    scen = {}
    for sid, res in results.items():
        if "error" in res:
            scen[sid] = {"passed": False, "error": res["error"]}
            failures.append(f"{sid}: did not run: {res['error']}")
            continue
        base = baseline.get("scenarios", {}).get(sid)
        verdict = analyze.compare(res["metrics"], base, baseline.get("tolerances"))
        pending = (baseline.get("pending") or {}).get(sid)
        if base is None and pending:
            verdict["checks"][0]["note"] = f"no baseline yet: {pending}"
        rec = (base or {}).get("recorded") or baseline.get("recorded") or {}
        moved = {k: (rec.get(k), v) for k, v in running.items()
                 if base and v and rec.get(k) and rec.get(k) != v}
        if moved:
            verdict["passed"] = False
            verdict["checks"].append({
                "metric": "versions", "ok": False,
                "value": {k: now for k, (_, now) in moved.items()},
                "baseline": {k: then for k, (then, _) in moved.items()},
                "note": "the baseline was recorded on "
                        + ", ".join(f"{k} {then}" for k, (then, _) in moved.items())
                        + "; the server runs "
                        + ", ".join(f"{now}" for _, now in moved.values())
                        + ": a different experiment, so re-record it (tools/sim/README.md)"})
        want_seq = (base or {}).get("sequence_sha256")
        if want_seq and res.get("sequence_sha256") and want_seq != res["sequence_sha256"]:
            verdict["passed"] = False
            verdict["checks"].append({
                "metric": "sequence", "ok": False, "value": res["sequence_sha256"],
                "baseline": want_seq,
                "note": "the sequence or its stimulus changed since the baseline was "
                        "recorded; re-record it (tools/sim/README.md)"})
        scen[sid] = {"passed": verdict["passed"], "session_id": res["metrics"]["session_id"],
                     "metrics": res["metrics"], "checks": verdict["checks"]}
        for c in verdict["checks"]:
            if c["ok"]:
                continue
            if c["metric"] in ("baseline", "sequence", "versions"):
                failures.append(f"{sid}: {c['note']}")
            else:
                failures.append(f"{sid}: {c['metric']} {c.get('value')} against limit "
                                f"{c.get('limit')} (baseline {c.get('baseline')})"
                                + (f"; {c['note']}" if c.get("note") else ""))
    return {
        "build": build,
        "build_from": why,
        "passed": bool(results) and not failures,
        "generated_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "server": server,
        "health": {"build": health.get("build"), "realtime_model": model,
                   "pipeline_version": (health.get("gateway") or {}).get("pipeline_version"),
                   "room_pacing_version": (health.get("gateway") or {}).get("room_pacing_version")},
        "baseline": {"recorded": baseline.get("recorded"), "realtime_model": want_model},
        "scenarios": scen,
        "failures": failures,
    }


def _print_summary(report: dict, out=sys.stdout) -> None:
    for sid, s in report["scenarios"].items():
        if "error" in s:
            print(f"  {sid}: DID NOT RUN ({s['error']})", file=out)
            continue
        m = s["metrics"]
        lat = m["speech_end_to_first_played_s"]
        print(f"  {sid}: {'pass' if s['passed'] else 'FAIL'}  said {m['lines_said']} "
              f"heard {m['lines_heard']} answered {m['lines_answered']}  "
              f"phantom {m['phantom_turns']}  refused {m['refused_creates']}  "
              f"voice_error {m['voice_error']}  reply_missing {m['reply_missing']}  "
              f"triggers {m['triggers_fired']}  speech_end->played p50 {lat['p50']} "
              f"p90 {lat['p90']} s (n={lat['n']})", file=out)
    for f in report["failures"]:
        print(f"    - {f}", file=out)
    print(f"  {'PASSED' if report['passed'] else 'FAILED'}: build {report['build']}", file=out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--scenarios", default=",".join(sequences.DEFAULT_SEQUENCES),
                    help="comma-separated (default: all four)")
    ap.add_argument("--server", help="use this running server instead of starting one")
    ap.add_argument("--allow-remote", action="store_true",
                    help="allow --server to be a non-local host (it will record internal "
                         "sessions there and spend its gateway budget)")
    ap.add_argument("--build", help="the build this report is for (default: see above)")
    ap.add_argument("--port", type=int, help="port for the server this starts (default: a free one)")
    ap.add_argument("--data-dir", type=Path, help="DATA_DIR for the server this starts")
    ap.add_argument("--steps", action="append", default=[], metavar="SID=STEPS",
                    help="override one scenario's sequence (the report then cannot pass)")
    ap.add_argument("--write-baseline", action="store_true",
                    help="record this run as tools/sim/baseline.json (keeps its tolerances)")
    ap.add_argument("--no-report", action="store_true", help="do not write a report file")
    args = ap.parse_args(argv)

    scenarios = [s.strip() for s in args.scenarios.split(",") if s.strip()]
    overrides = dict(s.split("=", 1) for s in args.steps)
    unknown = [s for s in scenarios if s not in sequences.DEFAULT_SEQUENCES and s not in overrides]
    if unknown:
        ap.error(f"no default sequence for {unknown}; pass --steps SID=...")
    if overrides and args.write_baseline:
        ap.error("--write-baseline records the DEFAULT sequences; it cannot take --steps")

    key = _read_key()
    proc, server = None, args.server
    if server:
        host = urllib.parse.urlparse(server).hostname or ""
        if host not in _LOCAL_HOSTS and not args.allow_remote:
            ap.error(f"{host} is not this machine. The check tests a build BEFORE it is "
                     f"deployed, so it runs against a local server; pass --allow-remote "
                     f"to drive {host} anyway")
    if args.build:
        build, why = args.build, "--build"
    elif server:
        build, why = "", ""
    else:
        build, why = default_build()

    log_path = None
    try:
        if not server:
            port = args.port or _free_port()
            data_dir = args.data_dir or Path(tempfile.mkdtemp(prefix="rf-sim-"))
            data_dir.mkdir(parents=True, exist_ok=True)
            log_path = data_dir / "server.log"
            server = f"http://127.0.0.1:{port}"
            sha = build.split("-")[0]
            print(f"starting a sim server from this checkout on {server} "
                  f"(sessions and server.log in {data_dir})", flush=True)
            with open(log_path, "a", encoding="utf-8") as logf:
                proc = subprocess.Popen(
                    [sys.executable, "-m", "tools.sim.serve", "--port", str(port),
                     "--data-dir", str(data_dir), "--build", sha],
                    cwd=REPO_ROOT, stdout=logf, stderr=subprocess.STDOUT)
        health = wait_healthy(server, proc)
        if not build:
            if not health.get("build"):
                ap.error(f"{server}/health reports no build; pass --build <sha> for the "
                         f"commit that server is running")
            build, why = health["build"], f"{server}/health"
        gw = health.get("gateway") or {}
        if not gw.get("ok"):
            raise RuntimeError(f"the server cannot reach the gateway: {gw.get('detail') or gw}")
        print(f"build {build} ({why}); {gw.get('realtime_model')}, pipeline "
              f"{gw.get('pipeline_version')}, room pacing {gw.get('room_pacing_version')}",
              flush=True)

        results: Dict[str, dict] = {}
        for sid in scenarios:
            steps = overrides.get(sid, sequences.DEFAULT_SEQUENCES.get(sid))
            print(f"\n== {sid}", flush=True)
            try:
                tl = asyncio.run(driver.run(server, sid, steps, key=key,
                                            tester=f"sim_{build}"[:24],
                                            log=lambda s: print(s, flush=True)))
                if not tl["lines"]:
                    # Nothing was said, so there is nothing to measure, and a
                    # row of zeros must not read as a clean run (or be
                    # recorded as a baseline).
                    reason = "; ".join(tl["error_frames"][:2]) or tl.get("closed_by_server") \
                        or "the session ended before the first line"
                    raise RuntimeError(f"the encounter did not run: {reason}")
                events = fetch_events(server, tl["session_id"], key)
                results[sid] = {"metrics": analyze.summarize(events, tl),
                                "sequence_sha256": sequence_fingerprint(steps)}
                if log_path is not None:
                    # Beside the sessions, for whoever has to explain a failure:
                    # what was said when, to line up with events.jsonl.
                    write_json(log_path.parent / f"{sid}.timeline.json", tl)
            except Exception as exc:  # noqa: BLE001 - one scenario may not sink the rest
                results[sid] = {"error": f"{exc.__class__.__name__}: {exc}"[:300]}
        baseline = load_baseline()
        report = build_report(build, why, server, health, results, baseline)
        if overrides:
            report["passed"] = False
            report["failures"].append("sequences were overridden with --steps: not comparable")
        print("", flush=True)
        _print_summary(report)
        if args.write_baseline:
            ok = {s: r for s, r in results.items() if "metrics" in r}
            base = load_baseline()
            base.setdefault("tolerances", dict(analyze.DEFAULT_TOLERANCES))
            base["realtime_model"] = (health.get("gateway") or {}).get("realtime_model")
            # Per scenario as well as overall: a baseline re-recorded for two
            # scenarios keeps the other two from an earlier run, and each entry
            # has to say which run it came from.
            recorded = {"build": build, "date": report["generated_at"][:10],
                        "pipeline_version": report["health"]["pipeline_version"],
                        "room_pacing_version": report["health"]["room_pacing_version"]}
            base["recorded"] = recorded
            base.setdefault("scenarios", {})
            for s, r in ok.items():
                base["scenarios"][s] = dict(analyze.baseline_entry(r["metrics"]),
                                            sequence_sha256=r["sequence_sha256"],
                                            recorded=dict(recorded, session_id=r["metrics"]["session_id"]))
                (base.get("pending") or {}).pop(s, None)
            if not base.get("pending"):
                base.pop("pending", None)
            write_json(BASELINE, base)
            print(f"  baseline written for {sorted(ok)}: {BASELINE.relative_to(REPO_ROOT)}")
        if not args.no_report:
            out = REPORTS / f"{build}.json"
            write_json(out, report)
            print(f"  report: {out.relative_to(REPO_ROOT)}")
        return 0 if report["passed"] else 1
    finally:
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                proc.kill()
            print(f"  sim server stopped (log: {log_path})", flush=True)


if __name__ == "__main__":
    sys.exit(main())
