"""Read container-local fake stats or close metadata, without exposing a port."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import ssl
import urllib.request


def settled(data, session_ids):
    for sid in session_ids:
        path = Path(data) / "sessions" / sid
        try:
            closed = json.loads((path / "manifest.json").read_text(encoding="utf-8"))["status"] == "closed"
        except (OSError, ValueError, KeyError):
            return False
        if not closed or not (path / "conversation_audio.wav").is_file():
            return False
    return bool(session_ids)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kind", choices=("fake", "settled", "settled-sessions"))
    parser.add_argument("session_ids", nargs="*")
    args = parser.parse_args()
    if args.kind == "fake":
        context = ssl.create_default_context(cafile="/load/cert.pem")
        with urllib.request.urlopen("https://localhost:8443/__load/stats", context=context, timeout=5) as reply:
            result = json.load(reply)
    elif args.kind == "settled-sessions":
        result = {"settled_sessions": [sid for sid in args.session_ids if settled("/data", [sid])]}
    else:
        result = {"settled": settled("/data", args.session_ids)}
    print(json.dumps(result))


if __name__ == "__main__":
    main()
