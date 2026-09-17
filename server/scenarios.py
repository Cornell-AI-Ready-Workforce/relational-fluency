"""Scenario objects, and the loader that compiles them from scenarios/v3/.

A scenario is one of the v3 specs (scenarios/v3/*.yaml), compiled by
scenarios_v3.compile_scenario into the flat cast-with-rendered-prompts shape the
engine and the voice runner consume. The legacy single-file YAML scenarios that
this module used to read directly were removed in 2026-09 (Study 1 scope,
docs/study1-plan.md); the dataclasses below are the shape everything downstream
still sees.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

from .persona import Persona


log = logging.getLogger(__name__)


@dataclass
class Branch:
    id: str
    label: str
    inject: str


@dataclass
class Reference:
    citation: str
    url: str
    relevance: str = ""


@dataclass
class Agent:
    """One AI character in a scenario. Single-agent scenarios have a 1-element
    cast with id='primary'. Group scenarios have one Agent per participant."""

    id: str
    name: str
    system_prompt: str
    role: str = ""  # short title shown under the name in the UI (e.g. "Product Manager")
    hidden_agenda: str = ""  # internal, never sent to the participant
    photo: str = "initials"  # "initials" or filename under static/agents/
    voice_id: Optional[str] = None
    defaults: Dict[str, float] = field(default_factory=dict)


@dataclass
class Scenario:
    id: str
    title: str
    intro: str
    mode: str = "single"  # 'single' | 'group'
    skill: str = ""
    # Shared situation context. engine.AgentEngine._system_prompt prepends it as
    # a "## Scene" block for EVERY agent, single and group alike, and the
    # director and steering prompts embed it too. So it is read by the
    # AI characters, never by the participant: it must be written about the
    # participant in the third person and must not contain anything the
    # participant is supposed to reveal in their own time. The participant's own
    # second-person briefing text is `intro`.
    scene: str = ""
    # The same context for the prompts that reason ABOUT the encounter rather
    # than act in it, the steering controller. They are
    # not in the scene, so the withholding that `scene` exists to do is only a
    # blindfold on them: for the influence encounters the participant's leverage
    # is the thing being scored. Empty means there is nothing extra to see and
    # `scene` is the whole picture, so those consumers should read
    # `analysis_scene or scene`.
    analysis_scene: str = ""
    intro_image: str = ""  # filename under static/agents/ shown on the brief screen (pre-start)
    cast: List[Agent] = field(default_factory=list)
    director_prompt: str = ""  # routing guidance for group mode
    # Agents to route on the very first reaction, served WITHOUT a director LLM
    # call so the meeting opens snappily.
    opener: List[str] = field(default_factory=list)
    branches: List[Branch] = field(default_factory=list)
    references: List[Reference] = field(default_factory=list)
    model: Optional[str] = None

    def initial_personas(self) -> Dict[str, Persona]:
        out = {}
        for a in self.cast:
            p = Persona()
            if a.defaults:
                p.update(**a.defaults)
            out[a.id] = p
        return out

    def agent(self, agent_id: str) -> Agent:
        for a in self.cast:
            if a.id == agent_id:
                return a
        raise KeyError(f"Unknown agent: {agent_id}")


# --- Caching ----------------------------------------------------------------
# list_scenarios() used to compile every spec on every call, on the event loop.
# /api/sessions calls it on every request while the researcher dashboard polls
# that endpoint every 3 s, and the same loop relays participant PCM to Gemini
# Live and feeds SilenceDetector, so an open dashboard froze a live encounter's
# audio for half a second at a time.
#
# The cache keys on a fingerprint of the files on disk, so editing a
# scenario while the server runs still takes effect on the next call. The
# fingerprint carries mtime AND size because two writes inside the filesystem's
# timestamp granularity would otherwise look identical. Nothing survives a
# restart, and no lock is needed: dict/attribute assignment is atomic under the
# GIL, so a concurrent threadpool caller can at worst redo the same work.
_Fingerprint = Tuple[Tuple[str, int, int], ...]

_list_cache: Optional[Tuple[_Fingerprint, List[Dict[str, Any]]]] = None


def _fingerprint(*dirs: Path) -> _Fingerprint:
    """Cheap stat-only signature of the YAML in these directories."""
    out: List[Tuple[str, int, int]] = []
    for d in dirs:
        try:
            for p in sorted(d.glob("*.yaml")):
                st = p.stat()
                out.append((str(p), st.st_mtime_ns, st.st_size))
        except OSError:
            continue
    return tuple(out)


def load_scenario(scenario_id: str, participant_key: str = "") -> Scenario:
    """The compiled v3 scenario with this id, or FileNotFoundError.

    Membership in the compiled bank is the only lookup: a scenario id reaches
    this from a participant-facing query parameter, and anything that is not
    exactly one of the bank's ids — a traversal, a retired name, a wrong-cased
    spelling — is the same miss, so the endpoint tells a caller nothing about
    what is on disk.
    """
    from .scenarios_v3 import available as _v3_available, compile_scenario

    if scenario_id in _v3_available():
        return compile_scenario(scenario_id, participant_key)
    raise FileNotFoundError(f"No scenario: {scenario_id!r}")


def list_scenarios() -> List[Dict[str, str]]:
    """Every scenario the app can offer, id/title/skill/mode/cast_size.

    Cached on the contents of scenarios/v3/. This is the hot one:
    /api/sessions calls it per request and the researcher dashboard polls that
    every 3 s, so an uncached call stalled live audio (see the caching note
    above). The listing is still built by compiling each v3 scenario rather than
    reading its spec fields directly, so that a spec which cannot be compiled is
    excluded here exactly as it would fail later, and a participant is never
    offered a scenario that would 500 when they picked it.
    """
    global _list_cache
    from .scenarios_v3 import V3_DIR, available as _v3_available, compile_scenario

    fp = _fingerprint(V3_DIR)
    hit = _list_cache
    if hit is not None and hit[0] == fp:
        # Rows are flat scalars, so a shallow copy per row is enough to keep a
        # caller (or FastAPI's serialiser) from editing the cached listing.
        return [dict(r) for r in hit[1]]

    out = []
    # Study scenarios first, these are what Phase 1 collects.
    for sid in _v3_available():
        # One incomplete/work-in-progress spec must not 500 the whole listing.
        try:
            sc = compile_scenario(sid)
        except Exception:
            continue
        out.append({
            "id": sc.id,
            "title": sc.title,
            "skill": sc.skill,
            "mode": sc.mode,
            "cast_size": len(sc.cast),
            "study": True,
            "variant": getattr(sc, "variant", None),
            "parallel_form": getattr(sc, "parallel_form", None),
        })
    _list_cache = (fp, out)
    return [dict(r) for r in out]
