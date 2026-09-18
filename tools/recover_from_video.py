"""Recover a transcript from a webcam recording.

The webcam video carries BOTH sides of the conversation (the recording bus
mixes the participant's microphone with the characters' playback), so an
encounter whose server-side record was lost can still be transcribed from it.
This is a recovery path, not the study's primary transcript: the live and the
retranscribed participant channel are separate and speaker-attributed by
construction; this one is diarised by the model.

    python -m tools.recover_from_video <dir-with-audio.mp3> [--scenario S2A] [--out transcript.md]
"""
from __future__ import annotations

import argparse
import base64
import json
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server.llm import gateway_api_key, gateway_base_url, setting  # noqa: E402

MODEL = setting("TRANSCRIBE_MODEL", "nto.gemini-2.5-pro")

CAST = {
    "S1A": "Riley, Sam", "S1B": "Mel, Drew", "S2A": "Morgan", "S2B": "Sasha",
    "S3A": "Alex, Jordan, Casey", "S3B": "Toni, Lee, Ari",
    "S4A": "Dan, Priya, Chris", "S4B": "Dan, Priya, Chris",
}

def prompt(scenario: str | None) -> str:
    cast = CAST.get(scenario or "", "")
    who = (f"The AI characters in this scene are: {cast}." if cast else
           "The AI characters may be any of: Riley, Sam, Mel, Drew, Morgan, Sasha, "
           "Alex, Jordan, Casey, Toni, Lee, Ari, Dan, Priya, Chris.")
    return (
        "This is a recording of a workplace role-play between one human PARTICIPANT "
        f"and one or more AI characters. {who}\n"
        "Transcribe it verbatim as a dialogue, one line per turn, in the form\n"
        "PARTICIPANT: ...\nNAME: ...\n"
        "Use the character's name when you can tell who is speaking, otherwise CHARACTER. "
        "Keep the participant's exact words including fillers. Mark unclear passages [inaudible]. "
        "Output only the dialogue, no commentary."
    )

def transcribe(mp3: Path, scenario: str | None, timeout: float = 600) -> str:
    audio = base64.b64encode(mp3.read_bytes()).decode()
    payload = {
        "model": MODEL, "max_tokens": 12000,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": prompt(scenario)},
            {"type": "input_audio", "input_audio": {"data": audio, "format": "mp3"}},
        ]}],
    }
    r = httpx.post(f"{gateway_base_url().rstrip('/')}/v1/chat/completions",
                   headers={"Authorization": f"Bearer {gateway_api_key()}",
                            "Content-Type": "application/json"},
                   json=payload, timeout=timeout)
    r.raise_for_status()
    return (r.json()["choices"][0]["message"]["content"] or "").strip()

def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("dir", type=Path)
    ap.add_argument("--scenario")
    ap.add_argument("--out", type=Path)
    a = ap.parse_args(argv)
    mp3 = a.dir / "audio.mp3"
    if not mp3.exists():
        print("no audio.mp3 in", a.dir, file=sys.stderr); return 2
    text = transcribe(mp3, a.scenario)
    out = a.out or (a.dir / "recovered_transcript.md")
    header = f"# Recovered transcript — {a.dir.name}\n\nScenario: {a.scenario or 'unknown'} · source: webcam.webm audio · model: {MODEL}\n\n"
    out.write_text(header + text + "\n", encoding="utf-8")
    print(out)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
