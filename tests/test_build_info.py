"""Which build is serving (server/build_info.py), and every place that says so.

The incident this answers: on 2026-09-24 a `tofu apply` from a stale branch
rolled production back from 4798e64 to ca77c2f for four days, and nothing a
tester could see named the build. These tests hold the three published
answers (/health, /api/run/config, llm.provenance and so the encounter record)
and the build commands that feed them: an image built without
`--build-arg BUILD_SHA` works perfectly and reports null everywhere, so a
documented build line that drops the argument is the regression to catch.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from server import app as appmod
from server import build_info, encounter_record, llm, storage

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture()
def no_build(monkeypatch):
    monkeypatch.delenv("BUILD_SHA", raising=False)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    from server import runs

    monkeypatch.setattr(runs, "DATA_DIR", tmp_path)
    monkeypatch.setattr(runs, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(storage, "DATA_DIR", tmp_path)
    monkeypatch.setattr(storage, "SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(storage, "PARTICIPANTS_DIR", tmp_path / "participants")
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "index.db")
    monkeypatch.setattr(appmod, "SESSION_KEY", "", raising=False)
    if appmod.ALLOWED_HOSTS and "testserver" not in appmod.ALLOWED_HOSTS:
        monkeypatch.setattr(appmod, "ALLOWED_HOSTS",
                            list(appmod.ALLOWED_HOSTS) + ["testserver"])
    with TestClient(appmod.app) as c:
        yield c


# --- the reader ----------------------------------------------------------------

@pytest.mark.parametrize("raw,want", [
    ("4798e64", "4798e64"),
    ("  4798E64\n", "4798e64"),
    ("0066b10c2f4a9e0b7d1e3c5a6f8b9d0e1f2a3b4c", "0066b10c2f4a9e0b7d1e3c5a6f8b9d0e1f2a3b4c"),
])
def test_a_commit_id_is_the_build(monkeypatch, raw, want):
    monkeypatch.setenv("BUILD_SHA", raw)
    assert build_info.build_sha() == want


@pytest.mark.parametrize("raw", ["", "   ", "latest", "bootstrap", "abc12",
                                 "4798e64-dirty", "main", "g4798e64"])
def test_anything_that_is_not_a_commit_is_unknown_not_a_build(monkeypatch, raw):
    """An empty ARG (a build without --build-arg) and every non-commit value
    are None: /health is public, and a value nothing can check is not a build."""
    monkeypatch.setenv("BUILD_SHA", raw)
    assert build_info.build_sha() is None


def test_unset_is_unknown(no_build):
    assert build_info.build_sha() is None


def test_a_checkouts_env_file_cannot_name_the_build(no_build, monkeypatch):
    """llm.setting() lets .env win over the environment. The build is a
    property of the image, so it is not read through that accessor."""
    monkeypatch.setitem(llm._FILE, "BUILD_SHA", "deadbee")
    assert build_info.build_sha() is None


# --- the three published answers ------------------------------------------------

def test_health_carries_the_build_at_top_level(client, monkeypatch):
    monkeypatch.setenv("BUILD_SHA", "4798e64")
    body = client.get("/health").json()
    assert body["build"] == "4798e64"
    # Beside active_sessions: the two things read around a deploy.
    assert "active_sessions" in body


def test_health_says_null_rather_than_omitting_the_key(client, no_build):
    """tools/check_prod_build.py distinguishes "this image predates BUILD_SHA"
    (key present, null) from "this is not the /health it expects" (no key)."""
    body = client.get("/health").json()
    assert "build" in body and body["build"] is None


def test_run_config_carries_the_build_for_the_page_tag(client, monkeypatch):
    monkeypatch.setenv("BUILD_SHA", "4798e64")
    assert client.get("/api/run/config").json()["build"] == "4798e64"


def test_run_config_build_is_null_when_unknown(client, no_build):
    body = client.get("/api/run/config").json()
    assert "build" in body and body["build"] is None


def test_provenance_names_the_build(monkeypatch):
    monkeypatch.setenv("BUILD_SHA", "4798e64")
    assert llm.provenance()["build"] == "4798e64"
    monkeypatch.delenv("BUILD_SHA")
    assert llm.provenance()["build"] is None


def test_every_encounter_record_carries_the_build(tmp_path):
    """provenance() lands on realtime_session_started; the record copies an
    explicit list of keys out of that event, so a key the list does not name
    never reaches record.json."""
    events = [
        {"t": 0.0, "type": "session_start", "scenario": "S2A"},
        {"t": 0.1, "type": "realtime_session_started", "model": "gpt-realtime-2.1",
         "pipeline_version": llm.PIPELINE_VERSION, "build": "4798e64"},
    ]
    (tmp_path / "events.jsonl").write_text(
        "".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")
    assert encounter_record.build(tmp_path)["provenance"]["build"] == "4798e64"


def test_the_analysis_database_keeps_the_build():
    from tools import load_analysis_db as L

    assert "build" in L.PIPELINE_KEYS
    assert L.pipeline_provenance({}, {"build": "4798e64"})["build"] == "4798e64"


# --- the image and the commands that build it ------------------------------------

def test_the_dockerfile_bakes_the_build_arg_into_the_environment():
    text = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    lines = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    arg = [i for i, ln in enumerate(lines) if re.match(r"ARG\s+BUILD_SHA\b", ln)]
    env = [i for i, ln in enumerate(lines) if re.match(r"ENV\s+BUILD_SHA=\$\{?BUILD_SHA\}?$", ln)]
    assert arg and env and arg[0] < env[0], (
        "the Dockerfile must declare ARG BUILD_SHA and copy it into ENV "
        "BUILD_SHA, or a --build-arg reaches the build and not the process")
    # After the last COPY: an ARG invalidates every cached layer from its first
    # use, and this one changes on every build.
    last_copy = max(i for i, ln in enumerate(lines) if ln.startswith(("COPY", "RUN pip")))
    assert arg[0] > last_copy, "ARG BUILD_SHA sits above the COPY/pip layers and busts their cache"


def _md_code_lines(rel: str):
    fence = re.compile(r"^```")
    inside = False
    for line in (ROOT / rel).read_text(encoding="utf-8").splitlines():
        if fence.match(line):
            inside = not inside
            continue
        if inside:
            yield line


def _documented_build_lines():
    """Every `docker build` a person or a workflow actually runs."""
    out = []
    for rel in ("docs/DEPLOY-AWS.md", "docs/OPERATIONS.md", "infra/README.md"):
        out += [(rel, ln) for ln in _md_code_lines(rel) if "docker build" in ln]
    for rel in ("Dockerfile", "infra/terraform/terraform.tfvars"):
        for ln in (ROOT / rel).read_text(encoding="utf-8").splitlines():
            if re.match(r"^#\s*(\d+\.\s*)?docker build\s", ln.strip()):
                out.append((rel, ln))
    wf = ROOT / ".github" / "workflows" / "build-platform-image.yml"
    for ln in wf.read_text(encoding="utf-8").splitlines():
        if "docker build" in ln and not ln.strip().startswith("#") and "echo" not in ln:
            out.append((wf.name, ln))
    return out


def test_every_documented_build_passes_the_build_sha_it_tags_with():
    lines = _documented_build_lines()
    # One per shell per runbook, the two comment copies, and the workflow.
    assert len(lines) >= 8, lines
    bad = []
    for where, ln in lines:
        tag = re.search(r"-t\s+\"?\$\{?REPO\}?:\$\{?(\w+)\}?", ln)
        arg = re.search(r"--build-arg\s+\"?BUILD_SHA=\"?\$\{?(\w+)\}?", ln)
        if not (tag and arg and tag.group(1) == arg.group(1)):
            bad.append(f"{where}: {ln.strip()}")
    assert not bad, (
        "these build commands do not pass --build-arg BUILD_SHA with the same "
        "value as the image tag, so the image they make reports build null on "
        "/health, on the page and in every record:\n  " + "\n  ".join(bad))
