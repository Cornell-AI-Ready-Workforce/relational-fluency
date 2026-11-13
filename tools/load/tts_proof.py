"""Harness-only WebSocket destination guard and independent TTS accounting."""
from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlsplit


def destination(uri):
    parsed = urlsplit(str(uri))
    if parsed.scheme not in {"ws", "wss"} or parsed.username is not None:
        raise ValueError("unsupported websocket destination")
    return parsed.scheme, parsed.hostname, parsed.port or (443 if parsed.scheme == "wss" else 80), parsed.path


def install_guard(expected, path, *, record_allowed=False):
    import websockets
    from websockets.asyncio import client
    from websockets.legacy import client as legacy
    allowed = destination(expected)

    def wrap(connect):
        def checked(uri, *args, **kwargs):
            try:
                actual = destination(uri)
            except ValueError:
                actual = ("invalid", None, None, None)
            row = {"allowed": actual == allowed, "destination": list(actual)}
            if record_allowed or not row["allowed"]:
                with Path(path).open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(row) + "\n")
            if not row["allowed"]:
                raise RuntimeError("blocked non-fake TTS websocket destination")
            return connect(uri, *args, **kwargs)
        return checked

    # Install before importing server modules, including clients that bind a
    # connect alias. Preserve the awaitable/context-manager returned by connect.
    public, modern, old = websockets.connect, client.connect, legacy.connect
    websockets.connect = wrap(public)
    client.connect = wrap(modern)
    legacy.connect = wrap(old)


def verify(fake_count, attempts, destinations, *, record_allowed=True):
    rows = [r for r in attempts if r.get("upstream") == "elevenlabs" and r.get("detail") != "slot_wait"]
    blocked = [r for r in destinations if not r.get("allowed")]
    accepted = sum(r.get("allowed") is True for r in destinations)
    reasons = []
    if blocked:
        reasons.append("non-fake ElevenLabs destination attempted (blocked)")
    if not isinstance(fake_count, int) or fake_count <= 0:
        reasons.append("no fake-side TTS connections proved")
    if fake_count != len(rows) or (record_allowed and accepted != len(rows)):
        reasons.append("fake/server/destination TTS connection counts do not match")
    return {"passed": not reasons, "fake_connections": fake_count,
            "server_elevenlabs_attempts": len(rows), "guard_allowed": accepted if record_allowed else None,
            "allowed_destination_journal": record_allowed,
            "blocked_destinations": blocked, "reasons": reasons}


async def verify_runtime(runtime):
    """Compare cumulative, settled fake/server counts; ramp journals blocked hosts only."""
    import asyncio
    from tools.load.measure import JsonlTail
    fake = await runtime.fake_stats()
    attempts, destinations = await asyncio.gather(
        asyncio.to_thread(JsonlTail(runtime.data / "upstream-attempts.jsonl").read),
        asyncio.to_thread(JsonlTail(runtime.data / "tts-destinations.jsonl").read))
    return verify(fake.get("counts", {}).get("tts_ws", 0), attempts, destinations,
                  record_allowed=runtime.record_tts_destinations)
