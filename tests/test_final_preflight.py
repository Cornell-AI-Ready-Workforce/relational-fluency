"""The boot check has to catch the configuration that kills a wave silently.

Two failures live here, and neither one announces itself.

The first is a model name. preflight() asked the gateway for /v1/models, checked
for a 200 and threw the body away â€” so a mistyped DIRECTOR_MODEL booted green,
every director call was refused (403 key_model_access_denied, measured against
the live gateway; not the 404 the code guessed at), director.py swallowed the
refusal into its cast[0] fallback, and every encounter in the wave completed
with audio, video, transcript and full trigger coverage. Everything except the
steering, which is the independent variable. tools/encounter_health.py was
written to find that after a wave has been collected; this finds it before
anyone joins.

The second is the tail of a wrapped credential. The redactor's shape rules each
find the half of a key that carries a landmark â€” the vendor prefix, the
"api-key" label, the Authorization scheme â€” and a key pasted across a line break
leaves the other half sitting at the start of the next line with no landmark of
its own. preflight() refuses to send a key with whitespace in it, which guards
the boot path and nothing else: the gateway's own error bodies, the voice
socket's header echo and the director's fallback line all reach the same
redactor by other routes.

Nothing here opens a socket. The model list is the real one, recorded off
api.ai.it.cornell.edu, because a fixture with three tidy names would not have
the near-miss neighbours that make the "did you mean" suggestion either useful
or dangerous.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import httpx
import pytest

from server import llm
from server.llm import redact_key

REPO_ROOT = Path(__file__).resolve().parents[1]


# The live gateway's answer to GET /v1/models, recorded 2026-09-10 with the
# study's own key. Trimmed to id + mode, which is all _served_model_ids reads.
#
# `mode` is here rather than dropped for one reason: it is populated for the gpt
# entries and null for every nto.gemini one, INCLUDING the live model the study
# actually runs. Anybody who later reaches for it to check that REALTIME_MODEL
# is a realtime model will flag the correct configuration, and this fixture is
# where they will find out.
LIVE_MODELS = {
    "object": "list",
    "data": [
        {"id": "gpt-realtime-2", "mode": "realtime", "owned_by": "openai"},
        {"id": "gpt-realtime-2.1", "mode": "realtime", "owned_by": "openai"},
        {"id": "gpt-realtime-2.1-mini", "mode": "realtime", "owned_by": "openai"},
        {"id": "gpt-6-astra", "mode": "chat", "owned_by": "openai"},
        {"id": "nto.gemini-live-2.5-flash", "mode": None},
        {"id": "nto.gemini-live-2.5-flash-native-audio", "mode": None},
        {"id": "nto.gemini-3.5-flash", "mode": None},
        {"id": "nto.gemini-3.6-flash", "mode": None},
        {"id": "nto.gemini-3.7-flash", "mode": None},
        {"id": "nto.gemini-3.8-flash", "mode": None},
        {"id": "nto.gemini-3.5-flash-lite", "mode": None},
        {"id": "nto.gemini-3.1-pro-preview", "mode": None},
        {"id": "nto.gemini-3.1-pro-preview-customtools", "mode": None},
        {"id": "nto.gemini-2.5-pro", "mode": None},
        {"id": "nto.gemini-2.5-flash", "mode": None},
        {"id": "nto.gemini-2.5-flash-lite", "mode": None},
        {"id": "nto.gemini-3.1-flash-lite", "mode": None},
    ],
}

# The typo that started this: one transposition away from a model the gateway
# really serves, which is what a mistyped model looks like in the wild and what
# makes a "did you mean" worth printing.
TYPO = "nto.gemini-3.6-flsah"

# Every model setting this deployment has, written out here rather than read
# out of llm._MODEL_ROLES. Reading the table would make this file agree with
# whatever the table happens to say â€” a row deleted from it would delete a test
# case with it, silently, which is the same class of failure as the one being
# fixed. Stated independently, a change to either side has to be a change to
# both.
EXPECTED_ROLES = (
    ("CLAUDE_MODEL", "nto.gemini-3.1-flash-lite", "the actor's text engine", True),
    ("DIRECTOR_MODEL", "nto.gemini-3.1-flash-lite", "the director", True),
    ("STEERING_MODEL", "nto.gemini-3.1-flash-lite", "the steering reviewer", True),
    ("REALTIME_MODEL", "nto.gemini-live-2.5-flash", "the voice socket", True),
    ("TRANSCRIBE_MODEL", "nto.gemini-2.5-pro", "the re-transcriber, offline", False),
)
ROLE_OF = {name: role for name, _, role, _ in EXPECTED_ROLES}
ON_THE_ENCOUNTER_PATH = [name for name, _, _, blocks in EXPECTED_ROLES if blocks]


def models_response(payload=LIVE_MODELS) -> httpx.Response:
    return httpx.Response(200, json=payload)


@pytest.fixture()
def stock_config(monkeypatch):
    """Every model variable unset, so each one resolves to its table default.

    .env wins over the ambient environment in llm._cfg, so emptying _FILE is the
    only way to be sure a test is measuring the code and not whatever the
    machine it runs on has configured.
    """
    monkeypatch.setattr(llm, "_FILE", {})
    for name, *_ in EXPECTED_ROLES:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture()
def gateway(monkeypatch):
    """preflight()'s one network call, answered from the recorded list."""
    monkeypatch.setattr(llm, "_FILE", {})
    monkeypatch.setenv("LITELLM_API_KEY", "sk-cornell-TESTKEY-AAAABBBBCCCCDDDD")
    for name, *_ in EXPECTED_ROLES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(httpx, "get", lambda *a, **kw: models_response())


def set_model(monkeypatch, name: str, value: str) -> None:
    monkeypatch.setenv(name, value)


# --- the wave-killer ---------------------------------------------------------


def test_a_mistyped_director_model_does_not_boot_green(gateway, monkeypatch):
    """The whole item, in one test.

    The gateway is healthy, the key is good, and the study cannot run. Before
    this check the answer was ok=True and a wave's worth of encounters with no
    steering in them.
    """
    set_model(monkeypatch, "DIRECTOR_MODEL", TYPO)

    result = llm.preflight()

    assert result["ok"] is False
    assert "DIRECTOR_MODEL" in result["detail"]
    assert TYPO in result["detail"]
    assert "does not serve" in result["detail"]


@pytest.mark.parametrize("name", ON_THE_ENCOUNTER_PATH)
def test_every_model_on_the_encounter_path_is_checked_by_name(gateway, monkeypatch,
                                                              name):
    """Four variables reach a live encounter and all four boot green when wrong.

    Named individually because "gateway ok: false" is not a thing an operator
    can act on: the fix is to correct one variable, and which one is the entire
    content of the message.
    """
    set_model(monkeypatch, name, TYPO)

    result = llm.preflight()

    assert result["ok"] is False
    assert result["model_problems"] == [
        f"{name} ({ROLE_OF[name]}) is set to {TYPO!r}, which this gateway does "
        f"not serve - did you mean 'nto.gemini-3.6-flash'?"
    ]


def test_the_named_problem_is_what_the_boot_log_prints(gateway, monkeypatch, capsys):
    """The boot line is where this gets read, so the sentence has to reach it.

    app.py prints `status or detail`, and on this path `status` would be 200 â€”
    which is both true and useless, and would leave the named variable visible
    only to somebody who thought to curl /health. preflight() withholds `status`
    when the config rather than the exchange is what is broken, so the sentence
    is what lands in CloudWatch.
    """
    from server import app as appmod

    set_model(monkeypatch, "STEERING_MODEL", TYPO)
    monkeypatch.setattr(appmod, "_preflight", llm.preflight)

    appmod._check_gateway()

    out = capsys.readouterr().out
    assert "STEERING_MODEL" in out, out
    assert TYPO in out, out
    assert "200" not in out, "the HTTP status is not the diagnosis here"


# --- and the same check must not cry wolf ------------------------------------


def test_the_configuration_this_study_runs_is_not_flagged(gateway):
    """The live model list against the live defaults. A check that fires on the
    correct configuration is worse than no check, because the warning it prints
    is the one an operator learns to scroll past."""
    result = llm.preflight()

    assert result["ok"] is True
    assert result["status"] == 200
    assert result["models_checked"] is True
    assert "model_problems" not in result
    assert "model_warnings" not in result
    assert "detail" not in result


def test_the_live_voice_model_is_accepted_despite_reporting_no_mode(gateway):
    """Guards the fixture's warning above with an assertion.

    nto.gemini-live-2.5-flash is what the participant talks to and the gateway
    lists it with mode=None, so any rule that required mode == "realtime" would
    take the study down on a correct configuration.
    """
    entry = next(e for e in LIVE_MODELS["data"]
                 if e["id"] == "nto.gemini-live-2.5-flash")
    assert entry["mode"] is None, "the gateway started reporting mode; re-record"

    assert llm.preflight()["ok"] is True


def test_a_gateway_that_lists_no_models_makes_no_claim(gateway, monkeypatch):
    """Absence of evidence. A proxy, a stub or a future gateway that answers
    some other shape must not be told at boot that every model it was given is
    missing â€” `models_checked` says the question could not be decided, which is
    a different answer from "everything checks out" and is published as one."""
    monkeypatch.setattr(httpx, "get",
                        lambda *a, **kw: httpx.Response(200, text="{}"))

    result = llm.preflight()

    assert result["ok"] is True
    assert result["models_checked"] is False
    assert "detail" not in result


def test_a_wildcard_allowlist_serves_everything(gateway, monkeypatch):
    """A LiteLLM key allowed "*" would otherwise be read as serving one model
    named "*", and every configured model reported missing on a deployment
    where nothing is."""
    monkeypatch.setattr(httpx, "get", lambda *a, **kw: models_response(
        {"data": [{"id": "*"}]}))
    set_model(monkeypatch, "DIRECTOR_MODEL", "anything-at-all")

    result = llm.preflight()

    assert result["ok"] is True
    assert "model_problems" not in result


def test_an_offline_tool_model_does_not_take_the_gateway_down(gateway, monkeypatch):
    """TRANSCRIBE_MODEL is read by the offline re-transcriber. Getting it wrong
    costs a run that can be repeated, not a wave that cannot be re-collected, and
    docs/OPERATIONS.md tells an operator that gateway.ok false means no
    encounter will work. Say it, do not escalate it."""
    set_model(monkeypatch, "TRANSCRIBE_MODEL", "nto.gemini-2.5-prooo")

    result = llm.preflight()

    assert result["ok"] is True, "an offline tool must not stop encounters"
    assert "model_problems" not in result
    assert "TRANSCRIBE_MODEL" in result["model_warnings"][0]


@pytest.mark.parametrize("body", [
    {"data": "not a list"},
    {"data": [{"no_id": 1}, {"id": ""}]},
    {"error": {"message": "upstream is having a day"}},
    [],
    "plain text, not json at all",
])
def test_a_body_the_check_cannot_read_is_never_a_failure(gateway, monkeypatch, body):
    """Any unreadable answer is `models_checked: false` and nothing else.

    This runs at boot on the one path that decides whether a deployment looks
    healthy. A diagnostic that can turn a working gateway red â€” or raise into
    run_preflights and skip the storage check standing behind it â€” is worse than
    the hole it was added to close.
    """
    if isinstance(body, str):
        monkeypatch.setattr(httpx, "get",
                            lambda *a, **kw: httpx.Response(200, text=body))
    else:
        monkeypatch.setattr(httpx, "get", lambda *a, **kw: models_response(body))

    result = llm.preflight()

    assert result["ok"] is True
    assert result["models_checked"] is False


def test_a_gateway_that_refuses_the_key_still_reports_the_status(gateway, monkeypatch):
    """The other half of the `status` contract: when the HTTP exchange is what
    failed, the status IS the diagnosis and is published as before."""
    monkeypatch.setattr(httpx, "get", lambda *a, **kw: httpx.Response(
        403, text='{"error":{"message":"key not allowed to access model"}}'))

    result = llm.preflight()

    assert result["ok"] is False
    assert result["status"] == 403
    assert "models_checked" not in result, "nothing was checked; claim nothing"


# --- the table is the thing that rots ----------------------------------------


_DEFAULT = r'(?:setting|_cfg)\(\s*"{name}"\s*,\s*"([^"]+)"\s*\)'


def _server_sources() -> list[Path]:
    return sorted(p for p in (REPO_ROOT / "server").rglob("*.py")
                  if "__pycache__" not in p.parts)


@pytest.mark.parametrize("name,default", [(n, d) for n, d, _, _ in EXPECTED_ROLES])
def test_the_table_matches_the_default_each_module_really_uses(name, default):
    """_MODEL_ROLES duplicates seven defaults that live in seven other modules.

    Duplication is the price of not importing server.director from here â€” it
    imports this module â€” and this is what keeps the copy honest. A default that
    drifts would leave the boot check validating a model the study does not run,
    which is the same silence the check exists to end, one level up.
    """
    found = set()
    for path in _server_sources():
        found.update(re.findall(_DEFAULT.format(name=name),
                                path.read_text(encoding="utf-8")))
    assert found, f"no module reads {name}; the table names a setting nothing uses"
    assert found == {default}, (
        f"{name} defaults to {sorted(found)} in server/, but llm._MODEL_ROLES "
        f"says {default!r}"
    )


def test_the_table_holds_exactly_the_settings_this_file_was_written_against():
    """EXPECTED_ROLES above is the independent copy; this is where the two meet.

    A row quietly dropped from llm._MODEL_ROLES is a model nothing checks at
    boot, and every parametrized test here would go on passing with one fewer
    case — the failure would be a test that stopped existing.
    """
    assert tuple(tuple(row) for row in llm._MODEL_ROLES) == EXPECTED_ROLES


def test_no_model_setting_is_missing_from_the_table():
    """Allowlists rot the other way too: a model variable added to server/ and
    not to the table is a model the boot check does not look at, and nothing
    else in the codebase would notice."""
    named = {n for n, _, _, _ in EXPECTED_ROLES}
    seen = set()
    for path in _server_sources():
        seen.update(re.findall(r'(?:setting|_cfg)\(\s*"([A-Z_]*MODEL)"\s*,',
                               path.read_text(encoding="utf-8")))
    assert seen <= named, f"not checked at boot: {sorted(seen - named)}"


# --- the redaction gap the same round found ----------------------------------
#
# Each of these is one credential split across a line break, with the landmark
# on the first line. Every rule in _SHAPES redacts as far as the break and
# stops; before _CONTINUATION the second line was published whole.

WRAPPED_HEAD = "sk-aB3dEfGh1jKlMn0pQrSt"
WRAPPED_TAIL = "uVwXyZ2345aBcDeFgH"


@pytest.fixture()
def no_key_here(monkeypatch):
    """A credential this process cannot look up: rotated an hour ago, or a
    colleague's, or the upstream provider's quoted back by the gateway. Still
    live, and the exact-value pass cannot reach it."""
    monkeypatch.setattr(llm, "_FILE", {})
    for name in llm._SECRET_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize("message", [
    # The gateway echoing the key it was sent, which is the vector the audit
    # reproduced.
    '{"error":{"message":"Invalid API key: %s\n%s","type":"auth_error"}}',
    # The same value with no label but its vendor prefix.
    "authentication failed for %s\n%s at the upstream",
    # The header echo, with a clause after it so the tail does not end its line
    # â€” which is what the old rule keyed on, and why it missed this one.
    "invalid Authorization header: Bearer %s\n%s (retrying in 2s)",
])
def test_the_tail_of_a_wrapped_key_survives_no_landmark(no_key_here, message):
    text = message.replace("%s\n%s", f"{WRAPPED_HEAD}\n{WRAPPED_TAIL}")

    out = redact_key(text)

    assert WRAPPED_HEAD not in out
    assert WRAPPED_TAIL not in out, "a live credential, just fewer characters"


def test_a_key_wrapped_over_three_lines_loses_every_line(no_key_here):
    """One re.sub pass does not rescan what it just wrote, so the third line
    needs a second pass to reach it."""
    parts = ["sk-aB3dEfGh1jKlMn0pQrSt", "uVwXyZ2345aBcDeFgH", "qQ7rR8sS9tT0uU1v"]

    out = redact_key("invalid Authorization header: Bearer " + "\n".join(parts))

    for part in parts:
        assert part not in out


def test_the_line_after_a_redacted_credential_is_still_diagnosis(no_key_here):
    """The rule this replaced was careful about exactly one thing and it has to
    stay careful about it: the line under a rejected header is usually the first
    frame of the traceback, which is what says where the connection was being
    opened from."""
    out = redact_key(
        "invalid Authorization header: Bearer Zm9vYmFyYmF6cXV4MDAwMTExMjIy\n"
        "Traceback (most recent call last):\n"
        '  File "server/realtime_voice_session.py", line 1867, in run\n'
    )

    assert "Zm9vYmFyYmF6cXV4MDAwMTExMjIy" not in out
    assert "Traceback (most recent call last):" in out
    assert "realtime_voice_session.py" in out


def test_redaction_across_a_break_is_still_idempotent(no_key_here):
    """`detail` passes two sinks â€” the boot log and /health â€” and a string that
    reads differently at each of them is a string an operator cannot compare."""
    once = redact_key(f"Invalid API key: {WRAPPED_HEAD}\n{WRAPPED_TAIL}")

    assert redact_key(once) == once


def test_the_model_names_this_check_prints_are_not_shredded(no_key_here, gateway,
                                                            monkeypatch):
    """The redactor and this check share a file and pull in opposite directions.

    A message whose only job is to name the value that is wrong is worthless if
    the value is redacted out of it â€” and llm.provenance() already publishes two
    of these same ids verbatim on the same unauthenticated /health, so treating
    them as secret here would be incoherent as well as useless.
    """
    typo = "nto.gemini-3.1-pro-preview-customtools-typo"
    set_model(monkeypatch, "DIRECTOR_MODEL", typo)

    detail = llm.preflight()["detail"]

    assert typo in detail
    assert "<redacted>" not in detail


def test_a_recorded_model_list_is_what_the_gateway_answers():
    """LIVE_MODELS is a recording, and a recording that has drifted from the
    gateway makes every test above a test of history.

    Not a network call â€” the check is that the fixture still contains the ids
    the running configuration depends on, which is the part that would break.
    """
    ids = {e["id"] for e in LIVE_MODELS["data"]}
    assert {d for _, d, _, _ in EXPECTED_ROLES} <= ids, (
        "the recorded list no longer serves this study's own defaults; re-record "
        "it against api.ai.it.cornell.edu before trusting anything above"
    )
    assert json.dumps(LIVE_MODELS)  # plain data, no surprises
