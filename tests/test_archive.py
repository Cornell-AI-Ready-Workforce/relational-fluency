"""Study 1 plan 5.2: a closed encounter is copied to the study bucket."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server import archive  # noqa: E402


class FakeS3:
    def __init__(self, fail=()):
        self.calls = []
        self.fail = set(fail)

    def upload_file(self, path, bucket, key):
        if Path(path).name in self.fail:
            raise RuntimeError("AccessDenied")
        self.calls.append((Path(path).name, bucket, key))


def _session(tmp_path):
    d = tmp_path / "s_1"
    d.mkdir()
    for n in ("manifest.json", "record.json", "events.jsonl", "user_audio.wav", "assistant_audio_dan.wav"):
        (d / n).write_bytes(b"x")
    (d / "webcam.webm").write_bytes(b"v")   # already in S3 by its own path
    return d


def test_every_record_file_lands_under_the_encounter_prefix(tmp_path):
    d = _session(tmp_path)
    s3 = FakeS3()
    res = archive.archive_session(d, client=s3, bucket="b")
    assert res["ok"] and not res["failed"]
    assert [c[0] for c in s3.calls] == ["manifest.json", "record.json", "events.jsonl",
                                        "assistant_audio_dan.wav", "user_audio.wav"]
    assert all(c[1] == "b" and c[2] == f"encounters/s_1/{c[0]}" for c in s3.calls)
    assert json.loads((d / "archive.json").read_text(encoding="utf-8"))["uploaded"] == res["uploaded"]


def test_one_refused_file_does_not_stop_the_rest_and_is_named(tmp_path):
    d = _session(tmp_path)
    res = archive.archive_session(d, client=FakeS3(fail={"record.json"}), bucket="b")
    assert res["ok"] is False
    assert [f["file"] for f in res["failed"]] == ["record.json"]
    assert "user_audio.wav" in res["uploaded"]


def test_no_credentials_is_written_down_not_raised(tmp_path, monkeypatch):
    d = _session(tmp_path)
    from server import video
    def boom():
        raise RuntimeError("Unable to locate credentials")
    monkeypatch.setattr(video, "_client", boom)
    res = archive.archive_session(d, bucket="b")
    assert res["ok"] is False and "credentials" in res["error"]
    assert (d / "archive.json").exists()


def test_the_close_path_archives_in_a_thread_and_can_be_switched_off(tmp_path, monkeypatch):
    d = _session(tmp_path)
    seen = []
    t = archive.archive_session_later(d, runner=lambda p: seen.append(p))
    t.join(5)
    assert seen == [d]
    monkeypatch.setenv(archive.ENABLED_ENV, "0")
    assert archive.archive_session_later(d, runner=lambda p: seen.append(p)) is None
