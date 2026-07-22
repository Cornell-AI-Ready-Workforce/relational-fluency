"""FastAPI service exposing the conversational agent to the simulation app."""

import os

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

load_dotenv()

app = FastAPI(title="Relational Fluency Agent Service")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],  # tighten for production
    allow_methods=["*"],
    allow_headers=["*"],
)


class Message(BaseModel):
    role: str  # "user" | "assistant"
    content: str


class ChatRequest(BaseModel):
    scenario_id: str
    messages: list[Message]


class ChatResponse(BaseModel):
    reply: str


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "model": os.getenv("AGENT_MODEL", "unset")}


@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest) -> ChatResponse:
    # TODO (Phase 2):
    #  1. Load scenario config (persona + situation) by req.scenario_id
    #     from reddit-analysis/scenarios/
    #  2. Build system prompt from prompts/ templates
    #  3. Call the LLM with conversation history
    #  4. Log the full turn to transcript storage
    return ChatResponse(
        reply=f"[placeholder reply for scenario '{req.scenario_id}' "
        f"after {len(req.messages)} turn(s)]"
    )
