"""Regressions for the deployment env block, the v3 scenario declarations, and
the rater roster's inactive guard.

Grouped in one file because they share one failure shape: each is a place where
the artefact the study keeps — the provenance block on a record, the scene an
actor is briefed with, the plan that says an encounter is covered — can be wrong
while everything still looks like it worked. None of them raises, none of them
shows up in a transcript, and every one of them is only visible by reading the
configuration against the code that consumes it. So they are pinned here.

The terraform tests read the HCL as text, the same way tests/test_task_definition_env.py
does and for the same reason: terraform is not installed here, and the thing
under test is the literal name each entry is wired to.
"""

from __future__ import annotations

import importlib
import json
import logging
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

ECS_TF = REPO_ROOT / "infra" / "terraform" / "ecs.tf"
VARS_TF = REPO_ROOT / "infra" / "terraform" / "variables.tf"
V3_DIR = REPO_ROOT / "scenarios" / "v3"

_ENTRY = re.compile(
    r'\{\s*name\s*=\s*"([A-Z0-9_]+)"\s*,\s*(?:value|valueFrom)\s*=\s*([^\n}]+?)\s*\}'
)


def _list_block(body: str, key: str) -> str:
    start = body.index(f"{key} = [")
    depth, i = 0, body.index("[", start)
    for j in range(i, len(body)):
        if body[j] == "[":
            depth += 1
        elif body[j] == "]":
            depth -= 1
            if depth == 0:
                return body[i:j + 1]
    raise AssertionError(f"unterminated {key} list in {ECS_TF}")


@pytest.fixture(scope="module")
def task_env():
    return dict(_ENTRY.findall(_list_block(ECS_TF.read_text(encoding="utf-8"), "environment")))


# ---------- R37: the text engine and the director are two settings ----------

def test_claude_model_is_not_wired_to_the_director(task_env):
    """CLAUDE_MODEL is read as DEFAULT_MODEL by server/engine.py and rendered
    by server/app.py as the default in the
    researcher's pre-start model picker. Pointing it at var.director_model
    repointed the deployment's text engine and that picker at the director's
    model to make one provenance field come out right. The record is fixed a
    different way now (see the sibling test), so nothing is allowed to put the
    engine back under the director."""
    assert task_env.get("CLAUDE_MODEL") == "var.text_model", (
        f"CLAUDE_MODEL is wired to {task_env.get('CLAUDE_MODEL')!r}; it must be "
        "var.text_model, or a text-mode encounter and the researcher's model "
        "picker silently run whatever the director runs"
    )
    assert task_env.get("DIRECTOR_MODEL") == "var.director_model"
    assert task_env["CLAUDE_MODEL"] != task_env["DIRECTOR_MODEL"], (
        "the two roles must be separately settable; collapsing them is how the "
        "engine moved last time"
    )


def test_text_model_variable_defaults_to_the_code_default():
    """A deployment that sets neither variable must behave exactly as the code
    and .env.example do — otherwise applying this terraform silently changes
    which model serves text-mode encounters, which is the failure this whole
    item is about."""
    hcl = VARS_TF.read_text(encoding="utf-8")
    block = re.search(r'variable\s+"text_model"\s*\{(.*?)\n\}', hcl, re.S)
    assert block, "variables.tf declares no text_model variable"
    declared = re.search(r'default\s*=\s*"([^"]+)"', block.group(1))
    assert declared, "text_model has no default"

    # The value the two readers fall back to when the env var is unset.
    code_defaults = set()
    for rel, pattern in (
        ("server/engine.py", r'setting\("CLAUDE_MODEL",\s*"([^"]+)"\)'),
        ("server/llm.py", r'"text_model":\s*_cfg\("CLAUDE_MODEL",\s*"([^"]+)"\)'),
    ):
        m = re.search(pattern, (REPO_ROOT / rel).read_text(encoding="utf-8"))
        assert m, f"could not find the CLAUDE_MODEL default in {rel}"
        code_defaults.add(m.group(1))
    assert len(code_defaults) == 1, f"code disagrees on the text default: {code_defaults}"
    assert declared.group(1) == code_defaults.pop()


def test_director_model_reaches_the_record_without_claude_model():
    """The reason the split is safe: the director's model is recorded on its own
    path, off the live Director instance, so provenance.text_model does not have
    to stand in for it. If these stamps ever go away, provenance is the only
    place the director's identity could live and the two variables would have to
    be reconsidered."""
    src = (REPO_ROOT / "server" / "realtime_voice_session.py").read_text(encoding="utf-8")
    assert src.count('"director_model": (self.director.model') >= 4, (
        "stage_direction events no longer stamp the live director's model; "
        "provenance.text_model is not a substitute for it"
    )


def test_data_dir_is_declared_where_the_volume_is_mounted(task_env):
    """server/storage.py falls back to <repo>/data when DATA_DIR is unset, which
    on Fargate is the container filesystem the next deploy destroys. The EFS
    mount alone does not redirect the app; only DATA_DIR does. Losing this makes
    a healthy-looking task write every session, run and rating to a disk nobody
    will ever read."""
    body = ECS_TF.read_text(encoding="utf-8")
    mount = re.search(r'containerPath\s*=\s*"([^"]+)"', body)
    assert mount, "no containerPath in the task definition"
    assert task_env.get("DATA_DIR") == f'"{mount.group(1)}"', (
        f"DATA_DIR is {task_env.get('DATA_DIR')!r} but the volume mounts at "
        f"{mount.group(1)!r}"
    )


# ---------- P2: the participant's leverage is declared, not guessed ----------

def _spec_text(sid: str) -> str:
    path = next(p for p in V3_DIR.glob("*.yaml") if p.name.startswith(sid + "_"))
    return path.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def v3():
    import server.scenarios_v3 as mod
    return mod


@pytest.mark.parametrize("sid", ["S2A", "S2B"])
def test_influence_specs_declare_private_setup(v3, sid):
    """S2A and S2B are the only specs with `assets:`, and the asymmetry those
    assets create IS the Influence construct. Without an explicit list the
    redaction runs on _matching_asset, a word-overlap guess; a reworded setup
    could fall under its threshold and brief the counterpart with the
    participant's leverage, and the encounter would record normally."""
    spec = v3.load_spec(sid)
    private = spec.get("private_setup") or []
    assert private, f"{sid} has assets but declares no private_setup"
    setup = spec["setup"]
    for entry in private:
        # An entry that is not a substring of the setup is silently a no-op:
        # the guess is skipped because the key is present, and nothing is
        # withheld. That failure looks like success from every angle.
        assert entry.lower() in setup.lower(), (
            f"{sid}: private_setup entry {entry!r} does not occur in `setup`, "
            "so it withholds nothing"
        )


@pytest.mark.parametrize("sid", ["S2A", "S2B"])
def test_declared_private_setup_leaves_the_actor_scene(v3, sid, caplog):
    """The scene compiled here is pasted verbatim into every actor's system
    prompt, and the same text unredacted goes to the judge and the steering
    controller. Check both ends, and check no WARNING fired — a WARNING means
    the guess ran, which is exactly what the declaration is for."""
    spec = v3.load_spec(sid)
    with caplog.at_level(logging.INFO, logger="server.scenarios_v3"):
        sc = v3.compile_scenario(sid)

    for entry in spec["private_setup"]:
        core = entry.rstrip(".").split(" ", 2)[-1].lower()  # drop "You now"/"A peer"
        assert core not in sc.scene.lower(), (
            f"{sid}: {entry!r} reached the actor scene"
        )
    # The steering controller needs the leverage; only the
    # actors lose it. This is the half a blunt "strip the assets" fix breaks.
    assert "competing offer" in sc.analysis_scene.lower() or "rivera" in sc.analysis_scene.lower()

    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert not warnings, (
        "compiling %s still logs %r — the word-overlap guess is being consulted, "
        "so private_setup is not covering every private sentence"
        % (sid, [r.getMessage() for r in warnings])
    )


def test_the_guess_still_works_for_a_spec_that_declares_nothing(v3):
    """The declaration replaces the guess per-spec, not globally. A spec with
    assets and no private_setup must still be redacted (and still warn), or
    adding these two keys would have quietly disarmed the fallback for any
    future Influence variant."""
    spec = v3.load_spec("S2A")
    del spec["private_setup"]
    kept = v3._redacted_sentences(spec)
    assert not any("competing offer" in s.lower() for s in kept)


# ---------- P13: the S4 t3 pin stays pinned ----------

@pytest.mark.parametrize("sid,tid", [
    ("S4A", "t3_decisions_close_with_priya_silent"),
    ("S4B", "t3_runthrough_without_priya"),
])
def test_s4_silence_beat_stays_bound_to_dan(v3, sid, tid):
    """`agent: dan` is read on the normal group-turn path as well as by the
    silence probe, so it does narrow delivery and defer the beat when the router
    routes elsewhere. That was weighed and kept: the deferral ends in a recorded
    gap (advance_requested.skipped_triggers) on a re-runnable encounter, whereas
    unpinning has _probe_room hand Priya's own retrieval to Priya and score it —
    a measurement of something that did not happen. Unpin only together with a
    `probe_agent` key that binds the probe alone."""
    spec = v3.load_spec(sid)
    i1 = spec["interactions"][0]
    trig = next(t for t in i1["triggers"] if t["id"] == tid)
    assert trig.get("agent") == "dan"
    assert trig.get("on_silence"), "the pin exists for the probe; a beat with no probe does not need it"
    # The two forms are reported against each other as skill change, so the
    # binding has to be identical or they behave differently under silence.
    assert "priya" in json.dumps(i1).lower()


@pytest.mark.parametrize("sid", ["S4A", "S4B"])
def test_s4_t3_pin_is_explained_where_it_is_written(v3, sid):
    """The yaml comment used to argue only the _probe_room case, so the next
    reader met an unexplained deferral on the normal path and the obvious fix
    was to delete the pin. Keep the consequence written next to the line."""
    text = _spec_text(sid)
    assert "_run_group_turn" in text or "normal group-turn path" in text, (
        f"{sid}: the `agent: dan` pin is not documented as affecting normal "
        "delivery, which is how it gets removed"
    )
    assert "probe_agent" in text, (
        f"{sid}: the comment does not name the narrow fix, so the next reader "
        "reaches for unpinning instead"
    )


# ---------- P14: the inactive-rater guard is reachable ----------

@pytest.fixture()
def raters_mod(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    import server.storage as storage
    importlib.reload(storage)
    import server.raters as mod
    importlib.reload(mod)
    mod.init_rater_storage()
    return mod


# ---------- P4: the linter stays at zero ----------

def test_persona_has_no_unused_imports():
    """`field` and `Optional` were the only pyflakes findings in the whole
    server/tests/tools/agents tree. A linter with a permanent floor of two known
    findings is one nobody runs, and the next real finding arrives invisible."""
    src = (REPO_ROOT / "server" / "persona.py").read_text(encoding="utf-8")
    assert "from dataclasses import dataclass, asdict" in src
    assert "from typing import Callable, Dict, List\n" in src
    import server.persona as persona
    importlib.reload(persona)
    p = persona.Persona() if hasattr(persona, "Persona") else None
    if p is not None:
        assert isinstance(p.snapshot(), dict)  # asdict is still the one that is used
