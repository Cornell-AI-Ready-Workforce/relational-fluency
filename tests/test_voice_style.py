"""Opt-in per-character accent and tone (server/voice_style.py)."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server import llm, voice_style  # noqa: E402
from server.engine import AgentEngine  # noqa: E402
from server.persona import Persona  # noqa: E402
from server.scenarios import load_scenario  # noqa: E402


def _prompt(sid, aid):
    sc = load_scenario(sid, "p_test")
    agent = next(a for a in sc.cast if a.id == aid)
    return AgentEngine(agent, sc, Persona(), client=object())._system_prompt([])


def _style_file(tmp_path, monkeypatch, body):
    f = tmp_path / "styles.yaml"
    f.write_text(body, encoding="utf-8")
    monkeypatch.setitem(llm._FILE, "VOICE_STYLE_FILE", str(f))
    return f


def test_unset_adds_nothing(monkeypatch):
    monkeypatch.setitem(llm._FILE, "VOICE_STYLE_FILE", "")
    assert "## How you sound" not in _prompt("S2A", "morgan")
    assert llm.provenance()["voice_style"] is None


def test_a_style_reaches_only_its_character(tmp_path, monkeypatch):
    _style_file(tmp_path, monkeypatch,
                "S3A:\n  alex: A dry Australian accent, flat and hard.\n")
    alex, jordan = _prompt("S3A", "alex"), _prompt("S3A", "jordan")
    assert "## How you sound\nA dry Australian accent, flat and hard." in alex
    assert "## How you sound" not in jordan
    prov = llm.provenance()["voice_style"]
    assert prov["scenarios"] == ["S3A"] and len(prov["sha256"]) == 16


def test_the_style_sits_after_the_brief_and_before_the_speech_rules(tmp_path, monkeypatch):
    _style_file(tmp_path, monkeypatch, "S2A:\n  morgan: Soft Scottish accent.\n")
    p = _prompt("S2A", "morgan")
    sc = load_scenario("S2A", "p_test")
    brief = next(a for a in sc.cast if a.id == "morgan").system_prompt.strip().splitlines()[-1]
    assert p.index(brief) < p.index("## How you sound")


def test_a_missing_or_broken_file_is_harmless(tmp_path, monkeypatch):
    monkeypatch.setitem(llm._FILE, "VOICE_STYLE_FILE", str(tmp_path / "nope.yaml"))
    assert "## How you sound" not in _prompt("S2A", "morgan")
    _style_file(tmp_path, monkeypatch, "S2A: [unclosed\n")
    assert "## How you sound" not in _prompt("S2A", "morgan")
    assert voice_style.provenance() is None
