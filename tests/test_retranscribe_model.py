"""Offline re-transcription: model default, empty-reply guard, cast-name hint.

No network: the gateway call is replaced by a fake httpx.post.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server import retranscribe as rt  # noqa: E402


def test_the_default_is_the_2026_09_23_choice():
    # The source default, read from the file so a TRANSCRIBE_MODEL in .env
    # cannot make this pass or fail. server/llm.py's preflight table states the
    # same default (pinned in tests/test_final_preflight.py).
    src = (ROOT / "server" / "retranscribe.py").read_text(encoding="utf-8")
    assert 'setting("TRANSCRIBE_MODEL", "nto.gemini-3.8-flash")' in src
    tool = (ROOT / "tools" / "recover_from_video.py").read_text(encoding="utf-8")
    assert 'setting("TRANSCRIBE_MODEL", "nto.gemini-3.8-flash")' in tool


@pytest.mark.parametrize("body", [
    {"choices": []},
    {"choices": [{"message": {"content": ""}}]},
    {"choices": [{"message": {"content": None}}]},
    {},
    [],
])
def test_an_empty_completion_is_not_text(body):
    assert rt._completion_text(body) is None


def test_a_completion_is_stripped():
    assert rt._completion_text({"choices": [{"message": {"content": "  Hello.\n"}}]}) == "Hello."


def _post_returning(*bodies):
    calls = []

    def fake_post(url, **kw):
        calls.append(kw["json"])
        body = bodies[min(len(calls), len(bodies)) - 1]
        return httpx.Response(200, json=body, request=httpx.Request("POST", url))
    return fake_post, calls


def _wav(tmp_path: Path) -> Path:
    import wave
    p = tmp_path / "user_audio.wav"
    with wave.open(str(p), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
        w.writeframes(b"\x01\x00" * 32000)
    return p


def test_an_empty_first_reply_is_retried(monkeypatch, tmp_path):
    fake, calls = _post_returning({"choices": []}, {"choices": [{"message": {"content": "Hi, Dan."}}]})
    monkeypatch.setattr(rt.httpx, "post", fake)
    monkeypatch.setattr(rt, "gateway_api_key", lambda: "test")
    assert rt.transcribe_file(_wav(tmp_path)) == "Hi, Dan."
    assert len(calls) == 2


def test_two_empty_replies_fail_loudly(monkeypatch, tmp_path):
    fake, calls = _post_returning({"choices": []})
    monkeypatch.setattr(rt.httpx, "post", fake)
    monkeypatch.setattr(rt, "gateway_api_key", lambda: "test")
    with pytest.raises(RuntimeError, match="empty completion"):
        rt.transcribe_file(_wav(tmp_path))
    assert len(calls) == 2


def test_cast_names_come_from_the_record_then_the_manifest(tmp_path):
    (tmp_path / "manifest.json").write_text(json.dumps({"agent_ids": ["dan", "priya"]}), encoding="utf-8")
    assert rt._cast_names(tmp_path) == ["Dan", "Priya"]
    (tmp_path / "record.json").write_text(json.dumps({"cast": [{"id": "dan", "name": "Dan"},
                                                                {"id": "chris", "name": "Chris"}]}),
                                        encoding="utf-8")
    assert rt._cast_names(tmp_path) == ["Dan", "Chris"]


def test_the_names_reach_the_prompt_and_only_as_vocabulary(monkeypatch, tmp_path):
    fake, calls = _post_returning({"choices": [{"message": {"content": "ok"}}]})
    monkeypatch.setattr(rt.httpx, "post", fake)
    monkeypatch.setattr(rt, "gateway_api_key", lambda: "test")
    rt.transcribe_file(_wav(tmp_path), names=["Dan", "Priya", "Chris"])
    prompt = calls[0]["messages"][0]["content"][0]["text"]
    assert "Dan, Priya, Chris" in prompt and "not audible" in prompt
    assert rt._prompt([]) == rt.PROMPT
