"""The parsing half of tools/deploy.sh.

deploy.sh does what a shell does well (git, aws, curl, tofu, exit codes) and
hands every question that needs parsing to this file, so the answers are
testable in Python and identical on macOS and Linux bash:

    python tools/deploy_guard.py pin                   # the pinned image, tab-separated
    python tools/deploy_guard.py health FILE|-         # active_sessions and build from /health
    python tools/deploy_guard.py relation OLD NEW      # forward / rollback / same / sideways / unknown
    python tools/deploy_guard.py sim-report TAG        # pass / missing / fail: why
    python tools/deploy_guard.py plan-summary FILE|- --pinned IMAGE [--allow-rollback]

plan-summary reads `tofu show -json tfplan.bin` and prints the plan's resource
changes with the IMAGE change set apart, because the image is the line that
decides what participants talk to and it is otherwise one attribute among
forty inside a replaced task definition. It exits 3 when the plan would
deploy something other than the pinned image, and 4 when it would move
production to an OLDER commit than it runs now: the 2026-09-24 incident, a
stale checkout's pin replacing 4798e64 with ca77c2f.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import pinned_image  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
REPORTS = REPO_ROOT / "tools" / "sim" / "reports"

WRONG_IMAGE, ROLLBACK = 3, 4


def _read(src: str) -> str:
    if src == "-":
        return sys.stdin.read()
    return Path(src).read_text(encoding="utf-8")


# --- /health ----------------------------------------------------------------------

def parse_health(text: str) -> Tuple[int, str]:
    """(active_sessions, build or "") from a /health body; ValueError if it is
    not the platform's /health."""
    body = json.loads(text)
    if not isinstance(body, dict) or not isinstance(body.get("active_sessions"), int):
        raise ValueError("no integer active_sessions: this is not the platform's /health")
    return body["active_sessions"], str(body.get("build") or "")


# --- git ----------------------------------------------------------------------------

def _git_ok(*args: str) -> Optional[bool]:
    r = subprocess.run(["git", *args], cwd=REPO_ROOT, capture_output=True)
    if r.returncode == 0:
        return True
    if r.returncode == 1:
        return False
    return None


def _is_commit(ref: str) -> bool:
    return bool(_git_ok("cat-file", "-e", f"{ref}^{{commit}}"))


def relation(old: str, new: str) -> Tuple[str, int]:
    """How moving from commit `old` to commit `new` relates in history:
    ("same"|"forward"|"rollback"|"sideways"|"unknown", commits moved)."""
    if pinned_image.same_commit(old, new):
        return "same", 0
    if not (_is_commit(old) and _is_commit(new)):
        return "unknown", 0
    if _git_ok("merge-base", "--is-ancestor", old, new):
        n = subprocess.run(["git", "rev-list", "--count", f"{old}..{new}"], cwd=REPO_ROOT,
                           capture_output=True, text=True, encoding="utf-8").stdout.strip()
        return "forward", int(n or 0)
    if _git_ok("merge-base", "--is-ancestor", new, old):
        n = subprocess.run(["git", "rev-list", "--count", f"{new}..{old}"], cwd=REPO_ROOT,
                           capture_output=True, text=True, encoding="utf-8").stdout.strip()
        return "rollback", int(n or 0)
    return "sideways", 0


# --- the sim report ------------------------------------------------------------------

def sim_report_status(tag: str, reports: Path = REPORTS) -> str:
    """"pass", "missing", or "fail: <why>" for tools/sim/reports/<tag>.json."""
    candidates = sorted(p for p in reports.glob("*.json")
                        if pinned_image.same_commit(p.stem, tag)) if reports.is_dir() else []
    if not candidates:
        return "missing"
    path = candidates[0]
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return f"fail: {path.name} is unreadable ({exc})"
    if not pinned_image.same_commit(str(report.get("build")), tag):
        return f"fail: {path.name} is a report for build {report.get('build')!r}, not {tag}"
    if report.get("passed") is not True:
        first = (report.get("failures") or ["passed is not true"])[0]
        return f"fail: {first}"
    return "pass"


# --- the plan ------------------------------------------------------------------------

def _images(container_definitions) -> Dict[str, str]:
    if isinstance(container_definitions, str):
        try:
            container_definitions = json.loads(container_definitions)
        except ValueError:
            return {}
    out = {}
    for c in container_definitions or []:
        if isinstance(c, dict) and c.get("image"):
            out[str(c.get("name", "?"))] = str(c["image"])
    return out


def summarize_plan(plan: dict) -> dict:
    """Resource changes and image changes from `tofu show -json` output."""
    changes: List[dict] = []
    counts = {"add": 0, "change": 0, "destroy": 0}
    images: List[dict] = []
    for rc in plan.get("resource_changes") or []:
        change = rc.get("change") or {}
        actions = list(change.get("actions") or [])
        if actions in ([], ["no-op"], ["read"]):
            continue
        if set(actions) == {"delete", "create"}:
            action = "replace"
            counts["add"] += 1
            counts["destroy"] += 1
        elif actions == ["create"]:
            action = "create"
            counts["add"] += 1
        elif actions == ["delete"]:
            action = "delete"
            counts["destroy"] += 1
        else:
            action = "update"
            counts["change"] += 1
        changes.append({"address": rc.get("address"), "action": action})
        if rc.get("type") == "aws_ecs_task_definition":
            before = _images((change.get("before") or {}).get("container_definitions"))
            after = _images((change.get("after") or {}).get("container_definitions"))
            for name in sorted(set(before) | set(after)):
                if before.get(name) != after.get(name):
                    images.append({"address": rc.get("address"), "container": name,
                                   "before": before.get(name), "after": after.get(name)})
    return {"counts": counts, "changes": changes, "images": images}


def _tag(image: Optional[str]) -> str:
    return (image or "").rsplit(":", 1)[-1] if image and ":" in image else ""


_SYMBOL = {"create": "+", "update": "~", "replace": "-/+", "delete": "-"}


def render_plan(summary: dict, pinned: str, *, allow_rollback: bool = False,
                color: bool = False) -> Tuple[int, str]:
    """(exit code, text) for the plan summary deploy.sh prints."""
    def paint(s: str, code: str) -> str:
        return f"\033[{code}m{s}\033[0m" if color else s

    c = summary["counts"]
    lines = [f"Plan: {c['add']} to add, {c['change']} to change, {c['destroy']} to destroy."]
    for ch in summary["changes"]:
        sym = _SYMBOL.get(ch["action"], "?")
        text = f"  {sym:>3} {ch['address']}  ({ch['action']})"
        lines.append(paint(text, "31") if ch["action"] == "delete" else text)
    deletes = [ch["address"] for ch in summary["changes"] if ch["action"] == "delete"]
    if deletes:
        lines.append(paint(f"  NOTE: this plan DESTROYS {len(deletes)} resource(s) outright: "
                           f"{', '.join(deletes)}", "1;31"))

    code = 0
    bar = "=" * 72
    lines.append("")
    if not summary["images"]:
        lines.append(bar)
        lines.append(f"  IMAGE: unchanged. The plan does not touch the running image; "
                     f"the pin is {_tag(pinned)}.")
        lines.append(bar)
        return code, "\n".join(lines)
    for im in summary["images"]:
        old, new = _tag(im["before"]), _tag(im["after"])
        if im["after"] != pinned:
            code = WRONG_IMAGE
            verdict = paint(f"NOT THE PIN: the pinned image is {pinned}", "1;31")
        else:
            rel, n = relation(old, new) if old else ("create", 0)
            verdict = {
                "forward": f"forward, {n} commit(s) newer than what runs now",
                "same": "same commit",
                "sideways": "production runs a commit that is not on the pinned "
                            "commit's history (a branch build?); this replaces it",
                "unknown": "cannot tell how these relate: one is not in this "
                           "repository's history",
                "create": "no image runs now",
            }.get(rel)
            if rel == "rollback":
                verdict = paint(f"ROLLBACK: {new} is {n} commit(s) OLDER than {old}, "
                                f"which production runs now", "1;31")
                if not allow_rollback:
                    code = max(code, ROLLBACK)
        lines.append(bar)
        lines.append(paint(f"  IMAGE CHANGE  {old or '(none)'}  ->  {new}", "1;33"))
        lines.append(f"    {im['address']} [{im['container']}]")
        lines.append(f"    before: {im['before'] or '(none)'}")
        lines.append(f"    after:  {im['after']}")
        lines.append(f"    {verdict}")
        lines.append(bar)
    return code, "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("pin")
    h = sub.add_parser("health")
    h.add_argument("src")
    r = sub.add_parser("relation")
    r.add_argument("old")
    r.add_argument("new")
    s = sub.add_parser("sim-report")
    s.add_argument("tag")
    p = sub.add_parser("plan-summary")
    p.add_argument("src")
    p.add_argument("--pinned", required=True)
    p.add_argument("--allow-rollback", action="store_true")
    args = ap.parse_args(argv)

    if args.cmd == "pin":
        try:
            pin = pinned_image.read()
        except (OSError, pinned_image.PinError) as exc:
            print(exc, file=sys.stderr)
            return 2
        print("\t".join((pin.image, pin.registry, pin.region, pin.repository, pin.tag)))
        return 0
    if args.cmd == "health":
        try:
            active, build = parse_health(_read(args.src))
        except ValueError as exc:
            print(exc, file=sys.stderr)
            return 2
        print(f"{active}\t{build}")
        return 0
    if args.cmd == "relation":
        rel, n = relation(args.old, args.new)
        print(f"{rel}\t{n}")
        return 0
    if args.cmd == "sim-report":
        print(sim_report_status(args.tag))
        return 0
    try:
        plan = json.loads(_read(args.src))
    except ValueError as exc:
        print(f"the plan JSON is unreadable: {exc}", file=sys.stderr)
        return 2
    color = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
    code, text = render_plan(summarize_plan(plan), args.pinned,
                             allow_rollback=args.allow_rollback, color=color)
    print(text)
    return code


if __name__ == "__main__":
    sys.exit(main())
