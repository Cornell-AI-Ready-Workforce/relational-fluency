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

import json
import os
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

app = FastAPI(title="Relational Fluency Director-Actor Agent")
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
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with open(LOG_DIR / f"{conversation_id}.jsonl", "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def check_auth(request: Request) -> None:
    if not AGENT_API_KEY:
        return
    auth = request.headers.get("authorization", "")
    if auth != f"Bearer {AGENT_API_KEY}":
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

    # 1) director
    direction = run_director(client(), scenario["director_policy"], messages)
    system = build_actor_system(persona, direction)
    chunk_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"

    if not stream:
        resp = client().messages.create(
            model=ACTOR_MODEL, max_tokens=400, system=system, messages=messages)
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
        with client().messages.stream(
                model=ACTOR_MODEL, max_tokens=400,
                system=system, messages=messages) as s:
            for text in s.text_stream:
                parts.append(text)
                yield sse_chunk(chunk_id, ACTOR_MODEL, text)
        yield sse_chunk(chunk_id, ACTOR_MODEL, None, finish="stop")
        yield "data: [DONE]\n\n"
        log_turn(conversation_id, direction, messages, "".join(parts))

    return StreamingResponse(generate(), media_type="text/event-stream")
