"""Every scenario tells the participant what they are there to do, and the page
keeps it in view while they talk (2026-10-15)."""
import re
from pathlib import Path

from server import scenarios_v3 as v3

ROOT = Path(__file__).resolve().parents[1]
V2 = ROOT / "static" / "v2.html"


def test_every_spec_has_a_goal_in_the_participants_terms():
    for sid in v3.available():
        spec = v3.load_spec(sid)
        goal = (spec.get("goal") or "").strip()
        assert goal, f"{sid} has no goal"
        assert goal.endswith("."), f"{sid}'s goal is not a sentence: {goal!r}"
        # The task, never the yardstick: no construct names, no ESCI language.
        low = goal.lower()
        for word in ("teamwork", "influence", "conflict", "inspirational", "esci",
                     "solicit", "credit without", "retrieve"):
            assert word not in low, f"{sid}'s goal names the instrument: {goal!r}"


def test_the_goal_rides_on_the_briefing_card():
    sc = v3.compile_scenario("S4A")
    assert sc.briefing["goal"].startswith("Leave the session with a rollout plan")


def test_the_page_shows_the_goal_on_the_card_and_under_the_header():
    src = V2.read_text(encoding="utf-8")
    assert 'id="goalStrip"' in src
    assert re.search(r"sit-goal.*Your goal", src), "the situation card has no goal line"
    # Shown when the conversation starts, from the brief, not typed into markup.
    assert "showGoalStrip();" in src and "goalText = (d.briefing && d.briefing.goal)" in src


def test_a_step_label_that_names_the_character_is_not_followed_by_with_the_character():
    """S1A's card read 'Hallway run-in with Sam, with Sam' (and the hand-off
    divider 'Hallway run-in with Sam, you are now with Sam'): the page appended
    the people to every label, even one that already named them."""
    src = V2.read_text(encoding="utf-8")
    assert "function labelNamesAll(label, names)" in src
    assert "!labelNamesAll(p.label, p.with)" in src, "the card appends the name again"
    assert "labelNamesAll(m.label, (m.present || []).map(p => p.name))" in src, "the divider does too"
    # The helper itself, run on the shipped labels.
    import json, shutil, subprocess
    node = shutil.which("node")
    if not node:
        import pytest
        pytest.skip("node is not installed")
    start = src.index("function labelNamesAll(label, names)")
    end = src.index("\n  }\n", start) + 4
    body = src[start:end]
    script = body + """
    const cases = [
      ['Hallway run-in with Sam', ['Sam'], true],
      ['Riley corners you', ['Riley'], true],
      ['Team meeting', ['Alex', 'Jordan', 'Casey'], false],
      ['Working session', ['Dan'], false],
      ['Your conversation with Morgan', ['Morgan'], true],
      ['Part 2', [], false],
    ];
    console.log(JSON.stringify(cases.map(([l, n]) => labelNamesAll(l, n))));
    """
    out = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout) == [True, True, False, False, True, False]
