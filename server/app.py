"""FastAPI server — participant + researcher endpoints.

Two paths:
  - /ws/participant  — text turn-taking (works without STT/TTS keys). Voice
                       endpoint /ws/participant/voice layers on STT/TTS.
  - /ws/researcher   — live transcript + steering controls

The text path is sufficient to "make sure the conversation is working well
and steer the model" — voice is layered on top once a researcher has
validated scenarios and prompts.
"""
from __future__ import annotations

import asyncio
import json
import os
import secrets
import time
from pathlib import Path
from typing import Optional

import io
import zipfile

import yaml
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from .engine import DEFAULT_MODEL
from .multi_agent_session import MultiAgentVoiceSessionRunner
from .scenarios import list_scenarios, load_scenario
from .session import Session, registry
from .storage import create_participant, get_participant, init_storage
from .voice_session import VoiceSessionRunner

load_dotenv()
init_storage()

SESSION_KEY = os.getenv("SESSION_KEY", "").strip()
ROOT_DIR = Path(__file__).parent.parent
STATIC_DIR = ROOT_DIR / "static"
CONFIG_DIR = ROOT_DIR / "config"
# Mirror storage.py — DATA_DIR may be a mounted volume in production.
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

if ALLOWED_HOSTS:
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=ALLOWED_HOSTS)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/health")
async def health() -> dict:
    """Liveness probe for the ALB target group. Deliberately unauthenticated and
    dependency-free: it answers whether this process can serve, not whether the
    model gateway is reachable, so a transient upstream blip cannot cause ECS to
    kill healthy tasks mid-encounter."""
    return {"status": "ok"}


def _load_consent() -> dict:
    return yaml.safe_load((CONFIG_DIR / "consent.yaml").read_text())


def check_key(key: Optional[str]) -> None:
    if SESSION_KEY and key != SESSION_KEY:
        raise HTTPException(status_code=401, detail="Bad or missing key")


# --- HTML routes ---

@app.get("/", response_class=HTMLResponse)
async def landing_page(scenario: Optional[str] = None, key: Optional[str] = None):
    """Landing page with scenario picker popup. If a scenario is passed via
    query (legacy v1 link), still serve the chat UI so old bookmarks work."""
    check_key(key)
    if scenario:
        return (STATIC_DIR / "participant.html").read_text()
    return (STATIC_DIR / "landing.html").read_text()


@app.get("/chat", response_class=HTMLResponse)
async def chat_page(scenario: Optional[str] = None, key: Optional[str] = None):
    """Legacy text-mode chat UI. /v2 is the recommended entry point."""
    check_key(key)
    return (STATIC_DIR / "participant.html").read_text()


@app.get("/researcher", response_class=HTMLResponse)
async def researcher_page(key: Optional[str] = None):
    check_key(key)
    return (STATIC_DIR / "researcher.html").read_text()


@app.get("/v2", response_class=HTMLResponse)
async def v2_page(scenario: Optional[str] = None, key: Optional[str] = None):
    """Zoom-like multi-agent voice UI. Works for both single-agent and group
    scenarios (single-agent just shows one tile)."""
    check_key(key)
    return (STATIC_DIR / "v2.html").read_text()


# --- REST helpers ---

@app.get("/api/scenarios")
async def api_scenarios(key: Optional[str] = None):
    check_key(key)
    return list_scenarios()


@app.get("/api/scenarios/{scenario_id}")
async def api_scenario_detail(scenario_id: str, key: Optional[str] = Query(None)):
    check_key(key)
    try:
        sc = load_scenario(scenario_id)
    except FileNotFoundError:
        raise HTTPException(404, "scenario not found")
    return {
        "id": sc.id,
        "title": sc.title,
        "intro": sc.intro,
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
            except (KeyError, ValueError, TypeError) as e:
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
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in sorted(sdir.iterdir()):
            if p.is_file():
                zf.write(p, arcname=p.name)
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{session_id}.zip"',
        },
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
    key: Optional[str] = Query(None),
):
    """Run (or re-run with force=1) the offline scorer. Blocking Claude call,
    so it runs in a threadpool to keep the event loop free."""
    check_key(key)
    _session_dir(session_id)
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
    key: Optional[str] = Query(None),
):
    """Run (or re-run with force=1) the per-persona debrief. Blocking Claude
    call, so it runs in a threadpool to keep the event loop free."""
    check_key(key)
    _session_dir(session_id)
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
    check_key(key)
    return _load_consent()


@app.post("/api/consent")
async def api_post_consent(payload: dict, key: Optional[str] = Query(None)):
    check_key(key)
    code = (payload.get("code") or "").strip()
    consent_given = bool(payload.get("consent_given"))
    if not code:
        raise HTTPException(400, "code required")
    if not consent_given:
        raise HTTPException(400, "consent_given must be true to proceed")
    version = _load_consent().get("version", "unknown")
    pid = create_participant(code=code, consent_given=True, consent_version=version)
    return {"participant_id": pid, "consent_text_version": version}


@app.get("/api/sessions")
async def api_sessions(key: Optional[str] = None):
    check_key(key)
    out = []
    active_ids = set(registry.list_ids())
    for sid in active_ids:
        s = registry.get(sid)
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
        })
    # Append recent closed sessions from SQLite so the researcher can browse +
    # download past data even after a session ends.
    import sqlite3
    from .storage import DB_PATH
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """SELECT id, scenario, model, started_at, n_turns, status, duration_s
               FROM sessions
               WHERE status != 'active'
               ORDER BY started_at DESC LIMIT 30"""
        ).fetchall()
        conn.close()
        # Map scenario id → title without re-reading every YAML each call.
        titles = {s["id"]: s["title"] for s in list_scenarios()}
        for r in rows:
            if r["id"] in active_ids:
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
            })
    except Exception:
        pass
    return out


# --- Participant: text path ---

@app.websocket("/ws/participant")
async def ws_participant_text(
    ws: WebSocket,
    scenario: str = Query(...),
    participant_id: Optional[str] = Query(None),
    model: Optional[str] = Query(None),
    launch: Optional[str] = Query(None),
    key: Optional[str] = Query(None),
):
    if SESSION_KEY and key != SESSION_KEY:
        await ws.close(code=4401)
        return
    if participant_id and not get_participant(participant_id):
        await ws.close(code=4403)
        return
    await ws.accept()

    try:
        session = registry.create(scenario, model=model, participant_id=participant_id, capture_audio=False)
    except FileNotFoundError as e:
        await ws.send_json({"type": "error", "message": str(e)})
        await ws.close()
        return

    # Researcher-configured launch: apply gear presets before the first turn.
    if launch and launch in LAUNCHES:
        await _apply_launch_config(session, LAUNCHES[launch])

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

    try:
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
                # shifts apply on the next turn.
                asyncio.create_task(session.auto_steer())

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
):
    if SESSION_KEY and key != SESSION_KEY:
        await ws.close(code=4401)
        return
    if not participant_id or not get_participant(participant_id):
        # Voice path requires consent — no anonymous voice capture.
        await ws.close(code=4403)
        return
    await ws.accept()

    try:
        session = registry.create(scenario, model=model, participant_id=participant_id, capture_audio=True)
    except FileNotFoundError as e:
        await ws.send_json({"type": "error", "message": str(e)})
        await ws.close()
        return

    # Researcher-configured launch: apply gear presets before the first turn.
    if launch and launch in LAUNCHES:
        await _apply_launch_config(session, LAUNCHES[launch])

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

    if session.is_group:
        runner = MultiAgentVoiceSessionRunner(session, ws)
    else:
        runner = VoiceSessionRunner(session, ws)
    try:
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
            t = msg.get("type")
            agent_id = msg.get("agent_id")  # optional — applies to that agent only
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
    except WebSocketDisconnect:
        pass
    finally:
        session.researcher_wss.discard(ws)


if __name__ == "__main__":
    import uvicorn

    host = os.getenv("HOST", "127.0.0.1")
    port = int(os.getenv("PORT", "8765"))
    print(f"Relational Fluency Platform → http://{host}:{port}")
    uvicorn.run("server.app:app", host=host, port=port, reload=False, log_level="info")
