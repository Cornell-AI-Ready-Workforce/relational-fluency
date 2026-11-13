"""How well the judge judges, measured on encounters whose defects are known.

    python -m tools.sim.judge_selftest                  # the committed set, live
    python -m tools.sim.judge_selftest -v               # and name what it missed
    python -m tools.sim.judge_selftest --validate FILE  # structure only, no model
    python -m tools.sim.judge_selftest --freeze FILE    # (re)take FILE's scenario text

tools/sim/judge.py is a model reading a transcript, and a model's reading is an
instrument like any other: it has a hit rate and a false-alarm rate, and they
move when JUDGE_MODEL or the rubric does. tools/sim/judge_cases.json is what
they are measured on: written encounters in which every character line carries
a label, either clean (and many are written to look wrong: a character lying
because its cue says to, a date said in other words) or planted with one known
defect. The set was written by authors who had not seen the judge's wording,
and each label was checked by a second reader who had not either; a label the
two disagree on is marked `ambiguous` and is not scored either way.

The file carries the scenario text its cases were written against (`scenarios`:
what tools/sim/judge.py source_from() made of each scenario file that day), and
the judge is handed that text, not today's. The cases name triggers, not
directions; each direction is composed here from the cue in that same text. So
a scenario reworded next month does not quietly turn this into a different
test, and nothing here needs git history. --freeze retakes the text from this
checkout, for a set whose cases have been rewritten to match it.

Run it after changing the judge's prompts, its constants or JUDGE_MODEL, and
put the numbers in the PR. It costs one to three model calls per encounter.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.sim import judge  # noqa: E402

CASES = Path(__file__).resolve().parent / "judge_cases.json"
CODES = ("FACT", "ROLE", "SAFE", "RULE")
BEATS = ("made", "none")
SOURCE_KEYS = ("scene", "facts", "briefs", "esci", "cues", "triggers")

# As the runner words them (server/realtime_voice_session.py); the judge is
# told to ignore the frame, so only the cue has to be exact.
_CUE = "Your next move, as {name}, now, in your own words: {cue}"
_SILENCE = ("They have gone quiet. Your next move, as {name}, now, in your own words, "
            "then stop and let them answer: {cue}")


def load(path: Path = CASES) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def write(path: Path, data: dict) -> None:
    """UTF-8, LF, one space of indent: a committed artefact that diffs cleanly
    on every platform. Key order is kept, so a case still reads top to bottom."""
    path.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n",
                    encoding="utf-8", newline="")


def freeze(data: dict) -> dict:
    """`data` with `scenarios` retaken from this checkout's scenario files, for
    every scenario a case names."""
    wanted = sorted({c.get("scenario") for c in data.get("cases") or [] if c.get("scenario")})
    return dict(data, scenarios={sid: judge.source_of(sid) for sid in wanted})


def problems(data: dict) -> List[str]:
    """What is wrong with a case file's structure, one sentence each.

    Only what code can know: that the file carries the scenario text each case
    is held to, that every turn is well formed, that a trigger exists in that
    text and belongs to the character who speaks it, that every character line
    is labelled and a beat label sits on a trigger turn and nowhere else.
    Whether a label is RIGHT is the two readers' business, not this function's.
    """
    out: List[str] = []
    sources = data.get("scenarios") or {}
    seen = set()
    for case in data.get("cases") or []:
        cid = case.get("id") or "?"
        if cid in seen:
            out.append(f"{cid}: the id is used twice")
        seen.add(cid)
        src = sources.get(case.get("scenario"))
        if not isinstance(src, dict) or any(k not in src for k in SOURCE_KEYS):
            out.append(f"{cid}: the file carries no scenario text for {case.get('scenario')!r} "
                       f"(run --freeze)")
            continue
        for k, t in enumerate(case.get("transcript") or []):
            where = f"{cid} turn {k}"
            if t.get("role") not in ("participant", "agent") or not (t.get("text") or "").strip():
                out.append(f"{where}: needs a role (participant or agent) and a text")
                continue
            if t["role"] == "participant":
                if t.get("expect") or t.get("trigger_id"):
                    out.append(f"{where}: a participant turn carries no label and no trigger")
                continue
            if t.get("agent_id") not in src["briefs"]:
                out.append(f"{where}: {t.get('agent_id')!r} is not a character of "
                           f"{case['scenario']}")
            exp, trig = t.get("expect"), t.get("trigger_id")
            if not isinstance(exp, dict) or not isinstance(exp.get("flags"), list):
                out.append(f"{where}: a character turn needs expect: {{beat, flags}}")
                continue
            if any(c not in CODES for c in exp["flags"]):
                out.append(f"{where}: flags are any of {', '.join(CODES)}")
            if trig:
                if trig not in src["triggers"]:
                    out.append(f"{where}: no trigger {trig!r} in {case['scenario']}")
                elif t.get("agent_id") not in src["triggers"][trig]["speakers"]:
                    out.append(f"{where}: {trig} is "
                               f"{' or '.join(src['triggers'][trig]['speakers'])}'s beat, "
                               f"not {t.get('agent_id')}'s")
                if exp.get("beat") not in BEATS:
                    out.append(f"{where}: a trigger turn's beat is 'made' or 'none'")
            elif exp.get("beat") is not None:
                out.append(f"{where}: a beat label needs a trigger_id")
            if t.get("heard_text") is not None and not t.get("interrupted"):
                out.append(f"{where}: heard_text is for an interrupted line")
    if not data.get("cases"):
        out.append("no cases")
    return out


def record_of(case: dict, src: dict) -> dict:
    """The case as an encounter record: each trigger turn under the direction
    the runner would have handed it, from the scenario text `src`."""
    turns = []
    for t in case["transcript"]:
        if t["role"] == "participant":
            turns.append({"role": "participant", "text": t["text"]})
            continue
        trig = t.get("trigger_id") or None
        direction = None
        if trig:
            cue, silence = src["cues"][trig]
            direction = (_SILENCE if t.get("on_silence") else _CUE).format(
                name=(src["briefs"].get(t["agent_id"]) or {}).get("name") or t["agent_id"],
                cue=silence if t.get("on_silence") else cue)
        turns.append({"role": "agent", "agent_id": t["agent_id"], "text": t["text"],
                      "stage_direction": direction, "trigger_id": trig,
                      "esci": list(src["triggers"][trig]["esci"]) if trig else [],
                      "interrupted": bool(t.get("interrupted")),
                      "heard_text": t.get("heard_text")})
    return {"encounter_id": case["id"], "scenario": case["scenario"], "participant_key": None,
            "provenance": {"build": None}, "transcript": turns}


def score(cases: Sequence[dict], results: Sequence[dict]) -> dict:
    """What the judge found of what was planted, and what it found that was not.

    Per code: `planted`, `found` (a flag with that code on that turn) and
    `false` (that code on a line not labelled with it). For beats: of those
    labelled none, how many the judge called none; of those labelled made, how
    many it called none. A turn marked `ambiguous` is not scored. An encounter
    with no verdict, or one whose FACT and rule were not judged, is counted in
    `no_verdict` and its labels are not scored: a hit rate over lines the judge
    never read would be about nothing.
    """
    tally = {c: {"planted": 0, "found": 0, "false": 0} for c in CODES}
    beats = {"none_planted": 0, "none_found": 0, "made": 0, "made_called_none": 0}
    missed: List[str] = []
    false: List[str] = []
    unread: List[str] = []
    lines = 0
    for case, r in zip(cases, results):
        if r.get("error") or r.get("spec_changed"):
            unread.append(f"{case['id']}: {r.get('error') or r['spec_changed']}")
            continue
        flagged = {(f["at"], f["c"]) for f in r["flags"] if f["c"] in CODES}
        verdict = {v["at"]: v["v"] for v in r["verdicts"]}
        for at, t in enumerate(case["transcript"]):
            if t["role"] != "agent" or t.get("ambiguous"):
                continue
            lines += 1
            exp = t["expect"]
            said = f"{case['id']} turn {at} {t['agent_id']}: \"{t['text'][:70]}\""
            for c in CODES:
                want, got = c in exp["flags"], (at, c) in flagged
                tally[c]["planted"] += want
                tally[c]["found"] += want and got
                tally[c]["false"] += got and not want
                if want and not got:
                    missed.append(f"{c} {said}")
                if got and not want:
                    false.append(f"{c} {said}")
            if exp.get("beat") == "none":
                beats["none_planted"] += 1
                if verdict.get(at) == "n":
                    beats["none_found"] += 1
                else:
                    missed.append(f"none ({verdict.get(at) or 'no verdict'}) {said}")
            elif exp.get("beat") == "made":
                beats["made"] += 1
                if verdict.get(at) == "n":
                    beats["made_called_none"] += 1
                    false.append(f"none {said}")
    return {"codes": tally, "beats": beats, "lines": lines, "no_verdict": len(unread),
            "unread": unread, "missed": missed, "false": false}


def report(s: dict, results: Sequence[dict], verbose: bool = False) -> List[str]:
    b = s["beats"]
    out = [f"judge selftest: {len(results)} encounters, {s['lines']} labelled lines, "
           f"rubric {judge.rubric_id()}, {results[0].get('model') if results else '?'}  "
           f"[{sum(r['usage']['calls'] for r in results)} calls, "
           f"{sum(r['usage']['in'] for r in results) / 1000:.1f}k in "
           f"{sum(r['usage']['out'] for r in results) / 1000:.1f}k out]",
           "  " + "   ".join(f"{c} {t['found']}/{t['planted']} found, {t['false']} false"
                             for c, t in s["codes"].items()),
           f"  beats not made {b['none_found']}/{b['none_planted']} found, "
           f"{b['made_called_none']} false of {b['made']} made"]
    if s["no_verdict"]:
        out.append(f"  NO VERDICT on {s['no_verdict']} encounters: " + "; ".join(s["unread"]))
    if verbose:
        out += [f"  missed: {m}" for m in s["missed"]] + [f"  false:  {f}" for f in s["false"]]
    return out


async def run(cases: Sequence[dict], sources: Dict[str, dict], model: Optional[str] = None,
              client=None) -> List[dict]:
    client, gate = client or judge._client(), asyncio.Semaphore(judge.CONCURRENCY)

    async def one(case):
        src = sources[case["scenario"]]
        async with gate:
            return await judge.judge_record(record_of(case, src), client=client, model=model,
                                            source=src)

    return list(await asyncio.gather(*(one(c) for c in cases)))


def main(argv=None, client=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--cases", type=Path, default=CASES,
                    help="case file (default: the committed set)")
    ap.add_argument("--validate", type=Path, metavar="FILE",
                    help="check FILE's structure against the scenario text it carries, and stop")
    ap.add_argument("--freeze", type=Path, metavar="FILE",
                    help="rewrite FILE with the scenario text of this checkout, and stop")
    ap.add_argument("--only", default="", help="comma-separated case ids to run")
    ap.add_argument("--model", help="judge model (default: JUDGE_MODEL, else the director's)")
    ap.add_argument("--json", type=Path, metavar="FILE", help="write the score and results here")
    ap.add_argument("-v", "--verbose", action="store_true", help="name each miss and false alarm")
    args = ap.parse_args(argv)

    path = args.freeze or args.validate or args.cases
    if not path.is_file():
        ap.error(f"no case file at {path}")
    data = load(path)
    if args.freeze:
        data = freeze(data)
        write(path, data)
    wrong = problems(data)
    if wrong:
        print("\n".join(wrong), file=sys.stderr)
        return 2
    if args.freeze or args.validate:
        n = sum(1 for c in data["cases"] for t in c["transcript"] if t["role"] == "agent")
        print(f"ok: {len(data['cases'])} cases, {n} character lines")
        return 0
    only = [s.strip() for s in args.only.split(",") if s.strip()]
    cases = [c for c in data["cases"] if not only or c["id"] in only]
    if not cases:
        ap.error("no such case")
    results = asyncio.run(run(cases, data["scenarios"], args.model, client))
    s = score(cases, results)
    print("\n".join(report(s, results, args.verbose)))
    if args.json:
        args.json.write_text(json.dumps({"score": s, "results": results}, indent=1,
                                        sort_keys=True, ensure_ascii=False) + "\n",
                             encoding="utf-8", newline="")
    return 1 if s["no_verdict"] else 0


if __name__ == "__main__":
    sys.exit(main())
