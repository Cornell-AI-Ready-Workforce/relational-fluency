"""FastAPI server, participant + researcher endpoints.

Two paths:
  - /ws/participant , text turn-taking (works without STT/TTS keys). Voice
                       endpoint /ws/participant/voice layers on STT/TTS.
  - /ws/researcher  , live transcript + steering controls

The text path is sufficient to "make sure the conversation is working well
and steer the model", voice is layered on top once a researcher has
validated scenarios and prompts.
"""
from __future__ import annotations

import json
import os
import secrets
import time
from pathlib import Path
from typing import Optional

import zipfile

import yaml
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from .engine import DEFAULT_MODEL
from .scenarios import list_scenarios, load_scenario
from .session import registry
from .storage import (
    create_participant, get_participant, init_storage, record_consent, record_decline,
)
from .realtime_voice_session import RealtimeVoiceSessionRunner

load_dotenv()
init_storage()

SESSION_KEY = os.getenv("SESSION_KEY", "").strip()
ROOT_DIR = Path(__file__).parent.parent
STATIC_DIR = ROOT_DIR / "static"
CONFIG_DIR = ROOT_DIR / "config"
# Mirror storage.py, DATA_DIR may be a mounted volume in production.
from .storage import SESSIONS_DIR as SESSIONS_DIR  # re-export

# Public hostnames. Cornell IT delegated ai-ready-workforce.ai.cornell.edu to
# Route 53; rf.* is the participant entrance (app + broker WSS) and api.rf.* is
# the backend API. Comma-separated overrides let staging and local dev differ.
APP_HOST = os.getenv("APP_HOST", "rf.ai-ready-workforce.ai.cornell.edu").strip()
API_HOST = os.getenv("API_HOST", "api.rf.ai-ready-workforce.ai.cornell.edu").strip()

# Host header allowlist. Empty ALLOWED_HOSTS disables the check (local dev).
_DEFAULT_ALLOWED = f"{APP_HOST},{API_HOST},localhost,127.0.0.1"
ALLOWED_HOSTS = [h.strip() for h in os.getenv("ALLOWED_HOSTS", _DEFAULT_ALLOWED).split(",") if h.strip()]

# Browser origins permitted to call the API. The app and the API are separate
# hostnames, so calls from the participant page are cross-origin.
_DEFAULT_ORIGINS = f"https://{APP_HOST},http://localhost:{os.getenv('PORT', '8765')},http://127.0.0.1:{os.getenv('PORT', '8765')}"
ALLOWED_ORIGINS = [o.strip() for o in os.getenv("ALLOWED_ORIGINS", _DEFAULT_ORIGINS).split(",") if o.strip()]

app = FastAPI(title="Relational Fluency Platform")

# Behind the ALB, honour X-Forwarded-Proto so generated URLs are https/wss.
app.add_middleware(ProxyHeadersMiddleware, trusted_hosts="*")

# Host-header allowlist. Implemented directly rather than with
# TrustedHostMiddleware because the ALB health check addresses the task by its
# private IP, which can never be in the allowlist, TrustedHostMiddleware would
# answer 400 and the target would be marked unhealthy forever. /health is
# therefore exempt; it exposes nothing.
if ALLOWED_HOSTS:

    @app.middleware("http")
    async def _guard_host(request, call_next):
        if request.url.path != "/health":
            host = (request.headers.get("host") or "").split(":")[0]
            if host and host not in ALLOWED_HOSTS:
                return JSONResponse({"detail": "Invalid host header"}, status_code=400)
        return await call_next(request)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

from .llm import preflight as _preflight

# Verify the model gateway before anyone can join. A wrong endpoint used to show
# up only as a 401 mid-encounter; now it is visible at boot and on /health.
_PREFLIGHT = _preflight()
if _PREFLIGHT.get("ambient_override_ignored"):
    print(
        f"  note: ignoring ambient ANTHROPIC_BASE_URL="
        f"{_PREFLIGHT['ambient_override_ignored']}, using {_PREFLIGHT['gateway']}"
    )
if not _PREFLIGHT.get("ok"):
    print(
        f"  WARNING: model gateway {_PREFLIGHT['gateway']} did not answer "
        f"({_PREFLIGHT.get('status') or _PREFLIGHT.get('detail')}). "
        f"Encounters will fail until this is fixed."
    )

# Participant-facing HTML must never be cached. A stale build is invisible to
# the participant and looks like a broken feature, and during collection it
# would mean people running different versions of the instrument.
@app.middleware("http")
async def _no_store_html(request, call_next):
    response = await call_next(request)
    ctype = response.headers.get("content-type", "")
    if ctype.startswith("text/html"):
        response.headers["Cache-Control"] = "no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
    return response


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/health")
async def health() -> dict:
    """Liveness probe for the ALB target group. Deliberately unauthenticated and
    dependency-free: it answers whether this process can serve, not whether the
    model gateway is reachable, so a transient upstream blip cannot cause ECS to
    kill healthy tasks mid-encounter."""
    # Nested so the liveness field cannot be shadowed by preflight keys.
    # active_sessions: a deploy rollout retires the old task within ~2 minutes,
    # which cuts any encounter running on it. Check this is 0 before applying.
    return {"status": "ok", "gateway": _PREFLIGHT, "active_sessions": len(registry.list_ids())}


def _load_consent() -> dict:
    return yaml.safe_load((CONFIG_DIR / "consent.yaml").read_text(encoding="utf-8"))


def check_key(key: Optional[str]) -> None:
    """Guard for researcher and study-data routes.

    SESSION_KEY protects recorded encounters, downloads, and the researcher
    views. It must NOT be required of participants: their link is handed to
    every recruited person, and a key that opens the whole dataset should not
    travel that way.
    """
    if SESSION_KEY and key != SESSION_KEY:
        raise HTTPException(status_code=401, detail="Bad or missing key")


# Participants arrive from Qualtrics with their CloudResearch key in the URL and
# nothing else. Set PARTICIPANT_KEY_REQUIRED=1 to also demand SESSION_KEY on
# their routes (useful while the study is not yet open).
PARTICIPANT_KEY_REQUIRED = os.getenv("PARTICIPANT_KEY_REQUIRED", "").strip() not in ("", "0", "false")


def check_participant(key: Optional[str]) -> None:
    if PARTICIPANT_KEY_REQUIRED:
        check_key(key)


# --- HTML routes ---

@app.get("/", response_class=HTMLResponse)
async def landing_page(scenario: Optional[str] = None, key: Optional[str] = None):
    """Landing page with scenario picker popup. If a scenario is passed via
    query (legacy v1 link), still serve the chat UI so old bookmarks work."""
    check_key(key)
    if scenario:
        return (STATIC_DIR / "participant.html").read_text(encoding="utf-8")
    return (STATIC_DIR / "landing.html").read_text(encoding="utf-8")


@app.get("/chat", response_class=HTMLResponse)
async def chat_page(scenario: Optional[str] = None, key: Optional[str] = None):
    """Legacy text-mode chat UI. /v2 is the recommended entry point."""
    check_key(key)
    return (STATIC_DIR / "participant.html").read_text(encoding="utf-8")


@app.get("/researcher", response_class=HTMLResponse)
async def researcher_page(key: Optional[str] = None):
    check_key(key)
    return (STATIC_DIR / "researcher.html").read_text(encoding="utf-8")


@app.get("/test")
async def start_test_run(
    name: Optional[str] = None,
    variant: Optional[str] = None,
):
    """Internal testing entry. Tags the run cohort=internal so test traffic can
    never be mistaken for study data, and needs no Qualtrics setup: pass a name
    so bug reports can say whose session it was."""
    from fastapi.responses import RedirectResponse

    from . import runs

    tester = (name or "anon").strip().replace(" ", "_")[:24]
    run = runs.create(
        f"test_{tester}_{int(time.time())}",
        variant=variant, cohort="internal",
    )
    # Mint the participant record here, exactly as /start does, and carry it in
    # the redirect. The cohort tag only reaches an encounter's manifest through
    # _run_context, which resolves the run from the participant *record* id on
    # the voice socket. Without a record minted against this run, the tester
    # consented with a bare code, POST /api/consent minted a second record that
    # no run pointed at, and the internal encounter recorded cohort=null — so it
    # was excluded from ?cohort=study but invisible to ?cohort=internal too, and
    # the tag was true only at the run level. Minting it here also means the
    # internal path exercises the same identity and consent code the study path
    # does, which is the point of a test entrance.
    #
    # consent_given=False for the same reason as /start: consent is the
    # participant's affirmative act, not something an entry point may assert.
    q = f"?run={run['run_id']}"
    try:
        pid_record = create_participant(
            code=run["participant_id"], consent_given=False, consent_version="",
        )
        run["participant_record_id"] = pid_record
        runs.save(run)
        q += f"&participant_id={pid_record}&consent=1"
    except Exception:  # noqa: BLE001, a test entrance must still open
        pass
    return RedirectResponse(url=f"/v2{q}", status_code=307)


@app.get("/api/runs")
async def api_runs_export(key: Optional[str] = None, cohort: Optional[str] = None):
    """The join table for analysis: every run with its participant key,
    Qualtrics response id, cohort, completion code, and the session ids of the
    encounters it produced.

    `cohort` is one of "study", "internal" (the /test entrance) or
    "unattributed" (a participant whose survey key did not pipe). Filtering to
    "study" is the supported way to get the analysis set; the unattributed runs
    are not discarded, because each one is a real consented encounter that a
    hand-join can still rescue."""
    check_key(key)
    from .runs import RUNS_DIR, completion_code

    out = []
    if RUNS_DIR.exists():
        for f in sorted(RUNS_DIR.glob("*.json")):
            try:
                run = json.loads(f.read_text(encoding="utf-8"))
            except ValueError:
                continue
            if cohort and run.get("cohort", "study") != cohort:
                continue
            out.append({
                "run_id": run["run_id"],
                "participant_id": run.get("participant_id"),
                "qualtrics_id": run.get("qualtrics_id"),
                "cohort": run.get("cohort", "study"),
                # Surfaced so a Qualtrics piping failure (a run carrying a
                # synthetic unattributed_* key) or a key presented with two
                # different survey responses is visible in the export itself,
                # not only in a server log line.
                "participant_key_status": run.get("participant_key_status"),
                # What actually arrived when the key was unusable. This is the
                # only thing an unattributed run can be hand-joined on, and the
                # export is where that join gets done, so withholding it here
                # would leave the run permanently unattributable. Null whenever
                # the key was fine.
                "raw_participant_key": run.get("raw_participant_key"),
                "qualtrics_id_conflict": bool(run.get("qualtrics_id_conflict")),
                "created_at": run.get("created_at"),
                "finished": run.get("index", 0) >= len(run.get("scenarios", [])),
                "completion_code": completion_code(run),
                # Present only on a run the participant stopped, or one that
                # ended because they declined the consent form. Without it the
                # export cannot distinguish a withdrawal from a dropout, and
                # "finished: false" reads the same for both — which is the
                # difference between a participant who left and one whose
                # browser died, and an IRB report needs the first number.
                "withdrawn": run.get("withdrawn"),
                # Which forms were steered by the cross-construct exclusion
                # rather than drawn, so an analyst who sees one variant
                # over-represented can tell design from chance.
                "form_exclusions": run.get("form_exclusions", []),
                "encounters": [
                    {
                        "scenario": c.get("id"),
                        "session_id": c.get("session_id"),
                        # The counterbalancing cell, which is otherwise only
                        # recoverable by re-deriving the order from `assigned`.
                        "position": i + 1,
                        "construct": c.get("construct"),
                        "variant": c.get("variant"),
                        "parallel_form": c.get("parallel_form"),
                        "no_participant_turns": bool(c.get("no_participant_turns")),
                    }
                    for i, c in enumerate(run.get("completed", []))
                ],
                "assigned": [sc.get("id") for sc in run.get("scenarios", [])],
            })
    return out


@app.get("/start")
async def start_run(
    key: Optional[str] = None,
    pid: Optional[str] = None,
    participant_id: Optional[str] = None,
    PROLIFIC_PID: Optional[str] = None,
    variant: Optional[str] = None,
    qid: Optional[str] = None,
    cohort: Optional[str] = None,
):
    """Entry point from Qualtrics.

    Qualtrics passes the participant key through as a query parameter; the exact
    name varies by how the survey is piped, so the common spellings are all
    accepted. The value itself is validated before use (see
    runs.normalize_participant_key): a broken pipe sends either nothing or the
    literal ${e://Field/...} placeholder, and taking either at face value
    silently corrupts the dataset. A returning participant with a usable key
    resumes their run rather than starting a second one under the same key.
    """
    check_participant(key)
    from fastapi.responses import RedirectResponse

    from . import runs

    raw_key = pid or participant_id or PROLIFIC_PID
    pkey, key_status = runs.normalize_participant_key(raw_key)
    if pkey is None:
        # Never turn a real participant away over the survey's broken link: they
        # are mid-study, and a 400 page costs the encounter data outright. They
        # proceed, but under a unique, clearly-marked identifier so the run
        # cannot masquerade as attributable study data and, crucially, so two
        # bad arrivals cannot collide into one shared run (which is exactly what
        # an unreplaced placeholder used to do: participant 2 landed inside
        # participant 1's half-finished run). The raw value is kept on the run
        # for a later hand-join, and the reject is logged so a piping failure
        # surfaces on the first arrival rather than at analysis time.
        pkey = f"unattributed_{secrets.token_hex(6)}"
        print(
            f"  WARNING: /start got an unusable participant key ({key_status}): "
            f"{raw_key!r}. Continuing as {pkey} in cohort 'unattributed'. "
            f"Check the Qualtrics ParticipantKey piping."
        )
    # A synthetic key is unique per arrival, so there is nothing to resume and
    # the directory scan would only ever miss.
    run = runs.find_for_participant(pkey) if key_status == "ok" else None
    if run is None:
        run = runs.create(
            pkey, variant=variant, qualtrics_id=qid,
            # An explicit ?cohort= is an operator's deliberate choice and is
            # honoured; otherwise a run only counts as study data when its key
            # is one we can actually attribute.
            cohort=(cohort or ("study" if key_status == "ok" else "unattributed")),
            key_status=key_status,
            raw_participant_key=(raw_key if key_status != "ok" else None),
        )
    elif qid and run.get("qualtrics_id") != qid:
        if not run.get("qualtrics_id"):
            # A returning participant may arrive with the qid we did not have yet.
            run["qualtrics_id"] = qid
        else:
            # A second, different survey response id under the same key means
            # the run<->survey join is no longer one-to-one (they retook the
            # survey, or two people are sharing a key). Keep the first id, the
            # one their encounters started under, but record every id seen and
            # flag the conflict so analysis notices instead of quietly joining
            # this run to the wrong response.
            seen = run.setdefault("qualtrics_ids_seen", [run["qualtrics_id"]])
            if qid not in seen:
                seen.append(qid)
            run["qualtrics_id_conflict"] = True
            print(
                f"  WARNING: run {run['run_id']} was presented a second "
                f"qualtrics_id ({qid!r}, first {run['qualtrics_id']!r}); "
                f"keeping the first and flagging the run."
            )
        runs.save(run)

    # Give the run one stable participant record and carry it in the redirect,
    # so identity.assign() hashes the same key across all four encounters (and
    # mid-encounter refreshes) instead of a fresh code per page load.
    #
    # The record is minted with consent_given=False. Minting it here is about
    # identity, not consent: this endpoint has no affirmative act from the
    # participant to record, and a record asserting consent that nobody gave is
    # exactly the defect the in-app gate exists to prevent. POST /api/consent
    # flips it once they have read the text and ticked the box. Until then the
    # voice endpoint refuses to open, so no capture can precede consent.
    pid_record = run.get("participant_record_id")
    if not pid_record:
        try:
            pid_record = create_participant(
                code=(pkey or run["run_id"]),
                consent_given=False,
                consent_version="",
            )
            run["participant_record_id"] = pid_record
            runs.save(run)
        except Exception:  # noqa: BLE001 , fall back to today's behavior
            pid_record = None

    q = f"?run={run['run_id']}"
    # Forward the key only when participant routes actually demand one.
    # SESSION_KEY is the researcher credential (see check_key): it opens
    # /api/runs, /api/encounters, the per-session download zips and the
    # researcher/director views. This redirect lands in the address bar of every
    # recruited person, so forwarding it unconditionally handed the whole
    # dataset's credential to ~100 participants. When PARTICIPANT_KEY_REQUIRED
    # is set the participant page cannot load without it, so it is forwarded
    # then and only then, and never in the normal open-collection deployment.
    if key and PARTICIPANT_KEY_REQUIRED:
        q += f"&key={key}"
    # Prefer the run's stable participant record; otherwise never drop a
    # participant_id that was handed to us.
    effective_pid = pid_record or participant_id
    if effective_pid:
        q += f"&participant_id={effective_pid}"
        # Tell the page whether this record still needs consent, so it shows the
        # gate on the first encounter of a run and skips it on the rest. Without
        # this the page would treat any participant_id as proof of consent and
        # never render the form.
        rec = get_participant(effective_pid)
        if not (rec or {}).get("consent_given"):
            q += "&consent=1"
    return RedirectResponse(url=f"/v2{q}", status_code=307)


@app.get("/v2", response_class=HTMLResponse)
async def v2_page(scenario: Optional[str] = None, key: Optional[str] = None):
    """Zoom-like multi-agent voice UI. Works for both single-agent and group
    scenarios (single-agent just shows one tile)."""
    check_participant(key)
    return (STATIC_DIR / "v2.html").read_text(encoding="utf-8")


# --- REST helpers ---

@app.get("/api/scenarios")
async def api_scenarios(key: Optional[str] = None):
    check_participant(key)
    return list_scenarios()


@app.get("/api/scenarios/{scenario_id}")
async def api_scenario_detail(scenario_id: str, key: Optional[str] = Query(None),
                              participant_id: Optional[str] = Query(None)):
    check_participant(key)
    try:
        sc = load_scenario(scenario_id, participant_id or "")
    except FileNotFoundError:
        raise HTTPException(404, "scenario not found")
    return {
        "id": sc.id,
        "title": sc.title,
        "intro": sc.intro,
        "briefing": getattr(sc, "briefing", None),
        "skill": sc.skill,
        "mode": sc.mode,
        "model": sc.model or DEFAULT_MODEL,  # effective default for the pre-start picker
        "intro_image": sc.intro_image,
        "cast": [
            {"id": a.id, "name": a.name, "role": a.role, "photo": a.photo}
            for a in sc.cast
        ],
        # Default persona values per agent, so the launch card can preselect
        # the current bands and stage only the gears the researcher changes.
        "personas": {aid: p.snapshot() for aid, p in sc.initial_personas().items()},
    }


# --- Researcher-initiated launch: configure gears first, then start ---
#
# POST /api/launch stores a one-shot config (scenario, model, gear presets,
# auto steering). The participant tab opens /v2?...&launch=<id>; when the
# participant's WS connects, the session is created and the presets are
# applied BEFORE the first turn, each logged as a knob_set event. The
# researcher page polls GET /api/launch/<id> until session_id appears, then
# connects and tracks the conversation live.

LAUNCHES: dict = {}


async def _apply_launch_config(session, cfg: dict) -> None:
    for aid, knobs in (cfg.get("knobs") or {}).items():
        if not isinstance(knobs, dict):
            continue
        for knob, value in knobs.items():
            try:
                await session.set_knob(
                    knob, float(value), agent_id=aid, reason="preset at launch"
                )
            except (KeyError, ValueError, TypeError, AttributeError) as e:
                session.store.event(
                    "launch_preset_error", agent_id=aid, knob=knob, message=str(e)
                )
    # Explicit both ways: a launch with the toggle Off overrides the
    # on-by-default behavior of participant-initiated sessions.
    await session.set_auto_steering(bool(cfg.get("auto_steering")))
    cfg["session_id"] = session.id


@app.post("/api/launch")
async def api_launch(body: dict, key: Optional[str] = Query(None)):
    check_key(key)
    scenario_id = body.get("scenario") or ""
    try:
        load_scenario(scenario_id)
    except FileNotFoundError:
        raise HTTPException(404, "scenario not found")
    launch_id = secrets.token_hex(4)
    model = body.get("model") or None
    LAUNCHES[launch_id] = {
        "scenario": scenario_id,
        "model": model,
        "auto_steering": bool(body.get("auto_steering")),
        "knobs": body.get("knobs") or {},
        "session_id": None,
    }
    url = f"/v2?scenario={scenario_id}&launch={launch_id}"
    if model:
        url += f"&model={model}"
    return {"launch_id": launch_id, "participant_url": url}


@app.get("/api/launch/{launch_id}")
async def api_launch_status(launch_id: str, key: Optional[str] = Query(None)):
    check_key(key)
    cfg = LAUNCHES.get(launch_id)
    if cfg is None:
        raise HTTPException(404, "unknown launch")
    return {"session_id": cfg["session_id"], "scenario": cfg["scenario"]}


def _session_dir(session_id: str) -> Path:
    """Resolve a session's on-disk directory, blocking path traversal."""
    if not session_id or "/" in session_id or ".." in session_id:
        raise HTTPException(400, "bad session_id")
    sdir = (SESSIONS_DIR / session_id).resolve()
    if not str(sdir).startswith(str(SESSIONS_DIR.resolve())):
        raise HTTPException(400, "bad session_id")
    if not sdir.is_dir():
        raise HTTPException(404, "session not found")
    return sdir


def _load_manifest(sdir: Path) -> dict:
    """Read a session's manifest, or 404 if it is missing/unreadable."""
    manifest = sdir / "manifest.json"
    if not manifest.exists():
        raise HTTPException(404, "session not found")
    try:
        return json.loads(manifest.read_text(encoding="utf-8"))
    except ValueError:
        raise HTTPException(404, "session not found")


def _require_session_owner(m: dict, participant_id: Optional[str]) -> None:
    """Bind a participant-open session route to the session's real owner.

    Without this, any known/guessed session id would let an outsider act on
    another participant's recorded session.
    """
    owner = m.get("participant_id")
    if not participant_id or not owner or participant_id != owner:
        raise HTTPException(403, "not your session")


def _require_owner_or_key(sdir: Path, participant_id: Optional[str], key: Optional[str]) -> None:
    """Gate a participant-open route that triggers a paid, blocking Claude call.

    A researcher (valid SESSION_KEY) or the participant who owns the session may
    proceed; anyone else is refused, so the open endpoint can't be used to spend
    the study's API budget on arbitrary session ids. When no SESSION_KEY is
    configured (local dev) the route stays open, matching the rest of the app.
    """
    if not SESSION_KEY:
        return
    if key == SESSION_KEY:
        return
    _require_session_owner(_load_manifest(sdir), participant_id)


def _count_user_turns(sdir: Path) -> int:
    """User turns actually recorded for a session, from its event trail."""
    ev = sdir / "events.jsonl"
    if not ev.exists():
        return 0
    n = 0
    try:
        for line in ev.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if e.get("type") == "user_turn":
                n += 1
    except Exception:  # noqa: BLE001
        return n
    return n


@app.get("/api/sessions/{session_id}/files")
async def api_session_files(session_id: str, key: Optional[str] = Query(None)):
    check_key(key)
    sdir = _session_dir(session_id)
    out = []
    for p in sorted(sdir.iterdir()):
        if p.is_file():
            out.append({"name": p.name, "size_bytes": p.stat().st_size})
    return out


@app.get("/api/sessions/{session_id}/download/{filename}")
async def api_session_file(
    session_id: str, filename: str, key: Optional[str] = Query(None)
):
    check_key(key)
    sdir = _session_dir(session_id)
    if "/" in filename or filename.startswith("."):
        raise HTTPException(400, "bad filename")
    p = (sdir / filename).resolve()
    if not str(p).startswith(str(sdir)) or not p.is_file():
        raise HTTPException(404, "file not found")
    return FileResponse(p, filename=f"{session_id}_{p.name}")


@app.get("/api/sessions/{session_id}/download.zip")
async def api_session_zip(session_id: str, key: Optional[str] = Query(None)):
    check_key(key)
    sdir = _session_dir(session_id)
    # Write the archive to a temp file on disk and stream it back with
    # FileResponse, so memory use is bounded regardless of session size (voice
    # sessions can hold hundreds of MB of WAV audio). ZIP_STORED, audio does
    # not compress, so deflate would only burn CPU.
    import tempfile
    from starlette.background import BackgroundTask

    fd, tmp_path = tempfile.mkstemp(prefix=f"{session_id}_", suffix=".zip")
    os.close(fd)
    try:
        with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_STORED) as zf:
            for p in sorted(sdir.iterdir()):
                if p.is_file():
                    # Under the encounter id, not bare. Every encounter's files
                    # are named identically (record.json, events.jsonl,
                    # user_audio.wav), so flat entries meant that unpacking a
                    # wave into one directory silently overwrote all but the
                    # last — and the documented bulk pull does exactly that.
                    zf.write(p, arcname=f"{session_id}/{p.name}")
    except Exception:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise
    return FileResponse(
        tmp_path,
        media_type="application/zip",
        filename=f"{session_id}.zip",
        background=BackgroundTask(os.remove, tmp_path),
    )


@app.get("/api/sessions/{session_id}/score")
async def api_get_score(session_id: str, key: Optional[str] = Query(None)):
    """Return the cached relational-fluency score for a session, if scored."""
    check_key(key)
    _session_dir(session_id)  # validates id / existence
    from .scoring import load_cached_score
    score = load_cached_score(session_id)
    if score is None:
        raise HTTPException(404, "not scored yet")
    return score


@app.post("/api/sessions/{session_id}/score")
async def api_post_score(
    session_id: str,
    force: bool = Query(False),
    model: Optional[str] = Query(None),
    participant_id: Optional[str] = Query(None),
    key: Optional[str] = Query(None),
):
    """Run (or re-run with force=1) the offline scorer. Blocking Claude call,
    so it runs in a threadpool to keep the event loop free."""
    # The /v2 feedback overlay lets a participant score their OWN session
    # (keyless in production), so gate on ownership-or-key: this is a paid Claude
    # call and must not be triggerable against arbitrary session ids.
    sdir = _session_dir(session_id)
    _require_owner_or_key(sdir, participant_id, key)
    from starlette.concurrency import run_in_threadpool
    from .scoring import TranscriptError, score_session
    try:
        return await run_in_threadpool(
            score_session, session_id, force=force, model=model
        )
    except TranscriptError as e:
        raise HTTPException(422, str(e))
    except Exception as e:
        raise HTTPException(500, f"scoring failed: {type(e).__name__}: {e}")


@app.get("/api/sessions/{session_id}/debrief")
async def api_get_debrief(session_id: str, key: Optional[str] = Query(None)):
    """Return the cached per-persona debrief for a group session, if generated."""
    check_key(key)
    _session_dir(session_id)
    from .debrief import load_cached_debrief
    debrief = load_cached_debrief(session_id)
    if debrief is None:
        raise HTTPException(404, "not debriefed yet")
    return debrief


@app.post("/api/sessions/{session_id}/debrief")
async def api_post_debrief(
    session_id: str,
    force: bool = Query(False),
    model: Optional[str] = Query(None),
    participant_id: Optional[str] = Query(None),
    key: Optional[str] = Query(None),
):
    """Run (or re-run with force=1) the per-persona debrief. Blocking Claude
    call, so it runs in a threadpool to keep the event loop free."""
    # Participant-invoked from the /v2 debrief overlay (keyless in production);
    # gate on ownership-or-key like the scorer so this paid call can't be run
    # against arbitrary sessions.
    sdir = _session_dir(session_id)
    _require_owner_or_key(sdir, participant_id, key)
    from starlette.concurrency import run_in_threadpool
    from .debrief import generate_debrief
    from .scoring import TranscriptError
    try:
        return await run_in_threadpool(
            generate_debrief, session_id, force=force, model=model
        )
    except TranscriptError as e:
        raise HTTPException(422, str(e))
    except Exception as e:
        raise HTTPException(500, f"debrief failed: {type(e).__name__}: {e}")


@app.get("/api/consent")
async def api_get_consent(key: Optional[str] = None):
    check_participant(key)
    return _load_consent()


@app.post("/api/consent")
async def api_post_consent(payload: dict, key: Optional[str] = Query(None)):
    check_participant(key)
    code = (payload.get("code") or "").strip()
    consent_given = bool(payload.get("consent_given"))
    existing = (payload.get("participant_id") or "").strip()
    if not consent_given:
        raise HTTPException(400, "consent_given must be true to proceed")
    version = _load_consent().get("version", "unknown")
    # A run that came through /start already has a participant record, minted
    # there with consent_given=False so identity is stable across the four
    # encounters. Flip that record rather than minting a second one, or the
    # participant would end up with one identity per encounter again.
    if existing:
        rec = record_consent(existing, version)
        if rec is None:
            raise HTTPException(404, "no such participant record")
        return {"participant_id": existing, "consent_text_version": version}
    if not code:
        raise HTTPException(400, "code required")
    pid = create_participant(code=code, consent_given=True, consent_version=version)
    return {"participant_id": pid, "consent_text_version": version}


@app.post("/api/consent/decline")
async def api_post_consent_decline(payload: dict, key: Optional[str] = Query(None)):
    """The participant read the consent form and chose not to take part.

    Recorded rather than ignored: how many people decline after reading the form
    is a number an IRB asks for, and without a record a refusal looks exactly
    like a browser crash. Nothing here can grant consent, so this is safe on the
    participant's side of the gate.

    Answers 200 even when there is no record to mark. The page has already told
    the participant they are finished, and re-prompting someone who has just
    refused would be a worse failure than a thinner record.
    """
    check_participant(key)
    from . import runs

    version = _load_consent().get("version", "unknown")
    pid = (payload.get("participant_id") or "").strip()
    run_id = (payload.get("run_id") or "").strip()
    recorded = False
    if pid:
        recorded = record_decline(pid, version, run_id=run_id or None) is not None
    # Stop the run as well, so reopening the study link cannot enrol someone who
    # declined into the encounters they just refused.
    if run_id and runs.get(run_id):
        runs.withdraw(run_id, reason="declined_consent")
    return {"recorded": recorded, "consent_text_version": version}


@app.post("/api/run")
async def api_run_create(request: Request, key: Optional[str] = None):
    """Start a run: four encounters, one per construct, counterbalanced.

    The other entrance to run creation, next to /start. It validates the
    participant key the same way and for the same reason: an unvalidated or
    absent key minted a cohort='study' run that no survey response could ever be
    joined to, and — worse — every arrival carrying the same unreplaced
    ${e://Field/...} placeholder collided into one shared run. Closing that on
    /start alone would have left the failure reachable through this endpoint,
    which is open in the normal deployment.
    """
    check_participant(key)
    from . import runs

    body = {}
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001, empty body is fine
        pass
    # The body is caller-supplied JSON, so participant_id need not be a string.
    # Anything else is treated as absent rather than handed to the validator.
    raw = body.get("participant_id")
    raw_key = raw if isinstance(raw, str) else None
    pkey, key_status = runs.normalize_participant_key(raw_key)
    if pkey is None:
        # Same trade as /start: the caller is not turned away, but the run is
        # given a unique synthetic identifier and marked unattributed so it
        # cannot pass as study data, with the raw value kept for a hand-join.
        pkey = f"unattributed_{secrets.token_hex(6)}"
        print(
            f"  WARNING: POST /api/run got an unusable participant key "
            f"({key_status}): {raw!r}. Continuing as {pkey} in cohort "
            f"'unattributed'."
        )
    run = runs.create(
        pkey,
        cohort=("study" if key_status == "ok" else "unattributed"),
        key_status=key_status,
        raw_participant_key=(raw_key if key_status != "ok" else None),
    )
    return runs.view(run)


@app.get("/api/sessions/{session_id}/video-upload-url")
async def api_video_upload_url(session_id: str, participant_id: Optional[str] = None,
                               key: Optional[str] = None):
    """Presigned PUT for the participant's webcam recording. Participant-open:
    it grants a write to exactly one object, for one hour.

    The session must exist and belong to the presenting participant, and a
    presign is refused once the object is already there, so nobody can point a
    write at another participant's session or overwrite a finished recording.
    """
    check_participant(key)
    from . import video

    sdir = _session_dir(session_id)  # existence + traversal check
    _require_session_owner(_load_manifest(sdir), participant_id)
    if video.uploaded_size(session_id) > 0:
        raise HTTPException(409, "video already uploaded")
    return video.presign_upload(session_id)


@app.post("/api/sessions/{session_id}/video-uploaded")
async def api_video_uploaded(session_id: str, participant_id: Optional[str] = None,
                             key: Optional[str] = None):
    """Client confirms the upload; verified against S3 and written into the
    session's event trail so verify_record can check for it."""
    check_participant(key)
    from . import video

    sdir = _session_dir(session_id)  # existence + traversal check
    _require_session_owner(_load_manifest(sdir), participant_id)

    size = video.uploaded_size(session_id)
    import json as _json
    import time as _time

    with open(sdir / "events.jsonl", "a", encoding="utf-8") as fh:
        fh.write(_json.dumps({
            "t": None, "wall": _time.time(), "type": "video_uploaded",
            "key": video.video_key(session_id), "bytes": size,
        }) + "\n")

    # Rebuild the aligned record now that the video is known.
    #
    # record.json is written by SessionStore.close, and the browser only
    # confirms the upload afterwards — it finishes the recording and closes the
    # socket in the same breath — so the record built at close always said the
    # encounter had no video. Measured over a synthetic wave: 25 of 27
    # encounters had a video and record.json reported none on every one. That
    # matters because record.json is the artefact the study ships to raters and
    # analysts, and the video is the thing Phase 2 rates. The API rebuilds the
    # record on read, so this only ever affected the stored copy — which is
    # exactly the copy that leaves the machine, in the download zip.
    try:
        from .encounter_record import write as _write_record
        _write_record(sdir)
    except Exception:  # noqa: BLE001, never fail the upload confirmation on this
        pass
    return {"ok": size > 0, "bytes": size, "key": video.video_key(session_id)}


@app.get("/api/run/config")
async def api_run_config(key: Optional[str] = None):
    """Where a finished participant is sent back to."""
    check_participant(key)
    return {
        "return_url": os.getenv("SURVEY_RETURN_URL", "").strip(),
        "return_label": os.getenv("SURVEY_RETURN_LABEL", "Return to the survey"),
    }


@app.get("/api/run/{run_id}")
async def api_run_get(run_id: str, key: Optional[str] = None):
    check_participant(key)
    from . import runs

    run = runs.get(run_id)
    if run is None:
        raise HTTPException(404, "no such run")
    return runs.view(run)


@app.post("/api/run/{run_id}/withdraw")
async def api_run_withdraw(run_id: str, payload: Optional[dict] = None,
                           key: Optional[str] = None):
    """The participant stopped the study.

    The consent text promises they may stop at any time, and honouring that
    needs more than ending the current conversation: the run has to stop handing
    out encounters, or reopening the study link enrols them in the rest. It also
    has to leave a trace, so an analyst can tell a withdrawal from a dropout.

    Returns the run view, which carries the completion code. Someone who stops
    part-way has still given us their time, and the partial code is what they
    take back to the survey to be paid.
    """
    check_participant(key)
    from . import runs

    body = payload or {}
    run = runs.withdraw(
        run_id,
        session_id=(body.get("session_id") or "").strip() or None,
        reason=(body.get("reason") or "").strip() or None,
    )
    if run is None:
        raise HTTPException(404, "no such run")
    return runs.view(run)


@app.post("/api/run/{run_id}/advance")
async def api_run_advance(run_id: str, session_id: Optional[str] = None,
                          key: Optional[str] = None):
    """Mark the current encounter complete and move to the next.

    The completion code is proof of completion, so an encounter is only marked
    done against a real recorded session that belongs to this run's
    participant, matches the current encounter's scenario, and actually had the
    participant speak. Otherwise the open endpoint would mint valid completion
    codes for runs that never happened.
    """
    check_participant(key)
    from . import runs

    run = runs.get(run_id)
    if run is None:
        raise HTTPException(404, "no such run")

    if not session_id:
        raise HTTPException(400, "session_id required")

    # Idempotency comes first: if this session was already recorded complete (a
    # retried or duplicated advance — e.g. the client re-POSTs after a dropped
    # response, or the End button races the auto-complete), return the run
    # unchanged. Re-running the checks below would compare against the NOW-current
    # encounter (the next scenario), so the scenario/owner checks would wrongly
    # 409/403 and permanently strand the participant.
    if any(c.get("session_id") == session_id for c in run.get("completed", [])):
        return runs.view(run)

    sdir = _session_dir(session_id)  # existence + traversal check
    m = _load_manifest(sdir)

    expected_owner = run.get("participant_record_id")
    if expected_owner and m.get("participant_id") != expected_owner:
        raise HTTPException(403, "session does not belong to this run")

    idx = run.get("index", 0)
    scenarios = run.get("scenarios", [])
    if idx < len(scenarios):
        expected_scenario = scenarios[idx].get("id")
        if m.get("scenario") != expected_scenario:
            raise HTTPException(409, "session scenario does not match current encounter")

    # An encounter that recorded nothing is a real problem, but refusing to
    # advance is the wrong response to it: the participant would be stuck on
    # encounter 1 for the rest of the study with no completion code and no way
    # out, and a broken microphone would cost us the whole session rather than
    # one encounter. Let the run move on and record that this one was empty, so
    # the run is visibly incomplete in the data instead of silently missing from
    # it. `runs.advance` copies the entry through, so the flag lands on the
    # completed encounter.
    empty = _count_user_turns(sdir) < 1 and (m.get("n_turns") or 0) < 1
    if empty:
        idx = run.get("index", 0)
        if idx < len(run.get("scenarios", [])):
            run["scenarios"][idx]["no_participant_turns"] = True
            runs.save(run)

    run = runs.advance(run_id, session_id)
    if run is None:
        raise HTTPException(404, "no such run")
    return runs.view(run)


@app.get("/director", response_class=HTMLResponse)
async def director_page(key: Optional[str] = None):
    """Steering dashboard, what the director told each actor, and what it said."""
    check_key(key)
    return (STATIC_DIR / "director.html").read_text(encoding="utf-8")


@app.get("/evidence", response_class=HTMLResponse)
async def evidence_page(key: Optional[str] = None):
    """Evidence Trace: the aligned research view — participant, the direction
    that shaped each reply, the actor's line, fidelity against the instrument,
    and a replay synced to turns."""
    check_key(key)
    return (STATIC_DIR / "evidence.html").read_text(encoding="utf-8")


def _encounter_status(m: dict) -> str:
    """A coarse, cheap quality status for the encounter list, derived from the
    manifest alone: active (still recording), complete, partial (a channel is
    missing), or failed (nothing recorded). 'flagged' (a validity flag needing
    a look) is a later, richer check."""
    if m.get("status") == "active":
        return "active"
    n = m.get("n_turns") or 0
    if n <= 0:
        return "failed"
    audio = m.get("audio") or {}
    ua = audio.get("user_audio_duration_s")
    aa = audio.get("assistant_audio_duration_s_by_agent") or {}
    if ua is None:
        return "complete"  # text-mode session: no audio channel expected
    if ua and any(v for v in aa.values()):
        return "complete"
    return "partial"


# How many session directories one /api/encounters call may open. A wave is
# ~100 participants x 4 encounters, so this is several waves' worth of sessions
# plus test traffic: a filtered dashboard poll cannot turn into a full scan of
# the volume, and no realistic study loses an encounter to the bound.
ENCOUNTER_SCAN_LIMIT = 2000


@app.get("/api/encounters")
async def api_encounters(key: Optional[str] = None, limit: int = 60,
                         cohort: Optional[str] = None):
    """Recorded encounters, newest first, for the steering dashboard.

    `cohort` filters on the manifest's own cohort tag, so internal test traffic
    can be excluded here rather than only through the /api/runs join. Three
    values are minted: "study", "internal" (the /test entrance) and
    "unattributed" (a participant whose survey key did not pipe, kept out of the
    study set until someone hand-joins them). Sessions recorded before the tag
    existed, and sessions started outside a run, have no cohort and are
    therefore excluded by any filter.
    """
    check_key(key)
    from .storage import SESSIONS_DIR

    out = []
    dirs = sorted(
        (d for d in SESSIONS_DIR.iterdir() if d.is_dir()),
        key=lambda d: d.stat().st_mtime,
        reverse=True,
    )
    for scanned, d in enumerate(dirs):
        # Collect `limit` entries rather than slicing the directory list first:
        # with a cohort filter a leading slice would return fewer than `limit`
        # matches (often none) even when older matching encounters exist. The
        # cost is that a filtered call no longer stops early — it keeps opening
        # manifests until it has `limit` matches or runs out of directories —
        # and the researcher dashboard polls this endpoint, so the scan is
        # bounded: newest-first, at most ENCOUNTER_SCAN_LIMIT directories
        # examined. (The listdir and the stat-based sort above are O(all
        # sessions) either way; what this bounds is the JSON parse.) A filter
        # that reaches the bound stops at the oldest encounter it saw; older
        # ones are still reachable through the /api/runs join.
        if len(out) >= limit or scanned >= ENCOUNTER_SCAN_LIMIT:
            break
        manifest = d / "manifest.json"
        if not manifest.exists():
            continue
        try:
            m = json.loads(manifest.read_text(encoding="utf-8"))
        except ValueError:
            continue
        if cohort and (m.get("cohort") or "") != cohort:
            continue
        entry = {
            "id": d.name,
            "scenario": m.get("scenario"),
            "started_at": m.get("started_at"),
            "participant_id": m.get("participant_id"),
            # Self-describing study context, straight off the manifest.
            "run_id": m.get("run_id"),
            "cohort": m.get("cohort"),
            "encounter_index": m.get("encounter_index"),
            "status": _encounter_status(m),
        }
        try:
            from .scenarios_v3 import load_spec
            spec = load_spec(m.get("scenario"))
            entry.update(
                construct=spec["construct"], variant=spec["variant"],
                title=spec["title"], study=True,
            )
        except Exception:
            entry["study"] = False
        out.append(entry)
    return out


@app.get("/api/encounters/{session_id}/record")
async def api_encounter_record(session_id: str, key: Optional[str] = None):
    """The aligned record: transcript, stage directions, triggers, provenance."""
    check_key(key)
    from .encounter_record import build
    from .storage import SESSIONS_DIR

    d = SESSIONS_DIR / session_id
    if not d.exists():
        raise HTTPException(status_code=404, detail="No such encounter")
    record = build(d)
    # Trigger firings are not in the aligned record; the dashboard wants them.
    #
    # The event log is append-only, so a beat that was briefed and then not
    # delivered leaves its trigger_fired line behind, cancelled by a later
    # trigger_undelivered. Counting the raw firings would report a beat nobody
    # spoke as reached, and that count is what a researcher uses to decide
    # whether an encounter is scoreable. verify_record owns the netting rule
    # (it is positional, because a retry re-fires at the same index); reuse it
    # rather than keeping a second copy that can drift.
    from .verify_record import _net_fired

    fired = []
    ev = d / "events.jsonl"
    if ev.exists():
        events = []
        for line in ev.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                events.append(json.loads(line))
            except ValueError:
                continue
        fired = [
            {
                "t": e.get("t"), "trigger_id": e.get("trigger_id"),
                "interaction": e.get("interaction"), "esci": e.get("esci", []),
                "probing": e.get("probing"),
            }
            for e in _net_fired(events)
        ]
    record["triggers_fired"] = fired

    # Attach the scenario's own plan so the dashboard can show coverage, which
    # interaction each turn belongs to by name, and which planted triggers were
    # reached out of those the instrument specifies.
    try:
        from .scenarios_v3 import load_spec
        spec = load_spec(record.get("scenario"))
        record["spec"] = {
            "construct": spec["construct"],
            "variant": spec["variant"],
            "title": spec["title"],
            "parallel_form": spec.get("parallel_form"),
            "skill_measured": spec.get("skill_measured", "").strip(),
            "esci_items": spec.get("esci_items", {}),
            "interactions": [
                {
                    "id": i["id"], "label": i.get("label", ""), "mode": i["mode"],
                    "observe": i.get("observe", ""),
                    "triggers": [
                        {"id": t["id"], "esci": t.get("esci", []), "cue": t.get("cue", ""),
                         "probe": bool(t.get("on_silence")), "scored": bool(t.get("scores"))}
                        for t in i.get("triggers", [])
                    ],
                }
                for i in spec.get("interactions", [])
            ],
        }
    except Exception:
        record["spec"] = None  # legacy scenario, not part of the study bank

    # The plan attached above is today's file, but this encounter ran against
    # the plan as it stood when it started, and the two diverge: S2 A gained two
    # planted beats between waves, which silently moved the denominator of every
    # S2 A encounter recorded before the edit from 4/4 to 4/6. Compare the
    # fingerprint stamped at session start (storage.spec_fingerprint) with the
    # current file and say so, so a coverage figure that dropped because the
    # instrument moved is not read as an encounter that went worse. Null when
    # they agree, and when the encounter predates the stamp — in that case
    # nothing can be said either way, which is why the stamp exists.
    record["spec_drift"] = None
    try:
        from .storage import spec_fingerprint

        stamped = record.get("spec_fingerprint") or None
        current = spec_fingerprint(record.get("scenario"))
        if stamped and current and stamped.get("sha256") != current.get("sha256"):
            was = list(stamped.get("trigger_ids") or [])
            now = list(current.get("trigger_ids") or [])
            record["spec_drift"] = {
                "recorded_sha256": stamped.get("sha256"),
                "current_sha256": current.get("sha256"),
                "recorded_trigger_ids": was,
                "current_trigger_ids": now,
                "added_since": [t for t in now if t not in was],
                "removed_since": [t for t in was if t not in now],
            }
    except Exception:  # noqa: BLE001, a drift check must not 500 the record view
        pass
    return record


@app.get("/api/sessions")
async def api_sessions(key: Optional[str] = None, cohort: Optional[str] = None,
                       limit: int = 30, offset: int = 0):
    """Live and recent sessions. `cohort` filters to exactly one of the three
    tags a run can carry — "study", "internal", "unattributed" — so internal
    test traffic and unpiped-key arrivals can both be kept out of a listing.
    Sessions started outside a run have no tag and match no filter.

    `limit`/`offset` page the closed sessions. The default of 30 is the
    dashboard's page size, but this is also the only bulk source of per-encounter
    duration, so a fixed cap of 30 made 370 of a 400-encounter wave invisible to
    every analysis that needed it. Capped at 1000 per call so a stray request
    cannot pull a whole wave through one synchronous query."""
    check_key(key)
    out = []
    active_ids = set(registry.list_ids())
    for sid in active_ids:
        s = registry.get(sid)
        if cohort and (s.cohort or "") != cohort:
            continue
        out.append({
            "id": s.id,
            "scenario": s.scenario.id,
            "title": s.scenario.title,
            "mode": s.scenario.mode,
            "model": s.model,
            "cast_size": len(s.scenario.cast),
            "turn_count": sum(1 for h in s.shared_history if h["speaker"] == "user"),
            "status": "active",
            "started_at": s.store.started_at,
            "run_id": s.run_id,
            "cohort": s.cohort,
        })
    # Append recent closed sessions from SQLite so the researcher can browse +
    # download past data even after a session ends.
    #
    # sqlite3 is synchronous and this coroutine shares its event loop with every
    # live encounter: while it blocks, participant PCM is not relayed and the
    # silence detector that fires the planted probes is not fed. The researcher
    # dashboard polls this route every few seconds throughout a wave, so the
    # disk-bound half — the query, and the scenario-title map on its cold call —
    # runs in a worker thread and the loop stays free for the audio.
    from starlette.concurrency import run_in_threadpool

    def _read_closed() -> tuple:
        import sqlite3
        from .storage import DB_PATH

        conn = sqlite3.connect(DB_PATH)
        try:
            conn.row_factory = sqlite3.Row
            # run_id/cohort were added to the sessions table later. A database
            # written before that migration must still list its closed sessions
            # (the caller swallows any failure here, so naming a missing column
            # would silently empty the researcher's list), so select them only
            # when they are actually there.
            have = {r[1] for r in conn.execute("PRAGMA table_info(sessions)")}
            extra = [c for c in ("run_id", "cohort") if c in have]
            rows = conn.execute(
                "SELECT id, scenario, model, started_at, n_turns, status, duration_s"
                + "".join(f", {c}" for c in extra)
                + " FROM sessions WHERE status != 'active'"
                  " ORDER BY started_at DESC LIMIT ? OFFSET ?",
                (max(1, min(int(limit), 1000)), max(0, int(offset))),
            ).fetchall()
        finally:
            # Closed in the thread that opened it, and closed even on failure:
            # a leaked connection here would hold the WAL open against the
            # writers that every live session's storage layer uses.
            conn.close()
        # Plain dicts, so nothing sqlite-owned outlives the worker thread.
        # Missing columns read back as None rather than raising, which is what
        # the pre-migration database needs.
        # Map scenario id → title without re-reading every YAML each call.
        titles = {s["id"]: s["title"] for s in list_scenarios()}
        return [dict(r) for r in rows], titles

    try:
        rows, titles = await run_in_threadpool(_read_closed)
    except Exception:  # noqa: BLE001, a missing index must not empty the listing
        rows, titles = [], {}
    for r in rows:
        if r["id"] in active_ids:
            continue
        row_cohort = r.get("cohort")
        if cohort and (row_cohort or "") != cohort:
            continue
        out.append({
            "id": r["id"],
            "scenario": r["scenario"],
            "title": titles.get(r["scenario"], r["scenario"]),
            "model": r["model"],
            "turn_count": r["n_turns"] or 0,
            "status": r["status"] or "closed",
            "started_at": r["started_at"],
            "duration_s": r["duration_s"],
            "run_id": r.get("run_id"),
            "cohort": row_cohort,
        })
    return out


def _run_context(participant_id: Optional[str], run_id: Optional[str] = None) -> Optional[dict]:
    """Resolve the run this encounter belongs to, server-side.

    A recorded encounter has to be self-describing. Without run id, cohort and
    participant key on its own manifest, the only link from a session back to
    its run was the entry the browser POSTs to /api/run/{id}/advance, so an
    encounter whose client never reported back was orphaned, and no offline tool
    (verify_record, scoring, retranscribe) could tell internal test traffic from
    study data.

    The socket carries the participant *record* id, so the run is looked up from
    that; a ?run= hint is honoured only when it names this same participant's
    run, so nobody can attach their encounter to a stranger's run. Any failure
    resolves to None: an encounter must never fail to start over bookkeeping.
    """
    if not participant_id:
        return None
    try:
        from . import runs

        run = runs.get(run_id) if run_id else None
        if run is None or run.get("participant_record_id") != participant_id:
            run = runs.find_by_participant_record(participant_id)
        if run is None:
            return None
        return {
            "run_id": run.get("run_id"),
            "cohort": run.get("cohort", "study"),
            "participant_key": run.get("participant_id"),
            # 1-based position in the four-encounter sequence as this encounter
            # starts, so the record keeps its place in the run's order even if
            # the run file is later lost.
            "encounter_index": (run.get("index") or 0) + 1,
        }
    except Exception:  # noqa: BLE001, never block an encounter on this
        return None


# --- Phase 2: human rating ---
#
# Phase 1 records encounters; Phase 2 turns them into gold labels. Two or three
# independent raters score each recorded encounter on all 22 ESCI Relationship
# Management items, and reliability is computed before anything is modelled.
#
# Two credentials meet in this section, and they are deliberately disjoint:
#
#   SESSION_KEY (check_key) opens the study data: the rater roster, the
#       assignment plan, the ratings export, the reliability report. It is the
#       researcher's credential and it is the same one that already opens
#       /researcher, /evidence and every download route.
#
#   rt_<32 hex> (a rater token) opens exactly one rater's own work: their
#       assignments, the packet for each of those assignments, and their own
#       submissions. Nothing else. It is issued per rater, it expires, and it
#       can be revoked.
#
# Neither credential is accepted where the other one belongs. check_key compares
# against SESSION_KEY and never consults the rater roster, so a rater token can
# never open a researcher route. The rater routes never call check_key, so
# SESSION_KEY cannot be used to walk into a rater's console and submit under
# their name — which matters, because the whole point of independent raters is
# that each score has one identifiable author.
#
# Rater auth is positive validation (resolve the token, or refuse), not a
# comparison against a configured secret. That is why the "SESSION_KEY is unset,
# so everything is open" posture of local development does not extend here: with
# no token, or an unknown or expired one, a rater route answers 401 regardless
# of how the server is configured.
#
# Two blinding rules are enforced by what these routes choose to return:
#
#   A rater sees a *rating code*, never a session id, and never the participant
#   key. The rating code is the only handle they can quote in a bug report or a
#   calibration meeting, and it cannot be turned back into a participant.
#
#   Requesting an assignment that belongs to somebody else is 404, not 403. A
#   403 would confirm that the assignment exists, which lets a rater enumerate
#   the wave one id at a time and learn how many encounters were recorded, and
#   who else is rating what. As far as a rater's token is concerned, the rest of
#   the study does not exist.

# The items are not ours to hand out. The rating instrument's own header says so
# and this is the point where the items leave the building, so the warning
# travels with them: on the rater's packet (where a human is about to read the
# items) and on the ratings export (where the item ids leave for analysis). It
# is a field of the response, not a comment in a file nobody opens.
ITEM_LICENSE_NOTICE = (
    "ESCI items are a proprietary instrument (Boyatzis, Goleman & Korn Ferry), "
    "reproduced for research reference only. Confirm licensing/permission "
    "before fielding."
)

# The modules that own the items attach their own wording under their own field
# names (rater_packet: instrument_notice, reliability: item_source_notice). This
# route layer's job is to make sure the warning is *there*, not to stamp a second
# copy of it next to theirs — two notices reading slightly differently is how a
# reader learns to skip both.
_NOTICE_FIELDS = ("notice", "instrument_notice", "item_source_notice")


def _with_notice(payload: dict) -> dict:
    """Guarantee the licensing warning on a body that carries the items."""
    if any(payload.get(f) for f in _NOTICE_FIELDS):
        return payload
    payload["notice"] = ITEM_LICENSE_NOTICE
    return payload


def _json_safe(value):
    """Replace non-finite floats with null, recursively.

    JSON has no NaN and no infinity, and Starlette serialises with
    allow_nan=False, so a single NaN anywhere in a response body is a 500 with
    nothing in it — not a missing field, the whole report. A reliability
    coefficient genuinely can be undefined (Krippendorff's alpha over an item
    with no observed disagreement has 0/0 in it, and the study wave produced
    exactly that on ESCI-15), and "undefined" is null.

    This is a serialisation repair, not a statistics decision: the module that
    computes the number should be saying None itself, and every row already
    carries the n and rater counts that say why a coefficient is missing. Until
    it does, a researcher gets the other twenty-one items instead of a 500.
    """
    if isinstance(value, float):
        return value if -float("inf") < value < float("inf") else None
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return value


def _rater_from_token(token: Optional[str]) -> dict:
    """Resolve a rater token, or refuse.

    The only entry point for rater-facing authentication. An unknown token and
    an expired one are the same answer on purpose: a rater whose 30 days ran out
    should be told to ask for a fresh link, not told that their token was once
    real.
    """
    from . import raters

    tok = (token or "").strip()
    if not tok:
        raise HTTPException(401, "Bad or missing rater token")
    rater = raters.rater_for_token(tok)
    if rater is None:
        raise HTTPException(401, "Bad or missing rater token")
    return rater


def _rater_assignment(rater: dict, assignment_id: str) -> dict:
    """Load one assignment and prove it belongs to this rater.

    Missing and not-yours collapse into the same 404 (see the blinding note
    above). Both are answered with the same message, so response text cannot be
    used to tell them apart either.
    """
    from . import raters

    assignment = raters.get_assignment((assignment_id or "").strip())
    if assignment is None or assignment.get("rater_id") != rater.get("rater_id"):
        raise HTTPException(404, "no such assignment")
    return assignment


def _rateable_sessions(cohort: str) -> list:
    """Session ids in one cohort that are worth putting in front of a rater.

    Assignment can be driven either by an explicit list of session ids or by a
    cohort, and the cohort form is the one a researcher actually uses at the end
    of a wave ("assign everything in `study`"). Resolving it here rather than in
    raters.assign keeps the roster module out of the session index.

    Two filters, both narrow on purpose. `status != 'active'` excludes an
    encounter that is still recording — its record.json does not exist yet.
    `n_turns > 0` excludes an encounter in which the participant never spoke;
    there is nothing to score, and an unscoreable packet costs a rater's time
    and pollutes the reliability denominator. Nothing else is filtered here:
    whether an encounter is *good enough* to rate is a study decision, and it is
    made by the researcher who picks the cohort, not by this query.
    """
    import sqlite3
    from .storage import DB_PATH

    conn = sqlite3.connect(DB_PATH)
    try:
        # cohort arrived in a later migration; a pre-migration index has no
        # such column and naming it would raise rather than return nothing.
        have = {r[1] for r in conn.execute("PRAGMA table_info(sessions)")}
        if "cohort" not in have:
            return []
        rows = conn.execute(
            "SELECT id FROM sessions"
            " WHERE status != 'active' AND cohort = ?"
            "   AND COALESCE(n_turns, 0) > 0"
            " ORDER BY started_at ASC",
            (cohort,),
        ).fetchall()
    finally:
        # Closed in the thread that opened it, and closed even on failure: a
        # leaked connection holds the WAL open against every live session's
        # writer.
        conn.close()
    return [r[0] for r in rows]


# --- Rating console: rater-facing, token only ---

@app.get("/rate", response_class=HTMLResponse)
async def rater_page(token: Optional[str] = Query(None)):
    """The rating console. Gated like every other console page in this file, on
    the credential that belongs to it: a rater arrives on a link that already
    carries their token, and a stale link should say so plainly here rather than
    render an empty console that fails on its first fetch."""
    _rater_from_token(token)
    page = STATIC_DIR / "rater.html"
    if not page.exists():
        raise HTTPException(404, "rating console is not installed")
    return page.read_text(encoding="utf-8")


@app.get("/api/rater/me")
async def api_rater_me(token: Optional[str] = Query(None)):
    """Who this token belongs to, and how much work is left."""
    from . import raters

    rater = _rater_from_token(token)
    pending = raters.assignments_for_rater(rater["rater_id"], status="pending")
    return {
        "rater_id": rater["rater_id"],
        "name": rater.get("name"),
        "kind": rater.get("kind"),
        "assignments_pending": len(pending),
        # Carried here as well as on the packet so the console can keep the
        # licensing notice in its chrome, visible on every screen, instead of
        # only on the one where the items are rendered.
        "notice": ITEM_LICENSE_NOTICE,
    }


@app.get("/api/rater/assignments")
async def api_rater_assignments_mine(token: Optional[str] = Query(None),
                                     status: Optional[str] = Query(None)):
    """This rater's queue.

    Four fields, and the omissions are the design. No session id (the rating
    code is the rater-facing handle). No construct: the instrument requires
    raters to be blind to the scenario's primary-competency designation, and
    every encounter is rated on all 22 items regardless, so telling a rater
    which competency the encounter was built to elicit would bias the other 16
    or 17 items. No participant key, no scenario id.
    """
    from . import rater_packet, raters

    rater = _rater_from_token(token)
    want = (status or "").strip() or None
    return [
        {
            "assignment_id": a.get("assignment_id"),
            "rating_code": rater_packet.rating_code(a.get("session_id")),
            "status": a.get("status"),
            "assigned_at": a.get("assigned_at"),
        }
        for a in raters.assignments_for_rater(rater["rater_id"], status=want)
    ]


@app.get("/api/rater/packet/{assignment_id}")
async def api_rater_packet(assignment_id: str, token: Optional[str] = Query(None)):
    """The blinded packet for one assignment: the situation the participant saw,
    the transcript, the video, and the items.

    What is *in* the packet is rater_packet.build's contract, not this route's,
    and this route does not second-guess it. In particular it does not mint a
    playback URL of its own: the packet builds its media block from
    video.playback_url and keeps three states apart there — a playable video, an
    encounter that never had one, and a video whose link could not be signed —
    and a second URL minted here would flatten that distinction and sign the
    same object twice per packet read. The two fields this route does add are
    about the assignment, which the packet has no reason to know about, and the
    licensing warning, added only if the packet did not already carry one.
    """
    from . import rater_packet

    rater = _rater_from_token(token)
    assignment = _rater_assignment(rater, assignment_id)
    # Seed the item order on the assignment, so this rater's order is their own
    # and is the same every time they reopen the packet.
    packet = rater_packet.build(assignment["session_id"], order_seed=assignment_id)
    if not packet:
        # The assignment exists but its encounter does not (a session directory
        # removed after assignment). Same 404 as an unknown assignment: there is
        # nothing here to rate either way.
        raise HTTPException(404, "no such assignment")
    packet = dict(packet)
    packet.setdefault("assignment_id", assignment.get("assignment_id"))
    packet.setdefault("status", assignment.get("status"))
    return _with_notice(packet)


@app.post("/api/rater/ratings/{assignment_id}")
async def api_rater_submit(assignment_id: str, request: Request,
                           token: Optional[str] = Query(None)):
    """One rater's scores for one encounter.

    Body: {scores: {item_id: 1..5 | null}, open_ended: {better, notable},
    seconds}. A null score is "not enough information to judge" — the instrument
    requires that option, and it is stored as null rather than as a number so it
    can be excluded pairwise at analysis instead of being averaged in as a 3.
    """
    from . import ratings

    rater = _rater_from_token(token)
    assignment = _rater_assignment(rater, assignment_id)

    body = {}
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001, empty body is fine, the checks below catch it
        pass
    if not isinstance(body, dict):
        raise HTTPException(400, "body must be a JSON object")

    scores = body.get("scores")
    if not isinstance(scores, dict):
        raise HTTPException(400, "scores must be an object of item_id -> 1..5 or null")
    open_ended = body.get("open_ended")
    if open_ended is None:
        open_ended = {}   # absent is fine: both prompts are optional
    if not isinstance(open_ended, dict):
        raise HTTPException(400, "open_ended must be an object")

    # Time on task, used to spot a rater who clicked through 22 items in ninety
    # seconds. It is diagnostic, not data: a browser that reports it wrongly must
    # not be able to reject a rating a human spent twenty minutes on. An unusable
    # value becomes None — "we do not know how long this took" — rather than 0,
    # which is a number nobody measured and which reads as straight-lining.
    raw_seconds = body.get("seconds")
    try:
        seconds = float(raw_seconds) if raw_seconds is not None else None
        if seconds is not None and seconds < 0:
            seconds = None
    except (TypeError, ValueError):
        seconds = None

    try:
        record = ratings.submit(
            assignment["assignment_id"], rater["rater_id"], scores, open_ended, seconds
        )
    except LookupError:
        # ratings.submit re-checks the assignment and answers "no such
        # assignment" for both missing and not-yours, exactly as this layer
        # does. Reachable only in a race — the assignment removed between the
        # ownership check above and the write — and answered the same way.
        raise HTTPException(404, "no such assignment")
    except ValueError as e:
        # esci.validate's problems reach the rater as text, because they are
        # written to be read by one ("ESCI-08: 7 is outside 1..5"). Only
        # ValueError: see the note on the assignment draw below.
        raise HTTPException(400, str(e))
    if isinstance(record, dict) and record.get("errors"):
        raise HTTPException(400, "; ".join(str(x) for x in record["errors"]))
    if not isinstance(record, dict):
        return {"ok": True, "submitted_at": None}
    # A repeat submission is an amendment, not a conflict. ratings.submit
    # appends version n+1 and leaves version n byte-identical, so a rater who
    # spots a mis-click can correct it and the original is still there to be
    # audited — which is a better answer than this layer refusing the second
    # POST and leaving the wrong numbers in the ICC. The version comes back so
    # the console can say which one it just filed.
    return {
        "ok": True,
        "submitted_at": record.get("submitted_at"),
        "version": record.get("version"),
        "amends": record.get("amends"),
    }


# --- Rating administration: researcher-facing, SESSION_KEY ---

@app.post("/api/raters")
async def api_rater_create(payload: Optional[dict] = None,
                           key: Optional[str] = Query(None)):
    """Enrol a rater. kind is crowd | trained | expert, and it is recorded
    because reliability computed over a pool of mixed provenance has to be
    reportable by pool."""
    check_key(key)
    from . import raters

    body = payload or {}
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "name is required")
    kind = (body.get("kind") or "crowd").strip()
    email = (body.get("email") or "").strip() or None
    try:
        return raters.create_rater(name, kind=kind, email=email)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/raters")
async def api_raters_list(key: Optional[str] = Query(None)):
    """The rater roster."""
    check_key(key)
    from . import raters

    # Stored token material never leaves the process, not even to a researcher
    # and not even as a hash. It cannot be turned back into a token, but this
    # listing is the kind of thing that gets pasted into a shared spreadsheet,
    # and credential material has no business travelling that way.
    return [
        {k: v for k, v in r.items() if "token" not in k.lower() and "hash" not in k.lower()}
        for r in raters.list_raters()
    ]


@app.post("/api/raters/{rater_id}/token")
async def api_rater_issue_token(rater_id: str, payload: Optional[dict] = None,
                                key: Optional[str] = Query(None)):
    """Issue a scoped token for one rater. Shown once: only a hash is stored, so
    a lost token is reissued, never recovered."""
    check_key(key)
    from . import raters

    if raters.get_rater(rater_id) is None:
        raise HTTPException(404, "no such rater")
    raw_days = (payload or {}).get("days", 30)
    try:
        days = int(raw_days)
    except (TypeError, ValueError):
        raise HTTPException(400, "days must be a whole number")
    # An unbounded expiry is a standing credential to participant video held by
    # somebody outside the study team, and the upper bound is deliberately
    # shorter than a study year.
    if not 1 <= days <= 365:
        raise HTTPException(400, "days must be between 1 and 365")
    return {
        "token": raters.issue_token(rater_id, days=days),
        "rater_id": rater_id,
        "days": days,
        "note": "Shown once. Only a hash is stored; a lost token must be reissued.",
    }


@app.post("/api/rater-assignments")
async def api_rater_assignments_create(payload: Optional[dict] = None,
                                       key: Optional[str] = Query(None)):
    """Build the rating plan: which raters see which encounters.

    Either name the encounters (`session_ids`) or name a cohort and let the
    session index supply them — "assign everything in study" is what a
    researcher does at the end of a wave, and spelling out 400 session ids by
    hand is how an encounter gets missed. `seed` makes the draw reproducible,
    which is what lets the assignment plan be reported in a methods section.
    """
    check_key(key)
    from starlette.concurrency import run_in_threadpool

    from . import raters

    body = payload or {}
    rater_ids = body.get("rater_ids") or []
    if not isinstance(rater_ids, list) or not all(isinstance(r, str) for r in rater_ids):
        raise HTTPException(400, "rater_ids must be a list of rater ids")
    if not rater_ids:
        raise HTTPException(400, "rater_ids is required")

    session_ids = body.get("session_ids")
    cohort = (body.get("cohort") or "").strip() or None
    if session_ids is not None:
        if not isinstance(session_ids, list) or not all(isinstance(s, str) for s in session_ids):
            raise HTTPException(400, "session_ids must be a list of session ids")
    elif cohort:
        # sqlite3 is synchronous and this coroutine shares its event loop with
        # every live encounter; a wave-sized query must not stall the audio.
        session_ids = await run_in_threadpool(_rateable_sessions, cohort)
    else:
        raise HTTPException(400, "pass session_ids or cohort")
    if not session_ids:
        raise HTTPException(400, "no encounters to assign")

    try:
        per_encounter = int(body.get("per_encounter", 3))
    except (TypeError, ValueError):
        raise HTTPException(400, "per_encounter must be a whole number")
    if per_encounter < 1:
        raise HTTPException(400, "per_encounter must be at least 1")
    if per_encounter > len(rater_ids):
        # Caught here rather than deep in the draw, because the message a
        # researcher needs is arithmetic, not a traceback: independent ratings
        # cannot come from the same rater twice.
        raise HTTPException(
            400,
            f"per_encounter {per_encounter} needs at least that many raters, "
            f"got {len(rater_ids)}",
        )
    seed = body.get("seed")
    if seed is not None and not isinstance(seed, int):
        try:
            seed = int(seed)
        except (TypeError, ValueError):
            raise HTTPException(400, "seed must be a whole number")

    try:
        created = raters.assign(
            session_ids, rater_ids, per_encounter=per_encounter, seed=seed
        )
    except ValueError as e:
        # A ValueError here is the draw telling the researcher their request
        # cannot be satisfied ("encounter s_x needs 2 more raters but only 1 of
        # the 3 given are not already on it"), which is exactly a 400. Anything
        # else is a bug in the draw and must surface as a 500 with a traceback,
        # not as a 400 that sends the researcher hunting for a mistake in their
        # own request. This is not hypothetical: the draw once answered
        # "_repair_connectivity() takes 3 positional arguments but 4 were given"
        # and it arrived looking like bad input.
        raise HTTPException(400, str(e))
    return {
        "created": len(created),
        "n_sessions": len(session_ids),
        "n_raters": len(rater_ids),
        "per_encounter": per_encounter,
        "cohort": cohort,
        "seed": seed,
        "assignments": created,
    }


@app.get("/api/rater-assignments")
async def api_rater_assignments_list(key: Optional[str] = Query(None),
                                     rater_id: Optional[str] = Query(None),
                                     cohort: Optional[str] = Query(None),
                                     status: Optional[str] = Query(None)):
    """The rating plan as it stands: who owes what.

    The researcher is not blinded, so this carries the session id *and* the
    rating code — the code is the only handle a rater can quote, so the join
    from "rater says RC-XXXXXXXXXX is broken" back to an encounter has to exist
    somewhere, and this is that somewhere.
    """
    check_key(key)
    from . import rater_packet, raters

    want_status = (status or "").strip() or None
    want_cohort = (cohort or "").strip() or None
    if rater_id:
        # One rater's queue is asked for by rater, because that is the question
        # ("what does this person still owe us") and because it is the only form
        # that has to 404 on an unknown rater rather than quietly return nothing.
        if raters.get_rater(rater_id) is None:
            raise HTTPException(404, "no such rater")
        found = raters.assignments_for_rater(rater_id, status=want_status)
        if want_cohort:
            found = [a for a in found if a.get("cohort") == want_cohort]
    else:
        found = raters.list_assignments(cohort=want_cohort, status=want_status)

    roster = {r.get("rater_id"): r for r in raters.list_raters()}
    out = []
    for a in found:
        entry = dict(a)
        who = roster.get(a.get("rater_id")) or {}
        entry["rater_name"] = who.get("name")
        entry["rater_kind"] = who.get("kind")
        entry["rating_code"] = rater_packet.rating_code(a.get("session_id"))
        out.append(entry)
    out.sort(key=lambda e: (e.get("assigned_at") or "", e.get("assignment_id") or ""))
    return out


@app.get("/api/ratings")
async def api_ratings_export(key: Optional[str] = Query(None),
                             cohort: Optional[str] = Query(None)):
    """Every submitted rating, for export.

    Wrapped in an object rather than returned as a bare array so the licensing
    notice can ride with it. This is the file that becomes a CSV on somebody's
    laptop and then an appendix; the items are not ours to redistribute, and the
    warning has to survive the trip.
    """
    check_key(key)
    from . import ratings

    rows = ratings.all_ratings(cohort)
    return {
        "notice": ITEM_LICENSE_NOTICE,
        "cohort": cohort,
        "n": len(rows),
        "ratings": rows,
    }


@app.get("/api/reliability")
async def api_reliability(key: Optional[str] = Query(None),
                          cohort: Optional[str] = Query(None)):
    """ICC, weighted kappa and Krippendorff's alpha, per construct and per item.

    Computed on demand rather than cached: it is read a handful of times at the
    end of a wave, and a stale reliability number is worse than a slow one.
    """
    check_key(key)
    from . import reliability

    report = reliability.report(cohort)
    if not isinstance(report, dict):
        return _json_safe(report)
    return _with_notice(_json_safe(dict(report)))


# --- Participant: text path ---

@app.websocket("/ws/participant")
async def ws_participant_text(
    ws: WebSocket,
    scenario: str = Query(...),
    participant_id: Optional[str] = Query(None),
    model: Optional[str] = Query(None),
    launch: Optional[str] = Query(None),
    key: Optional[str] = Query(None),
    run: Optional[str] = Query(None),
):
    if PARTICIPANT_KEY_REQUIRED and SESSION_KEY and key != SESSION_KEY:
        await ws.close(code=4401)
        return
    if participant_id and not get_participant(participant_id):
        await ws.close(code=4403)
        return
    await ws.accept()

    # The model is a study variable: honour only a researcher-authenticated
    # launch override, never a participant-supplied ?model=. Otherwise a
    # participant could bill an unapproved (costlier) model and contaminate the
    # recorded model provenance.
    launch_cfg = LAUNCHES.get(launch) if launch else None
    effective_model = launch_cfg.get("model") if launch_cfg else None

    try:
        session = registry.create(
            scenario, model=effective_model, participant_id=participant_id,
            capture_audio=False, run_context=_run_context(participant_id, run),
        )
    except FileNotFoundError as e:
        await ws.send_json({"type": "error", "message": str(e)})
        await ws.close()
        return

    # From here a Session exists in the registry, so everything runs inside the
    # try whose finally drops it, an early disconnect during setup would
    # otherwise leak the session forever.
    try:
        # Researcher-configured launch: apply gear presets before the first turn.
        if launch_cfg:
            await _apply_launch_config(session, launch_cfg)

        session.participant_ws = ws
        await ws.send_json({
            "type": "session",
            "session_id": session.id,
            "scenario": {
                "id": session.scenario.id,
                "title": session.scenario.title,
                "intro": session.scenario.intro,
            },
        })

        while True:
            msg = await ws.receive_json()
            if msg.get("type") != "user_text":
                continue
            user_text = (msg.get("text") or "").strip()
            if not user_text:
                continue

            async with session.lock:
                t_user = time.time()
                session.append_user(user_text)
                session.store.event("user_turn", text=user_text, channel="text")
                await session.broadcast({
                    "type": "transcript",
                    "role": "user",
                    "text": user_text,
                })

                engine = session.primary_engine
                agent_id = engine.agent.id

                full = []
                t_first = None
                async for delta in engine.stream_reply(
                    session.shared_history,
                    session.triggered_branches,
                    session.name_lookup,
                ):
                    if t_first is None:
                        t_first = time.time()
                    full.append(delta)
                    await ws.send_json({"type": "assistant_text_delta", "text": delta})

                assistant_text = "".join(full)
                session.append_agent(agent_id, assistant_text)
                latency_first = round((t_first or time.time()) - t_user, 3)
                latency_total = round(time.time() - t_user, 3)
                session.store.event(
                    "assistant_turn",
                    channel="text",
                    agent_id=agent_id,
                    text=assistant_text,
                    model=engine.model,
                    persona=session.personas[agent_id].snapshot(),
                    live_notes=list(engine.live_notes),
                    triggered_branches=[b.id for b in session.triggered_branches],
                    latency_to_first_token_s=latency_first,
                    latency_total_s=latency_total,
                )
                await ws.send_json({
                    "type": "assistant_done",
                    "text": assistant_text,
                    "latency_to_first_token_s": latency_first,
                    "latency_total_s": latency_total,
                })
                await session.broadcast({
                    "type": "transcript",
                    "role": "assistant",
                    "agent_id": agent_id,
                    "text": assistant_text,
                    "latency_to_first_token_s": latency_first,
                    "latency_total_s": latency_total,
                })
                await session.broadcast({"type": "state", **session.snapshot()})
                # Auto steering: review off the turn lock's hot path; gear
                # shifts apply on the next turn. Tracked on the session so it is
                # not GC-cancelled and is cancelled on teardown.
                session.spawn_auto_steer()

    except WebSocketDisconnect:
        pass
    except Exception as e:
        session.log.event("error", where="participant_ws", message=str(e))
        try:
            await ws.send_json({"type": "error", "message": str(e)})
        except Exception:
            pass
    finally:
        registry.drop(session.id)


# --- Participant: voice path ---

@app.websocket("/ws/participant/voice")
async def ws_participant_voice(
    ws: WebSocket,
    scenario: str = Query(...),
    participant_id: Optional[str] = Query(None),
    model: Optional[str] = Query(None),
    launch: Optional[str] = Query(None),
    key: Optional[str] = Query(None),
    run: Optional[str] = Query(None),
):
    if PARTICIPANT_KEY_REQUIRED and SESSION_KEY and key != SESSION_KEY:
        await ws.close(code=4401)
        return
    # Voice capture needs recorded consent, and the record existing is not the
    # same as consent having been given: /start mints one with consent_given
    # False so identity is stable before the participant has agreed to anything.
    # Check the flag, not just the record, or the gate is decorative.
    _p = get_participant(participant_id) if participant_id else None
    if not _p or not _p.get("consent_given"):
        await ws.close(code=4403)
        return
    await ws.accept()

    # The model is a study variable: honour only a researcher-authenticated
    # launch override, never a participant-supplied ?model=.
    launch_cfg = LAUNCHES.get(launch) if launch else None
    effective_model = launch_cfg.get("model") if launch_cfg else None

    try:
        session = registry.create(
            scenario, model=effective_model, participant_id=participant_id,
            capture_audio=True, run_context=_run_context(participant_id, run),
        )
    except FileNotFoundError as e:
        await ws.send_json({"type": "error", "message": str(e)})
        await ws.close()
        return

    # From here a Session exists in the registry, so everything runs inside the
    # try whose finally drops it, an early disconnect during setup would
    # otherwise leak the session forever.
    try:
        # Researcher-configured launch: apply gear presets before the first turn.
        if launch_cfg:
            await _apply_launch_config(session, launch_cfg)

        session.participant_ws = ws
        await ws.send_json({
            "type": "session",
            "session_id": session.id,
            "scenario": {
                "id": session.scenario.id,
                "title": session.scenario.title,
                "intro": session.scenario.intro,
                "mode": session.scenario.mode,
            },
            "cast": [
                {"id": a.id, "name": a.name, "role": a.role, "photo": a.photo}
                for a in session.scenario.cast
            ],
            "audio": {"sample_rate": 16000, "channels": 1, "encoding": "pcm_s16le"},
        })

        # Every scenario runs as consecutive 1:1 conversations on Gemini Live,
        # the cast is played one character at a time, in order.
        runner = RealtimeVoiceSessionRunner(session, ws)
        await runner.run()
    except WebSocketDisconnect:
        pass
    except Exception as e:
        session.log.event("error", where="voice_ws", message=str(e))
        try:
            await ws.send_json({"type": "error", "message": str(e)})
        except Exception:
            pass
    finally:
        registry.drop(session.id)


# --- Researcher ---

@app.websocket("/ws/researcher")
async def ws_researcher(ws: WebSocket, session_id: str = Query(...), key: Optional[str] = Query(None)):
    # The researcher channel exposes the full live transcript and the steering
    # controls, so it must require the session key whenever one is configured,
    # exactly like the HTTP researcher routes (check_key). It must NOT depend on
    # PARTICIPANT_KEY_REQUIRED, which is off in the normal deployment.
    if SESSION_KEY and key != SESSION_KEY:
        await ws.close(code=4401)
        return

    try:
        session = registry.get(session_id)
    except KeyError:
        await ws.close(code=4404)
        return

    await ws.accept()
    session.researcher_wss.add(ws)
    await ws.send_json({"type": "state", **session.snapshot()})
    # Replay shared history so the researcher view is populated even if they
    # joined late. Each entry carries speaker (user|agent_id).
    for entry in session.shared_history:
        role = "user" if entry["speaker"] == "user" else "assistant"
        await ws.send_json({
            "type": "transcript",
            "role": role,
            "agent_id": entry["speaker"] if role == "assistant" else None,
            "text": entry["text"],
        })
    # Replay gear switches (launch presets, manual, auto) so the steering log
    # is complete even for late joiners.
    for entry in session.steering_log:
        await ws.send_json(entry)

    try:
        while True:
            msg = await ws.receive_json()
            # A malformed control frame (missing key, non-numeric value, unknown
            # knob/agent/branch) must not tear the socket down: report it and
            # keep the researcher's live monitoring/steering connection open.
            try:
                t = msg.get("type")
                agent_id = msg.get("agent_id")  # optional, applies to that agent only
                if t == "set_knob":
                    await session.set_knob(msg["knob"], float(msg["value"]), agent_id=agent_id)
                elif t == "note":
                    await session.add_note(msg["text"], agent_id=agent_id)
                elif t == "clear_notes":
                    await session.clear_notes(agent_id=agent_id)
                elif t == "branch":
                    await session.trigger_branch(msg["id"])
                elif t == "set_model":
                    await session.set_model(msg["model"])
                elif t == "set_auto_steering":
                    await session.set_auto_steering(bool(msg.get("enabled")))
                else:
                    await ws.send_json({"type": "error", "message": f"unknown type: {t}"})
                    continue
                await session.broadcast({"type": "state", **session.snapshot()})
            except (KeyError, ValueError, TypeError) as e:
                await ws.send_json(
                    {"type": "error", "message": f"{type(e).__name__}: {e}"}
                )
    except WebSocketDisconnect:
        pass
    finally:
        session.researcher_wss.discard(ws)


if __name__ == "__main__":
    import sys
    import uvicorn

    # Windows consoles default to cp1252, which cannot encode most of the
    # punctuation in this codebase — and the very first thing `python -m
    # server.app` did was print an arrow, so on Windows the documented way to
    # start the server crashed before uvicorn was reached. Every scenario title,
    # persona brief and participant transcript in this study also carries em
    # dashes and curly quotes, so any later print of study content would have
    # failed the same way. Reconfigure the streams once, here, rather than
    # policing every print; errors="replace" means an unprintable character
    # degrades to a placeholder instead of killing the process.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            # Not a real console (a pipe, a service manager, an older Python).
            # Nothing to reconfigure, and the ASCII banner below still prints.
            pass

    host = os.getenv("HOST", "127.0.0.1")
    port = int(os.getenv("PORT", "8765"))
    print(f"Relational Fluency Platform -> http://{host}:{port}")
    uvicorn.run("server.app:app", host=host, port=port, reload=False, log_level="info")
