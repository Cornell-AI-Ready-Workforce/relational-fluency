"""Session attribution through the real runner, clients, registry, and store."""
import asyncio
import json

import httpx
import httpx2
import pytest

from server import conversation_audio, llm, session as sessions, storage
from server.realtime_voice_session import RealtimeVoiceSessionRunner
from server.voice import cascade, realtime
from test_cascade_voice import MODEL, PCM, _fake_tts, _handler
from test_live_realism_pipeline import FakeWS
from test_upstream_telemetry import records
from test_upstream_telemetry_sites import tool_response


@pytest.mark.parametrize("scenario_id", ["S2A", "S3A"], ids=["one_to_one", "group"])
def test_session_attempts_reach_own_event_file_and_listener(
        records, monkeypatch, tmp_path, scenario_id):
    # Storage and sessions are real. Only upstream transports, the participant
    # websocket, and post-close rendering/archiving are replaced.
    monkeypatch.setattr(storage, "DATA_DIR", tmp_path)
    monkeypatch.setattr(storage, "SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(storage, "PARTICIPANTS_DIR", tmp_path / "participants")
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "index.db")
    monkeypatch.setattr(sessions.registry, "_sessions", {})
    monkeypatch.setattr(conversation_audio, "finish_later", lambda path: None)
    monkeypatch.setattr(llm, "_FILE", {})
    monkeypatch.setattr(realtime, "MODEL", MODEL)
    monkeypatch.setattr(realtime, "gateway_api_key", lambda: "test-key")
    monkeypatch.setenv("LITELLM_API_KEY", "test-key")
    _fake_tts(monkeypatch, {})

    async def run():
        target = None
        other = None
        runner = None
        bridge = None
        event_stream = None

        def messages(request):
            body = json.loads(request.content)
            tool = body["tool_choice"]["name"]
            data = ({"speakers": [{"agent_id": target.scenario.cast[0].id,
                                    "intent": "Ask about the deadline."}]}
                    if tool == "set_speakers" else {"adjustments": []})
            return httpx2.Response(200, json=tool_response(tool, data))

        async with (
            httpx.AsyncClient(transport=httpx.MockTransport(_handler({}))) as http,
            httpx2.AsyncClient(transport=httpx2.MockTransport(messages)) as sdk_http,
        ):
            client = llm.text_client().with_options(http_client=sdk_http)
            monkeypatch.setattr(sessions, "text_client", lambda: client)
            monkeypatch.setattr(cascade, "_shared_http", lambda: http)
            try:
                target = sessions.registry.create(scenario_id, capture_audio=True)
                other = sessions.registry.create("S2A", capture_audio=True)
                target.auto_steering = False
                runner = RealtimeVoiceSessionRunner(target, FakeWS())
                gates = {purpose: asyncio.Event() for purpose in
                         ("stt", "actor", "tts", "director", "steering")}

                def observed(event):
                    if event["session_id"] == target.id:
                        gates[event["purpose"]].set()

                from server import upstream_telemetry
                upstream_telemetry.add_listener(observed)

                if scenario_id == "S2A":
                    bridge = runner._new_session(
                        instructions=runner._instructions(), voice=runner._voice())
                    await bridge.connect(open_conversation=False)
                    await bridge.send_audio(PCM)
                    await bridge.commit_turn()
                    event_stream = bridge.events()

                    async def reply():
                        async for event in event_stream:
                            assert event["type"] != "error", event
                            if event["type"] == "response_done":
                                return
                        pytest.fail("one-to-one reply ended without response_done")

                    await asyncio.wait_for(reply(), 3)
                else:
                    # _open_room passes the ID to every member and the scribe.
                    # Its normal relay pumps consume their bridge events.
                    await runner._open_room()
                    room = runner.room
                    await room.hear(b"\x00\x20" * 24000)
                    assert await room.close_participant_turn()
                    await asyncio.wait_for(gates["stt"].wait(), 3)
                    choices = await target.director.route(
                        [{"speaker": "user", "text": "I missed the deadline."}],
                        "I missed the deadline.")
                    member = await room.give_floor(choices[0]["agent_id"])
                    await asyncio.wait_for(gates["tts"].wait(), 3)
                    await asyncio.wait_for(member.ws._reply, 3)

                await target.steering.review(
                    [{"speaker": "user", "text": "I missed the deadline."}],
                    target.personas, target.name_lookup)
                expected = {"stt", "actor", "tts", "steering"}
                if scenario_id == "S3A":
                    expected.add("director")
                await asyncio.wait_for(asyncio.gather(
                    *(gates[purpose].wait() for purpose in expected)), 3)
                rows = [json.loads(line) for line in
                        target.store.events_path.read_text(encoding="utf-8").splitlines()]
                attempts = [row for row in rows if row["type"] == "upstream_attempt"]
                assert len(attempts) == len(records) == len(expected)
                assert {row["purpose"] for row in attempts} == expected
                assert all(row["session_id"] == target.id and row["outcome"] == "ok"
                           for row in attempts + records)
                assert [{k: v for k, v in row.items() if k != "t"} for row in records] == [
                    {k: v for k, v in row.items() if k not in {"type", "t", "wall"}}
                    for row in attempts]
                unrelated = other.store.events_path.read_text(encoding="utf-8")
                assert "upstream_attempt" not in unrelated
            finally:
                if event_stream is not None:
                    await event_stream.aclose()
                if bridge is not None:
                    await bridge.close()
                if runner is not None:
                    pumps = list(runner._pumps)
                    await runner._close_room()
                    await asyncio.gather(*pumps, return_exceptions=True)
                for encounter in (target, other):
                    if encounter is not None:
                        sessions.registry.drop(encounter.id)

    asyncio.run(run())
