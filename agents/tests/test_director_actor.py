from agents.director_actor.director import (FALLBACK, format_transcript,
                                            parse_direction)
from agents.director_actor.scenarios import get_scenario, render_persona
from agents.director_actor.server import build_actor_system, sse_chunk


def test_parse_direction_valid():
    raw = ('Here is my analysis: {"pressure_point": 4, "yield_score": 2, '
           '"drift": false, "stage_direction": "Warm slightly; offer the VP case."}')
    d = parse_direction(raw)
    assert d["pressure_point"] == 4
    assert d["yield_score"] == 2
    assert d["stage_direction"].startswith("Warm")


def test_parse_direction_malformed_falls_back():
    d = parse_direction("no json here at all")
    assert d == FALLBACK
    d2 = parse_direction('{"stage_direction": ""}')
    assert d2["stage_direction"] == FALLBACK["stage_direction"]


def test_format_transcript_roles():
    msgs = [{"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
            {"role": "system", "content": "ignored"}]
    t = format_transcript(msgs)
    assert "PARTICIPANT: hi" in t and "ACTOR: hello" in t and "ignored" not in t


def test_persona_render_and_actor_system():
    sc = get_scenario("S2A")
    persona = render_persona(sc, {"participant_name": "Jinsook"})
    assert "Jinsook" in persona and "{participant_name}" not in persona
    system = build_actor_system(persona, {"stage_direction": "Hold the line."})
    assert system.rstrip().endswith("Hold the line.")
    assert "DIRECTOR NOTE" in system


def test_sse_chunk_format():
    line = sse_chunk("chatcmpl-abc", "m", "hi")
    assert line.startswith("data: ") and '"content": "hi"' in line
    done = sse_chunk("chatcmpl-abc", "m", None, finish="stop")
    assert '"finish_reason": "stop"' in done
