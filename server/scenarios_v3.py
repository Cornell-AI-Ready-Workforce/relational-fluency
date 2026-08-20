"""Compile v3 scenario specs into runnable Scenario objects.

The specs in scenarios/v3/ are the research artefact — they carry the structure
the study measures: two interactions per encounter, an ordered list of planted
triggers, the ESCI items each trigger maps to, an on_silence probe, and the
scored sample answers from Research Note v3. The engine wants a flat cast with
rendered system prompts.

Compiling rather than hand-copying keeps the spec as the single source of truth:
edit the YAML the researchers reason about, and the runnable form follows.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from .scenarios import Agent, Scenario

V3_DIR = Path(__file__).parent.parent / "scenarios" / "v3"

def _join(names: list) -> str:
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + f" and {names[-1]}"


# Gemini Live voices, assigned per cast position when the spec does not name one.
_VOICES = ["Puck", "Charon", "Kore", "Fenrir", "Aoede"]


def _spec_files() -> Dict[str, Path]:
    return {
        yaml.safe_load(p.read_text())["id"]: p
        for p in sorted(V3_DIR.glob("*.yaml"))
    }


def available() -> List[str]:
    return sorted(_spec_files())


def load_spec(scenario_id: str) -> Dict[str, Any]:
    files = _spec_files()
    if scenario_id not in files:
        raise FileNotFoundError(f"No v3 scenario {scenario_id!r} in {V3_DIR}")
    return yaml.safe_load(files[scenario_id].read_text())


def _render_prompt(spec: dict, key: str, agent: dict) -> str:
    """The character's brief: who they are, then the shared situation, then the
    behaviour policy from the spec. The triggers themselves are injected by the
    runner as they fire, not dumped up front — an agent that can see every
    planted beat tends to rush through them."""
    name = agent["name"]
    others = [a["name"] for k, a in spec["agents"].items() if k != key]
    parts = [f"# You are {name}", "", agent["system_prompt"].strip()]
    if spec.get("setup"):
        parts += ["", "## Situation", spec["setup"].strip()]
    parts += [
        "",
        "## Identity — this matters",
        f"- You ARE {name}. Speak as {name}, in the first person, always.",
        f"- Never narrate {name}'s actions or refer to {name} in the third person.",
    ]
    if others:
        parts += [
            f"- {_join(others)} {'is' if len(others) == 1 else 'are'} "
            f"{'another person' if len(others) == 1 else 'other people'} in this scene, "
            "not you. You never speak for them or as them.",
        ]
    parts += [
        "- If asked who you are, answer as yourself and stay in the scene.",
        "",
        "## Manner",
        "- This is a live spoken conversation. One to three sentences per turn.",
        "- Never read out stage directions, JSON, or anything meta.",
        "- Stay in character. Do not summarise or coach the participant.",
    ]
    return "\n".join(parts)


def compile_scenario(scenario_id: str) -> Scenario:
    spec = load_spec(scenario_id)
    agents_spec: Dict[str, dict] = spec["agents"]

    cast: List[Agent] = []
    for idx, (aid, a) in enumerate(agents_spec.items()):
        cast.append(Agent(
            id=aid,
            name=a["name"],
            role=a.get("role", ""),
            system_prompt=_render_prompt(spec, aid, a),
            voice_id=a.get("voice") or _VOICES[idx % len(_VOICES)],
        ))

    interactions = spec.get("interactions", [])
    # An encounter is group-mode if any interaction puts several characters in
    # the room at once.
    is_group = any(i.get("mode") == "group" for i in interactions)

    scenario = Scenario(
        id=spec["id"],
        title=f"{spec['title']} ({spec['construct'].replace('_', ' ')}, var. {spec['variant']})",
        intro=spec.get("setup", "").strip(),
        mode="group" if is_group else "single",
        skill=spec["construct"],
        scene=spec.get("setup", "").strip(),
        cast=cast,
        director_prompt=_director_prompt(spec),
    )
    # Carried for the runner: interaction order, planted triggers, ESCI map,
    # and the parallel form used at attempt 2.
    scenario.interactions = interactions            # type: ignore[attr-defined]
    scenario.esci_items = spec.get("esci_items", {})  # type: ignore[attr-defined]
    scenario.construct = spec["construct"]           # type: ignore[attr-defined]
    scenario.variant = spec["variant"]               # type: ignore[attr-defined]
    scenario.parallel_form = spec.get("parallel_form")  # type: ignore[attr-defined]
    return scenario


def _director_prompt(spec: dict) -> str:
    lines = [
        f"Encounter measuring {spec['construct'].replace('_', ' ')}.",
        spec.get("skill_measured", "").strip(),
        "",
        "Characters:",
    ]
    for aid, a in spec["agents"].items():
        lines.append(f"- {a['name']} ({aid}): {a.get('role', '')}")
    lines += [
        "",
        "Planted triggers fire in order. Keep the scene moving toward the next",
        "one; do not let the conversation drift or resolve early.",
    ]
    return "\n".join(l for l in lines if l is not None)


def triggers_for(scenario_id: str, interaction_index: int) -> List[dict]:
    spec = load_spec(scenario_id)
    interactions = spec.get("interactions", [])
    if interaction_index >= len(interactions):
        return []
    return interactions[interaction_index].get("triggers", [])
