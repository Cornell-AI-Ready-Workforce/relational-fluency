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
    assert sc.briefing["goal"].startswith("You want all four of you to leave the session with a rollout plan")


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


def test_every_scenario_has_its_own_intro_card_and_they_read_alike():
    """The intro card (2026-10-20): every scenario draws the same layout, one
    `parts` entry per conversation (consecutive interactions with the same
    people are one), and the how-to says one person or several as the room
    does."""
    for sid in v3.available():
        b = v3.compile_scenario(sid).briefing
        card = b.get("card")
        assert card, f"{sid} has no intro_card"
        assert card["situation"].strip() and "{" not in card["situation"], sid
        convs = []
        for p in b["parts"]:
            if not convs or convs[-1] != p["with"]:
                convs.append(p["with"])
        assert len(card["parts"]) == len(convs), f"{sid}: {len(convs)} conversations, {len(card['parts'])} card parts"
        assert [h["icon"] for h in card["howto"]] == ["mic", "message", "bulb", "skip"], sid
        assert card["start"] == "I am ready, let's start", sid
        talk = card["howto"][1]["text"]
        group = any(p["mode"] == "group" for p in b["parts"])
        assert ("The other people hear you and reply." in talk) == group, (sid, talk)
        assert ("The other person hears you and replies." in talk) == (not group), (sid, talk)


def test_the_page_draws_the_intro_card_and_no_longer_says_what_to_do():
    src = V2.read_text(encoding="utf-8")
    assert "function introCardHtml(d)" in src and "introCardHtml(d)" in src.split("function showSituation(d)")[1]
    assert "What to do" not in src
    assert src.count("How to take part") >= 2
