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
