"""Per-persona debrief for group scenarios.

After a group session ends, this produces, for EACH AI persona, a structured
read of three things the live characters were implicitly tracking:

  * felt_heard      , did the participant make this persona feel heard?
  * concern_addressed, was the persona's *underlying* concern (their hidden
                        agenda, not just their stated objection) addressed?
  * stance_shift    , how did this persona's position move across the scene?

It also surfaces each persona's hidden agenda (which the participant never saw
during the scene) so the debrief can explain what was *really* driving the room.

This is intentionally separate from scoring.py: scoring measures the
participant's relational skill against a construct rubric; the debrief explains
the room back to the participant (and the researcher) persona by persona. The
result is cached to data/sessions/{sid}/debrief.json so it sits alongside the
other downloadable session artifacts, that file is the researcher-facing copy,
and the same JSON is rendered on-screen at the end of the session.

CLI:
    python -m server.debrief <session_id> [--force] [--model claude-opus-4-8]
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from .llm import sync_text_client, setting
from dotenv import load_dotenv

load_dotenv()

from .scenarios import load_scenario
from .scoring import TranscriptError, load_transcript, _render_transcript
from .storage import SESSIONS_DIR

# Opus 4.8, the debrief is reflective and shown to the participant; use the
# most capable model by default. Override with --model or DEBRIEF_MODEL.
DEFAULT_DEBRIEF_MODEL = setting("DEBRIEF_MODEL", "nto.gemini-2.5-pro")
DEBRIEF_FILENAME = "debrief.json"

_RATING_ENUM = ["no", "barely", "somewhat", "mostly", "yes"]
_DIRECTION_ENUM = ["hardened", "unchanged", "softened", "shifted"]


def _session_dir(session_id: str) -> Path:
    return SESSIONS_DIR / session_id


def load_cached_debrief(session_id: str) -> Optional[Dict[str, Any]]:
    path = _session_dir(session_id) / DEBRIEF_FILENAME
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _build_schema(agent_ids: List[str], has_agendas: bool = True) -> dict:
    concern_desc = (
        "Did the participant address this persona's UNDERLYING concern (their "
        "hidden agenda), not just their stated objection? 1-2 sentences."
        if has_agendas else
        "Did the participant address this persona's concern (grounded in their "
        "role and stated position), not just deflect it? 1-2 sentences."
    )
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["personas", "room_summary"],
        "properties": {
            "room_summary": {
                "type": "string",
                "description": (
                    "2-4 sentences on how the room treated the participant overall and "
                    "how well they held their own and stayed engaged across the conversation."
                ),
            },
            "personas": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "agent_id", "felt_heard", "felt_heard_evidence",
                        "concern_addressed", "concern_addressed_evidence",
                        "stance_start", "stance_end", "stance_direction", "stance_note",
                    ],
                    "properties": {
                        "agent_id": {"type": "string", "enum": agent_ids},
                        "felt_heard": {"type": "string", "enum": _RATING_ENUM},
                        "felt_heard_evidence": {
                            "type": "string",
                            "description": "1-2 sentences + a short quote if possible.",
                        },
                        "concern_addressed": {"type": "string", "enum": _RATING_ENUM},
                        "concern_addressed_evidence": {
                            "type": "string",
                            "description": concern_desc,
                        },
                        "stance_start": {
                            "type": "string",
                            "description": "Their position at the start, in a short phrase.",
                        },
                        "stance_end": {
                            "type": "string",
                            "description": "Their position by the end, in a short phrase.",
                        },
                        "stance_direction": {"type": "string", "enum": _DIRECTION_ENUM},
                        "stance_note": {
                            "type": "string",
                            "description": "1-2 sentences on what moved (or failed to move) them.",
                        },
                    },
                },
            },
        },
    }


def _build_prompt(scenario, transcript: Dict[str, Any]) -> tuple[str, str]:
    has_agendas = any(a.hidden_agenda for a in scenario.cast)
    cast_blocks = []
    for a in scenario.cast:
        block = [f"### {a.name} (agent_id: {a.id})"]
        if a.role:
            block.append(f"Role: {a.role}")
        # The hidden agenda, where the scenario supplies one, tells the judge
        # what this persona's *underlying* concern actually was, behind their
        # stated objections. Not every scenario has hidden agendas.
        if a.hidden_agenda:
            block.append(f"Hidden agenda (their real, unspoken motive): {a.hidden_agenda}")
        cast_blocks.append("\n".join(block))
    cast_text = "\n\n".join(cast_blocks)

    if has_agendas:
        cast_intro = (
            "The other people in the room are AI personas, each with a STATED "
            "position and a HIDDEN AGENDA (their real motive, which the "
            "participant could not see)."
        )
        concern_step = (
            "2. concern_addressed, Did the participant address this persona's "
            "UNDERLYING concern (the hidden agenda), not merely their surface "
            "objection? A participant can answer the stated question while "
            "completely missing the real driver. Judge against the hidden "
            "agenda."
        )
    else:
        cast_intro = (
            "The other people in the room are AI personas, each with a role and "
            "a stated position in the scene (no hidden agendas are supplied for "
            "this scenario)."
        )
        concern_step = (
            "2. concern_addressed, Did the participant address this persona's "
            "concern, grounded in their role and stated position, rather than "
            "merely deflecting it? A participant can answer the surface words "
            "while missing what actually matters to this person. Judge against "
            "their stated role and position."
        )

    system = f"""You are debriefing a recorded multi-party workplace conversation for a relational-fluency research platform.

The PARTICIPANT is the person described in the Scene below. {cast_intro} Your job
is to read the transcript and, for each persona, assess three things honestly:

1. felt_heard, Did the participant make this persona feel genuinely heard
   (acknowledged their point, reflected it back, did not just defend)? Rate on
   the scale and cite evidence. Being under attack does not lower this; the
   question is purely what the PARTICIPANT did toward this persona.

{concern_step}

3. stance_shift, Where did this persona start, where did they end, and which
   direction did they move (hardened / unchanged / softened / shifted)? Be
   faithful to the transcript, many personas will NOT soften.

Be specific and grounded in the transcript. Do not flatter the participant. If
the room steamrolled them, say so in room_summary.

## Scene
{scenario.scene or scenario.intro}

## The personas
{cast_text}

Return your analysis via the `debrief` tool. Include exactly one entry per persona listed above."""

    user = f"""## Transcript
[U#] marks the participant's turns in order; indented lines are the personas.

{_render_transcript(transcript['turns'])}

Produce the per-persona debrief now."""
    return system, user


def _json_from_text(text: str) -> Optional[dict]:
    """Best-effort JSON from a text block: strip a ``` fence, try json.loads, then
    fall back to the outermost {..} span. Returns None if nothing parses. Mirrors
    scoring._extract_json's text fallback for gateway models that ignore the tool."""
    candidate = text.strip()
    if candidate.startswith("```"):
        lines = candidate.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        candidate = "\n".join(lines).strip()
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        i, j = candidate.find("{"), candidate.rfind("}")
        if i != -1 and j > i:
            try:
                return json.loads(candidate[i:j + 1])
            except json.JSONDecodeError:
                return None
    return None


def generate_debrief(
    session_id: str,
    *,
    force: bool = False,
    model: Optional[str] = None,
) -> Dict[str, Any]:
    """Run (or load cached) the per-persona debrief. Blocking Claude call."""
    if not force:
        cached = load_cached_debrief(session_id)
        if cached is not None:
            return cached

    sdir = _session_dir(session_id)
    if not sdir.is_dir():
        raise TranscriptError(f"no session dir: {session_id}")
    transcript = load_transcript(sdir)

    try:
        scenario = load_scenario(transcript["scenario"])
    except FileNotFoundError:
        raise TranscriptError(
            f"scenario {transcript['scenario']!r} no longer exists; cannot debrief"
        )
    if scenario.mode != "group" or not scenario.cast:
        raise TranscriptError("debrief is only available for group scenarios")

    if transcript["n_user_turns"] == 0:
        raise TranscriptError("no participant turns to debrief")

    agent_ids = [a.id for a in scenario.cast]
    name_by_id = {a.id: a.name for a in scenario.cast}
    role_by_id = {a.id: a.role for a in scenario.cast}
    agenda_by_id = {a.id: a.hidden_agenda for a in scenario.cast}
    has_agendas = any(agenda_by_id.values())
    schema = _build_schema(agent_ids, has_agendas)
    system, user = _build_prompt(scenario, transcript)

    client = sync_text_client()
    used_model = model or scenario.model or DEFAULT_DEBRIEF_MODEL
    resp = client.messages.create(
        model=used_model,
        max_tokens=4000,
        system=system,
        tools=[{"name": "debrief", "description": "Return the per-persona debrief.", "input_schema": schema}],
        tool_choice={"type": "tool", "name": "debrief"},
        messages=[{"role": "user", "content": user}],
    )
    if getattr(resp, "stop_reason", None) == "max_tokens":
        raise TranscriptError(
            "debrief judge response truncated at max_tokens; not caching"
        )

    payload: Optional[dict] = None
    for block in resp.content:
        if getattr(block, "type", None) == "tool_use" and block.name == "debrief":
            payload = block.input
            break
    if payload is None:
        # A LiteLLM-gateway model (the default judge is Gemini) may ignore the
        # forced tool and emit the debrief JSON as a text block instead. Parse it
        # the same defensive way scoring does before giving up.
        text = next(
            (b.text for b in resp.content if getattr(b, "type", None) == "text"), None
        )
        if text:
            payload = _json_from_text(text)
    if payload is None:
        raise TranscriptError("debrief judge returned no structured output")

    # Enrich each persona entry with display fields + the revealed hidden agenda.
    personas_out = []
    seen = set()
    for p in payload.get("personas", []):
        aid = p.get("agent_id")
        if aid not in name_by_id or aid in seen:
            continue
        seen.add(aid)
        p["name"] = name_by_id[aid]
        p["role"] = role_by_id.get(aid, "")
        # Reveal the hidden agenda only where the scenario actually supplied one;
        # a null (rather than an empty string) makes clear there is nothing to
        # reveal instead of "revealing" a blank agenda.
        p["hidden_agenda"] = agenda_by_id.get(aid) or None
        personas_out.append(p)
    # Keep cast order stable for the UI.
    personas_out.sort(key=lambda x: agent_ids.index(x["agent_id"]))

    # Every cast member must be judged; a missing persona means a truncated or
    # incomplete debrief, do not cache it and serve it forever.
    missing = set(agent_ids) - seen
    if missing:
        raise TranscriptError(
            f"debrief missing personas: {', '.join(sorted(missing))}; not caching"
        )

    result = {
        "session_id": session_id,
        "scenario": transcript["scenario"],
        "title": transcript["title"],
        "skill": transcript["skill"],
        "model": used_model,
        "n_user_turns": transcript["n_user_turns"],
        "room_summary": payload.get("room_summary", ""),
        "personas": personas_out,
    }
    (sdir / DEBRIEF_FILENAME).write_text(
        json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return result


def main() -> None:
    ap = argparse.ArgumentParser(description="Generate a per-persona session debrief.")
    ap.add_argument("session_id")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--model", default=None)
    args = ap.parse_args()
    out = generate_debrief(args.session_id, force=args.force, model=args.model)
    print(json.dumps(out, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
