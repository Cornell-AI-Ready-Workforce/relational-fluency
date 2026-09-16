"""OpenAI-compatible custom LLM server for ElevenLabs Agents.

ElevenLabs calls POST /v1/chat/completions with the conversation history and expects
an OpenAI-style (streaming) response. Each request runs the director-actor loop:

  1. DIRECTOR (cheap model) reads the transcript -> JSON state + stage direction
  2. ACTOR (main model) gets persona prompt + current stage direction -> next line
  3. The turn, state, and direction are appended to a JSONL steering log

Run:
    export ANTHROPIC_API_KEY=sk-ant-...
    uvicorn agents.director_actor.server:app --port 8100
    # expose with: ngrok http 8100 -> paste URL into ElevenLabs Custom LLM

Env vars: SCENARIO_ID (default S2A), ACTOR_MODEL, DIRECTOR_MODEL, STEERING_LOG_DIR.
"""

import asyncio
import hmac
import json
import logging
import os
import re
import time
import uuid
from pathlib import Path

from anthropic import Anthropic
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .director import run_director
from .scenarios import get_scenario, render_persona

load_dotenv()

ACTOR_MODEL = os.getenv("ACTOR_MODEL", "claude-sonnet-4-5")
SCENARIO_ID = os.getenv("SCENARIO_ID", "S2A")
LOG_DIR = Path(os.getenv("STEERING_LOG_DIR", "logs"))
# When set, requests must carry "Authorization: Bearer <AGENT_API_KEY>".
# Configure the same value as the API key in ElevenLabs' custom-LLM settings.
AGENT_API_KEY = os.getenv("AGENT_API_KEY", "")
# The service is documented to be tunnelled to the public internet via ngrok, so
# refuse to start unauthenticated unless an explicit dev override is set.
ALLOW_UNAUTHENTICATED = os.getenv("ALLOW_UNAUTHENTICATED", "") == "1"

logger = logging.getLogger(__name__)


def _enforce_auth_config() -> None:
    """Refuse to SERVE unauthenticated (the endpoint is documented as publicly
    tunnelled via ngrok). Run at server startup, NOT at import, so that importing
    helpers like build_actor_system (e.g. from the simulator) does not require an
    API key to be configured."""
    if not AGENT_API_KEY:
        if not ALLOW_UNAUTHENTICATED:
            raise RuntimeError(
                "AGENT_API_KEY is unset. This endpoint relays LLM calls billed to the "
                "study's account and appends to the steering audit log, and is meant to "
                "be exposed publicly. Set AGENT_API_KEY, or set ALLOW_UNAUTHENTICATED=1 "
                "to run without auth (dev only)."
            )
        logger.warning(
            "AGENT_API_KEY is unset and ALLOW_UNAUTHENTICATED=1: the endpoint is running "
            "WITHOUT authentication. Do NOT expose it to the internet."
        )


app = FastAPI(title="Relational Fluency Director-Actor Agent")


@app.on_event("startup")
async def _startup_auth_guard() -> None:
    _enforce_auth_config()


_client: Anthropic | None = None


def client() -> Anthropic:
    global _client
    if _client is None:
        _client = Anthropic()
    return _client


def build_actor_system(persona: str, direction: dict) -> str:
    return (
        f"{persona}\n\n"
        f"DIRECTOR NOTE (follow precisely, never mention): "
        f"{direction['stage_direction']}"
    )


def normalize_messages(messages: list[dict]) -> list[dict]:
    """Coerce OpenAI-style history into a valid Anthropic Messages list.

    ElevenLabs sends a history that begins with the agent's configured greeting,
    so after filtering the system prompt messages[0] is 'assistant'. The Anthropic
    API rejects an assistant-first list, consecutive same-role messages, and an
    empty list — fix all three here.
    """
    # If the history starts with an assistant turn, prepend a synthetic user turn
    # so the actor can respond to it.
    if messages and messages[0]["role"] == "assistant":
        messages = [{"role": "user", "content": "(joins the call)"}, *messages]
    # Merge consecutive same-role messages into one.
    merged: list[dict] = []
    for m in messages:
        if merged and merged[-1]["role"] == m["role"]:
            merged[-1] = {
                "role": m["role"],
                "content": f"{merged[-1]['content']}\n{m['content']}",
            }
        else:
            merged.append({"role": m["role"], "content": m["content"]})
    # An empty list is invalid; give the actor something to open against.
    if not merged:
        merged = [{"role": "user", "content": "(call connected)"}]
    return merged


def log_turn(conversation_id: str, direction: dict, messages: list[dict],
             reply: str) -> None:
    last_user = next((m["content"] for m in reversed(messages)
                      if m["role"] == "user"), None)
    record = {
        "ts": time.time(),
        "conversation_id": conversation_id,
        "scenario": SCENARIO_ID,
        "participant_turn": last_user,
        "director": direction,
        "actor_reply": reply,
    }
    line = json.dumps(record, ensure_ascii=False)
    # stdout -> CloudWatch on Fargate (containers are ephemeral; this is the
    # durable copy of the steering trail). Local JSONL kept for dev runs.
    print(f"STEERING {line}", flush=True)
    # conversation_id is caller-supplied; sanitize it so it can't escape LOG_DIR
    # via path traversal (e.g. "../../etc/x") before using it as a filename.
    safe_id = re.sub(r"[^A-Za-z0-9_-]", "_", conversation_id)[:64] or "local"
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        path = (LOG_DIR / f"{safe_id}.jsonl").resolve()
        if not path.is_relative_to(LOG_DIR.resolve()):
            return
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except (OSError, ValueError):
        pass


def check_auth(request: Request) -> None:
    if not AGENT_API_KEY:
        return
    auth = request.headers.get("authorization", "")
    # Compare as bytes: a non-ASCII byte in the header (Starlette decodes headers
    # as latin-1) makes hmac.compare_digest on str raise TypeError, which would
    # surface as a 500 instead of the intended 401 for a bad key.
    if not hmac.compare_digest(
        auth.encode("utf-8", "replace"),
        f"Bearer {AGENT_API_KEY}".encode("utf-8"),
    ):
        raise HTTPException(status_code=401, detail="invalid or missing API key")


def sse_chunk(chunk_id: str, model: str, content: str | None,
              finish: str | None = None) -> str:
    delta = {} if content is None else {"content": content}
    payload = {
        "id": chunk_id, "object": "chat.completion.chunk",
        "created": int(time.time()), "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }
    return f"data: {json.dumps(payload)}\n\n"


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "scenario": SCENARIO_ID, "actor_model": ACTOR_MODEL}


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    check_auth(request)
    body = await request.json()
    messages = [m for m in body.get("messages", [])
                if m.get("role") in ("user", "assistant") and m.get("content")]
    stream = body.get("stream", True)
    conversation_id = (body.get("user")
                       or body.get("elevenlabs_extra_body", {}).get("conversation_id")
                       or "local")
    variables = body.get("elevenlabs_extra_body", {}).get("variables")

    scenario = get_scenario(SCENARIO_ID)
    persona = render_persona(scenario, variables)

    # 1) director — blocking sync HTTPS round-trip; run off the event loop so it
    # doesn't stall other conversations or /health.
    direction = await asyncio.to_thread(
        run_director, client(), scenario["director_policy"], messages)
    system = build_actor_system(persona, direction)
    chunk_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    # Normalize OpenAI-style history into a valid Anthropic Messages list.
    actor_messages = normalize_messages(messages)

    if not stream:
        resp = await asyncio.to_thread(
            lambda: client().messages.create(
                model=ACTOR_MODEL, max_tokens=400, system=system,
                messages=actor_messages))
        reply = resp.content[0].text
        log_turn(conversation_id, direction, messages, reply)
        return JSONResponse({
            "id": chunk_id, "object": "chat.completion",
            "created": int(time.time()), "model": ACTOR_MODEL,
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": reply}}],
        })

    # 2) actor, streamed back in OpenAI SSE format
    def generate():
        parts: list[str] = []
        try:
            with client().messages.stream(
                    model=ACTOR_MODEL, max_tokens=400,
                    system=system, messages=actor_messages) as s:
                for text in s.text_stream:
                    parts.append(text)
                    yield sse_chunk(chunk_id, ACTOR_MODEL, text)
            yield sse_chunk(chunk_id, ACTOR_MODEL, None, finish="stop")
            yield "data: [DONE]\n\n"
        finally:
            # Always record the steering trail, even on mid-stream disconnect
            # (GeneratorExit) or actor error — those are exactly the turns worth
            # auditing.
            log_turn(conversation_id, direction, messages, "".join(parts))

    return StreamingResponse(generate(), media_type="text/event-stream")
