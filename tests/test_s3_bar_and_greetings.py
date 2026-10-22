"""S3A and S3B (2026-10-22): a names-free progress bar, no 'Keep going' note,
and a hello before anything else.

The advisor's notes on S3A: the 'Keep going — you can end this conversation in
about 4 minutes.' notice goes; the page draws a progress bar like S1's, with the
End-unlocks marker and no names on it (one room, one conversation); and every
character greets the room before going to what they came to say.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server import scenarios_v3 as v3  # noqa: E402

V2 = ROOT / "static" / "v2.html"
S3 = {"S3A": ("alex", "jordan", "casey"), "S3B": ("toni", "lee", "ari")}


def test_only_s2_s3_and_s4_ask_for_the_plain_bar():
    for sid in v3.available():
        flag = v3.compile_scenario(sid).briefing["timeline"]
        assert (flag == "bar") == (sid[:2] in ("S2", "S3", "S4")), (sid, flag)


def test_floor_held_no_longer_prints_a_notice():
    src = V2.read_text(encoding="utf-8")
    assert "'Keep going" not in src and '"Keep going —' not in src.replace('// "Keep going —', '')
    branch = src[src.index("m.type === 'floor_held'"):src.index("m.type === 'encounter_clock'")]
    assert "showGateNote" not in branch


def test_the_plain_bar_model_has_no_names_and_puts_the_marker_at_the_floor():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    src = V2.read_text(encoding="utf-8")
    a = src.index("const HANDOFF_PAD_S")
    b = src.index("  function renderTimeline(s)")
    script = """
    let timelineParts = [], timelineKind = 'bar';
    let floorStartMs = 0, timerStartMs = 0, MAX_S = 600, MIN_S = 300;
    const partStartsS = [];
    let started = true;
    """ + src[a:b] + """
    timerStartMs = Date.now() - 30000; floorStartMs = timerStartMs + 7000;
    const out = { plain: timelineModel(30) };
    timelineKind = '';
    out.none = timelineModel(30);
    console.log(JSON.stringify(out));
    """
    res = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=30)
    assert res.returncode == 0, res.stderr
    out = json.loads(res.stdout)
    m = out["plain"]
    assert m["plain"] is True and m["n1"] == "" and m["n2"] == ""
    assert m["unlock"] == 7 + 300 and m["total"] == 600 and m["now"] == 30
    assert out["none"] is None, "no flag and no fixed first part: no bar"


@pytest.mark.parametrize("sid", sorted(S3))
def test_every_character_greets_before_what_they_came_to_say(sid):
    spec = v3.load_spec(sid)
    for aid in S3[sid]:
        prompt = spec["agents"][aid]["system_prompt"]
        section = prompt.split("In the team meeting:\n", 1)[1]
        first_bullet = re.split(r"\n- ", "\n" + section.lstrip("\n"), maxsplit=2)[1]
        assert re.search(r"\bhello\b", first_bullet), (sid, aid, first_bullet[:120])
    beats = spec["interactions"][0]["triggers"]
    first = beats[0]
    assert first["id"] == "t0_hello" and first["esci"] == []
    assert re.search(r"\bhello\b", first["cue"], re.I)
    assert "good morning" in first["cue"] and "never a time of day" in first["cue"]
    assert not re.search(r"resign|pay|plan", first["on_silence"], re.I), "the opening beat is a hello and nothing else"
    # what each came to say waits for the lead to ask, or to turn to them
    assert re.match(r"When they ask", beats[1]["cue"])
    for cue in (beats[2]["cue"], beats[3]["cue"]):
        assert re.search(r"\bhello\b", cue[:260], re.I)


@pytest.mark.parametrize("sid", sorted(S3))
def test_the_card_tells_the_participant_to_greet_and_ask_each_to_share(sid):
    card = v3.compile_scenario(sid).briefing["card"]
    assert any("ask each of them what is going on" in (h.get("text") or "") for h in card["howto"])
