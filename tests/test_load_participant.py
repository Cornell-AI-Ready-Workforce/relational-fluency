import asyncio
import json
import threading

import httpx
import pytest

from tools.load.participant import Participant, VIDEO_BYTES, full_sequence, upload_webcam
from tools.sim import driver, sequences, stim


def test_participant_preserves_cookie_and_sends_real_time_playback_acks(monkeypatch):
    sent, headers, urls = [], [], []
    monkeypatch.setattr(stim, "load", lambda name: b"\0\x10" * 1600)
    monkeypatch.setattr(stim, "text", lambda name: "Hello Morgan.")
    monkeypatch.setattr(stim, "room_tone", lambda seconds: driver.SILENCE)

    class Wire:
        def __init__(self):
            self.queue = asyncio.Queue()
            self.replied = False
        async def __aenter__(self): return self
        async def __aexit__(self, *args):
            self.queue.put_nowait(None)
        async def send(self, raw):
            sent.append(raw)
            if isinstance(raw, bytes):
                if not self.replied:
                    self.replied = True
                    for row in ({"type": "user_transcript", "final": True, "text": "Hello Morgan."},
                                {"type": "assistant_started", "turn": 1}, b"\0" * 3200,
                                {"type": "assistant_done"}):
                        self.queue.put_nowait(row if isinstance(row, bytes) else json.dumps(row))
            else:
                row = json.loads(raw)
                if row["type"] == "client_audio_settings":
                    self.queue.put_nowait(json.dumps({"type": "session", "session_id": "s_1_abcdef"}))
                if row["type"] == "playback" and row["phase"] == "end":
                    self.queue.put_nowait('{"type":"encounter_complete"}')
        def __aiter__(self): return self
        async def __anext__(self):
            row = await self.queue.get()
            if row is None: raise StopAsyncIteration
            return row

    def connector(url, **kwargs):
        urls.append(url)
        headers.append(kwargs["additional_headers"])
        return Wire()

    async def run():
        def http(request):
            assert request.url.path == "/test"
            return httpx.Response(307, headers={"location": "/v2?participant_id=p_test&run=run_test",
                                                "set-cookie": "AWSALB=sticky; Path=/"})
        async with httpx.AsyncClient(transport=httpx.MockTransport(http)) as client:
            participant = Participant("http://server", "S2A", "say:a1", index=1, key="secret-door",
                                      connect=connector, http=client)
            timeline = await asyncio.wait_for(participant.run(), 2)
            assert not participant.active
            assert timeline["completed"] and not timeline["unexpected_disconnect"]
            assert timeline["conversation_started"] is not None
            assert timeline["lines"][0]["text"] == "Hello Morgan."
            assert timeline["websocket_disconnects"] == 1
    asyncio.run(run())
    acks = [json.loads(row) for row in sent if isinstance(row, str) and json.loads(row)["type"] == "playback"]
    assert [row["phase"] for row in acks] == ["start", "end"]
    assert all(row["turn"] == 1 and row["lag_s"] >= 0 for row in acks)
    assert headers[0]["Cookie"] == "AWSALB=sticky"
    assert "secret-door" not in urls[0]


def test_synthetic_webcam_uses_page_route_owner_sequence_mime_and_150kb():
    requests = []
    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={"status": "ok"})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            await upload_webcam(http, "http://server", "s_1_abcdef", "p_test", 7)
    asyncio.run(run())
    request = requests[0]
    assert request.url.path == "/api/sessions/s_1_abcdef/video-chunk"
    assert dict(request.url.params) == {"participant_id": "p_test", "seq": "7"}
    assert request.headers["content-type"] == "video/webm"
    assert request.content == VIDEO_BYTES and len(request.content) == 150_000


def test_full_sequences_reuse_all_stimuli_and_extend_short_scenarios():
    for scenario, original in sequences.DEFAULT_SEQUENCES.items():
        extended = full_sequence(scenario, 600)
        assert extended.startswith(original + ",cycle:")
        assert set(sequences.lines_used(extended)) == set(sequences.lines_used(original))
        assert sequences.parse(extended)[-2][1].endswith(":600")


def test_sim_driver_moves_entry_and_stimulus_reads_off_event_loop(monkeypatch):
    loop_thread = threading.get_ident()
    seen = []
    def off_loop(name):
        assert threading.get_ident() != loop_thread
        seen.append(name)
    def load(name): off_loop("load"); return b"\0" * 3200
    def text(name): off_loop("text"); return "hello"
    def tone(seconds): off_loop("tone"); return b""
    def door(*args): off_loop("door"); return {"participant_id": "p_test", "run": "run_test"}
    class Stopped(Exception): pass
    class StopSocket:
        async def __aenter__(self): raise Stopped
        async def __aexit__(self, *args): pass
    monkeypatch.setattr(stim, "load", load)
    monkeypatch.setattr(stim, "text", text)
    monkeypatch.setattr(stim, "room_tone", tone)
    monkeypatch.setattr(driver, "open_test_door", door)
    monkeypatch.setattr(driver.websockets, "connect", lambda *a, **kw: StopSocket())
    with pytest.raises(Stopped):
        asyncio.run(driver.run("http://server", "S2A", "say:a1", log=lambda text: None))
    assert seen == ["load", "text", "tone", "door"]
