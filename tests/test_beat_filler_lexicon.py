"""#297: which participant turns a planted beat is held over (is_beat_filler).

Ben's two S3A cascade sessions spent 6 of 15 beats on a turn like "Um",
"I think" or "So". The rule is lexical, and it must keep every real answer and
every sim stimulus line, or sim triggers_fired would move.
"""
from pathlib import Path

import pytest

from server.voice import realtime as R

FILLER = ["Um", "Um.", "I think", "Uh, what", "Okay, um.", "So", "Well.",
          "Okay.", "Wait, what?", "Um, so", "Okay, um", "I think I think"]
KEPT = ["", "   ", "Hello.", "Hi", "Who wants to take them?", "Um, tomorrow.",
        "Um, why not, Jordan?", "clear desk", "Okay, we will.",
        "Okay, sounds good.", "No.", "Yes.",
        "That sounds good with me, but I think we should get",
        # Answers, not hesitation: a phrase followed by "so" stays.
        "I think so.", "Yeah, I think so.", "I guess so"]


@pytest.mark.parametrize("text", FILLER)
def test_filler_turns(text):
    assert R.is_beat_filler(text)


@pytest.mark.parametrize("text", KEPT)
def test_kept_turns(text):
    assert not R.is_beat_filler(text)


def test_no_sim_stimulus_line_is_a_filler():
    path = Path(__file__).resolve().parents[1] / "tools/sim/stim/lines.txt"
    texts = [line.split("|")[2] for line in path.read_text(encoding="utf-8").splitlines()
             if line.strip() and not line.startswith("#")]
    assert len(texts) >= 40
    assert [t for t in texts if R.is_beat_filler(t)] == []


def test_the_knob(monkeypatch):
    monkeypatch.delenv("ROOM_BEAT_FILLER_DEFER", raising=False)
    assert R.room_beat_filler_defers() == 3
    monkeypatch.setenv("ROOM_BEAT_FILLER_DEFER", "0")
    assert R.room_beat_filler_defers() == 0
    monkeypatch.setenv("ROOM_BEAT_FILLER_DEFER", "-2")
    assert R.room_beat_filler_defers() == 0
