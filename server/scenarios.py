"""Scenario loading + system-prompt composition.

A scenario is a YAML file. Two shapes are supported (auto-detected by loader):

  Single-agent (v1 — legacy, used by missed_deadlines etc.):
    id, title, intro, system_prompt, defaults, voice_id, branches, references

  Multi-agent (v2 — group/team meeting scenarios):
    id, title, intro, mode: group, scene, director_prompt,
    cast: [ {id, name, photo, system_prompt, voice_id, defaults} ],
    branches, references

Internally, legacy single-agent scenarios are normalized to a 1-element cast
with id="primary", so downstream code only ever sees the cast model.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

import yaml

from .persona import Persona


SCENARIOS_DIR = Path(__file__).parent.parent / "scenarios"


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
    hidden_agenda: str = ""  # internal — never sent to the participant pre/in-session; revealed at debrief
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
    scene: str = ""  # shared situation context (used by director, prepended for groups)
    intro_image: str = ""  # filename under static/agents/ shown on the brief screen (pre-start)
    cast: List[Agent] = field(default_factory=list)
    director_prompt: str = ""  # routing guidance for group mode
    # When False, skip the per-gap route_continuation calls — use when the
    # director already returns complete multi-speaker sequences (faster).
    chain_continuations: bool = True
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


def _find_scenario_file(scenario_id: str) -> Path:
    direct = SCENARIOS_DIR / f"{scenario_id}.yaml"
    if direct.exists():
        return direct
    for p in SCENARIOS_DIR.glob("*.yaml"):
        try:
            data = yaml.safe_load(p.read_text())
        except Exception:
            continue
        if data and data.get("id") == scenario_id:
            return p
    raise FileNotFoundError(f"No scenario: {scenario_id}")


def load_scenario(scenario_id: str) -> Scenario:
    path = _find_scenario_file(scenario_id)
    data = yaml.safe_load(path.read_text())
    return _scenario_from_dict(data)


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
        # Legacy single-agent — synthesize a one-element cast.
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
        chain_continuations=data.get("chain_continuations", True),
        opener=data.get("opener", []) or [],
        branches=branches,
        references=references,
        model=data.get("model"),
    )


def list_scenarios() -> List[Dict[str, str]]:
    out = []
    for p in sorted(SCENARIOS_DIR.glob("*.yaml")):
        data = yaml.safe_load(p.read_text())
        out.append({
            "id": data["id"],
            "title": data["title"],
            "skill": data.get("skill", ""),
            "mode": data.get("mode", "single" if "cast" not in data else "group"),
            "cast_size": len(data.get("cast", [])) if "cast" in data else 1,
        })
    return out


def compose_system_prompt_single(
    scenario: Scenario,
    persona: Persona,
    live_notes: List[str],
    triggered_branches: List[Branch],
) -> str:
    """Compose the v1 single-agent system prompt. Used by the legacy
    ConversationEngine — group mode goes through engine.AgentEngine instead.
    """
    agent = scenario.cast[0]
    parts = [agent.system_prompt, "", "## Tone and manner"]
    parts.extend(f"- {f}" for f in persona.tone_fragments())
    incivility = persona.incivility_fragments()
    if incivility:
        parts.append("")
        parts.append("## Incivility behaviors (active — research dial)")
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
        "for speech — not chat-text bullets. Do not narrate or describe what you are doing."
    )
    return "\n".join(parts)


# Backward-compat re-export so existing imports keep working.
compose_system_prompt = compose_system_prompt_single
