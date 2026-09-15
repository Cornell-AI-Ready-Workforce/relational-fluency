"""The deployed task definition must configure the app the app actually reads.

Nothing else in the repo joins infra/terraform/ecs.tf to the names server/ looks
up, so a variable that is set under one name and read under another survives
review and shows up only in the deployed environment. That is how CLAUDE_MODEL
went missing: the task set DIRECTOR_MODEL (which server/director.py reads) but
never CLAUDE_MODEL, so llm.provenance() fell back to its hardcoded default and
stamped a text model that never ran onto every encounter record — the artefact
raters and analysts read. Locally the two agree (.env.example sets CLAUDE_MODEL
and no DIRECTOR_MODEL), so the divergence existed only in the deployment, where
no test had ever looked.

These tests read the HCL as text on purpose. Terraform is not installed in this
environment and the point is the literal names in the file, not a plan.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

ECS_TF = ROOT / "infra" / "terraform" / "ecs.tf"

_ENTRY = re.compile(r'\{\s*name\s*=\s*"([A-Z0-9_]+)"\s*,\s*(?:value|valueFrom)\s*=\s*([^\n}]+?)\s*\}')


def _block(body: str, key: str) -> str:
    """The text of the `key = [ ... ]` list in the container definition."""
    start = body.index(f"{key} = [")
    depth, i = 0, body.index("[", start)
    for j in range(i, len(body)):
        if body[j] == "[":
            depth += 1
        elif body[j] == "]":
            depth -= 1
            if depth == 0:
                return body[i:j + 1]
    raise AssertionError(f"unterminated {key} list in {ECS_TF}")


@pytest.fixture(scope="module")
def task_env():
    body = ECS_TF.read_text(encoding="utf-8")
    return dict(_ENTRY.findall(_block(body, "environment")))


@pytest.fixture(scope="module")
def task_secrets():
    body = ECS_TF.read_text(encoding="utf-8")
    return dict(_ENTRY.findall(_block(body, "secrets")))


def _read_by_server(name: str) -> bool:
    needle = f'"{name}"'
    for p in (ROOT / "server").rglob("*.py"):
        if needle in p.read_text(encoding="utf-8"):
            return True
    return False


def test_provenance_text_model_is_the_model_that_runs(task_env):
    """The regression: CLAUDE_MODEL unset meant record.json named a model that
    never served the encounter. It must be set, and from its own variable.

    An earlier revision of this test required CLAUDE_MODEL == DIRECTOR_MODEL, on
    the premise that a deployment has one text model. That was wrong in a way
    worth recording, because collapsing them fixed provenance and broke two
    other things: CLAUDE_MODEL is read by the text-mode engine and as the
    default in the researcher's pre-start model picker, so pointing it at the
    director's model silently repointed both. Provenance does not need them
    equal — every stage_direction event carries director_model from the live
    Director instance, so the record can state each independently and
    truthfully.
    """
    assert "CLAUDE_MODEL" in task_env, (
        "the task never sets CLAUDE_MODEL, so llm.provenance() reports its "
        "hardcoded default and every encounter record names a text model that "
        "did not run"
    )
    assert task_env["CLAUDE_MODEL"] == "var.text_model", (
        "CLAUDE_MODEL must come from its own text_model variable: it is the "
        "text-mode engine and the researcher's default model, not a label"
    )
    assert task_env["DIRECTOR_MODEL"] == "var.director_model", (
        "DIRECTOR_MODEL must stay independently settable — the director is "
        "deliberately a cheaper model than the text engine"
    )


@pytest.mark.parametrize("name", [
    # Model + gateway wiring: without these the task runs on code defaults that
    # nobody chose, and the record says so afterwards.
    "REALTIME_MODEL", "DIRECTOR_MODEL", "CLAUDE_MODEL", "LLM_BASE_URL",
    # Hostnames feed the Host-header allowlist and the CORS origins.
    "APP_HOST", "API_HOST",
    # Webcam capture: server/video.py signs uploads against these two.
    "S3_BUCKET", "AWS_REGION",
    # Listening socket, which the ALB target group health check depends on.
    "HOST", "PORT",
    # Where the study is written. server/storage.py:57 falls back to
    # <repo>/data, which on Fargate is the container filesystem — destroyed on
    # the next deploy, with the EFS volume mounted and empty beside it. Live
    # revisions 35 through 38 all shipped without this and nothing said so:
    # /health stayed 200 while every run file, participant record, transcript,
    # WAV and the SQLite index went to disposable disk. See
    # tests/test_terraform_persistence.py, which pins the volume and the mount
    # point this name has to agree with.
    "DATA_DIR",
    # Where a participant goes after the fourth encounter. Unset, they are not
    # returned to Qualtrics and the survey half of their response never
    # completes — a silently partial record rather than an error.
    "SURVEY_RETURN_URL",
    # Which approved consent wording the survey is showing. Unset,
    # server/storage.py records no study consent at all: /api/consent 404s,
    # voice sockets close 4403, runs keep being minted, and the wave collects
    # zero encounters uniformly from the first participant onward.
    "UPSTREAM_CONSENT_VERSION",
])
def test_required_env_is_set(task_env, name):
    """The forward direction, and the one that actually cost records.

    The other tests in this file pin the reverse: a name set in the task
    definition that no module reads. That direction is cheap to catch and cheap
    to survive — dead configuration. The expensive direction is a name the code
    reads and the deployment never sets, because its failure mode is a healthy
    task quietly doing the wrong thing. DATA_DIR, SURVEY_RETURN_URL and
    UPSTREAM_CONSENT_VERSION were all missing from the live task definition at
    the same time, for four revisions, while every surface an operator watches
    stayed green.
    """
    assert name in task_env, f"{name} is not set in the ECS task definition"


@pytest.mark.parametrize("name", ["ANTHROPIC_API_KEY", "SESSION_KEY"])
def test_credentials_arrive_as_secrets_not_plaintext(task_secrets, task_env, name):
    assert name in task_secrets, f"{name} must be injected from Secrets Manager"
    assert name not in task_env, f"{name} must not be a plaintext environment entry"


def test_no_environment_variable_the_server_never_reads(task_env, task_secrets):
    """A name the code does not look up is configuration that silently does
    nothing — the ACTOR_MODEL trap the comment above REALTIME_MODEL records."""
    unread = sorted(n for n in {**task_env, **task_secrets} if not _read_by_server(n))
    assert not unread, f"set in the task definition but never read by server/: {unread}"
