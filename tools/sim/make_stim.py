"""Regenerate stimulus audio from tools/sim/stim/lines.txt. macOS only.

    python -m tools.sim.make_stim              # every line with no audio yet
    python -m tools.sim.make_stim s1 s4        # these lines, overwriting
    python -m tools.sim.make_stim --pcm DIR    # import existing 16 kHz PCM files

The committed .ulaw files are the stimulus; this is how they were made, kept so
a new line can be added the same way. It needs `say` and `afconvert`, which
ship with macOS and nowhere else, which is why the audio is committed rather
than generated at run time: the sim check has to run on the Linux and Windows
machines researchers also use.

Changing an existing line changes the stimulus under tools/sim/baseline.json,
so re-record the baseline in the same change (tools/sim/README.md).
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.sim import stim  # noqa: E402


def _rows():
    out = {}
    for raw in stim.LINES.read_text(encoding="utf-8").splitlines():
        if not raw.strip() or raw.startswith("#"):
            continue
        parts = raw.split("|", 3)
        name, voice, text = (p.strip() for p in parts[:3])
        out[name] = (voice, parts[3].strip() if len(parts) > 3 else text)
    return out


def synthesize(name: str, voice: str, say_input: str) -> Path:
    for tool in ("say", "afconvert"):
        if not shutil.which(tool):
            sys.exit(f"{tool} not found: stimulus audio is made on macOS")
    with tempfile.TemporaryDirectory() as tmp:
        aiff, wav = Path(tmp) / "x.aiff", Path(tmp) / "x.wav"
        subprocess.run(["say", "-v", voice, "-o", str(aiff), say_input], check=True)
        subprocess.run(["afconvert", "-f", "WAVE", "-d", f"LEI16@{stim.RATE}", "-c", "1",
                        str(aiff), str(wav)], check=True)
        with wave.open(str(wav), "rb") as w:
            assert w.getframerate() == stim.RATE and w.getnchannels() == 1 and w.getsampwidth() == 2
            pcm = w.readframes(w.getnframes())
    out = stim.STIM_DIR / f"{name}{stim.EXT}"
    out.write_bytes(stim.ulaw_encode(pcm))
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("names", nargs="*")
    ap.add_argument("--pcm", type=Path,
                    help="import <name>.pcm (16 kHz mono s16le) from this directory instead")
    args = ap.parse_args(argv)
    rows = _rows()
    names = args.names or [n for n in rows if not (stim.STIM_DIR / f"{n}{stim.EXT}").exists()]
    for name in names:
        if name not in rows:
            sys.exit(f"{name} is not in {stim.LINES}")
        if args.pcm:
            src = args.pcm / f"{name}.pcm"
            if not src.is_file():
                print(f"  skip {name}: no {src}")
                continue
            out = stim.STIM_DIR / f"{name}{stim.EXT}"
            out.write_bytes(stim.ulaw_encode(src.read_bytes()))
        else:
            out = synthesize(name, *rows[name])
        print(f"  {out.name}  {out.stat().st_size / stim.RATE:.1f} s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
