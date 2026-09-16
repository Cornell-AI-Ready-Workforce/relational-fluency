"""Qualtrics integration: pull survey responses and join them to app runs.

The study chain is CloudResearch key -> Qualtrics survey -> app run -> back to
Qualtrics. This module closes the loop from the analysis side: it exports the
survey's responses over the Qualtrics API and merges them with /api/runs, so
"who replied to the survey and how did they respond" is one table with the
run id, completion code, and encounter session ids attached.

    python -m server.qualtrics whoami
    python -m server.qualtrics export            # raw responses to a JSON file
    python -m server.qualtrics join              # responses merged with runs

Credentials come from .env (QUALTRICS_API_TOKEN, QUALTRICS_SURVEY_ID,
QUALTRICS_BASE_URL). The token is a credential: it stays out of git and out of
logs, and only travels in the X-API-TOKEN header.
"""

from __future__ import annotations

import io
import json
import sys
import time
import zipfile
from typing import Dict, List, Optional

import httpx

from .llm import setting
from .storage import DATA_DIR

# The DATACENTER host, not the brand vanity host. They are not
# interchangeable: cornell.qualtrics.com answers /whoami and /surveys happily
# and then refuses /export-responses with
#   "This endpoint is unavailable through datacenter proxying, please retry
#    using the url of the datacenter the API user belongs to: yul1.qualtrics.com"
# so a wrong value here fails only at the one call that matters, months after
# anyone set it. Note also that /whoami reports datacenter "viawest" while the
# routable host is "yul1" — the name it reports is NOT the name to put here, so
# read the failure message rather than whoami's field. Measured against the
# Cornell brand on 11 Sep 2026.
BASE = setting("QUALTRICS_BASE_URL", "https://yul1.qualtrics.com").rstrip("/")
SURVEY = setting("QUALTRICS_SURVEY_ID", "")
EXPORT_DIR = DATA_DIR / "qualtrics"


def _raise(r: "httpx.Response", what: str) -> None:
    """Fail with what Qualtrics SAID, not merely with the status code.

    httpx's own raise_for_status() renders a 400 as "Client error '400 Bad
    Request' for url ..." plus a link to the MDN page for 400, and throws the
    response body away. Qualtrics puts the actionable part in that body: the
    datacenter-proxying refusal above names the exact host to use, which turns a
    half-hour of guessing into a one-line fix. An error that carries the answer
    and discards it is worse than no error, because it sends the reader to the
    wrong place with confidence.
    """
    if r.status_code < 400:
        return
    detail = ""
    try:
        meta = r.json().get("meta", {})
        err = meta.get("error", {}) or {}
        detail = err.get("errorMessage") or ""
        code = err.get("errorCode")
        if code:
            detail = f"{detail} [{code}]".strip()
    except Exception:  # noqa: BLE001 - a non-JSON body is still worth showing
        detail = (r.text or "")[:300]
    raise RuntimeError(
        f"Qualtrics refused the {what} request ({r.status_code}) at {r.request.url}: "
        f"{detail or '(no message in the response body)'}")


def _headers() -> Dict[str, str]:
    token = setting("QUALTRICS_API_TOKEN", "")
    if not token:
        raise RuntimeError("QUALTRICS_API_TOKEN is not set")
    return {"X-API-TOKEN": token, "Content-Type": "application/json"}


def whoami() -> dict:
    r = httpx.get(f"{BASE}/API/v3/whoami", headers=_headers(), timeout=20)
    _raise(r, "whoami")
    return r.json()["result"]


def export_responses(survey_id: Optional[str] = None, *, timeout: float = 180) -> List[dict]:
    """The documented three-step export: start, poll, download."""
    sid = survey_id or SURVEY
    if not sid:
        raise RuntimeError("QUALTRICS_SURVEY_ID is not set")

    base = f"{BASE}/API/v3/surveys/{sid}/export-responses"
    start = httpx.post(base, headers=_headers(), json={"format": "json"}, timeout=30)
    _raise(start, "export start")
    progress_id = start.json()["result"]["progressId"]

    deadline = time.time() + timeout
    file_id = None
    while time.time() < deadline:
        p = httpx.get(f"{base}/{progress_id}", headers=_headers(), timeout=30)
        _raise(p, "export progress")
        result = p.json()["result"]
        if result["status"] == "complete":
            file_id = result["fileId"]
            break
        if result["status"] == "failed":
            raise RuntimeError(f"Qualtrics export failed: {result}")
        time.sleep(1.5)
    if file_id is None:
        raise TimeoutError("Qualtrics export did not complete in time")

    f = httpx.get(f"{base}/{file_id}/file", headers=_headers(), timeout=60)
    _raise(f, "export download")
    with zipfile.ZipFile(io.BytesIO(f.content)) as z:
        name = z.namelist()[0]
        payload = json.loads(z.read(name))
    return payload.get("responses", [])


def _flatten(resp: dict) -> dict:
    """One row per response: metadata plus answer values."""
    values = resp.get("values", {})
    return {
        "response_id": resp.get("responseId"),
        "recorded": values.get("recordedDate"),
        "progress": values.get("progress"),
        "finished": values.get("finished"),
        "duration_s": values.get("duration"),
        # Embedded data fields land in values under their own names; the
        # participant key field, whatever it is called, will be among these.
        "values": values,
        "labels": resp.get("labels", {}),
    }


def join_with_runs(responses: List[dict]) -> List[dict]:
    """Merge survey responses with app runs.

    Primary key: the run's qualtrics_id equals the ResponseID (piped through
    /start?qid=...). Fallback: the participant key, for responses collected
    before qid piping was configured.
    """
    from .runs import RUNS_DIR, completion_code

    runs = []
    if RUNS_DIR.exists():
        for f in RUNS_DIR.glob("*.json"):
            try:
                runs.append(json.loads(f.read_text(encoding="utf-8")))
            except ValueError:
                pass

    by_qid = {r["qualtrics_id"]: r for r in runs if r.get("qualtrics_id")}
    by_pid: Dict[str, dict] = {}
    for r in runs:
        pid = r.get("participant_id")
        if pid:
            by_pid.setdefault(pid, r)

    rows = []
    for resp in responses:
        flat = _flatten(resp)
        run = by_qid.get(flat["response_id"])
        matched = "response_id" if run else None
        if run is None:
            values = flat["values"]
            for key in ("ParticipantKey", "participant_key", "pid", "PROLIFIC_PID", "connectId"):
                pid = values.get(key)
                if pid and pid in by_pid:
                    run = by_pid[pid]
                    matched = f"participant_id via {key}"
                    break
        rows.append({
            **flat,
            "matched_by": matched,
            "run": None if run is None else {
                "run_id": run["run_id"],
                "participant_id": run.get("participant_id"),
                "cohort": run.get("cohort", "study"),
                "completion_code": completion_code(run),
                "finished": run.get("index", 0) >= len(run.get("scenarios", [])),
                "encounters": [
                    {"scenario": c.get("id"), "session_id": c.get("session_id")}
                    for c in run.get("completed", [])
                ],
            },
        })
    return rows


def main(argv: List[str]) -> int:
    cmd = argv[0] if argv else "help"
    if cmd == "whoami":
        w = whoami()
        print(f"brand={w.get('brandId')} dc={w.get('datacenter')} user={w.get('userId')}")
        return 0
    if cmd == "export":
        responses = export_responses()
        EXPORT_DIR.mkdir(parents=True, exist_ok=True)
        out = EXPORT_DIR / f"responses_{int(time.time())}.json"
        out.write_text(json.dumps(responses, indent=2), encoding="utf-8")
        print(f"{len(responses)} responses -> {out}")
        return 0
    if cmd == "join":
        rows = join_with_runs(export_responses())
        EXPORT_DIR.mkdir(parents=True, exist_ok=True)
        out = EXPORT_DIR / f"joined_{int(time.time())}.json"
        out.write_text(json.dumps(rows, indent=2), encoding="utf-8")
        linked = sum(1 for r in rows if r["run"])
        print(f"{len(rows)} responses, {linked} linked to runs -> {out}")
        for r in rows[:10]:
            tag = r["run"]["run_id"] if r["run"] else "UNLINKED"
            print(f"  {r['response_id']}  finished={r['finished']}  -> {tag} ({r['matched_by']})")
        return 0
    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
