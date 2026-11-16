"""Fixed server-frame CPU workload. Scores are comparable only within this version."""
from __future__ import annotations

import argparse
import base64
import json
import math
import platform
import time

from server.voice.realtime import _ratecv, resampler_name


# Keep existing C-path references valid; the Python fallback has a different
# CPU cost and must not silently compare against those references.
VERSION = "pcm100ms-json-base64-ratecv-v1" + (
    "-linear-py" if resampler_name(48000, 16000) == "linear-py" else "")


def calibrate(seconds=10.0):
    pcm = bytes(range(256)) * 37 + bytes(range(128))  # 9600 bytes: 100 ms PCM16, 48 kHz
    packet = json.dumps({"audio": base64.b64encode(pcm).decode("ascii"), "sample_rate": 48000})
    start = time.perf_counter()
    count, checksum = 0, 0
    while time.perf_counter() - start < seconds:
        row = json.loads(packet)
        decoded = base64.b64decode(row["audio"], validate=True)
        frame, _ = _ratecv(decoded, row["sample_rate"], 16000, None)
        checksum += len(frame)
        count += 1
    elapsed = time.perf_counter() - start
    return {"version": VERSION, "frames": count, "elapsed_s": elapsed,
            "frames_per_second": count / elapsed, "checksum": checksum,
            "python": platform.python_version(), "architecture": platform.machine()}


def compare(score, reference):
    value = reference.get("frames_per_second", 0)
    if reference.get("version") != VERSION or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ValueError("Fargate reference needs the same calibration version and a positive score")
    return score["frames_per_second"] / reference["frames_per_second"]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=float, default=10.0)
    print(json.dumps(calibrate(parser.parse_args().seconds)))
