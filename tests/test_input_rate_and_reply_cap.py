"""Phase 1 of the #21-#25 fixes, items 1, 2 and 5: what the gpt route is sent.

  1. Input rate (#21, #22). The gpt row said 16 kHz "straight through", and
     the gateway read those frames as 24 kHz, so everyone was heard 1.5x fast.
     The row now says 24000, send_audio resamples the browser's 16 kHz up to
     it, and commit_turn's "is there anything in the buffer" bar is 100 ms at
     THAT rate, since pending_input counts bytes as they went on the wire.
  2. Transcriber (#21). gpt-4o-transcribe by default, INPUT_TRANSCRIPTION_MODEL
     to change it, the language hint and nothing else on the dict, and the
     record says whichever one ran.
  3. Reply cap (#23). 1200 tokens as a runaway guard, REALTIME_MAX_OUTPUT_TOKENS
     to change it (blank or 0 sends none), and the record says what was sent.

No network: the frames are captured off a stand-in socket.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server import llm  # noqa: E402
from server.voice import realtime  # noqa: E402
from server.voice.realtime import (  # noqa: E402
    CLIENT_RATE, RealtimeVoiceSession, capabilities_for,
)

GPT = "gpt-realtime-2.1"
GEMINI = "nto.gemini-live-2.5-flash"
NATIVE = "nto.gemini-live-2.5-flash-native-audio"


class Wire:
    """Just enough socket to capture what the bridge sends."""

    def __init__(self):
        self.sent = []

    async def send(self, payload):
        self.sent.append(json.loads(payload))

    async def close(self):
        pass

    def appended_bytes(self):
        import base64
        return sum(len(base64.b64decode(f["audio"])) for f in self.sent
                   if f.get("type") == "input_audio_buffer.append")

    def types(self):
        return [f.get("type") for f in self.sent]


def _session(model=GPT):
    rt = RealtimeVoiceSession("x", model=model, voice="", api_key="k")
    rt.ws = Wire()
    return rt


# --------------------------------------------------------------------------
# 1. Input rate
# --------------------------------------------------------------------------

def test_the_gpt_row_sends_what_the_gateway_reads():
    assert capabilities_for(GPT).input_rate == 24000
    assert realtime.input_rate_for_model(GPT) == 24000
    # The routes that were measured at their own rates are untouched.
    assert realtime.input_rate_for_model(NATIVE) == 24000
    assert realtime.input_rate_for_model(GEMINI) == CLIENT_RATE


def test_browser_audio_is_resampled_up_and_counted_as_sent():
    """One second of the page's 16 kHz goes out as one second of 24 kHz, in
    the 100 ms pieces the page sends, and pending_input counts wire bytes."""
    async def go():
        rt = _session()
        step = CLIENT_RATE * 2 // 10
        pcm = b"\x10\x00" * CLIENT_RATE
        for i in range(0, len(pcm), step):
            await rt.send_audio(pcm[i:i + step])
        return rt
    rt = asyncio.run(go())
    sent = rt.ws.appended_bytes()
    assert abs(sent - 24000 * 2) <= 4, sent
    assert rt.pending_input == sent


def test_the_commit_bar_is_100_ms_at_the_rate_on_the_wire():
    """3200 bytes was 100 ms at 16 kHz and is 67 ms at 24 kHz. A buffer that
    small now gets the 300 ms pad before it is committed."""
    async def go(pending):
        rt = _session()
        rt.pending_input = pending
        await rt.commit_turn()
        return rt.ws
    short = asyncio.run(go(4000))
    assert short.types()[-1] == "input_audio_buffer.commit"
    # The pad is 300 ms of the page's rate, resampled like everything else.
    assert abs(short.appended_bytes() - 24000 * 2 * 3 // 10) <= 4
    enough = asyncio.run(go(4800))
    assert enough.types() == ["input_audio_buffer.commit"]


def test_a_probe_pad_of_100_ms_still_reaches_the_bar():
    """The watchdog probes append 3200 bytes of the page's rate (100 ms) and
    commit. That is 4798-4800 bytes on the wire; the first resample call can
    come out two bytes short, and then commit_turn tops it up rather than
    committing a 99.96 ms buffer the gateway may call empty."""
    async def go():
        rt = _session()
        await rt.send_audio(b"\x00" * 3200)
        await rt.commit_turn()
        return rt.ws
    ws = asyncio.run(go())
    assert ws.types()[-1] == "input_audio_buffer.commit"
    assert ws.appended_bytes() >= 4800


# --------------------------------------------------------------------------
# 2. Transcriber
# --------------------------------------------------------------------------

def test_the_default_transcriber_is_gpt_4o_transcribe_with_nothing_else(monkeypatch):
    monkeypatch.setenv("TRANSCRIPTION_LANG", "en")
    payload = RealtimeVoiceSession("x", model=GPT, api_key="k")._session_payload()
    # The language hint and the model, and no prompt or noise_reduction: a
    # prompt made gpt-4o-transcribe invent more, not less (diag track1).
    assert payload["input_audio_transcription"] == {
        "model": "gpt-4o-transcribe", "language": "en"}
    assert "audio" not in payload
    assert realtime.audio_provenance(GPT)["input_transcription_model"] == "gpt-4o-transcribe"


def test_input_transcription_model_changes_the_session_and_the_record(monkeypatch):
    monkeypatch.setenv("INPUT_TRANSCRIPTION_MODEL", "whisper-1")
    payload = RealtimeVoiceSession("x", model=GPT, api_key="k")._session_payload()
    assert payload["input_audio_transcription"]["model"] == "whisper-1"
    assert llm.provenance(GPT)["input_transcription_model"] == "whisper-1"


def test_input_transcription_model_never_reaches_a_gemini_session(monkeypatch):
    """The Gemini rows transcribe on their own and were never measured with a
    model key; a knob for the gpt route must not put one on them."""
    monkeypatch.setenv("INPUT_TRANSCRIPTION_MODEL", "whisper-1")
    for model in (GEMINI, NATIVE):
        payload = RealtimeVoiceSession("x", model=model, api_key="k")._session_payload()
        assert "model" not in (payload.get("input_audio_transcription") or {})
        assert realtime.audio_provenance(model)["input_transcription_model"] is None


def test_a_blank_transcriber_setting_keeps_the_row(monkeypatch):
    monkeypatch.setenv("INPUT_TRANSCRIPTION_MODEL", "")
    assert realtime.input_transcription_model_for(capabilities_for(GPT)) == "gpt-4o-transcribe"


# --------------------------------------------------------------------------
# 3. Reply cap
# --------------------------------------------------------------------------

def test_the_default_cap_is_a_runaway_guard():
    payload = RealtimeVoiceSession("x", model=GPT, api_key="k")._session_payload()
    assert payload["max_output_tokens"] == 1200
    assert realtime.audio_provenance(GPT)["max_output_tokens"] == 1200


@pytest.mark.parametrize("raw,expect", [
    ("900", 900),
    ("", None),        # blank: no cap at all
    ("0", None),       # 0: the same
    ("abc", 1200),     # a typo must not uncap a live study
    ("-5", 1200),
])
def test_realtime_max_output_tokens(raw, expect, monkeypatch):
    monkeypatch.setenv("REALTIME_MAX_OUTPUT_TOKENS", raw)
    payload = RealtimeVoiceSession("x", model=GPT, api_key="k")._session_payload()
    assert payload.get("max_output_tokens") == expect
    if expect is None:
        assert "max_output_tokens" not in payload
    # The record says what went on the wire, whatever the knob said.
    assert llm.provenance(GPT)["max_output_tokens"] == expect


def test_the_cap_knob_never_puts_a_cap_on_a_gemini_session(monkeypatch):
    monkeypatch.setenv("REALTIME_MAX_OUTPUT_TOKENS", "900")
    for model in (GEMINI, NATIVE):
        payload = RealtimeVoiceSession("x", model=model, api_key="k")._session_payload()
        assert "max_output_tokens" not in payload
        assert realtime.audio_provenance(model)["max_output_tokens"] is None


def test_setting_if_set_tells_blank_from_unset(monkeypatch):
    monkeypatch.delenv("RF_TEST_KNOB", raising=False)
    assert llm.setting_if_set("RF_TEST_KNOB") is None
    monkeypatch.setenv("RF_TEST_KNOB", "")
    assert llm.setting_if_set("RF_TEST_KNOB") == ""
    monkeypatch.setenv("RF_TEST_KNOB", " 7 ")
    assert llm.setting_if_set("RF_TEST_KNOB") == "7"


def test_the_pipeline_version_moved_with_these_changes():
    """Bumped in the same commit as the behaviour, as llm.py's comment asks."""
    assert llm.PIPELINE_VERSION >= "2026-09-23b"
    assert llm.provenance(GPT)["pipeline_version"] == llm.PIPELINE_VERSION
