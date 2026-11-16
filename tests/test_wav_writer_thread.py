"""Session WAV files are written off the event loop.

append() ran a seek, write, header patch and flush for every microphone frame
on the event loop. A slow disk then stopped every participant on the server
(205 and 374 ms measured with one participant, 2026-11-15). These tests pin
the replacement: append() only queues, one writer thread writes in order, and
close() leaves the same file as before.
"""
import threading
import time
import wave

import pytest

from server import storage
from server.storage import SAMPLE_RATE, WavAppender


def frames(path):
    with wave.open(str(path), "rb") as w:
        assert (w.getnchannels(), w.getsampwidth(), w.getframerate()) == (1, 2, SAMPLE_RATE)
        return w.readframes(w.getnframes())


def test_close_writes_every_frame_in_order(tmp_path):
    a = WavAppender(tmp_path / "a.wav")
    chunks = [bytes([i % 256]) * 640 for i in range(500)]
    for c in chunks:
        a.append(c)
    a.close()
    assert frames(a.path) == b"".join(chunks)
    assert a.duration_s == pytest.approx(500 * 640 / (SAMPLE_RATE * 2))


def test_two_files_do_not_mix(tmp_path):
    a, b = WavAppender(tmp_path / "a.wav"), WavAppender(tmp_path / "b.wav")
    for i in range(200):
        a.append(b"\x01\x00" * 160)
        b.append(b"\x02\x00" * 160)
    b.close()
    a.close()
    assert frames(a.path) == b"\x01\x00" * 160 * 200
    assert frames(b.path) == b"\x02\x00" * 160 * 200


def test_append_does_not_wait_for_a_slow_disk(tmp_path, monkeypatch):
    a = WavAppender(tmp_path / "a.wav")
    release = threading.Event()
    real = a._write

    def slow(pcm):
        release.wait(5)
        real(pcm)
    monkeypatch.setattr(a, "_write", slow)
    t0 = time.perf_counter()
    for _ in range(50):
        a.append(b"\x03\x00" * 160)
    waited = time.perf_counter() - t0
    assert a.duration_s == pytest.approx(50 * 320 / (SAMPLE_RATE * 2))  # counted at once
    release.set()
    a.close()
    assert waited < 0.5, "append() waited for the disk"
    assert frames(a.path) == b"\x03\x00" * 160 * 50


def test_drain_makes_an_open_file_readable(tmp_path):
    a = WavAppender(tmp_path / "a.wav")
    a.append(b"\x04\x00" * 1600)
    assert a.drain()
    assert frames(a.path) == b"\x04\x00" * 1600   # header already patched
    a.close()


def test_close_twice_and_append_after_close(tmp_path):
    a = WavAppender(tmp_path / "a.wav")
    a.append(b"\x05\x00" * 160)
    a.close()
    a.close()
    a.append(b"\x06\x00" * 160)
    assert frames(a.path) == b"\x05\x00" * 160
    assert a.duration_s == pytest.approx(320 / (SAMPLE_RATE * 2))


def test_a_write_error_is_logged_once_and_does_not_stop_other_files(tmp_path, caplog):
    bad, good = WavAppender(tmp_path / "bad.wav"), WavAppender(tmp_path / "good.wav")
    bad._fh.close()                      # every write to it now raises
    with caplog.at_level("ERROR", logger=storage.log.name):
        for _ in range(3):
            bad.append(b"\x07\x00" * 160)
        good.append(b"\x08\x00" * 160)
        good.close()
        bad.close()
    assert frames(good.path) == b"\x08\x00" * 160
    assert sum("could not write audio" in r.getMessage() for r in caplog.records) == 1


def test_append_from_the_event_loop_thread_never_opens_the_file(tmp_path, monkeypatch):
    """The writes happen on the writer thread, not the caller's."""
    a = WavAppender(tmp_path / "a.wav")
    seen = []
    real = a._write
    monkeypatch.setattr(a, "_write", lambda pcm: (seen.append(threading.current_thread().name), real(pcm)))
    a.append(b"\x09\x00" * 160)
    a.close()
    assert seen == ["wav-writer"]
