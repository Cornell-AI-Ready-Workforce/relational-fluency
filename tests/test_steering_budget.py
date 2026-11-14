"""The steering budget (pipeline 2026-11-13a, #235).

Steering is the manipulated variable. With a 10 s budget on Argon about one
review in three timed out on both attempts, so the default is now 20 s, split
into two 10 s attempts. These pin the default and the split, so a change to
either is a reviewed diff with a PIPELINE_VERSION bump beside it.
"""
from __future__ import annotations

import importlib

from server import llm, steering


def test_the_default_budget_is_twenty_seconds_in_two_attempts(monkeypatch):
    monkeypatch.setattr(llm, "_FILE", {})
    monkeypatch.delenv("STEERING_TIMEOUT_S", raising=False)
    try:
        fresh = importlib.reload(steering)
        assert fresh.STEERING_TIMEOUT_S == 20.0
        assert fresh.STEERING_ATTEMPT_TIMEOUT_S == 10.0
        assert fresh.STEERING_MAX_RETRIES == 1
    finally:
        monkeypatch.undo()
        importlib.reload(steering)


def test_the_old_budget_can_be_restored_and_a_bad_value_falls_back(monkeypatch):
    monkeypatch.setattr(llm, "_FILE", {})
    monkeypatch.setenv("STEERING_TIMEOUT_S", "10")
    assert steering._timeout_setting() == 10.0
    for bad in ("0", "-3", "soon"):
        monkeypatch.setenv("STEERING_TIMEOUT_S", bad)
        assert steering._timeout_setting() == 20.0


def test_the_pipeline_version_names_the_change():
    assert llm.PIPELINE_VERSION >= "2026-11-13a"
