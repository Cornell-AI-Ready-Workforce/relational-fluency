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


def verify(fake_count, attempts, destinations):
    rows = [r for r in attempts if r.get("upstream") == "elevenlabs" and r.get("detail") != "slot_wait"]
    blocked = [r for r in destinations if not r.get("allowed")]
    accepted = sum(r.get("allowed") is True for r in destinations)
    reasons = []
    if blocked:
        reasons.append("non-fake ElevenLabs destination attempted (blocked)")
    if not isinstance(fake_count, int) or fake_count <= 0:
        reasons.append("no fake-side TTS connections proved")
    if fake_count != len(rows) or accepted != len(rows):
        reasons.append("fake/server/destination TTS connection counts do not match")
    return {"passed": not reasons, "fake_connections": fake_count,
            "server_elevenlabs_attempts": len(rows), "guard_allowed": accepted,
            "blocked_destinations": blocked, "reasons": reasons}
