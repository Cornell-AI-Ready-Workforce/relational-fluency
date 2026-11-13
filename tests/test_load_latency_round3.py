"""Reply-path-only rate decisions and accurate stop attribution."""
import asyncio
import json

import pytest

from tools.load import measure, run, smoke


OK = {"upstream": "gateway", "purpose": "actor", "route": "chat_completions_stream", "status": 200, "outcome": "ok"}
ERROR = {"upstream": "gateway", "purpose": "steering", "route": "messages", "status": None,
         "outcome": "timeout", "detail": "APITimeoutError"}


@pytest.mark.parametrize("purpose", ["steering", "director", "unknown", None])
def test_thirty_background_timeouts_in_forty_attempts_do_not_stop(purpose):
    guard = measure.LiveGuard()
    rows = [{**ERROR, "purpose": purpose}] * 30 + [OK] * 10
    for row in rows:
        assert guard.observe(row, 1) is None
    assert (guard.gateway_attempts, guard.gateway_errors) == (40, 30)
    assert (guard.attempts, guard.errors) == (10, 0)
    assert len(measure.gateway_errors(rows)) == 30
    # Passing the counted sample floor later still cannot include those errors.
    for _ in range(20):
        assert guard.observe(OK, 1) is None
    assert (guard.attempts, guard.errors) == (30, 0)


@pytest.mark.parametrize("purpose,route", [("stt", "chat_completions"), ("actor", "chat_completions_stream"),
                                          ("preflight", "models"), ("preflight", "messages")])
def test_two_reply_path_errors_in_twenty_five_counted_attempts_stop(purpose, route):
    guard = measure.LiveGuard()
    for i in range(25):
        assert guard.observe(ERROR, 1) is None  # Never counts in numerator or denominator.
        row = {**OK, "purpose": purpose, "route": route}
        if i in {3, 24}:
            row.update(outcome="timeout", status=None, detail="CountedTimeout")
        stop = guard.observe(row, 1)
        assert bool(stop) == (i == 24)
    assert stop["reason"] == "gateway attempt error rate > 5%"
    assert (stop["attempts"], stop["errors"]) == (25, 2)
    assert (stop["gateway_attempts"], stop["gateway_errors"]) == (50, 27)
    assert stop["purpose"] == purpose and stop["detail"] == "CountedTimeout"
    assert stop["error_counts_by_purpose"] == {purpose: 2}


@pytest.mark.parametrize("purpose,status", [("steering", 429), ("steering", 401), ("director", 403)])
def test_immediate_safety_stop_applies_to_uncounted_purposes(purpose, status):
    guard = measure.LiveGuard()
    stop = guard.observe({**ERROR, "purpose": purpose, "status": status, "outcome": "http_error"}, 1)
    assert stop["reason"] == f"first gateway {status}"
    assert stop["gateway_attempts"] == stop["gateway_errors"] == 1
    assert stop["attempts"] == stop["errors"] == 0
    assert stop["purpose"] == purpose and stop["status"] == status


def test_successful_threshold_trigger_names_last_counted_error_not_background_error():
    guard = measure.LiveGuard()
    for i in range(1, 20):
        row = {**OK, "t": i}
        if i == 3:
            row.update(outcome="http_error", status=502, detail="ActorError")
        elif i == 18:
            row.update(purpose="stt", route="chat_completions", outcome="timeout", status=None, detail="LastCountedError")
        assert guard.observe(row, 1) is None
    assert guard.observe({**ERROR, "t": 19.5}, 1) is None
    trigger = {**OK, "t": 20, "purpose": "preflight", "route": "models"}
    stop = guard.observe(trigger, 1)
    assert (stop["attempts"], stop["errors"]) == (20, 2)
    assert (stop["purpose"], stop["route"], stop["outcome"], stop["detail"]) == (
        "stt", "chat_completions", "timeout", "LastCountedError")
    assert stop["last_counted_error"]["t"] == 18 and stop["t"] == 20
    assert stop["trigger"]["purpose"] == "preflight" and stop["trigger"]["outcome"] == "ok"
    assert stop["error_counts_by_purpose"] == {"actor": 1, "stt": 1}
    assert stop["counted_purposes"] == ["preflight", "stt", "actor"]
    serialized = json.loads(json.dumps(stop))
    assert serialized["detail"] == "LastCountedError"


def test_smoke_configuration_lists_counted_purposes_and_retains_background_error(tmp_path):
    async def diagnostic(args, directory):
        return {"ran": True, "verdict": "PASS", "gateway_errors": measure.gateway_errors([ERROR])}
    args = run.parse_args(["--smoke", "--report-dir", str(tmp_path)])
    report, directory = asyncio.run(smoke.run_with_smoke(args, smoke_runner=diagnostic))
    assert report["verdict"] == "PASS"
    assert report["configuration"]["live_guard_counted_purposes"] == ["preflight", "stt", "actor"]
    assert "preflight, stt, actor" in report["configuration"]["live_guard_rule"]
    assert report["gateway_errors"][0]["purpose"] == "steering"
    assert "APITimeoutError" in (directory / "report.md").read_text(encoding="utf-8")
