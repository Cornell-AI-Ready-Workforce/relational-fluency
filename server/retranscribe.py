"""High-quality re-transcription of the participant channel.

The live transcript comes from the realtime bridge's own transcriber, which
cannot be configured, passing a transcription model to session.update is
accepted and ignored (verified 2026-08-20). It is good enough to steer on, but
it drops words, and the study's transcript is what raters read and what the
scorer trains on.

So the recorded participant audio is re-transcribed offline against a stronger
multimodal model, and the result is stored alongside the live transcript rather
than replacing it, the live text is the record of what the agent actually
heard and reacted to, which is not the same thing as what was said.

    python -m server.retranscribe <session_id>
    python -m server.retranscribe --all [--force]
"""

from __future__ import annotations

import base64
import json
import sys
import wave
from pathlib import Path
from typing import List, Optional

import httpx

from .llm import gateway_api_key, gateway_base_url, setting
from .storage import SESSIONS_DIR

MODEL = setting("TRANSCRIBE_MODEL", "nto.gemini-2.5-pro")

PROMPT = (
    "Transcribe this audio verbatim. It is one side of a workplace conversation "
    "only the participant is audible. Output only the transcript text, with "
    "normal punctuation and no speaker labels, timestamps, or commentary. "
    "If a passage is inaudible, write [inaudible]."
)


def _duration(path: Path) -> float:
    try:
        with wave.open(str(path), "rb") as w:
            return w.getnframes() / float(w.getframerate() or 1)
    except Exception:
        return 0.0


def transcribe_file(path: Path, *, model: str = MODEL, timeout: float = 300) -> str:
    audio = base64.b64encode(path.read_bytes()).decode()
    # max_tokens has to be generous: a 10-minute turn-dense encounter runs to
    # thousands of tokens, and a low cap silently truncates the transcript.
    payload = {
        "model": model,
        "max_tokens": 8000,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": PROMPT},
                {"type": "input_audio", "input_audio": {"data": audio, "format": "wav"}},
            ],
        }],
    }
    r = httpx.post(
        f"{gateway_base_url().rstrip('/')}/v1/chat/completions",
        headers={"Authorization": f"Bearer {gateway_api_key()}",
                 "Content-Type": "application/json"},
        json=payload, timeout=timeout,
    )
    r.raise_for_status()
    data = r.json()
    return (data["choices"][0]["message"]["content"] or "").strip()


def retranscribe(session_dir: Path, *, force: bool = False) -> Optional[dict]:
    out_path = session_dir / "transcript_participant_hq.json"
    if out_path.exists() and not force:
        # A corrupt cache (killed / disk-full mid-write) must be treated as absent
        # and regenerated, not crash every subsequent run, as elsewhere.
        try:
            return json.loads(out_path.read_text(encoding="utf-8"))
        except ValueError:
            pass

    wav = session_dir / "user_audio.wav"
    if not wav.exists() or _duration(wav) < 1.0:
        return None

    text = transcribe_file(wav)
    result = {
        "source": "user_audio.wav",
        "model": MODEL,
        "duration_s": round(_duration(wav), 1),
        "text": text,
    }
    out_path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")

    # Fold it into the aligned record so downstream readers get it for free.
    rec_path = session_dir / "record.json"
    if rec_path.exists():
        try:
            rec = json.loads(rec_path.read_text(encoding="utf-8"))
            rec["participant_transcript_hq"] = result
            rec_path.write_text(json.dumps(rec, indent=2, ensure_ascii=False), encoding="utf-8")
        except ValueError:
            pass
    return result


def main(argv: List[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    force = "--force" in argv
    positional = [a for a in argv if not a.startswith("--")]
    if "--all" in argv:
        targets = (
            [d for d in sorted(SESSIONS_DIR.iterdir()) if d.is_dir()]
            if SESSIONS_DIR.exists() else []
        )
    elif positional:
        targets = [SESSIONS_DIR / positional[0]]
    else:
        print("usage: retranscribe <session_id> | --all [--force]")
        return 2
    done = 0
    for d in targets:
        if not d.exists():
            print(f"no such session: {d.name}")
            continue
        try:
            res = retranscribe(d, force=force)
        except Exception as exc:  # noqa: BLE001
            print(f"{d.name}: FAILED, {exc}")
            continue
        if res is None:
            print(f"{d.name}: skipped (no usable audio)")
            continue
        # A run that produced no text is not a transcription, and counting it as
        # one is how "5 transcribed" gets printed over a wave with no repaired
        # text in it — the same false-confidence failure verify_record exists to
        # catch, committed by the tool the operator was sent to. It also latches:
        # retranscribe() returns any cache it can parse, so this state survives
        # every plain re-run and only --force gets past it. Say that here, in the
        # place the operator is looking, rather than printing "0 chars".
        #
        # Read defensively, because what comes back on the cache path is
        # whatever JSON is on disk: a file written by an older build (or by
        # hand) can be missing `duration_s`, or not be an object at all, and
        # indexing it killed the whole --all loop at the first such session,
        # taking every later encounter with it. One bad cache must cost one
        # session, not the wave.
        text = res.get("text") if isinstance(res, dict) else None
        if not isinstance(text, str) or not text.strip():
            print(f"{d.name}: NO USABLE TEXT — the re-transcription produced "
                  "none; re-run this session with --force to try again")
            continue
        text = text.strip()
        done += 1
        print(f"{d.name}: {res.get('duration_s')}s → {len(text)} chars")
        print(f"   {text[:150]}")
    print(f"\n{done} transcribed with {MODEL}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
