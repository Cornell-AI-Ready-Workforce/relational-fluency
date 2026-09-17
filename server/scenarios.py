"""Scenario loading + system-prompt composition.

A scenario is a YAML file. Two shapes are supported (auto-detected by loader):

  Single-agent (v1, legacy, used by missed_deadlines etc.):
    id, title, intro, system_prompt, defaults, voice_id, branches, references

  Multi-agent (v2, group/team meeting scenarios):
    id, title, intro, mode: group, scene, director_prompt,
    cast: [ {id, name, photo, system_prompt, voice_id, defaults} ],
    branches, references

Internally, legacy single-agent scenarios are normalized to a 1-element cast
with id="primary", so downstream code only ever sees the cast model.
"""
from __future__ import annotations

import copy
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

from .persona import Persona
from .storage import is_safe_path_component


SCENARIOS_DIR = Path(__file__).parent.parent / "scenarios"

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
    # `scene` is the whole picture, which is the case for every legacy scenario,
    # so those consumers should read `analysis_scene or scene`.
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
# Everything below used to re-read and re-parse the YAML on every call, on the
# event loop. `list_scenarios()` cost ~0.5 s and ~90 yaml.safe_load calls, and
# /api/sessions calls it on every request while the researcher dashboard polls
# that endpoint every 3 s. The same loop relays participant PCM to Gemini Live
# and feeds SilenceDetector, so an open dashboard froze a live encounter's audio
# for half a second at a time. No legacy file is even named after the id it
# holds (01_missed_deadlines.yaml holds id: missed_deadlines), so the fast path
# in _find_scenario_file never hit and every lookup scanned the directory.
#
# All three caches below key on a fingerprint of the files on disk, so editing a
# scenario while the server runs still takes effect on the next call. The
# fingerprint carries mtime AND size because two writes inside the filesystem's
# timestamp granularity would otherwise look identical. Nothing survives a
# restart, and no lock is needed: dict/attribute assignment is atomic under the
# GIL, so a concurrent threadpool caller can at worst redo the same work.
_Fingerprint = Tuple[Tuple[str, int, int], ...]

_legacy_cache: Dict[str, Tuple[Tuple[int, int], Optional[dict]]] = {}
_legacy_index: Optional[Tuple[_Fingerprint, Dict[str, Path]]] = None
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


def _load_legacy(p: Path) -> Optional[dict]:
    """Parsed legacy scenario file, re-read only when it changed on disk.

    Returns None for anything unreadable or not a YAML mapping, and caches that
    verdict too, so a malformed file is not re-parsed on every listing.

    A file that does not parse is dropped rather than raised, so every caller
    below treats it as absent: the listing skips it and load_scenario reports it
    as a missing scenario, which reaches the researcher as a 404. That makes a
    corrupted file look exactly like a typo'd scenario id, so say what happened
    in the log, the way the v3 loader already does for its specs. The verdict is
    cached, so this is logged once per edit rather than once per dashboard poll.
    """
    try:
        st = p.stat()
    except OSError as exc:
        log.warning("cannot read scenario %s: %s", p.name, exc)
        return None
    key = (st.st_mtime_ns, st.st_size)
    hit = _legacy_cache.get(str(p))
    if hit is not None and hit[0] == key:
        return hit[1]
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8"))
    except Exception as exc:
        log.warning("skipping unparseable scenario %s: %s", p.name, exc)
        data = None
    if not isinstance(data, dict):
        if data is not None:
            log.warning("skipping scenario %s: not a YAML mapping", p.name)
        data = None
    _legacy_cache[str(p)] = (key, data)
    return data


def _legacy_by_id() -> Dict[str, Path]:
    """id -> file for the legacy scenarios/*.yaml, built once per directory
    change instead of once per lookup."""
    global _legacy_index
    fp = _fingerprint(SCENARIOS_DIR)
    hit = _legacy_index
    if hit is not None and hit[0] == fp:
        return hit[1]
    out: Dict[str, Path] = {}
    for p in sorted(SCENARIOS_DIR.glob("*.yaml")):
        data = _load_legacy(p)
        if data and data.get("id"):
            # First file wins, matching the old scan order.
            out.setdefault(data["id"], p)
    _legacy_index = (fp, out)
    return out


# A scenario id is a filename fragment, so it has to look like one before it is
# joined onto a path. It arrives from a participant-controlled websocket query
# parameter (/ws/participant and /ws/participant/voice both hand ?scenario=
# straight to registry.create), and unvalidated it was neither bounded by the
# scenarios directory nor by the study: "../../.." escaped SCENARIOS_DIR
# entirely, so any .yaml on disk that happened to be a mapping with
# id/title/system_prompt became a live encounter — its system_prompt briefing
# the actor, its id stamped onto the recording — and a miss told the caller
# whether an arbitrary path existed. A slash alone was enough for the milder
# version, "archive/mundane_chitchat", which needs no traversal to run a retired
# scenario and label the record with it. Every id the app actually offers is
# read out of the files themselves (list_scenarios) and every shipped one —
# S1A..S4B, missed_deadlines, g1_hidden_profile_vendor — matches this, so
# nothing legitimate is turned away.
_SCENARIO_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")


def _exists_exact(path: Path) -> bool:
    """path.exists(), but case-exact on every platform.

    Path.exists() asks the filesystem, and the filesystem answers differently
    per platform: Windows and a default (case-insensitive APFS) macOS volume say
    yes to "S1a.yaml" for a file named "S1A.yaml", Linux says no. A scenario id
    reaches this module from a participant-facing query parameter and is stamped
    onto the recording and into the manifest, so a wrong-cased link that loads a
    scenario on the researcher's Mac, 404s on the Linux container, and labels the
    encounter with whichever spelling the URL carried is a data problem, not a
    cosmetic one. Comparing against the directory's own listing gives one answer
    everywhere. (The dict lookups below — _legacy_by_id and the v3 registry — are
    already case-exact by construction; this probe was the one that was not.)
    """
    try:
        return any(entry.name == path.name for entry in path.parent.iterdir())
    except OSError:
        return False


def _find_scenario_file(scenario_id: str) -> Path:
    if not _SCENARIO_ID_RE.fullmatch(scenario_id or "") \
            or not is_safe_path_component(scenario_id):
        # Same error as an unknown id, deliberately: the caller learns nothing
        # about the filesystem from the shape of the string it sent. The pattern
        # admits no separator, so the join below cannot leave SCENARIOS_DIR. The
        # second check adds what a charset cannot express: "nul", "con", "aux",
        # "com1" and friends match [A-Za-z0-9_-] but are character DEVICES on
        # Windows, where opening one succeeds and reads back nothing — a
        # scenario that silently loads as empty rather than reporting "no such
        # scenario".
        raise FileNotFoundError(f"No scenario: {scenario_id!r}")
    direct = SCENARIOS_DIR / f"{scenario_id}.yaml"
    if _exists_exact(direct):
        return direct
    path = _legacy_by_id().get(scenario_id)
    if path is None:
        raise FileNotFoundError(f"No scenario: {scenario_id}")
    return path


def load_scenario(scenario_id: str, participant_key: str = "") -> Scenario:
    # v3 study scenarios are compiled from their spec files; the legacy
    # exploratory YAMLs load directly.
    from .scenarios_v3 import available as _v3_available, compile_scenario

    if scenario_id in _v3_available():
        return compile_scenario(scenario_id, participant_key)

    path = _find_scenario_file(scenario_id)
    data = _load_legacy(path)
    if data is None:
        raise FileNotFoundError(f"No scenario: {scenario_id}")
    # _scenario_from_dict hands the file's own `defaults` and `opener` lists
    # straight to the Scenario it returns, so give it a private copy: a caller
    # mutating a Persona default or an opener list must not edit what the cache
    # will serve to the next participant.
    return _scenario_from_dict(copy.deepcopy(data))


def _scenario_from_dict(data: dict) -> Scenario:
    branches = [Branch(**b) for b in data.get("branches", [])]
    references = [Reference(**r) for r in data.get("references", [])]
    mode = data.get("mode", "single" if "cast" not in data else "group")

    cast: List[Agent] = []
    if "cast" in data:
        for entry in data["cast"]:
            cast.append(Agent(
                id=entry["id"],
                name=entry["name"],
                system_prompt=entry["system_prompt"].strip(),
                role=entry.get("role", "").strip(),
                hidden_agenda=(entry.get("hidden_agenda") or "").strip(),
                photo=entry.get("photo", "initials"),
                voice_id=entry.get("voice_id"),
                defaults=entry.get("defaults", {}),
            ))
    else:
        # Legacy single-agent, synthesize a one-element cast.
        cast.append(Agent(
            id="primary",
            name=data.get("agent_name", "AI"),
            system_prompt=data["system_prompt"].strip(),
            photo=data.get("photo", "initials"),
            voice_id=data.get("voice_id"),
            defaults=data.get("defaults", {}),
        ))

    return Scenario(
        id=data["id"],
        title=data["title"],
        intro=data["intro"],
        mode=mode,
        skill=data.get("skill", ""),
        scene=data.get("scene", "").strip(),
        intro_image=(data.get("intro_image") or "").strip(),
        cast=cast,
        director_prompt=data.get("director_prompt", "").strip(),
        opener=data.get("opener", []) or [],
        branches=branches,
        references=references,
        model=data.get("model"),
    )


def list_scenarios() -> List[Dict[str, str]]:
    """Every scenario the app can offer, id/title/skill/mode/cast_size.

    Cached on the contents of both scenario directories. This is the hot one:
    /api/sessions calls it per request and the researcher dashboard polls that
    every 3 s, so an uncached call stalled live audio (see the caching note
    above). The listing is still built by compiling each v3 scenario rather than
    reading its spec fields directly, so that a spec which cannot be compiled is
    excluded here exactly as it would fail later, and a participant is never
    offered a scenario that would 500 when they picked it.
    """
    global _list_cache
    from .scenarios_v3 import V3_DIR, available as _v3_available, compile_scenario

    fp = _fingerprint(V3_DIR, SCENARIOS_DIR)
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
    for p in sorted(SCENARIOS_DIR.glob("*.yaml")):
        data = _load_legacy(p)
        if data is None or "id" not in data or "title" not in data:
            continue
        out.append({
            "id": data["id"],
            "title": data["title"],
            "skill": data.get("skill", ""),
            "mode": data.get("mode", "single" if "cast" not in data else "group"),
            "cast_size": len(data.get("cast", [])) if "cast" in data else 1,
        })
    _list_cache = (fp, out)
    return [dict(r) for r in out]


def compose_system_prompt_single(
    scenario: Scenario,
    persona: Persona,
    live_notes: List[str],
    triggered_branches: List[Branch],
) -> str:
    """Compose the v1 single-agent system prompt. Used by the legacy
    ConversationEngine, group mode goes through engine.AgentEngine instead.
    """
    agent = scenario.cast[0]
    parts = [agent.system_prompt, "", "## Tone and manner"]
    parts.extend(f"- {f}" for f in persona.tone_fragments())
    incivility = persona.incivility_fragments()
    if incivility:
        parts.append("")
        parts.append("## Incivility behaviors (active, research dial)")
        parts.extend(f"- {f}" for f in incivility)
    if triggered_branches:
        parts.append("")
        parts.append("## Situational updates")
        for b in triggered_branches:
            parts.append(f"- {b.inject}")
    if live_notes:
        parts.append("")
        parts.append("## Live direction from the researcher")
        for note in live_notes:
            parts.append(f"- {note}")
    parts.append("")
    parts.append(
        "Speak as if in a real-time voice conversation. Keep replies natural-length "
        "for speech, not chat-text bullets. Do not narrate or describe what you are doing."
    )
    return "\n".join(parts)


# Backward-compat re-export so existing imports keep working.
compose_system_prompt = compose_system_prompt_single
