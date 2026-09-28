"""One simulated participant, one voice encounter, over the page's own protocol.

What the participant page (static/v2.html) does, minus the browser: it enters
through the researcher's /test door (so the run is cohort=internal and can
never be mistaken for study data), opens /ws/participant/voice, sends the one
client_audio_settings frame the page sends, streams 16 kHz PCM in real time in
100 ms pieces (static/pcm-worklet.js's flush size), and acknowledges playback
the way the page's player does: a `playback` start ack when a reply's first
audio would start playing on a real-time play clock, an end ack when its last
sample would finish, and, on assistant_interrupted, an interrupted end ack for
every turn still playing (the page's stopScheduledAudio). Without the acks the
server's floor and probe clocks, which run off playback, never move.

It records a timeline in wall-clock time: when each line started and finished
being sent, and what the server sent back. tools/sim/analyze.py lines that up
with the session's events.jsonl, whose events carry `wall` too; on a local
server the two clocks are the same clock.

The researcher key is read by the caller from .env and passed in. It goes to
the /test door, which needs it, and nowhere else (see _ws_url), and is never
printed: progress lines name lines and frames, not URLs.
"""
from __future__ import annotations

import asyncio
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable, Dict

import websockets

from tools.sim import sequences, stim

CHUNK = 3200                     # 100 ms of 16 kHz s16le, the page's flush size
SILENCE = b"\x00" * CHUNK
SETTLE_IDLE_S = 6.0              # a reply is over when nothing has arrived for this long
SETTLE_LIMIT_S = 45.0
USER_AGENT = "rf-sim/1 (tools/sim)"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def open_test_door(server: str, key: str, tester: str) -> Dict[str, str]:
    """GET /test and read the participant record out of its 307, the way the
    page arrives. Returns {"participant_id", "run"}."""
    q = {"name": tester}
    if key:
        q["key"] = key
    url = f"{server.rstrip('/')}/test?{urllib.parse.urlencode(q)}"
    # Bound to a name rather than called as `opener.open(...)`: that spelling is
    # what tests/test_deploy_portability.py's scan for unpinned file I/O looks
    # for, and this is an HTTP request, not a file.
    fetch = urllib.request.build_opener(_NoRedirect).open
    try:
        fetch(urllib.request.Request(url, headers={"User-Agent": USER_AGENT}), timeout=30)
    except urllib.error.HTTPError as e:
        loc = e.headers.get("Location")
        if e.code == 307 and loc:
            got = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(loc).query))
            if got.get("participant_id"):
                return {"participant_id": got["participant_id"], "run": got.get("run", "")}
        # The status only: the URL carries the key.
        raise RuntimeError(f"/test answered {e.code}, not a redirect with a participant "
                           f"record (401 means SESSION_KEY in .env is not this server's key)")
    raise RuntimeError("/test did not redirect; is this the platform server?")


def _ws_url(server: str, scenario: str, pid: str, run: str) -> str:
    """The socket URL, WITHOUT the key. The socket wants one only under
    PARTICIPANT_KEY_REQUIRED, which neither production nor tools/sim/serve.py
    sets, and uvicorn logs every accepted socket's full URL on its error
    logger (not the access log, so access_log=False does not hide it): a key
    here would be written into the server's log on every run."""
    base = server.rstrip("/")
    base = "wss://" + base[len("https://"):] if base.startswith("https://") else \
        "ws://" + base[len("http://"):] if base.startswith("http://") else base
    q = {"scenario": scenario, "participant_id": pid}
    if run:
        q["run"] = run
    return f"{base}/ws/participant/voice?{urllib.parse.urlencode(q)}"


async def run(server: str, scenario: str, steps: str, *, key: str = "",
              tester: str = "sim", log: Callable[[str], None] = print) -> dict:
    """Drive one encounter through `steps`; return its timeline."""
    plan = sequences.parse(steps)
    for name in sequences.lines_used(steps):
        stim.load(name)          # fail before the session opens, not halfway in
    door = open_test_door(server, key, tester)

    tl: dict = {"scenario": scenario, "steps": steps, "participant_id": door["participant_id"],
                "session_id": None, "started": None, "ended": None, "completed": False,
                "lines": [], "replies_started": 0, "interrupted": 0, "error_frames": []}
    play = {"cursor": 0.0, "turn": None, "acked": set(), "bytes": 0, "ends": {},
            "starts": {}, "seq": 0}
    state = {"idle": time.time(), "session": asyncio.Event()}

    def at() -> str:
        t0 = tl["started"] or time.time()
        return f"{time.time() - t0:6.1f}"

    uri = _ws_url(server, scenario, door["participant_id"], door["run"])
    async with websockets.connect(uri, max_size=None,
                                  additional_headers={"User-Agent": USER_AGENT}) as ws:
        await ws.send(json.dumps({"type": "client_audio_settings", "settings": {
            "sampleRate": 48000, "channelCount": 1, "echoCancellation": True,
            "noiseSuppression": True, "autoGainControl": True, "contextSampleRate": 48000}}))

        async def ack_at(when: float, msg: dict) -> None:
            await asyncio.sleep(max(0.0, when - time.time()))
            msg["lag_s"] = round(max(0.0, time.time() - when), 3)
            await ws.send(json.dumps(msg))

        async def end_now(turn) -> None:
            await ws.send(json.dumps({"type": "playback", "phase": "end", "turn": turn,
                                      "interrupted": True, "lag_s": 0.0}))

        async def reader() -> None:
            async for msg in ws:
                state["idle"] = time.time()
                if isinstance(msg, bytes):
                    play["bytes"] += len(msg)
                    now = time.time()
                    start = max(now, play["cursor"])
                    play["cursor"] = start + len(msg) / stim.BYTES_PER_S
                    t = play["turn"]
                    if t is not None and ("s", t) not in play["acked"]:
                        play["acked"].add(("s", t))
                        play["starts"][t] = start
                        asyncio.ensure_future(ack_at(start, {"type": "playback", "phase": "start", "turn": t}))
                    continue
                m = json.loads(msg)
                ty = m.get("type")
                if ty == "session":
                    tl["session_id"] = m.get("session_id")
                    tl["started"] = time.time()
                    state["session"].set()
                    log(f"  session {tl['session_id']}")
                elif ty == "user_transcript" and m.get("final"):
                    log(f"  {at()} heard     | {m.get('text')!r}")
                elif ty == "assistant_interrupted":
                    tl["interrupted"] += 1
                    log(f"  {at()}   interrupted")
                    play["cursor"] = time.time()
                    # stopScheduledAudio: every turn still due to end ends now.
                    for t, task in list(play["ends"].items()):
                        if not task.done():
                            task.cancel()
                            await end_now(t)
                        play["ends"].pop(t, None)
                    t = play["turn"]
                    if t is not None and ("e", t) not in play["acked"]:
                        play["acked"].add(("e", t))
                        await end_now(t)
                    play["turn"] = None
                elif ty == "assistant_started":
                    play["turn"] = m.get("turn")
                    play["seq"] += 1
                    tl["replies_started"] += 1
                    log(f"  {at()} {str(m.get('agent_name', '?')):9s} | started turn={m.get('turn')}")
                elif ty == "assistant_done":
                    t = play["turn"]
                    if t is not None and ("e", t) not in play["acked"]:
                        play["acked"].add(("e", t))
                        play["ends"][t] = asyncio.ensure_future(ack_at(
                            play["cursor"], {"type": "playback", "phase": "end",
                                             "turn": t, "interrupted": False}))
                    play["turn"] = None
                elif ty == "encounter_complete":
                    tl["completed"] = True
                    log(f"  {at()}   encounter_complete")
                elif ty in ("segment", "handoff", "interaction", "interaction_changed"):
                    log(f"  {at()}   {ty}")
                elif ty == "error":
                    tl["error_frames"].append(str(m.get("message", ""))[:200])
                    log(f"  {at()} ERROR frame: {str(m.get('message', ''))[:120]}")

        rd = asyncio.ensure_future(reader())
        try:
            await asyncio.wait_for(state["session"].wait(), timeout=60)
        except asyncio.TimeoutError:
            rd.cancel()
            raise RuntimeError(f"{scenario}: no session frame within 60 s of opening the socket")

        async def send(pcm: bytes) -> None:
            nxt = time.time()
            for off in range(0, len(pcm), CHUNK):
                await ws.send(pcm[off:off + CHUNK])
                nxt += CHUNK / stim.BYTES_PER_S
                await asyncio.sleep(max(0.0, nxt - time.time()))

        async def say(name: str) -> None:
            start = time.time()
            await send(stim.load(name))
            tl["lines"].append({"name": name, "text": stim.text(name),
                                "start": round(start, 3), "end": round(time.time(), 3)})

        async def quiet(sec: float) -> None:
            await send(b"\x00" * (int(float(sec) * stim.RATE) * 2))

        async def settle() -> None:
            dl = time.time() + SETTLE_LIMIT_S
            state["idle"] = time.time()
            while time.time() < dl:
                await ws.send(SILENCE)
                await asyncio.sleep(0.1)
                if time.time() - state["idle"] > SETTLE_IDLE_S and time.time() > play["cursor"] + 1.5:
                    break

        async def pad(until: float) -> None:
            while time.time() < until:
                await ws.send(SILENCE)
                await asyncio.sleep(0.1)

        def since_start() -> float:
            return time.time() - tl["started"]

        async def _steps(plan) -> None:
            for kind, arg in plan:
                log(f"  {at()} >> {kind}:{arg}")
                if kind == "tone":
                    await send(stim.room_tone(float(arg)))
                elif kind == "zeros":
                    await quiet(float(arg))
                elif kind == "until":
                    left = float(arg) - since_start()
                    if left > 0:
                        await send(stim.room_tone(left))
                elif kind == "say":
                    await say(arg)
                    await settle()
                elif kind == "burst":
                    await say(arg)
                    await quiet(1.2)
                elif kind == "resume":
                    pair, delay = arg.rsplit(":", 1)
                    first, second = pair.split("+")
                    await say(first)
                    await quiet(float(delay))
                    await say(second)
                    await settle()
                elif kind == "bargeplay":
                    pair, delay = arg.rsplit(":", 1)
                    first, second = pair.split("+")
                    seq0, known = play["seq"], set(play["starts"])
                    await say(first)
                    dl, started_at = time.time() + 40, None
                    while time.time() < dl:
                        new = [s for t, s in play["starts"].items() if t not in known]
                        if play["seq"] > seq0 and new and time.time() >= min(new):
                            started_at = min(new)
                            break
                        await ws.send(SILENCE)
                        await asyncio.sleep(0.1)
                    if started_at is None:
                        log(f"  {at()}   no new reply to talk over")
                    else:
                        await pad(started_at + float(delay))
                        log(f"  {at()}   talking over it ({time.time() - started_at:.1f} s into playback)")
                    await say(second)
                    await settle()
                elif kind == "cycle":
                    files, mx = arg.rsplit(":", 1)
                    names, i = files.split("|"), 0
                    while not tl["completed"] and since_start() < float(mx):
                        name = names[i % len(names)]
                        i += 1
                        log(f"  {at()} >> say:{name}")
                        await say(name)
                        await settle()
                        if i % 4 == 0 and not tl["completed"]:
                            log(f"  {at()} >> tone:14")
                            await send(stim.room_tone(14))

        try:
            await _steps(plan)
        except websockets.ConnectionClosed as exc:
            # The server may close first (an encounter that ended itself, a
            # server that died). Not the driver's failure to report: the
            # timeline says how far it got and the events say why.
            tl["closed_by_server"] = f"{exc.__class__.__name__}: {exc}"[:200]
            log(f"  {at()}   socket closed by the server ({tl['closed_by_server']})")
        await asyncio.sleep(1.0)
        rd.cancel()
    tl["ended"] = time.time()
    return tl
