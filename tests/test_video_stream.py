"""The streamed webcam copy, and what becomes of it.

The defect: the page uploads the webcam recording once, from endSession, so a
participant who closed the tab, reloaded or pressed Back mid-conversation left
a complete audio record and no video at all — the recording was a Blob in a
page that no longer existed. The page now streams each MediaRecorder timeslice
to POST /api/sessions/{id}/video-chunk as it is recorded, and video.salvage
promotes that copy to the recording only when the page's own upload never
arrived.

Run from the repo root:

    python -m pytest tests/test_video_stream.py
"""

from __future__ import annotations

import json
import time

import pytest
from botocore.exceptions import NoCredentialsError

from server import video
from test_client_blockers import _run  # noqa: F401  (writes stub.js for the harness)
from test_video_route import (  # noqa: F401  (fixtures)
    PARTICIPANT, SESSION_ID, FakeS3, live_wired, s3, sessions_root, webm,
)

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHUNK_URL = f"/api/sessions/{SESSION_ID}/video-chunk"
LIMIT = 10 * 1024 * 1024


class UploadingS3(FakeS3):
    """FakeS3 that also takes upload_file, the way salvage calls it."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.uploads: list = []
        self.objects: dict = {}

    def upload_file(self, path, bucket, key):
        self.uploads.append((bucket, key))
        self.objects[key] = Path(path).read_bytes()
        if key.endswith("/webcam.webm"):
            self.data = self.objects[key]


def closed(sessions_root, participant="p_x", session_id=SESSION_ID):
    (sessions_root / session_id / "manifest.json").write_text(
        json.dumps({"session_id": session_id, "participant_id": participant,
                    "status": "closed"}), encoding="utf-8")


def stream(n_chunks=3, size=1000, session_id=SESSION_ID):
    pieces = [webm(size)] + [bytes([i]) * size for i in range(1, n_chunks)]
    for i, p in enumerate(pieces):
        assert video.append_chunk(session_id, i, p, limit=LIMIT)["status"] == "stored"
    return b"".join(pieces)


def events(sessions_root, session_id=SESSION_ID):
    p = sessions_root / session_id / "events.jsonl"
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()] if p.exists() else []




def later():
    return time.time() + video.SALVAGE_GRACE_SECONDS + 1


@pytest.fixture(autouse=True)
def keep_everyone(monkeypatch):
    monkeypatch.setattr(video, "salvage_guard", lambda pid: True)


# --- append_chunk -------------------------------------------------------------

def test_chunks_append_in_order_and_concatenate(sessions_root):
    data = stream()
    assert (sessions_root / SESSION_ID / video.VIDEO_STREAM_NAME).read_bytes() == data


def test_a_resent_chunk_is_a_duplicate_not_a_second_copy(sessions_root):
    data = stream(2)
    out = video.append_chunk(SESSION_ID, 1, b"zz", limit=LIMIT)
    assert out["status"] == "duplicate" and out["next_seq"] == 2
    assert (sessions_root / SESSION_ID / video.VIDEO_STREAM_NAME).read_bytes() == data


def test_a_gap_is_refused_with_the_seq_expected(sessions_root):
    stream(1)
    out = video.append_chunk(SESSION_ID, 5, b"zz", limit=LIMIT)
    assert out == {"status": "out_of_order", "next_seq": 1, "bytes": 1000}


def test_bytes_past_the_acknowledged_length_are_cut_before_the_next_append(sessions_root):
    data = stream(2)
    part = sessions_root / SESSION_ID / video.VIDEO_STREAM_NAME
    with open(part, "ab") as fh:     # a write that crashed before its state update
        fh.write(b"torn")
    video.append_chunk(SESSION_ID, 2, b"next", limit=LIMIT)
    assert part.read_bytes() == data + b"next"


def test_the_stream_is_capped(sessions_root):
    with pytest.raises(video.StreamTooLarge):
        video.append_chunk(SESSION_ID, 0, b"x" * 11, limit=10)


def test_the_stream_is_not_named_like_a_recording():
    # encounter_record.build and app._video_state glob webcam* for a stored one.
    assert not video.VIDEO_STREAM_NAME.startswith("webcam")
    assert not video.VIDEO_STREAM_STATE.startswith("webcam")


# --- salvage ------------------------------------------------------------------

def test_a_closed_tab_leaves_a_recording_in_the_bucket(sessions_root, monkeypatch):
    bucket = UploadingS3()
    monkeypatch.setattr(video, "_s3", bucket)
    closed(sessions_root)
    data = stream()
    assert video.salvage(SESSION_ID, now=later()) == "s3"
    # The video first, then the re-archive of the rebuilt record beside it.
    assert bucket.uploads[0] == (video.BUCKET, video.video_key(SESSION_ID))
    assert bucket.objects[video.video_key(SESSION_ID)] == data
    assert f"encounters/{SESSION_ID}/record.json" in bucket.objects or \
        f"encounters/{SESSION_ID}/events.jsonl" in bucket.objects
    ev = events(sessions_root)[-1]
    assert ev["type"] == "video_uploaded" and ev["status"] == "ok"
    assert ev["bytes"] == len(data) and ev["partial"] is True and ev["via"] == "stream"
    assert video.upload_receipt(SESSION_ID) is not None
    assert not (sessions_root / SESSION_ID / video.VIDEO_STREAM_NAME).exists()


def test_with_no_credentials_it_is_kept_on_local_disk(sessions_root, monkeypatch):
    monkeypatch.setattr(video, "_s3", FakeS3(head_error=NoCredentialsError()))
    closed(sessions_root)
    data = stream()
    assert video.salvage(SESSION_ID, now=later()) == "local"
    assert video.local_path(SESSION_ID).read_bytes() == data
    assert events(sessions_root)[-1]["via"] == "stream_local"
    assert video.exists(SESSION_ID)


def test_nothing_is_promoted_inside_the_grace_window(sessions_root, s3):
    closed(sessions_root)
    stream()
    assert video.salvage(SESSION_ID) == "waiting"
    assert events(sessions_root) == []


def test_an_open_session_waits_for_the_stale_bound_not_the_grace(sessions_root, s3):
    stream()      # no manifest status: the store never closed
    assert video.salvage(SESSION_ID, now=later()) == "waiting"


def test_a_real_upload_supersedes_the_stream(sessions_root, monkeypatch):
    bucket = UploadingS3(data=webm(9000))      # the page's own PUT landed
    monkeypatch.setattr(video, "_s3", bucket)
    closed(sessions_root)
    stream()
    assert video.salvage(SESSION_ID, now=later()) == "superseded"
    assert bucket.uploads == [] and len(bucket.data) == 9000
    assert not (sessions_root / SESSION_ID / video.VIDEO_STREAM_NAME).exists()


def test_a_local_upload_supersedes_the_stream(sessions_root, s3):
    closed(sessions_root)
    stream()
    video.local_path(SESSION_ID).write_bytes(webm(500))
    assert video.salvage(SESSION_ID, now=later()) == "superseded"
    assert len(video.local_path(SESSION_ID).read_bytes()) == 500


def test_an_unanswered_head_waits_rather_than_overwriting(sessions_root, monkeypatch):
    from test_video_route import aws_error
    monkeypatch.setattr(video, "_s3", UploadingS3(head_error=aws_error("SlowDown", status=503)))
    closed(sessions_root)
    stream()
    assert video.salvage(SESSION_ID, now=later()) == "waiting"
    assert (sessions_root / SESSION_ID / video.VIDEO_STREAM_NAME).exists()


def test_a_withdrawn_participant_is_not_kept(sessions_root, monkeypatch):
    bucket = UploadingS3()
    monkeypatch.setattr(video, "_s3", bucket)
    monkeypatch.setattr(video, "salvage_guard", lambda pid: False)
    closed(sessions_root)
    stream()
    assert video.salvage(SESSION_ID, now=later()) == "withdrawn"
    assert bucket.uploads == [] and events(sessions_root) == []
    assert not (sessions_root / SESSION_ID / video.VIDEO_STREAM_NAME).exists()


def test_an_unreadable_withdrawal_store_neither_keeps_nor_deletes(sessions_root, s3, monkeypatch):
    def unknown(pid):
        raise RuntimeError("withdrawal status unknown")
    monkeypatch.setattr(video, "salvage_guard", unknown)
    closed(sessions_root)
    stream()
    assert video.salvage(SESSION_ID, now=later()) == "waiting"
    assert (sessions_root / SESSION_ID / video.VIDEO_STREAM_NAME).exists()


def test_the_rebuilt_record_says_the_encounter_has_video(sessions_root, monkeypatch):
    monkeypatch.setattr(video, "_s3", UploadingS3())
    closed(sessions_root)
    stream()
    seen = []
    import server.encounter_record as er
    monkeypatch.setattr(er, "write", lambda sdir: seen.append(Path(sdir).name))
    video.salvage(SESSION_ID, now=later())
    assert seen == [SESSION_ID]


def test_sweep_arms_every_stream_on_disk(sessions_root, monkeypatch):
    armed = []
    monkeypatch.setattr(video, "salvage_later", lambda sid, delay=None: armed.append(sid) or object())
    stream()
    assert video.sweep_streams() == 1 and armed == [SESSION_ID]


# --- the route ----------------------------------------------------------------

def post(wired, seq, body, pid=PARTICIPANT):
    return wired.client.post(CHUNK_URL, params={"seq": seq, "participant_id": pid},
                             content=body, headers={"Content-Type": "video/webm"})


def test_the_route_appends_in_order(live_wired):
    assert post(live_wired, 0, b"aa").json()["status"] == "stored"
    assert post(live_wired, 1, b"bb").json()["next_seq"] == 2
    assert post(live_wired, 1, b"bb").json()["status"] == "duplicate"
    r = post(live_wired, 4, b"cc")
    assert r.status_code == 409 and r.json()["next_seq"] == 2
    assert (live_wired.root / SESSION_ID / video.VIDEO_STREAM_NAME).read_bytes() == b"aabb"


def test_the_route_refuses_someone_elses_session(live_wired):
    assert post(live_wired, 0, b"aa", pid="p_someone_else").status_code == 403
    assert not (live_wired.root / SESSION_ID / video.VIDEO_STREAM_NAME).exists()


def test_the_route_refuses_once_a_recording_is_stored(live_wired):
    (live_wired.root / SESSION_ID / video.LOCAL_VIDEO_NAME).write_bytes(webm(100))
    assert post(live_wired, 0, b"aa").status_code == 409


def test_the_route_refuses_an_empty_chunk(live_wired):
    assert post(live_wired, 0, b"").status_code == 400


def test_closing_the_store_arms_the_salvage(tmp_path, monkeypatch):
    import inspect
    from server import storage
    src = inspect.getsource(storage.SessionStore.close)
    assert "salvage_later" in src and "video_stream.part" in src
    assert video.VIDEO_STREAM_NAME == "video_stream.part"


# --- the page -----------------------------------------------------------------

def test_the_page_streams_every_timeslice_and_flushes_on_exit():
    src = (ROOT / "static" / "v2.html").read_text(encoding="utf-8")
    handler = src[src.index("videoRecorder.ondataavailable"):][:300]
    assert "streamVideoChunk(stream, e.data)" in handler
    assert "/video-chunk" in src
    assert "addEventListener('pagehide', () => { flushVideoStreamOnExit(); })" in src
    # Bound to the session announced for THIS recording, not the page global
    # that still names the previous encounter when the recorder starts.
    assert "videoStream.sid = m.session_id" in src
