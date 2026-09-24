"""Per-character accent and tone, opt-in, for listening tests.

VOICE_STYLE_FILE names a YAML file mapping scenario id -> agent id -> one short
"how you sound" description. When it is set, each character's brief gains a
"## How you sound" section with that text (server/engine.py). When it is unset,
nothing changes: this is how the study runs today.

Why a separate file and not a field in scenarios/v3/*.yaml: an accent is part
of the stimulus. Putting one in the scenario bank changes every encounter that
draws that form, and A/B forms have to stay matched. A file the researcher
points a local server at lets them hear the effect first. Any encounter run
with a style file carries its fingerprint in provenance.voice_style, so a
session with accents can never be mistaken for one without.

Measured 2026-09-24, one sample per condition, blind-classified by an audio
model: nto.gemini-live-2.5-flash-native-audio produced the requested accent
4/4 (plain, Scottish, London, Southern US); gpt-realtime-2.1 answered all four
in General American. On gpt the file changes tone at most.
"""
from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Dict, Optional, Tuple

import yaml

from .llm import setting

log = logging.getLogger(__name__)

_cache: Tuple[Optional[str], float, Dict[str, Dict[str, str]], Optional[str]] = (None, 0.0, {}, None)


def _load() -> Tuple[Dict[str, Dict[str, str]], Optional[str]]:
    """(styles, sha256 of the file), re-read when the file changes."""
    global _cache
    path = (setting("VOICE_STYLE_FILE", "") or "").strip()
    if not path:
        return {}, None
    p = Path(path).expanduser()
    try:
        mtime = p.stat().st_mtime
    except OSError:
        log.warning("VOICE_STYLE_FILE %s does not exist; no voice styles applied", p)
        return {}, None
    if _cache[0] == str(p) and _cache[1] == mtime:
        return _cache[2], _cache[3]
    raw = p.read_bytes()
    try:
        data = yaml.safe_load(raw.decode("utf-8")) or {}
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        log.warning("VOICE_STYLE_FILE %s could not be read (%s); no voice styles applied", p, exc)
        return {}, None
    styles: Dict[str, Dict[str, str]] = {}
    if isinstance(data, dict):
        for sid, cast in data.items():
            if isinstance(cast, dict):
                styles[str(sid)] = {str(a): str(t).strip() for a, t in cast.items()
                                    if isinstance(t, str) and t.strip()}
    sha = hashlib.sha256(raw).hexdigest()[:16]
    _cache = (str(p), mtime, styles, sha)
    return styles, sha


def style_for(scenario_id: Optional[str], agent_id: Optional[str]) -> Optional[str]:
    """The "how you sound" text for one character, or None."""
    if not scenario_id or not agent_id:
        return None
    styles, _ = _load()
    return styles.get(str(scenario_id), {}).get(str(agent_id)) or None


def provenance() -> Optional[dict]:
    """{"file", "sha256"} when a style file is in force, else None."""
    styles, sha = _load()
    if sha is None:
        return None
    return {"file": Path(setting("VOICE_STYLE_FILE", "")).name, "sha256": sha,
            "scenarios": sorted(styles)}
