"""#222, the remainder: S4A's Dan no longer calls staging slower.

Dan's version of Chris's pilot is itself staged (Sales on Monday the 9th and
the rest right behind it), so a Dan briefed to call any staging "slower" argues
against the plan he is claiming. Option b of the issue: keep his date, drop
"slower" from form A. t8 keeps its job (Dan dismisses spreading the teams out
or moving the date and puts the 9th back) and its scoring.
"""
from pathlib import Path

import yaml

SPECS = Path(__file__).resolve().parents[1] / "scenarios" / "v3"
A = SPECS / "S4A_internal_rollout.yaml"
B = SPECS / "S4B_client_presentation.yaml"


def _triggers(spec: dict) -> dict:
    return {t["id"]: t for i in spec["interactions"] for t in i.get("triggers") or []}


def test_s4a_dan_does_not_argue_that_staging_is_slower():
    spec = yaml.safe_load(A.read_text(encoding="utf-8"))
    brief = spec["agents"]["dan"]["system_prompt"]
    t8 = _triggers(spec)["t8_splitting_it_up_is_slower"]
    for text in (brief, t8["cue"], t8["on_silence"]):
        assert "slower" not in text and "staging it" not in text, text
    # The argument is the one date, not speed: "speed" against a spread-out
    # plan is what produced "slower" (#222 review).
    assert "Speed is the argument" not in brief and "One date is the argument" in brief


def test_s4a_t8_still_dismisses_spreading_out_and_puts_the_9th_back():
    spec = yaml.safe_load(A.read_text(encoding="utf-8"))
    t8 = _triggers(spec)["t8_splitting_it_up_is_slower"]
    assert t8["agent"] == "dan"
    assert t8["esci"] == ["not_cooperate_r", "encourages_cooperation", "respectful"]
    assert "spreading the teams out" in t8["cue"] and "moving the date" in t8["cue"]
    assert "the date is the date" in t8["cue"] and "the 9th" in t8["cue"]
    assert "the 9th" in t8["on_silence"] and "say yes" in t8["on_silence"]
    # Dan's own version of the pilot still keeps his date.
    t2 = _triggers(spec)["t2_idea_relabelled"]
    assert "Sales on Monday the 9th" in t2["cue"]


def test_s4a_does_not_say_s4b_lacks_the_pilot_beat():
    assert "t11_chris_puts_the_reorder_on_the_table" in _triggers(
        yaml.safe_load(B.read_text(encoding="utf-8")))
    flat = " ".join(line.strip().lstrip("#").strip()
                    for line in A.read_text(encoding="utf-8").splitlines())
    assert "S4B has NOT been given the matching beat" not in flat
