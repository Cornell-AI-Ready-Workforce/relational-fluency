"""Offline scoring layer — measures relational skill from a session transcript.

Reads ``data/sessions/{sid}/events.jsonl``, reconstructs the conversation, and
runs a Claude judge against the construct rubric for that scenario's skill (see
``rubrics.py``). The judge does both layers in one structured pass:

  * per participant turn: which behavioral codes are present (+ evidence),
  * per construct dimension: a 1-5 anchored score with rationale + quotes,
  * an overall 1-5 with a short narrative, strengths, and growth edges.

Behavior *rates* are then computed deterministically in Python from the judge's
per-turn tags (not trusted to a model aggregate), so the behavioral-coding layer
is reproducible from the tags.

The result is cached to ``data/sessions/{sid}/score.json`` so it sits alongside
the other downloadable session artifacts.

CLI:
    python -m server.scoring <session_id> [--force] [--model claude-opus-4-8]
    python -m server.scoring --all [--force]

The judge model defaults to Claude Opus 4.8 (most capable — appropriate for a
research instrument). Override with --model or the JUDGE_MODEL env var. Scoring
is intentionally offline/batch: it never runs in the live conversation path.
"""
from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv()  # so the CLI picks up ANTHROPIC_API_KEY from .env

from .rubrics import Construct, construct_for_skill
from .scenarios import load_scenario
from .storage import SESSIONS_DIR

# Opus 4.8 — most capable; the right default for a measurement instrument.
# Configurable for cost (e.g. claude-sonnet-4-6) via env or --model.
DEFAULT_JUDGE_MODEL = os.getenv("JUDGE_MODEL", "nto.gemini-2.5-pro")
SCORE_FILENAME = "score.json"


# --------------------------------------------------------------------------
# Transcript reconstruction
# --------------------------------------------------------------------------

class TranscriptError(Exception):
    pass


def _load_events(session_dir: Path) -> List[dict]:
    path = session_dir / "events.jsonl"
    if not path.exists():
        raise TranscriptError(f"no events.jsonl in {session_dir}")
    out = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def load_transcript(session_dir: Path) -> Dict[str, Any]:
    """Reconstruct an ordered transcript + metadata from events.jsonl.

    Participant (user) turns are indexed 0..N-1 in order; the judge tags turns
    by these indices. Agent turns carry the speaking character's name.
    """
    events = _load_events(session_dir)
    start = next((e for e in events if e.get("type") == "session_start"), None)
    if not start:
        raise TranscriptError("no session_start event")

    scenario_id = start.get("scenario", "")
    # Names for agents come from the scenario cast; fall back to the agent id.
    names: Dict[str, str] = {}
    skill = ""
    title = scenario_id
    intro = ""
    try:
        sc = load_scenario(scenario_id)
        skill = sc.skill
        title = sc.title
        intro = sc.intro
        names = {a.id: a.name for a in sc.cast}
    except Exception:
        pass  # old/renamed scenario — score what we can; skill stays blank

    turns: List[Dict[str, Any]] = []
    user_idx = 0
    for e in events:
        t = e.get("type")
        if t == "user_turn":
            text = (e.get("text") or "").strip()
            if not text:
                continue
            turns.append({"role": "user", "index": user_idx, "text": text})
            user_idx += 1
        elif t == "assistant_turn":
            text = (e.get("text") or "").strip()
            if not text:
                continue
            aid = e.get("agent_id") or "primary"
            turns.append({
                "role": "agent",
                "agent_id": aid,
                "name": names.get(aid, aid),
                "text": text,
            })

    return {
        "scenario": scenario_id,
        "title": title,
        "intro": intro,
        "skill": skill,
        "mode": start.get("mode", "single"),
        "model": start.get("model", ""),
        "participant_id": start.get("participant_id"),
        "started_at": start.get("wall") or start.get("started_at"),
        "turns": turns,
        "n_user_turns": user_idx,
    }


def _render_transcript(turns: List[Dict[str, Any]]) -> str:
    lines = []
    for tn in turns:
        if tn["role"] == "user":
            lines.append(f"[U{tn['index']}] PARTICIPANT: {tn['text']}")
        else:
            lines.append(f"        {tn['name']}: {tn['text']}")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Judge prompt + schema
# --------------------------------------------------------------------------

def _build_schema(construct: Construct) -> dict:
    behavior_ids = [b.id for b in construct.behaviors] + ["none"]
    dimension_keys = [d.key for d in construct.dimensions]
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["turns", "dimensions", "overall"],
        "properties": {
            "turns": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["index", "tags", "note"],
                    "properties": {
                        "index": {"type": "integer"},
                        "tags": {
                            "type": "array",
                            "items": {"type": "string", "enum": behavior_ids},
                        },
                        "note": {"type": "string"},
                    },
                },
            },
            "dimensions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["key", "score", "rationale", "evidence"],
                    "properties": {
                        "key": {"type": "string", "enum": dimension_keys},
                        "score": {"type": "integer", "enum": [1, 2, 3, 4, 5]},
                        "rationale": {"type": "string"},
                        "evidence": {"type": "array", "items": {"type": "string"}},
                    },
                },
            },
            "overall": {
                "type": "object",
                "additionalProperties": False,
                "required": ["score", "summary", "strengths", "growth_edges"],
                "properties": {
                    "score": {"type": "integer", "enum": [1, 2, 3, 4, 5]},
                    "summary": {"type": "string"},
                    "strengths": {"type": "array", "items": {"type": "string"}},
                    "growth_edges": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
    }


def _system_prompt(construct: Construct) -> str:
    codebook = "\n".join(
        f"  - {b.id} ({'helps' if b.polarity == '+' else 'works against'}): "
        f"{b.label} — {b.definition}"
        for b in construct.behaviors
    )
    dims = "\n".join(
        f"  - {d.key} ({d.label}):\n"
        f"      1 = {d.anchor_1}\n"
        f"      3 = {d.anchor_3}\n"
        f"      5 = {d.anchor_5}"
        for d in construct.dimensions
    )
    return (
        "You are a careful research rater scoring how well a PARTICIPANT (the "
        "human) demonstrates a single relational skill in a workplace "
        "conversation. The other speakers are AI characters; score ONLY the "
        "participant's turns (marked [U#]). Judge what the participant actually "
        "does and says, grounded in the transcript — never reward intentions "
        "you have to assume.\n\n"
        f"## Construct: {construct.name}\n{construct.frame}\n\n"
        "## Behavioral codebook (tag each participant turn)\n"
        "For every participant turn, list the codes whose behavior is present "
        "in that turn. A turn may have several codes, or just [\"none\"] if no "
        "code applies. Tag only what is actually present.\n"
        f"{codebook}\n\n"
        "## Rubric dimensions (score 1-5 each, anchored)\n"
        "Score the whole conversation on each dimension using the anchors. "
        "Interpolate (2, 4) between anchors. Give a one- to three-sentence "
        "rationale and 1-3 short verbatim quotes from the participant as "
        "evidence.\n"
        f"{dims}\n\n"
        "## Overall\n"
        "Give an overall 1-5 for the construct (holistic, not a mechanical "
        "average), a 2-4 sentence summary, 1-3 concrete strengths, and 1-3 "
        "growth edges phrased as actionable next steps. Do not use em dashes.\n\n"
        "Be calibrated and fair: a 3 is a competent, ordinary attempt; reserve "
        "5 for genuinely skilled handling and 1 for clear absence or "
        "counter-productive behavior. Return only the structured result."
    )


def _user_prompt(meta: Dict[str, Any], transcript: str) -> str:
    parts = [f"SCENARIO: {meta['title']}"]
    if meta.get("intro"):
        parts.append("\nSITUATION (what the participant was told):\n" + meta["intro"].strip())
    parts.append(f"\nThe participant had {meta['n_user_turns']} turns, indexed U0..U{meta['n_user_turns'] - 1}.")
    parts.append("\nTRANSCRIPT:\n" + transcript)
    return "\n".join(parts)


def _extract_json(response) -> dict:
    # output_config.format guarantees a text block of valid JSON; thinking
    # blocks (if any) precede it.
    text = next((b.text for b in response.content if b.type == "text"), None)
    if text is None:
        raise TranscriptError("judge returned no text block")
    return json.loads(text)


# --------------------------------------------------------------------------
# Aggregation
# --------------------------------------------------------------------------

def _aggregate(construct: Construct, verdict: dict, meta: Dict[str, Any]) -> Dict[str, Any]:
    n = max(meta["n_user_turns"], 1)
    # Deterministic behavior rates from the per-turn tags.
    counts = {b.id: 0 for b in construct.behaviors}
    by_index = {t["index"]: t for t in verdict.get("turns", []) if isinstance(t.get("index"), int)}
    for t in by_index.values():
        for tag in t.get("tags", []):
            if tag in counts:
                counts[tag] += 1

    behavior_rates = {}
    for b in construct.behaviors:
        behavior_rates[b.id] = {
            "label": b.label,
            "polarity": b.polarity,
            "count": counts[b.id],
            "rate": round(counts[b.id] / n, 3),
        }

    # Enrich each participant turn with its text + tags for the UI.
    user_turns = [t for t in meta["turns"] if t["role"] == "user"]
    enriched_turns = []
    for ut in user_turns:
        tagged = by_index.get(ut["index"], {})
        enriched_turns.append({
            "index": ut["index"],
            "text": ut["text"],
            "tags": [tag for tag in tagged.get("tags", []) if tag != "none"],
            "note": tagged.get("note", ""),
        })

    dimensions = []
    for d in construct.dimensions:
        entry = next((x for x in verdict.get("dimensions", []) if x.get("key") == d.key), {})
        dimensions.append({
            "key": d.key,
            "label": d.label,
            "score": entry.get("score"),
            "rationale": entry.get("rationale", ""),
            "evidence": entry.get("evidence", []),
        })

    return {
        "behavior_rates": behavior_rates,
        "turns": enriched_turns,
        "dimensions": dimensions,
        "overall": verdict.get("overall", {}),
    }


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------

def _session_dir(session_id: str) -> Path:
    return SESSIONS_DIR / session_id


def load_cached_score(session_id: str) -> Optional[dict]:
    path = _session_dir(session_id) / SCORE_FILENAME
    if path.exists():
        try:
            return json.loads(path.read_text())
        except json.JSONDecodeError:
            return None
    return None


def score_session(
    session_id: str,
    *,
    force: bool = False,
    model: Optional[str] = None,
    client: Optional[Anthropic] = None,
) -> dict:
    """Score one session against its construct rubric. Cached to score.json.

    Synchronous (one Claude call). Call from async code via a threadpool.
    """
    sdir = _session_dir(session_id)
    if not sdir.is_dir():
        raise TranscriptError(f"session not found: {session_id}")

    if not force:
        cached = load_cached_score(session_id)
        if cached:
            return cached

    meta = load_transcript(sdir)
    if meta["n_user_turns"] == 0:
        raise TranscriptError("no participant turns to score")

    construct = construct_for_skill(meta["skill"])
    if construct is None:
        raise TranscriptError(
            f"scenario skill {meta['skill']!r} has no rubric; cannot score"
        )

    judge_model = model or DEFAULT_JUDGE_MODEL
    client = client or Anthropic()
    transcript = _render_transcript(meta["turns"])

    response = client.messages.create(
        model=judge_model,
        max_tokens=16000,
        thinking={"type": "adaptive"},
        system=_system_prompt(construct),
        messages=[{"role": "user", "content": _user_prompt(meta, transcript)}],
        output_config={
            "format": {
                "type": "json_schema",
                "schema": _build_schema(construct),
            }
        },
    )
    verdict = _extract_json(response)
    agg = _aggregate(construct, verdict, meta)

    result = {
        "session_id": session_id,
        "scenario": meta["scenario"],
        "scenario_title": meta["title"],
        "skill": meta["skill"],
        "construct": construct.key,
        "construct_name": construct.name,
        "mode": meta["mode"],
        "conversation_model": meta["model"],
        "judge_model": judge_model,
        "scored_at": time.time(),
        "n_user_turns": meta["n_user_turns"],
        "references": construct.references,
        **agg,
    }

    (sdir / SCORE_FILENAME).write_text(json.dumps(result, indent=2, ensure_ascii=False))
    return result


def _iter_session_ids() -> List[str]:
    if not SESSIONS_DIR.is_dir():
        return []
    return sorted(p.name for p in SESSIONS_DIR.iterdir() if p.is_dir())


def main() -> None:
    ap = argparse.ArgumentParser(description="Score relational-fluency sessions.")
    ap.add_argument("session_id", nargs="?", help="session id to score")
    ap.add_argument("--all", action="store_true", help="score every session")
    ap.add_argument("--force", action="store_true", help="re-score even if cached")
    ap.add_argument("--model", default=None, help="judge model id")
    args = ap.parse_args()

    if not args.all and not args.session_id:
        ap.error("provide a session_id or --all")

    client = Anthropic()
    targets = _iter_session_ids() if args.all else [args.session_id]
    for sid in targets:
        try:
            r = score_session(sid, force=args.force, model=args.model, client=client)
        except TranscriptError as e:
            print(f"SKIP {sid}: {e}")
            continue
        except Exception as e:  # surface API/other errors without aborting --all
            print(f"FAIL {sid}: {type(e).__name__}: {e}")
            continue
        ov = r.get("overall", {})
        print(
            f"OK   {sid}  {r['construct_name']:22s} "
            f"overall={ov.get('score')}/5  ({r['n_user_turns']} turns, judge={r['judge_model']})"
        )


if __name__ == "__main__":
    main()
