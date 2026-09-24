"""Two fixes from the P6 verification (pipeline 2026-09-24c).

1. A transcript with no letter or digit ("..." / "." / "```") is the
   transcriber describing a sound. Across the verification runs three of them,
   over 300-500 ms of voice (playback bleed, a breath), were kept as
   low_confidence turns and a character replied to each. They are now
   suppressed as no_speech at any voiced level.

2. At an interaction boundary with the same character (S2A i1 -> i2) the
   runner cancels on a fresh session; the gateway answers
   response_cancel_not_active, and the page painted "Something went wrong.
   Please try again." It is now recorded and kept off the page.

No network: the runner and the bridge are driven with the fakes the
participant-turn tests already use.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from server.voice import realtime as R  # noqa: E402

from test_participant_turn_integrity import bridge, runner_for  # noqa: E402


@pytest.mark.parametrize("text", ["...", ".", "```", "。", " … "])
@pytest.mark.parametrize("voiced", [0, 400, 1200, None])
def test_a_wordless_transcript_is_never_a_turn(text, voiced):
    runner, session, ws = runner_for("S4A")
    asyncio.run(runner._record_user_turn(text, item_id="i7", voiced_ms=voiced))
    (sup,) = session.store.of("user_turn_suppressed")
    assert sup["reason"] == "no_speech" and sup["text"] == text
    assert not session.store.of("user_turn")
    assert not ws.frames("user_transcript"), "no caption"
    assert session.shared_history == [], "no steering or director input"
    # Still an arrival (the scribe is working), and an unreliable one, so a
    # room turn for it is skipped rather than routed.
    assert runner._transcripts_arrived == 1


@pytest.mark.parametrize("text", ["Hmm.", "Okay", "Two.", "No.", "Casey?", "Hello?", "20%"])
def test_short_lines_with_a_letter_or_digit_are_kept(text):
    runner, session, _ws = runner_for("S4A")
    asyncio.run(runner._record_user_turn(text, voiced_ms=700))
    (turn,) = session.store.of("user_turn")
    assert turn["text"] == text


def test_the_wordless_rule_is_a_knob(monkeypatch):
    monkeypatch.setenv("PARTICIPANT_DROP_WORDLESS", "0")
    runner, session, _ws = runner_for("S4A")
    asyncio.run(runner._record_user_turn("...", voiced_ms=700))
    assert session.store.of("user_turn"), "0 must restore the 24b behaviour"


def test_is_wordless():
    assert R.is_wordless("...") and R.is_wordless("```") and R.is_wordless("。")
    assert not R.is_wordless("") and not R.is_wordless("Hmm.") and not R.is_wordless("2")


def _first_event(rt, **gateway_event):
    async def go():
        rt.ws.feed(**gateway_event)
        agen = rt.events()
        try:
            return await asyncio.wait_for(agen.__anext__(), 2)
        finally:
            await agen.aclose()
    return asyncio.run(go())


def test_a_cancel_with_nothing_to_cancel_is_benign():
    rt = bridge()
    assert not rt._response_active
    ev = _first_event(rt, type="error", error={
        "type": "invalid_request_error", "code": "response_cancel_not_active",
        "message": "Cancellation failed: no active response found"})
    assert ev["type"] == "error" and ev["benign"] and ev["recoverable"]
    assert "response_cancel_not_active" in ev["message"], "still recorded verbatim"


def test_any_other_gateway_error_is_not_marked_benign():
    rt = bridge()
    ev = _first_event(rt, type="error", error={
        "type": "invalid_request_error", "code": "invalid_api_key", "message": "bad key"})
    assert ev["type"] == "error" and not ev.get("benign")


def test_the_one_to_one_pump_keeps_a_benign_error_off_the_page():
    src = (ROOT / "server" / "realtime_voice_session.py").read_text(encoding="utf-8")
    block = src[src.index('                if ev.get("benign"):'):][:400]
    assert "continue" in block
    # ...and it comes after the voice_error row is written, so the record keeps it.
    head = src[:src.index('                if ev.get("benign"):')]
    assert head.rstrip().endswith(")") and '"voice_error", where="model"' in head[-2500:]
