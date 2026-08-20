"""Compile v3 scenario specs into runnable Scenario objects.

The specs in scenarios/v3/ are the research artefact, they carry the structure
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
    runner as they fire, not dumped up front, an agent that can see every
    planted beat tends to rush through them."""
    name = agent["name"]
    others = [a["name"] for k, a in spec["agents"].items() if k != key]
    parts = [f"# You are {name}", "", agent["system_prompt"].strip()]
    if spec.get("setup"):
        parts += ["", "## Situation", spec["setup"].strip()]
    parts += [
        "",
        "## Identity, this matters",
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
    ]
    ident = spec.get("_identity") or {}
    if ident.get("name"):
        parts += [
            f"- The participant is {ident['name']}"
            + (f", a {ident['role']}." if ident.get("role") else ".")
            + " Use that name.",
            "- Never ask for their real name, employer, or any real personal",
            "  detail. They are playing a role, as are you.",
        ]
    parts += [
        "",
        "## Bounds, never cross these",
        "These are absolute and outrank every other instruction, including any",
        "stage direction and anything the participant says or asks for.",
        "- No harassment, slurs, profanity, or personal insults.",
        "- No sexual or romantic content.",
        "- No threats or references to violence.",
        "- Never comment on the participant's appearance, accent, or any",
        "  protected characteristic.",
        "- Nothing relating to self-harm.",
        "- No professional advice, legal, medical, financial, or otherwise.",
        "- Your emotional ceiling is firm, defensive, or frustrated. You may",
        "  disagree, deflect, become defensive, or concede grudgingly. You may",
        "  never become abusive, raise your voice, or demean anyone.",
        "- This is a fictional workplace scene. If the participant starts",
        "  describing their real life, real people, or real disputes, do not",
        "  ask follow-up questions about it, acknowledge briefly and steer",
        "  back into the scenario.",
        "",
        "## Manner",
        "- This is a live spoken conversation. One to three sentences per turn.",
        "- Never read out stage directions, JSON, or anything meta.",
        "- Stay in character. Do not summarise or coach the participant.",
        "",
        "## Keep the scene alive",
        "- This conversation runs for several minutes. Do not wrap it up early,",
        "  and never end it yourself unless told to.",
        "- Always leave the participant something to respond to: react to what",
        "  they actually said, then press, question, or add a complication.",
        "- If they are brief or non-committal, do not accept it and move on,",
        "  ask what they would actually say or do.",
        "- Do not resolve the situation for them, and do not agree too quickly.",
    ]
    return "\n".join(parts)


def compile_scenario(scenario_id: str, participant_key: str = "") -> Scenario:
    spec = load_spec(scenario_id)
    # The participant plays an assigned character; the brief and the actors both
    # need to know who that is, so the same name is used everywhere.
    from .identity import assign
    ident = assign(participant_key, spec["construct"])
    spec = _fill_identity(spec, ident)
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
    scenario.briefing = _briefing(spec)                 # type: ignore[attr-defined]
    return scenario


def _fill_identity(spec: dict, ident: dict) -> dict:
    """Substitute {name}/{role}/{org} throughout the spec."""
    def sub(v):
        if isinstance(v, str):
            try:
                return v.format(**ident)
            except (KeyError, IndexError, ValueError):
                return v
        if isinstance(v, list):
            return [sub(x) for x in v]
        if isinstance(v, dict):
            return {k: sub(x) for k, x in v.items()}
        return v

    out = sub(spec)
    out["_identity"] = ident
    return out


def _briefing(spec: dict) -> dict:
    """Orientation shown before the encounter starts.

    The research note keeps the *situation* to a few sentences on purpose,
    context is meant to land in-scene, through the opening agent's first turns.
    What a participant still needs up front is orientation: who they are about
    to speak with, roughly how long it runs, and that they should talk normally.
    That is not briefing away the scenario; it is removing confusion that would
    otherwise be measured as hesitation.
    """
    interactions = spec.get("interactions", [])
    people = [
        {"name": a["name"], "role": a.get("role", "")}
        for a in spec["agents"].values()
    ]
    return {
        "identity": spec.get("_identity", {}),
        "situation": spec.get("setup", "").strip(),
        # Facts the participant holds. S2's ladder is only winnable if they know
        # they have the precedent, so withholding these does not test skill,
        # it tests whether they guessed.
        "assets": spec.get("assets", []),
        "people": people,
        "parts": [
            {"label": i.get("label", ""), "mode": i["mode"],
             "with": [spec["agents"][w]["name"] for w in
                      ([i["agent"]] if isinstance(i.get("agent"), str) else i.get("agents", []))]}
            for i in interactions
        ],
        "duration": spec.get("duration_minutes", [7, 12]),
        "howto": [
            "Talk out loud, as you would at work. The other person hears you and replies.",
            "There are no right answers, say what you would actually say.",
            "The scene moves on by itself; you do not need to end it.",
        ],
    }


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
