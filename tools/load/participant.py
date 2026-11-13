"""One participant using the page's PCM, playback ACK, and webcam protocols."""
from __future__ import annotations

import asyncio
import json
import time
from urllib.parse import parse_qs, urlparse

import httpx
import websockets

from tools.sim import driver, sequences, stim


VIDEO_BYTES = b"\x1a\x45\xdf\xa3" + b"\0" * (150_000 - 4)


def full_sequence(scenario, ceiling_s):
    original = sequences.DEFAULT_SEQUENCES[scenario]
    names = sequences.lines_used(original)
    return original + ",cycle:" + "|".join(names) + f":{ceiling_s},tone:3"


async def upload_webcam(http, base, sid, pid, seq, payload=VIDEO_BYTES):
    response = await http.post(f"{base}/api/sessions/{sid}/video-chunk",
                               params={"participant_id": pid, "seq": seq}, content=payload,
                               headers={"Content-Type": "video/webm"})
    response.raise_for_status()


async def open_identity(http, base, key, index):
    query = {"name": f"load_{index}"}
    if key:
        query["key"] = key
    door = await http.get(base + "/test", params=query)
    params = parse_qs(urlparse(door.headers.get("location", "")).query)
    if door.status_code != 307 or "participant_id" not in params:
        raise RuntimeError(f"test door status {door.status_code}; no participant redirect")
    return {"participant_id": params["participant_id"][0], "run": params.get("run", [""])[0]}


class Participant:
    def __init__(self, base, scenario, steps, *, index, key="", duration_s=630,
                 webcam=False, connect=websockets.connect, http=None, identity=None):
        self.base, self.scenario, self.steps = base.rstrip("/"), scenario, steps
        self.index, self.key, self.duration_s = index, key, duration_s
        self.webcam, self.connect, self.given_http = webcam, connect, http
        self.identity = identity
        self.active = False
        self.tl = {"scenario": scenario, "steps": steps, "session_id": None,
                   "participant_id": None, "started": None, "ended": None,
                   "conversation_started": None, "hangup_requested": None,
                   "completed": False, "lines": [], "error_frames": [],
                   "websocket_disconnects": 0, "unexpected_disconnect": False,
                   "video_chunks": 0, "replies_started": 0}
        self.pending = set()
        self.ws = None
        self.play_cursor = 0.0
        self.turn = None
        self.play_starts = {}
        self.play_ends = {}
        self.acked_start = set()
        self.acked_end = set()
        self.last_activity = time.monotonic()
        self.session_ready = asyncio.Event()
        self.completed = asyncio.Event()

    def spawn(self, coro):
        task = asyncio.create_task(coro)
        self.pending.add(task)
        task.add_done_callback(self.pending.discard)
        return task

    async def ack(self, when, phase, turn, interrupted=False):
        await asyncio.sleep(max(0, when - time.monotonic()))
        try:
            await self.ws.send(json.dumps({"type": "playback", "phase": phase,
                                           "turn": turn, "interrupted": interrupted,
                                           "lag_s": max(0.0, time.monotonic() - when)}))
        except websockets.ConnectionClosed:
            pass

    async def read(self):
        async for raw in self.ws:
            if isinstance(raw, bytes):
                now = time.monotonic()
                self.last_activity = now
                start = max(now, self.play_cursor)
                self.play_cursor = start + len(raw) / stim.BYTES_PER_S
                if self.turn is not None and self.turn not in self.acked_start:
                    self.acked_start.add(self.turn)
                    self.play_starts[self.turn] = start
                    self.spawn(self.ack(start, "start", self.turn))
                continue
            message = json.loads(raw)
            kind = message.get("type")
            if kind == "session":
                self.tl["session_id"] = message["session_id"]
                self.tl["started"] = time.time()
                self.session_ready.set()
            elif kind == "user_transcript" and message.get("final"):
                self.last_activity = time.monotonic()
                if self.tl["conversation_started"] is None:
                    self.tl["conversation_started"] = time.time()
                    self.active = True
            elif kind == "assistant_started":
                self.last_activity = time.monotonic()
                self.turn = message.get("turn")
                self.tl["replies_started"] += 1
            elif kind == "assistant_done":
                self.last_activity = time.monotonic()
                if self.turn is not None and self.turn not in self.acked_end:
                    self.acked_end.add(self.turn)
                    self.play_ends[self.turn] = self.spawn(self.ack(self.play_cursor, "end", self.turn))
                self.turn = None
            elif kind == "assistant_interrupted":
                self.play_cursor = time.monotonic()
                for turn, task in list(self.play_ends.items()):
                    if not task.done():
                        task.cancel()
                        await self.ack(self.play_cursor, "end", turn, True)
                if self.turn is not None and self.turn not in self.acked_end:
                    self.acked_end.add(self.turn)
                    await self.ack(self.play_cursor, "end", self.turn, True)
                self.turn = None
            elif kind == "encounter_complete":
                self.tl["completed"] = True
                self.completed.set()
            elif kind == "error":
                self.tl["error_frames"].append(str(message.get("message", ""))[:200])
        if not self.completed.is_set() and not self.tl["hangup_requested"]:
            self.tl["unexpected_disconnect"] = True

    async def send(self, pcm):
        due = time.monotonic()
        for offset in range(0, len(pcm), driver.CHUNK):
            if self.completed.is_set():
                return
            piece = pcm[offset:offset + driver.CHUNK]
            await self.ws.send(piece)
            due += len(piece) / stim.BYTES_PER_S
            await asyncio.sleep(max(0, due - time.monotonic()))

    async def quiet(self, seconds, *, tone=False):
        end = time.monotonic() + max(0, seconds)
        chunk = self.tone if tone else driver.SILENCE
        while time.monotonic() < end and not self.completed.is_set():
            await self.send(chunk)

    async def say(self, name):
        pcm, text = self.clips[name]
        start = time.time()
        await self.send(pcm)
        self.tl["lines"].append({"name": name, "text": text, "start": start, "end": time.time()})

    async def settle(self):
        end = time.monotonic() + driver.SETTLE_LIMIT_S
        self.last_activity = time.monotonic()
        while time.monotonic() < end and not self.completed.is_set():
            await self.send(driver.SILENCE)
            now = time.monotonic()
            if now - self.last_activity > driver.SETTLE_IDLE_S and now > self.play_cursor + 1.5:
                return

    async def execute(self):
        for kind, arg in sequences.parse(self.steps):
            if self.completed.is_set():
                return
            if kind in {"tone", "zeros"}:
                await self.quiet(float(arg), tone=kind == "tone")
            elif kind == "until":
                await self.quiet(float(arg) - (time.time() - self.tl["started"]), tone=True)
            elif kind in {"say", "burst"}:
                await self.say(arg)
                await (self.settle() if kind == "say" else self.quiet(1.2))
            elif kind in {"resume", "bargeplay"}:
                pair, delay = arg.rsplit(":", 1)
                first, second = pair.split("+")
                known = set(self.play_starts)
                await self.say(first)
                if kind == "resume":
                    await self.quiet(float(delay))
                else:
                    deadline = time.monotonic() + 40
                    new = []
                    while time.monotonic() < deadline and not self.completed.is_set():
                        new = [when for turn, when in self.play_starts.items() if turn not in known]
                        if new:
                            break
                        await self.send(driver.SILENCE)
                    if new:
                        await self.quiet(min(new) + float(delay) - time.monotonic())
                await self.say(second)
                await self.settle()
            elif kind == "cycle":
                names, until = arg.rsplit(":", 1)
                cycle = names.split("|")
                count = 0
                while not self.completed.is_set() and time.time() - self.tl["started"] < float(until):
                    await self.say(cycle[count % len(cycle)])
                    await self.settle()
                    count += 1
                    if count % 4 == 0:
                        await self.quiet(14, tone=True)

    async def video(self, http):
        seq = 0
        while not self.completed.is_set():
            await asyncio.sleep(2)
            if self.completed.is_set():
                return
            try:
                await upload_webcam(http, self.base, self.tl["session_id"], self.tl["participant_id"], seq)
                self.tl["video_chunks"] += 1
                seq += 1
            except (httpx.HTTPError, ValueError) as exc:
                self.tl["video_error"] = type(exc).__name__
                return

    async def run(self):
        def preload():
            return ({name: (stim.load(name), stim.text(name)) for name in sequences.lines_used(self.steps)},
                    stim.room_tone(0.1))
        self.clips, self.tone = await asyncio.to_thread(preload)
        http = self.given_http or await asyncio.to_thread(
            httpx.AsyncClient, timeout=30, follow_redirects=False, trust_env=False)
        reader = None
        try:
            identity = self.identity or await open_identity(http, self.base, self.key, self.index)
            pid, run = identity["participant_id"], identity["run"]
            self.tl["participant_id"] = pid
            # Preserve the HTTP cookie jar if the caller uses an ALB.
            headers = {"User-Agent": "rf-load/1"}
            cookie = "; ".join(f"{k}={v}" for k, v in http.cookies.items())
            if cookie:
                headers["Cookie"] = cookie
            async with self.connect(driver._ws_url(self.base, self.scenario, pid, run),
                                    additional_headers=headers, max_size=None) as ws:
                self.ws = ws
                await ws.send(json.dumps({"type": "client_audio_settings", "settings": {
                    "sampleRate": 48000, "channelCount": 1, "echoCancellation": True,
                    "noiseSuppression": True, "autoGainControl": True, "contextSampleRate": 48000}}))
                reader = self.spawn(self.read())
                await asyncio.wait_for(self.session_ready.wait(), 60)
                if self.webcam:
                    self.spawn(self.video(http))
                try:
                    await asyncio.wait_for(self.execute(), self.duration_s)
                except asyncio.TimeoutError:
                    self.tl["duration_limit_reached"] = True
                self.tl["hangup_requested"] = time.time()
        except asyncio.CancelledError:
            self.tl["cancelled"] = True
            raise
        except (websockets.ConnectionClosed, httpx.HTTPError, RuntimeError, TimeoutError) as exc:
            if not self.tl["completed"]:
                self.tl["unexpected_disconnect"] = True
                self.tl["driver_error"] = type(exc).__name__
        finally:
            self.tl["hangup_requested"] = self.tl["hangup_requested"] or time.time()
            self.active = False
            self.tl["ended"] = time.time()
            if self.ws is not None:
                self.tl["websocket_disconnects"] += 1
            for task in list(self.pending):
                task.cancel()
            await asyncio.gather(*self.pending, return_exceptions=True)
            if reader and reader.done() and not reader.cancelled() and reader.exception():
                self.tl["driver_error"] = type(reader.exception()).__name__
                self.tl["unexpected_disconnect"] = True
            if self.given_http is None:
                await http.aclose()
        return self.tl
