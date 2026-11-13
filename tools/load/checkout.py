"""Read-only checkout prerequisite checks; never import the selected server."""
from __future__ import annotations

import ast
from pathlib import Path


async def revision(checkout):
    from tools.load.runtime import command
    code, sha, _ = await command("git", "rev-parse", "HEAD", cwd=checkout, check=False)
    status, dirty, _ = await command("git", "status", "--porcelain", cwd=checkout, check=False)
    return {"checkout": str(checkout), "commit": sha.strip() if code == 0 else None,
            "dirty": bool(dirty.strip()) if status == 0 else None}


def validate_checkout(root, *, require_traces=False):
    root = Path(root).resolve()
    missing = []

    def read(relative):
        try:
            return ast.parse((root / relative).read_text(encoding="utf-8"))
        except (OSError, SyntaxError, UnicodeError):
            return ast.Module(body=[], type_ignores=[])

    def reads(tree, key):
        return any(isinstance(node, ast.Call) and node.args and
                   isinstance(node.args[0], ast.Constant) and node.args[0].value == key and
                   ((isinstance(node.func, ast.Name) and node.func.id in {"setting", "getenv"}) or
                    (isinstance(node.func, ast.Attribute) and node.func.attr in {"get", "getenv"}))
                   for node in ast.walk(tree))

    for name in ("server/app.py", "infra/terraform/ecs.tf"):
        if not (root / name).is_file():
            missing.append(name)
    telemetry = read("server/upstream_telemetry.py")
    definitions = {node.name for node in telemetry.body if isinstance(node, (ast.FunctionDef, ast.ClassDef))}
    if not {"add_listener", "record", "_Attempt"} <= definitions:
        missing.append("server/upstream_telemetry.py (add_listener, record, _Attempt)")
    if not reads(read("server/voice/cascade.py"), "ELEVENLABS_WS_URL"):
        missing.append("ELEVENLABS_WS_URL support in server/voice/cascade.py")
    if not any(reads(read(name), "STORAGE_PREFLIGHT") for name in ("server/app.py", "server/video.py")):
        missing.append("STORAGE_PREFLIGHT support in server/app.py or server/video.py")
    if require_traces:
        trace = read("server/reply_trace.py")
        classes = [n for n in trace.body if isinstance(n, ast.ClassDef) and n.name == "ReplyTrace"]
        flush = next((n for c in classes for n in c.body if isinstance(n, ast.FunctionDef) and n.name == "flush"), None)
        kinds = {n.value for n in ast.walk(trace) if isinstance(n, ast.Constant) and isinstance(n.value, str)}
        headers = any(isinstance(n, ast.Call) and
                      {k.arg: k.value.value for k in n.keywords if isinstance(k.value, ast.Constant)}.items() >=
                      {"schema_version": 1, "clock": "encounter_s", "units": "seconds", "finalized_at": "encounter_teardown"}.items()
                      for n in ast.walk(flush)) if flush else False
        if not headers or not {"reply_trace", "commit_trace"} <= kinds:
            missing.append("server/reply_trace.py ReplyTrace.flush schema v1 encounter_s/seconds teardown contract")
    if missing:
        raise ValueError("incompatible server checkout; missing: " + "; ".join(missing))
    return root
