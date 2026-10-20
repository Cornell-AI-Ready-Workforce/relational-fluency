"""conversation_audio.wav: both sides of the encounter on one clock.

The per-speaker WAVs are gapless — the mic drops samples while muted and an
agent channel accumulates only while it speaks, faster than real time — so
none of them can be played as the conversation that happened. SessionStore now
keeps a Timeline of every chunk it appends and the close renders the mix.

Run from the repo root:

    python -m pytest tests/test_conversation_audio.py
"""

from __future__ import annotations

import importlib
import json
import struct
import time
from array import array

import pytest

from server import conversation_audio as ca
from server import storage

SR = ca.SAMPLE_RATE
SID = "s_1772460300_44c9a2"


def tone(value: int, seconds: float) -> bytes:
    return array("h", [value] * int(seconds * SR)).tobytes()


def wav_samples(path) -> array:
    data = path.read_bytes()
    assert data[:4] == b"RIFF" and data[8:12] == b"WAVE"
    (n,) = struct.unpack("<I", data[40:44])
    out = array("h")
    out.frombytes(data[44:44 + n])
    return out


def write_wav(path, pcm: bytes) -> None:
    path.write_bytes(ca._wav_header(len(pcm)) + pcm)


# --- Timeline -----------------------------------------------------------------

def test_a_continuous_mic_stream_stays_continuous_despite_jitter():
    tl = ca.Timeline(started_at=100.0)
    chunk = 320 * 2                       # 20 ms
    for i, jitter in enumerate([0.0, 0.03, -0.01, 0.05, 0.0]):
        tl.mic("user_audio.wav", chunk, now=100.0 + 0.02 * (i + 1) + jitter)
    assert tl.runs["user_audio.wav"] == [[0, 0, 5 * chunk]]


def test_a_mute_starts_a_new_mic_run_at_the_time_it_was_captured():
    tl = ca.Timeline(started_at=0.0)
    tl.mic("user_audio.wav", 2 * SR, now=1.0)          # 1 s, captured 0-1
    tl.mic("user_audio.wav", 2 * SR, now=6.0)          # muted 1-5, captured 5-6
    assert tl.runs["user_audio.wav"] == [[0, 0, 2 * SR], [5 * SR, 2 * SR, 2 * SR]]


def test_agent_audio_delivered_in_a_burst_is_laid_out_at_playback_speed():
    tl = ca.Timeline(started_at=0.0)
    for _ in range(3):                                  # 3 s delivered at t=2
        tl.agent("assistant_audio.wav", 2 * SR, now=2.0)
    assert tl.runs["assistant_audio.wav"] == [[2 * SR, 0, 6 * SR]]
    tl.agent("assistant_audio.wav", 2 * SR, now=10.0)  # the next reply, later
    assert tl.runs["assistant_audio.wav"][-1] == [10 * SR, 6 * SR, 2 * SR]


def test_an_interrupted_reply_is_cut_where_the_page_stopped_it():
    tl = ca.Timeline(started_at=0.0)
    tl.agent("assistant_audio.wav", 10 * SR, now=1.0)  # 5 s from t=1
    tl.cut("assistant_audio.wav", 3.0)
    assert tl.runs["assistant_audio.wav"] == [[SR, 0, 4 * SR]]
    tl.agent("assistant_audio.wav", 2 * SR, now=3.5)   # next reply is not queued behind it
    assert tl.runs["assistant_audio.wav"][-1][0] == int(3.5 * SR)


# --- render -------------------------------------------------------------------

def test_render_places_and_sums_both_sides(tmp_path):
    write_wav(tmp_path / "user_audio.wav", tone(1000, 2.0))
    write_wav(tmp_path / "assistant_audio.wav", tone(500, 1.0) + tone(32000, 1.0))
    tl = ca.Timeline(started_at=0.0)
    tl.mic("user_audio.wav", 4 * SR, now=2.0)                 # 0-2 s
    tl.agent("assistant_audio.wav", 2 * SR, now=1.5)          # 1.5-2.5 s
    tl.agent("assistant_audio.wav", 2 * SR, now=4.0)          # 4-5 s, loud
    ca.write_timeline(tmp_path, tl)

    out = ca.render(tmp_path)
    mix = wav_samples(out)
    assert len(mix) == 5 * SR
    assert mix[int(0.5 * SR)] == 1000                          # participant alone
    assert mix[int(1.75 * SR)] == 1500                         # overlap, summed
    assert mix[int(2.25 * SR)] == 500                          # agent alone
    assert mix[int(3.0 * SR)] == 0                             # silence
    assert mix[int(4.5 * SR)] == 32000


def test_overlap_is_clipped_not_wrapped(tmp_path):
    write_wav(tmp_path / "user_audio.wav", tone(30000, 1.0))
    write_wav(tmp_path / "assistant_audio.wav", tone(30000, 1.0))
    tl = ca.Timeline(started_at=0.0)
    tl.mic("user_audio.wav", 2 * SR, now=1.0)
    tl.agent("assistant_audio.wav", 2 * SR, now=0.0)
    ca.write_timeline(tmp_path, tl)
    assert set(wav_samples(ca.render(tmp_path))) == {32767}


def test_render_without_a_timeline_is_nothing(tmp_path):
    assert ca.render(tmp_path) is None
    assert not (tmp_path / ca.CONVERSATION_NAME).exists()


def test_render_ignores_a_path_in_the_timeline(tmp_path):
    (tmp_path / ca.TIMELINE_NAME).write_text(json.dumps(
        {"runs": {"../escape.wav": [[0, 0, 2]]}}), encoding="utf-8")
    assert ca.render(tmp_path) is None


def test_the_mix_is_archived_and_not_mistaken_for_a_speaker():
    from server import archive
    assert any(ca.CONVERSATION_NAME.endswith(g.lstrip("*")) for g in archive.ARCHIVE_GLOBS)
    assert not ca.CONVERSATION_NAME.startswith(("user_audio", "assistant_audio"))


# --- through SessionStore -----------------------------------------------------

@pytest.fixture()
def store_env(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("ARCHIVE_SESSIONS_TO_S3", "0")
    importlib.reload(storage)
    storage.init_storage()
    yield storage
    monkeypatch.undo()
    importlib.reload(storage)


def test_a_closed_session_leaves_the_whole_conversation(store_env, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    store = store_env.SessionStore(SID, scenario="t", model="m",
                                   participant_id=None, capture_audio=True)
    threads = []
    monkeypatch.setattr(ca, "finish_later",
                        lambda d, _real=ca.finish_later: threads.append(_real(d)))

    for _ in range(50):                                   # participant, 0-1 s
        clock[0] += 0.02
        store.append_user_audio(tone(1000, 0.02))
    store.append_assistant_audio(tone(2000, 2.0))         # reply at 1 s, burst
    store.event("play_end", turn=1, agent_id=None, at=2.0, interrupted=True)
    clock[0] += 3.0
    store.close()
    for t in threads:
        t.join(10)

    sdir = store.dir
    mix = wav_samples(sdir / ca.CONVERSATION_NAME)
    assert len(mix) == 2 * SR                              # reply cut at 2 s
    assert mix[int(0.5 * SR)] == 1000
    assert mix[int(1.5 * SR)] == 2000
    record = json.loads((sdir / "record.json").read_text(encoding="utf-8"))
    assert record["audio"]["conversation"] == ca.CONVERSATION_NAME
    # The speaker channels are untouched.
    assert len(wav_samples(sdir / "assistant_audio.wav")) == 2 * SR
