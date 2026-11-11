"""Generate one illustration per v3 scenario with the ElevenLabs image API.

    python tools/gen_scenario_images.py            # all eight, skips files that exist
    python tools/gen_scenario_images.py S1A S3A    # only these
    python tools/gen_scenario_images.py --force    # regenerate

POST /v1/flows/image starts an asynchronous generation; GET /v1/flows/image/{id}
is polled until it completes and the signed content_url is downloaded. Needs
ELEVENLABS_API_KEY (from the environment or .env) with the Image & Video
permission, and a Pro plan or above: on a lower plan the API answers 402. The
four A-form pictures in static/scenarios/ were made in the ElevenLabs app from
these prompts for that reason. Output: static/scenarios/<ID>.<ext>, which the
intro card shows when the spec's `intro_card.image` names the file.

The scenes show the situation only. Figures are faceless and drawn alike so a
picture does not give a character an age, a look or a mood the voice has not.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "static" / "scenarios"
API = "https://api.elevenlabs.io/v1"
MODEL = "gemini-3-pro-image"

STYLE = (
    "Flat editorial illustration, clean vector shapes with a soft paper grain, "
    "muted palette of warm off-white, slate blue, sage green and one terracotta "
    "accent. Calm, neutral mood. Modern office setting. People are simplified "
    "figures with no facial features, plain everyday work clothes, varied skin "
    "tones and hair. Wide 16:9 composition with generous empty space. "
    "No text, no letters, no numbers, no logos, no watermark, no speech "
    "balloons other than one the scene describes."
)

SCENES = {
    # Each scene is the conversation the participant is about to have, in the
    # place the scenario puts it, with everyone in a neutral posture. Nothing
    # from the backstory and nothing that hints at what is measured (who gets
    # talked over, who is disengaged). Genders follow the cast voices.
    "S1A": "One image split into two side-by-side panels with a thin gap between "
           "them, read left to right, showing the same main person twice in the "
           "same clothes. Left panel: at an office desk, the main person sits "
           "seen from behind while a woman colleague leans in beside the desk, "
           "one hand raised mid-sentence, telling them something. Above her "
           "floats one large speech balloon with its tail pointing to her, and "
           "inside the balloon is a small picture, not words: a man standing at "
           "a wall screen with a chart, presenting to three seated people at a "
           "meeting table. Right panel, "
           "later: an office corridor beside the elevators, where the main "
           "person, seen from behind, stands facing a man colleague who has just "
           "turned from the elevator doors, the two a few steps apart and alone.",
    "S1B": "An office kitchenette in the morning. Two colleagues stand at the "
           "coffee machine, alone in the room. One is in the foreground, seen from "
           "behind, holding an empty mug. The other, a man, stands beside the "
           "machine with a mug, turning toward them.",
    "S2A": "A small manager's office in daylight. Two people sit facing each other "
           "across a desk in a one-on-one meeting. The person in the foreground is "
           "seen from behind, with a single envelope on the desk in front of them. "
           "The manager opposite, a woman, sits upright with hands on the desk.",
    "S2B": "A small glass-walled meeting room inside an office. Two people sit "
           "facing each other across a table in a one-on-one meeting. The person "
           "in the foreground is seen from behind, with a closed laptop in front "
           "of them. The manager opposite is a woman. Through the glass behind "
           "her, an open-plan office with rows of occupied desks.",
    "S3A": "A small office meeting room. Four people sit around one table in a "
           "team meeting. The team lead is in the foreground at the near end, seen "
           "from behind. Facing the lead sit two men and one woman, visibly worn "
           "down and unhappy, shown through posture only: one leans back with "
           "arms folded, one slumps with head propped on a hand, one hunches "
           "forward over the table. In front of each of them is a tall, uneven "
           "pile of contract folders and loose papers, far more than one person "
           "can handle, crowding their laptops. Two further chairs at the table "
           "are empty, with bare table in front of them.",
    "S3B": "A small office meeting room. Four people sit around one table in a "
           "team meeting. The team lead is in the foreground at the near end, seen "
           "from behind. Facing the lead sit one man and two women, each with a "
           "laptop or a phone on the table. No empty chairs.",
    "S4A": "An office meeting room. Four colleagues sit around one table as "
           "equals in a working session, all seated, each with an open laptop. One "
           "is in the foreground, seen from behind. The other three are two men "
           "and one woman. On the wall, a screen shows a monthly calendar as a "
           "grid of blank squares with no digits.",
    "S4B": "An office meeting room. Four colleagues sit around one table as "
           "equals in a working session, all seated, each with printed pages and "
           "a laptop. One is in the foreground, seen from behind. The other three "
           "are two men and one woman. On the wall, a screen shows a presentation "
           "slide made of abstract blocks and bars, no words.",
}


def _key() -> str:
    key = os.environ.get("ELEVENLABS_API_KEY", "")
    env = ROOT / ".env"
    if not key and env.exists():
        for line in env.read_text().splitlines():
            if line.startswith("ELEVENLABS_API_KEY="):
                key = line.split("=", 1)[1].strip().strip("'\"")
    if not key:
        sys.exit("ELEVENLABS_API_KEY is not set")
    return key


def _call(method: str, path: str, key: str, body: dict | None = None) -> dict:
    req = urllib.request.Request(
        API + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"xi-api-key": key, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        sys.exit(f"{method} {path} -> {e.code}: {e.read().decode(errors='replace')[:600]}")


def generate(sid: str, key: str) -> Path:
    gen = _call("POST", "/flows/image", key, {
        "model_id": MODEL, "prompt": f"{SCENES[sid]} {STYLE}",
        "aspect_ratio": "16:9", "resolution": "2K"})
    deadline = time.time() + 300
    while True:
        res = _call("GET", f"/flows/image/{gen['id']}", key)
        if res["status"] in ("completed", "failed"):
            break
        if time.time() > deadline:
            sys.exit(f"{sid}: still {res['status']} after 300 s (id {gen['id']})")
        time.sleep(3)
    if res["status"] == "failed":
        sys.exit(f"{sid}: {res.get('failure_reason')}: {res.get('error_message')}")
    ext = {"image/jpeg": ".jpg", "image/webp": ".webp"}.get(res.get("content_mime_type"), ".png")
    path = OUT / f"{sid}{ext}"
    with urllib.request.urlopen(res["content_url"], timeout=120) as r:
        path.write_bytes(r.read())
    return path


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    force = "--force" in sys.argv
    key = _key()
    OUT.mkdir(parents=True, exist_ok=True)
    for sid in args or list(SCENES):
        if not force and list(OUT.glob(f"{sid}.*")):
            print(f"{sid}: exists, skipped")
            continue
        print(f"{sid}: {generate(sid, key).relative_to(ROOT)}", flush=True)


if __name__ == "__main__":
    main()
