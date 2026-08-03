"""The director: watches the transcript, returns a stage direction for the actor.

Closed-loop steering per the study design (Research Note v2): instead of relying on a
long system prompt for a 12-minute conversation, a cheap model classifies the
conversation state each turn and injects a fresh, playable instruction. Every
direction is returned to the caller so it can be logged - the steering trail is part
of the study's audit record.
"""

import json
import os
import re

DIRECTOR_MODEL = os.getenv("DIRECTOR_MODEL", "claude-haiku-4-5")
MAX_TRANSCRIPT_CHARS = 6000

FALLBACK = {
    "pressure_point": 0,
    "yield_score": 0,
    "drift": False,
    "stage_direction": (
        "Stay in character; remain warm but yield nothing new this turn."
    ),
}


def format_transcript(messages: list[dict]) -> str:
    """Compact transcript of user/assistant turns, most recent last."""
    lines = []
    for m in messages:
        if m["role"] == "user":
            lines.append(f"PARTICIPANT: {m['content']}")
        elif m["role"] == "assistant":
            lines.append(f"ACTOR: {m['content']}")
    text = "\n".join(lines)
    return text[-MAX_TRANSCRIPT_CHARS:]


def parse_direction(raw: str) -> dict:
    """Parse the director's JSON; fall back safely on malformed output."""
    match = re.search(r"\{.*\}", raw, re.S)
    if not match:
        return dict(FALLBACK)
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return dict(FALLBACK)
    out = dict(FALLBACK)
    for key in out:
        if key in data:
            out[key] = data[key]
    if not isinstance(out.get("stage_direction"), str) or not out["stage_direction"]:
        out["stage_direction"] = FALLBACK["stage_direction"]
    return out


def run_director(client, policy: str, messages: list[dict]) -> dict:
    """One director step. `client` is an anthropic.Anthropic instance."""
    transcript = format_transcript(messages)
    if not transcript.strip():
        return {**FALLBACK,
                "stage_direction": "Deliver a natural, neutral reaction to however "
                                   "the participant opens; do not raise compensation "
                                   "yourself."}
    try:
        resp = client.messages.create(
            model=DIRECTOR_MODEL,
            max_tokens=300,
            system=policy,
            messages=[{"role": "user",
                       "content": f"TRANSCRIPT SO FAR:\n{transcript}\n\nJSON:"}],
        )
        return parse_direction(resp.content[0].text)
    except Exception:
        return dict(FALLBACK)
