"""The gateway credential must not survive a trip through an error path.

Three sinks make this a disclosure rather than a log-hygiene nit: the encounter's
events.jsonl, which is archived per encounter and shipped whole in the
per-session download.zip an IRB reviewer reads; the boot/CloudWatch warning line
the director writes on every routing fallback; and the returned fallback entry
the voice runner records next to the speaker it played.

Nothing here opens a socket. The two exception shapes that carry a credential
are produced by the real libraries — websockets.exceptions.InvalidHeaderValue
stringifies the whole Authorization header it rejected, and
anthropic.AuthenticationError stringifies the gateway's response body — because
a hand-rolled stand-in would not carry the key in its message, which is the
entire point.
"""
from __future__ import annotations

import asyncio
import json
import logging

import pytest

from server import llm, storage
from server.director import Director
from server.llm import redact_key
from server.scenarios import load_scenario


# Shaped like the real thing (LiteLLM issues "sk-" plus a long random tail) and
# long enough that every needle it produces clears the redactor's length floor.
GATEWAY_KEY = "sk-cornell-LIVEKEY-AAAABBBBCCCCDDDDEEEE"

# Variables that hold a live credential on some deployment of this app. The
# gateway key is the one that matters here, but a rotated key left in a
# neighbouring variable is still a live credential in the same error string.
_SECRET_VARS = (
    "LITELLM_API_KEY", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
    "OPENAI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY",
    "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
)


@pytest.fixture()
def clean_env(monkeypatch):
    """No credential anywhere the redactor can look it up.

    .env wins over the ambient environment in llm._cfg, so emptying _FILE as
    well is the only way to be sure the redaction under test came from the
    shape of the string and not from a value the test machine happened to have.
    """
    monkeypatch.setattr(llm, "_FILE", {})
    for name in _SECRET_VARS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture()
def gateway_key(clean_env, monkeypatch):
    """The key arrives AFTER import, which is how it arrives in the lab."""
    monkeypatch.setenv("LITELLM_API_KEY", GATEWAY_KEY)
    assert llm.gateway_api_key() == GATEWAY_KEY
    return GATEWAY_KEY


_RUN = 12


def _leaks(blob: str, key: str) -> bool:
    """Any run of the key long enough to be worth finding in a log counts.

    Whole-key containment is not a strong enough test on its own: `detail` is
    truncated to 300 characters, so the realistic failure is a surviving PREFIX
    of the credential rather than the whole of it, and a wrapped key arrives as
    two independently useful lines. Hence every 12-character window.
    """
    if key in blob:
        return True
    if any(len(p) >= 6 and p in blob for p in key.split()):
        return True
    return any(key[i:i + _RUN] in blob for i in range(len(key) - _RUN + 1))


def _auth_error(message_body: str):
    """A real anthropic.AuthenticationError whose str() is the gateway body."""
    import httpx2
    import anthropic

    body = {"error": {"message": message_body, "type": "authentication_error"}}
    request = httpx2.Request("POST", "https://api.ai.it.cornell.edu/v1/messages")
    response = httpx2.Response(401, request=request, json=body)
    return anthropic.AuthenticationError(json.dumps(body), response=response, body=body)


class _RaisingClient:
    """Minimal stand-in for AsyncAnthropic that fails the way a gateway fails.

    Deliberately not a MagicMock: Director._bounded() hands a
    non-AsyncAnthropic straight back untouched, while a mock's auto-generated
    with_options would swap out the object injected here and the Director would
    fail for the wrong reason.
    """

    def __init__(self, exc):
        self._exc = exc
        self.messages = self

    async def create(self, **kwargs):
        raise self._exc


# --- the two shapes that actually carry a credential -------------------------


def test_the_transport_echoes_the_whole_authorization_header(gateway_key):
    """websockets puts the rejected header value verbatim into str(exc).

    This is the wrapped-paste case: _cfg().strip() leaves an interior newline
    alone, so the newline reaches the header, websockets refuses it, and its
    complaint quotes the credential back.
    """
    from websockets.exceptions import InvalidHeaderValue

    exc = InvalidHeaderValue("Authorization", f"Bearer {gateway_key}")
    assert gateway_key in str(exc), "websockets stopped quoting the header value"

    assert not _leaks(redact_key(str(exc)), gateway_key)
    assert "<redacted>" in redact_key(str(exc))


def test_a_wrapped_key_in_the_header_echo_loses_both_of_its_lines(clean_env,
                                                                  monkeypatch):
    """A key pasted across two lines produces two needles, and both must go.

    Redacting only the first line leaves the tail of a live credential in the
    record, which is the same disclosure with fewer characters.
    """
    from websockets.exceptions import InvalidHeaderValue

    wrapped = "sk-cornell-LIVEKEY-AAAABBBB\nCCCCDDDDEEEE-TAIL"
    monkeypatch.setenv("LITELLM_API_KEY", wrapped)
    exc = InvalidHeaderValue("Authorization", f"Bearer {wrapped}")

    assert not _leaks(redact_key(str(exc)), wrapped)


def test_a_gateway_that_quotes_the_key_in_its_401_body(gateway_key):
    """anthropic.AuthenticationError.__str__ is the response body."""
    exc = _auth_error(f"Invalid API key: {gateway_key}")
    assert gateway_key in str(exc), "the SDK stopped stringifying the body"

    assert not _leaks(redact_key(str(exc)), gateway_key)


# --- every variable a credential is actually kept in -------------------------


@pytest.mark.parametrize("var,value", [
    ("LITELLM_API_KEY", GATEWAY_KEY),
    ("ANTHROPIC_API_KEY", GATEWAY_KEY),
    ("ANTHROPIC_AUTH_TOKEN", GATEWAY_KEY),
    ("OPENAI_API_KEY", "sk-proj-0Ab1Cd2Ef3Gh4Ij5Kl6Mn7Op8Qr9St"),
    ("GEMINI_API_KEY", "AIzaSyD-9tSrke72PouQMnMX-a7eZSW0jkFMBWY"),
    ("GOOGLE_API_KEY", "AIzaSyD-9tSrke72PouQMnMX-a7eZSW0jkFMBWY"),
    ("AWS_ACCESS_KEY_ID", "AKIAIOSFODNN7EXAMPLE"),
    ("AWS_SECRET_ACCESS_KEY", "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"),
    ("AWS_SESSION_TOKEN", "FQoGZXIvYXdzEBYaDHRlc3RzZXNzaW9udG9rZW4"),
])
def test_a_credential_in_any_of_these_variables_is_redacted(clean_env, monkeypatch,
                                                            var, value):
    """The gateway key is not the only live credential in this process.

    The realtime bridge signs S3 URLs and the offline CLIs read a second
    provider variable, and all of them end up in the same error strings.
    """
    monkeypatch.setenv(var, value)

    out = redact_key(f"upstream refused the request: {value} is not valid")

    assert not _leaks(out, value), f"{var} survived redaction"


def test_the_environment_is_read_at_call_time_not_at_import(clean_env, monkeypatch):
    """The key is pasted into the shell after the server is already up.

    A needle set captured at import would be empty for exactly the process that
    has something to protect, so this asserts the lookup is late. The value is
    deliberately shapeless — no vendor prefix, too short and too hyphenated for
    the entropy rule — so that only the environment lookup can catch it and the
    test cannot pass on a shape rule instead.
    """
    shapeless = "cornell-lab-gateway-value"
    line = f"gateway said {shapeless} is invalid"
    assert redact_key(line) == line, "nothing to look up yet, so nothing changes"

    monkeypatch.setenv("LITELLM_API_KEY", shapeless)

    assert not _leaks(redact_key(line), shapeless)


# --- credentials this process cannot look up ---------------------------------


@pytest.mark.parametrize("secret", [
    "sk-ant-api03-7Qw9xLmZ0pR4tYbN2vCjKeHgFdSaUiOl",
    "sk-cornell-ROTATED-9f8e7d6c5b4a3210",
    "AKIAIOSFODNN7EXAMPLE",
    "ASIAY34FZKBOKMUTVV7A",
    "ghp_16C7e42F292c6912E7710c838347Ae178B4a",
    "AIzaSyD-9tSrke72PouQMnMX-a7eZSW0jkFMBWY",
    "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
])
def test_a_key_shaped_token_goes_even_when_it_is_in_no_variable_here(clean_env,
                                                                     secret):
    """The gateway echoes whatever it was sent, which is not always ours.

    A key rotated an hour ago, a colleague's key pasted into a shared .env, or
    the gateway quoting the upstream provider's own credential are all live
    secrets that no lookup in this process can find. The shape has to be enough.
    """
    out = redact_key(f'{{"error":{{"message":"Invalid API key: {secret}"}}}}')

    assert secret not in out
    assert "<redacted>" in out


def test_an_authorization_header_is_redacted_by_its_scheme_alone(clean_env):
    """When the value is unknown and shapeless, the scheme word is the landmark.

    websockets names the header it rejected, so everything after `Bearer` on
    that line is by construction a credential and nothing else.
    """
    opaque = "Zm9vYmFyYmF6cXV4MDAwMTExMjIyMzMz"
    out = redact_key(f"invalid Authorization header: Bearer {opaque}")

    assert opaque not in out
    assert "Bearer" in out, "the diagnostic must still say what kind of header"


def test_a_wrapped_key_this_process_cannot_look_up_loses_its_tail_too(clean_env):
    """The same wrapped paste, but the value is in no variable here.

    A key rotated an hour ago is still live, and the header echo splits it at
    the newline. Redaction by exact value cannot reach the second line and
    neither can the length rules — the tail is too short and the leading line
    carries the only vendor prefix — so the scheme landmark has to carry both.
    """
    from websockets.exceptions import InvalidHeaderValue

    rotated = "sk-cornell-ROTATED-AAAABBBB\nCCCCDDDDEEEE-TAIL"
    exc = InvalidHeaderValue("Authorization", f"Bearer {rotated}")
    assert rotated in str(exc)

    out = redact_key(str(exc))

    for line in rotated.split():
        assert line not in out, "a live credential, just fewer characters"


def test_the_header_rule_stops_at_the_end_of_the_header(clean_env):
    """A line that follows a rejected header is diagnosis, not credential.

    The continuation rule is what makes a wrapped key vanish whole; a version
    of it that swallowed whatever came next would eat the first frame of the
    traceback under it, which is the line that says where the connection was
    being opened from.
    """
    out = redact_key(
        "invalid Authorization header: Bearer Zm9vYmFyYmF6cXV4MDAwMTExMjIy\n"
        "Traceback (most recent call last):\n"
        '  File "server/realtime_voice_session.py", line 1867, in run\n'
    )

    assert "Zm9vYmFyYmF6cXV4MDAwMTExMjIy" not in out
    assert "Traceback (most recent call last):" in out
    assert "realtime_voice_session.py" in out


# --- the redactor must not cost the diagnostic its meaning -------------------


@pytest.mark.parametrize("line", [
    "Connection refused to api.ai.it.cornell.edu: max retries exceeded",
    "nto.gemini-live-2.5-flash returned no adjust_persona tool_use block",
    "director exceeded its 9.0s budget (model=nto.gemini-3.1-flash-lite)",
    "session s_1772460300_44c9a2 closed with 9 turns",
    r"could not open C:\Users\rf\Downloads\relational-fluency-main\data",
    "unpacked into C--Users-rf-Downloads-relational-fluency-main--1-",
    "Unknown knob: passive_agression (did you mean passive_aggression?)",
])
def test_an_ordinary_diagnostic_survives_unchanged(clean_env, line):
    """The line an operator reads to find out why the gateway is down.

    The redactor substitutes by shape as well as by value now, and a shape rule
    loose enough to eat a hostname, a model id or a build path would destroy
    the diagnostic at exactly the moment someone is reading it — the same loss
    the length floor was added to prevent.
    """
    assert redact_key(line) == line


def test_a_placeholder_key_still_does_not_shred_the_diagnostic(clean_env,
                                                               monkeypatch):
    """The floor that keeps LITELLM_API_KEY=x from substituting on every x."""
    monkeypatch.setenv("LITELLM_API_KEY", "x")
    message = "Connection refused: max retries exceeded; check the dummy stack"

    assert redact_key(message) == message


# --- properties an error-path helper has to have -----------------------------


def test_redaction_is_idempotent(gateway_key):
    """The same string can pass two sinks (a log line and the record).

    A second pass that redacted the marker, or that found new needles inside
    it, would make the two copies disagree about what happened.
    """
    once = redact_key(f"invalid Authorization header: Bearer {gateway_key}")

    assert redact_key(once) == once


@pytest.mark.parametrize("value", [None, 17, b"bytes", ["a", "list"], ""])
def test_the_redactor_never_raises(gateway_key, value):
    """It runs while something has already gone wrong.

    Callers hand it str(exc), and an exception whose __str__ returns a non-str
    (or a caller that forgets the str()) would otherwise turn a recoverable
    gateway failure into a crash inside the handler for that failure.
    """
    out = redact_key(value)

    assert isinstance(out, str)


# --- the director: the record, the log line and the returned entry -----------


def _group_director(exc, events):
    scenario = load_scenario("S3A")
    return Director(scenario, client=_RaisingClient(exc),
                    on_event=lambda type_, **f: events.append((type_, f)))


def test_the_directors_fallback_does_not_put_the_key_in_the_record(gateway_key,
                                                                   caplog):
    """The finding, end to end: a 401 body reaching events.jsonl.

    route() degrades to cast[0] rather than costing the participant the turn,
    and _fallback records why. `detail` is the gateway's own words, so on a
    gateway that echoes the credential every one of the three sinks — the
    warning line, the recorded event, the returned entry — published it.
    """
    events: list = []
    director = _group_director(_auth_error(f"Invalid API key: {gateway_key}"), events)

    with caplog.at_level(logging.WARNING, logger="server.director"):
        routed = asyncio.run(director.route([], "so what do we do about the vendor?"))

    assert routed and routed[0]["fallback"] is True
    assert events and events[0][0] == "director_error"

    assert not _leaks(events[0][1]["detail"], gateway_key), "recorded in events.jsonl"
    assert not _leaks(routed[0]["detail"], gateway_key), "handed back to the runner"
    assert not _leaks(caplog.text, gateway_key), "written to the boot/CloudWatch log"


def test_the_fallback_still_says_what_failed(gateway_key):
    """Redaction must not cost the reader the diagnosis a month later."""
    events: list = []
    director = _group_director(_auth_error(f"Invalid API key: {gateway_key}"), events)

    asyncio.run(director.route([], "your read?"))

    assert events[0][1]["detail"].startswith("AuthenticationError:")
    assert "<redacted>" in events[0][1]["detail"]


def test_a_key_near_the_truncation_boundary_is_still_redacted(gateway_key):
    """detail is cut to 300 characters, so the order of the two operations matters.

    Slicing first can cut the credential in half and leave a prefix that no
    longer matches any needle — a leak that only shows up when the gateway is
    chatty, which is precisely when a real 401 body is long.
    """
    padding = "the upstream provider rejected this request; " * 5
    exc = _auth_error(f"{padding}key {gateway_key} is not valid")
    events: list = []
    director = _group_director(exc, events)

    routed = asyncio.run(director.route([], "who speaks?"))

    assert len(routed[0]["detail"]) <= 300
    assert not _leaks(routed[0]["detail"], gateway_key)
    assert not _leaks(events[0][1]["detail"], gateway_key)


def test_a_timeout_with_no_message_still_names_the_budget(gateway_key):
    """The redactor sits in front of the empty-message path too."""
    events: list = []
    director = _group_director(asyncio.TimeoutError(), events)

    asyncio.run(director.route([], "who speaks?"))

    assert events[0][0] == "director_timeout"
    assert "budget" in events[0][1]["detail"]


# --- the session: auto_steer_error into the archived record ------------------


@pytest.fixture()
def scratch_storage(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DATA_DIR", tmp_path)
    monkeypatch.setattr(storage, "SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "index.db")
    monkeypatch.setattr(storage, "PARTICIPANTS_DIR", tmp_path / "participants")
    storage.init_storage()
    return tmp_path


def _events(session) -> list:
    return [
        json.loads(line)
        for line in (session.store.dir / "events.jsonl").read_text(
            encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_a_failed_steering_review_does_not_write_the_key_into_the_record(
        gateway_key, scratch_storage):
    """auto_steer_error carries str(exc) straight from the gateway.

    events.jsonl is the encounter's own archived trail and ships whole in the
    per-session download.zip, so this one is permanent in a way a log line is
    not.
    """
    from server.session import Session

    session = Session("S1A")
    session.auto_steering = True
    session.steering.client = _RaisingClient(
        _auth_error(f"Invalid API key: {gateway_key}")
    )
    session.shared_history.append(
        {"speaker": "user", "text": "That is not what I asked for.", "t": 0.0}
    )

    asyncio.run(session.auto_steer())

    errors = [e for e in _events(session) if e.get("type") == "auto_steer_error"]
    assert errors, "a failed review must still leave a trace"
    assert not _leaks(errors[0]["message"], gateway_key)
    assert not _leaks(json.dumps(_events(session)), gateway_key)


def test_a_steering_review_that_ignored_the_tool_call_still_reads_clearly(
        gateway_key, scratch_storage):
    """The common failure is not a 401, and its message must survive intact."""
    from server.session import Session

    session = Session("S1A")
    session.auto_steering = True
    session.steering.client = _RaisingClient(
        RuntimeError("steering review returned no adjust_persona tool_use block "
                     "(model=nto.gemini-3.1-flash-lite, stop_reason=max_tokens)")
    )
    session.shared_history.append(
        {"speaker": "user", "text": "Fine, do it your way.", "t": 0.0}
    )

    asyncio.run(session.auto_steer())

    errors = [e for e in _events(session) if e.get("type") == "auto_steer_error"]
    assert "adjust_persona" in errors[0]["message"]
    assert "nto.gemini-3.1-flash-lite" in errors[0]["message"]
