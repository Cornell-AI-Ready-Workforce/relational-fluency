"""The sim check's reports, and the judge's results, as tables a dashboard can read.

    python -m tools.sim.dashboard_data OUT_DIR
    python -m tools.sim.dashboard_data OUT_DIR --encounters judge.json --selftest selftest.json

Every sim check leaves a report in tools/sim/reports, one JSON file per build,
and nothing reads them side by side: whether the wait before a reply crept up
over five releases, or the beats made fell, is a question about sixteen files.
This flattens them into CSV, one row per scenario per run (sim_runs.csv) and
one row per thing the judge found (findings.csv), which is the form the QA
dashboard is fed.

--encounters takes the file `python -m tools.sim.judge ... --json` writes
(recorded encounters, judged) and --selftest the one
`python -m tools.sim.judge_selftest --json` writes; each becomes a table of
its own (encounters.csv, selftest.csv).

What is left out on purpose: no line of a recorded encounter is written, only
where it is (session, turn, character). A sim run's lines are a simulated
participant's and a character's; a recorded encounter's may be a person's.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import re
import sys
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

REPORTS = Path(__file__).resolve().parent / "reports"

SIM_COLUMNS = (
    "key", "run", "run_at", "day", "build", "model", "pipeline", "scenario",
    "said", "heard", "answered", "answered_pct", "phantom", "voice_error", "reply_missing",
    "triggers", "wait_p50", "wait_p90",
    "judged", "verdict", "beats_made", "beats_judged", "beats_part", "beats_lost", "made_pct",
    "fact", "role", "safe", "hard", "rule", "loop", "stock", "disputed",
    "esci_met", "esci_of", "judge_calls", "judge_tokens")
FINDING_COLUMNS = ("id", "run", "day", "scenario", "kind", "fails", "turn", "character",
                   "trigger", "line", "against")
ENCOUNTER_COLUMNS = ("session", "day", "scenario", "build", "verdict", "beats_made",
                     "beats_judged", "beats_lost", "fact", "role", "safe", "hard", "rule",
                     "loop", "stock", "disputed", "esci_met", "esci_of", "note")
SELFTEST_COLUMNS = ("defect", "planted", "found", "false_alarms", "encounters", "lines",
                    "rubric", "model")

# How a judge code reads on a page: the three that fail a scenario keep their
# capitals, the counted ones are lower case, as in the judge's own line.
_KIND = {"FACT": "FACT", "ROLE": "ROLE", "SAFE": "SAFE", "RULE": "rule", "LOOP": "loop",
         "STOCK": "stock"}
_HARD = ("FACT", "ROLE", "SAFE")


def _pct(num, den) -> Optional[float]:
    return round(100 * num / den, 1) if den else None


def _judge_cells(j: Optional[dict]) -> dict:
    """The judge's part of a row; blank where there was no verdict."""
    if not j or j.get("error"):
        return {"judged": 0, "verdict": "no verdict" if j else ""}
    b, c = j["beats"], j["counts"]
    made, judged = b["y"] + b["p"], b["y"] + b["p"] + b["n"]
    return {"judged": 1, "verdict": "pass" if j["passed"] else "FAIL",
            "beats_made": made, "beats_judged": judged, "beats_part": b["p"],
            "beats_lost": b["x"], "made_pct": _pct(made, judged),
            "fact": c["FACT"], "role": c["ROLE"], "safe": c["SAFE"],
            "hard": sum(c[k] for k in _HARD), "rule": c["RULE"], "loop": c["LOOP"],
            "stock": c["STOCK"], "disputed": len(j.get("disputed") or []),
            "esci_met": j["chances"]["met"], "esci_of": j["chances"]["of"],
            "judge_calls": j["usage"]["calls"],
            "judge_tokens": j["usage"]["in"] + j["usage"]["out"]}


def sim_rows(reports: Dict[str, dict]) -> List[dict]:
    """One row per scenario that ran, per report, oldest run first. `reports`
    is {name: report}: the name is the file's, since a re-run of one scenario
    is a report of its own beside the build's."""
    rows = []
    for name, r in reports.items():
        health = r.get("health") or {}
        for sid, s in (r.get("scenarios") or {}).items():
            m = s.get("metrics")
            if not m:
                continue                       # it did not run: no numbers to chart
            lat = m.get("speech_end_to_first_played_s") or {}
            rows.append({
                "key": f"{name}|{sid}", "run": name, "run_at": r.get("generated_at"),
                "day": (r.get("generated_at") or "")[:10], "build": r.get("build"),
                "model": health.get("realtime_model"),
                "pipeline": health.get("pipeline_version"), "scenario": sid,
                "said": m.get("lines_said"), "heard": m.get("lines_heard"),
                "answered": m.get("lines_answered"),
                "answered_pct": _pct(m.get("lines_answered") or 0, m.get("lines_heard")),
                "phantom": m.get("phantom_turns"), "voice_error": m.get("voice_error"),
                "reply_missing": m.get("reply_missing"), "triggers": m.get("triggers_fired"),
                "wait_p50": lat.get("p50"), "wait_p90": lat.get("p90"),
                **_judge_cells(s.get("judge"))})
    return sorted(rows, key=lambda x: (x["run_at"] or "", x["run"], x["scenario"]))


def _found(j: dict, with_lines: bool) -> Iterable[dict]:
    """What one verdict found: its flags, the ones readings disputed, and the
    beats not made."""
    for f in j.get("flags") or []:
        yield {"kind": _KIND.get(f["c"], f["c"]), "fails": "yes" if f["c"] in _HARD else "",
               "turn": f["i"], "character": f["agent"], "trigger": "",
               "line": f["line"] if with_lines else "",
               "against": (f.get("source") or f.get("q") or "") if with_lines else ""}
    for f in j.get("disputed") or []:
        yield {"kind": f"disputed {_KIND.get(f['c'], f['c'])}", "fails": "",
               "turn": f["i"], "character": f["agent"], "trigger": "",
               "line": f["line"] if with_lines else "",
               "against": (f.get("source") or f.get("q") or "") if with_lines else ""}
    for m in j.get("missed") or []:
        yield {"kind": "beat not made", "fails": "", "turn": m["i"], "character": m["agent"],
               "trigger": m.get("trigger") or "", "line": m["line"] if with_lines else "",
               "against": ""}


def finding_rows(reports: Dict[str, dict]) -> List[dict]:
    """One row per thing the judge found in a sim run, newest run first."""
    rows = []
    for name, r in reports.items():
        for sid, s in (r.get("scenarios") or {}).items():
            j = s.get("judge")
            if not j or j.get("error"):
                continue
            for k, f in enumerate(_found(j, with_lines=True)):
                rows.append({"id": f"{name}|{sid}|{k}", "run": name,
                             "day": (r.get("generated_at") or "")[:10], "scenario": sid, **f})
    return sorted(rows, key=lambda x: (x["day"], x["run"]), reverse=True)


_EPOCH = re.compile(r"s_(\d{9,11})_")


def _day_of(session_id: str) -> str:
    """The UTC day a session id says it started on, or ''."""
    m = _EPOCH.match(session_id or "")
    if not m:
        return ""
    return dt.datetime.fromtimestamp(int(m.group(1)), dt.timezone.utc).strftime("%Y-%m-%d")


def encounter_rows(results: Sequence[dict]) -> List[dict]:
    """One row per recorded encounter the judge was run on, newest first.
    Numbers and places only: never a line."""
    rows = []
    for r in results:
        sid = r.get("session_id") or ""
        cells = _judge_cells(r)
        note = ("no verdict: " + r["error"]) if r.get("error") else (
            "FACT, rule not judged: " + r["spec_changed"] if r.get("spec_changed") else "")
        rows.append({"session": sid, "day": _day_of(sid), "scenario": r.get("scenario"),
                     "build": r.get("build") or "", "note": note,
                     **{k: cells.get(k) for k in ENCOUNTER_COLUMNS if k in cells}})
    return sorted(rows, key=lambda x: x["session"], reverse=True)


def selftest_rows(payload: dict) -> List[dict]:
    """The selftest's score, one row per kind of defect."""
    s, results = payload["score"], payload.get("results") or []
    same = {"encounters": len(results), "lines": s["lines"],
            "rubric": next((r.get("rubric") for r in results if r.get("rubric")), ""),
            "model": next((r.get("model") for r in results if r.get("model")), "")}
    rows = [{"defect": _KIND.get(c, c), "planted": t["planted"], "found": t["found"],
             "false_alarms": t["false"], **same} for c, t in s["codes"].items()]
    b = s["beats"]
    rows.append({"defect": "beat not made", "planted": b["none_planted"],
                 "found": b["none_found"], "false_alarms": b["made_called_none"], **same})
    return rows


def write_csv(path: Path, columns: Sequence[str], rows: Sequence[dict]) -> None:
    """UTF-8, LF, a blank for what a row does not have."""
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(columns)
        for r in rows:
            w.writerow(["" if r.get(c) is None else r.get(c) for c in columns])


def load_reports(folder: Path = REPORTS) -> Dict[str, dict]:
    return {p.stem: json.loads(p.read_text(encoding="utf-8"))
            for p in sorted(folder.glob("*.json"))}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("out", type=Path, help="directory to write the CSV files into")
    ap.add_argument("--reports", type=Path, default=REPORTS,
                    help="the sim reports (default: tools/sim/reports)")
    ap.add_argument("--encounters", type=Path, metavar="FILE",
                    help="results of `python -m tools.sim.judge ... --json`")
    ap.add_argument("--selftest", type=Path, metavar="FILE",
                    help="results of `python -m tools.sim.judge_selftest --json`")
    args = ap.parse_args(argv)

    reports = load_reports(args.reports)
    if not reports:
        ap.error(f"no report in {args.reports}")
    args.out.mkdir(parents=True, exist_ok=True)
    made = [("sim_runs.csv", SIM_COLUMNS, sim_rows(reports)),
            ("findings.csv", FINDING_COLUMNS, finding_rows(reports))]
    if args.encounters:
        made.append(("encounters.csv", ENCOUNTER_COLUMNS, encounter_rows(
            json.loads(args.encounters.read_text(encoding="utf-8")))))
    if args.selftest:
        made.append(("selftest.csv", SELFTEST_COLUMNS, selftest_rows(
            json.loads(args.selftest.read_text(encoding="utf-8")))))
    for name, columns, rows in made:
        write_csv(args.out / name, columns, rows)
        print(f"{name}: {len(rows)} rows")
    return 0


if __name__ == "__main__":
    sys.exit(main())
