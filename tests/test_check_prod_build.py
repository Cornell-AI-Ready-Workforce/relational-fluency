"""The daily drift check: production /health "build" against main's pin.

tools/check_prod_build.py is the whole guard against a repeat of 2026-09-24
(four days on a rolled-back image, nothing visible), so each answer it can
give is pinned here with a fake /health, and the workflow that runs it is held
to the properties that make it trustworthy: scheduled, manual, main, no
secrets.
"""
from __future__ import annotations

import io
import json
import re
import urllib.error
from pathlib import Path

import pytest
import yaml

from tools import check_prod_build as C
from tools import pinned_image as P

ROOT = Path(__file__).resolve().parent.parent
IMG = "540586745717.dkr.ecr.us-east-1.amazonaws.com/relational-fluency/platform"
WORKFLOW = ROOT / ".github" / "workflows" / "prod-build-drift.yml"


def _tfvars(tmp_path, tag="4798e64", extra=""):
    p = tmp_path / "terraform.tfvars"
    p.write_text(f'# header\ncontainer_image = "{IMG}:{tag}"\n{extra}',
                 encoding="utf-8")
    return p


def _run(tmp_path, health, tag="4798e64", capsys=None):
    tf = _tfvars(tmp_path, tag)
    code = C.main(["--tfvars", str(tf), "--url", "https://example.invalid"],
                  fetch=lambda url: health)
    return code


# --- the pin parser shared with tools/deploy.sh and tools/sim ---------------------

def test_the_committed_pin_parses():
    pin = P.read()
    assert pin.repository == "relational-fluency/platform"
    assert pin.region == "us-east-1" and re.fullmatch(r"\d{12}", pin.registry)
    assert re.fullmatch(r"[0-9a-f]{7,40}", pin.tag)


@pytest.mark.parametrize("body,why", [
    ('actor_model = "x"\n', "does not set"),
    (f'container_image = "{IMG}:latest"\n', "not a git commit"),
    (f'container_image = "{IMG}:bootstrap"\n', "not a git commit"),
    ('container_image = "nginx:4798e64"\n', "not an ECR image"),
    (f'container_image = "{IMG}:4798e64"\ncontainer_image = "{IMG}:ca77c2f"\n', "2 times"),
])
def test_a_pin_nothing_can_check_is_refused(body, why):
    with pytest.raises(P.PinError, match=why):
        P.parse(body)


def test_a_commented_out_pin_is_not_the_pin():
    pin = P.parse(f'# container_image = "{IMG}:ca77c2f"\ncontainer_image = "{IMG}:4798e64"\n')
    assert pin.tag == "4798e64"


@pytest.mark.parametrize("a,b,same", [
    ("4798e64", "4798e64", True),
    ("4798e64", "4798e64c1", True),          # --short grew by a character
    ("4798E64", "4798e64", True),
    ("4798e64", "ca77c2f", False),
    ("4798e64", None, False),
    (None, None, False),
    ("abc", "abc", False),                   # too short to be a commit id
])
def test_same_commit(a, b, same):
    assert P.same_commit(a, b) is same


# --- the four answers -------------------------------------------------------------

def test_same_build_passes(tmp_path, capsys):
    assert _run(tmp_path, {"status": "ok", "build": "4798e64"}) == C.OK
    assert "4798e64" in capsys.readouterr().out


def test_a_longer_abbreviation_of_the_same_commit_passes(tmp_path):
    assert _run(tmp_path, {"build": "4798e64a"}) == C.OK


def test_a_different_build_fails_loudly_naming_both(tmp_path, capsys):
    """The 2026-09-24 state: production on ca77c2f while main pinned 4798e64."""
    assert _run(tmp_path, {"build": "ca77c2f"}) == C.DRIFT
    err = capsys.readouterr().err
    assert "PRODUCTION BUILD DRIFT" in err
    assert "ca77c2f" in err and "4798e64" in err


@pytest.mark.parametrize("health", [{"status": "ok"}, {"status": "ok", "build": None}])
def test_no_build_fails_and_says_the_live_image_predates_build_sha(tmp_path, capsys, health):
    """4798e64 has no "build" key at all; an image built without the argument
    says null. Both fail, and the message must say this is expected until the
    next deploy, or a daily red run teaches people to ignore it."""
    assert _run(tmp_path, health) == C.DRIFT
    err = capsys.readouterr().err
    assert C.LAST_IMAGE_WITHOUT_BUILD_SHA in err
    assert "predates" in err and "next deploy" in err


def test_unreachable_production_is_its_own_exit_code(tmp_path, capsys):
    def down(url):
        raise RuntimeError("could not read https://example.invalid/health: timed out")

    tf = _tfvars(tmp_path)
    assert C.main(["--tfvars", str(tf)], fetch=down) == C.UNCHECKED
    assert "unreachable" in capsys.readouterr().err


def test_an_unreadable_pin_is_its_own_exit_code(tmp_path):
    tf = tmp_path / "terraform.tfvars"
    tf.write_text('container_image = "x:latest"\n', encoding="utf-8")
    assert C.main(["--tfvars", str(tf)], fetch=lambda u: {"build": "4798e64"}) == C.UNCHECKED


def test_in_actions_the_reason_is_an_annotation_and_a_summary(tmp_path, capsys, monkeypatch):
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    assert _run(tmp_path, {"build": "ca77c2f"}) == C.DRIFT
    out = capsys.readouterr().out
    assert out.startswith("::error title=") and "%0A" in out
    assert "ca77c2f" in summary.read_text(encoding="utf-8")


def test_fetch_retries_then_gives_up_with_the_url(monkeypatch):
    calls = []

    def opener(req, timeout):
        calls.append(req.full_url)
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(C.time, "sleep", lambda s: None)
    with pytest.raises(RuntimeError, match="example.invalid/health"):
        C.fetch_health("https://example.invalid/", attempts=3, opener=opener)
    assert calls == ["https://example.invalid/health"] * 3


def test_fetch_reads_the_json_body():
    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    body = Resp(json.dumps({"build": "4798e64"}).encode())
    assert C.fetch_health("https://x", opener=lambda req, timeout: body) == {"build": "4798e64"}


# --- the workflow -------------------------------------------------------------------

def _workflow():
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def test_the_workflow_runs_daily_and_on_demand():
    wf = _workflow()
    on = wf.get("on", wf.get(True))  # YAML 1.1 reads a bare `on` as True
    assert "workflow_dispatch" in on
    crons = [s["cron"] for s in on["schedule"]]
    assert crons and all(len(c.split()) == 5 and c.split()[2:] == ["*", "*", "*"] for c in crons), crons


def test_the_workflow_holds_no_secret_and_reads_only():
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "secrets." not in text, "the drift check must need no credential: /health is public"
    assert _workflow()["permissions"] == {"contents": "read"}


def test_the_workflow_compares_against_main_with_the_tested_script():
    steps = _workflow()["jobs"]["compare"]["steps"]
    checkout = next(s for s in steps if str(s.get("uses", "")).startswith("actions/checkout"))
    assert checkout.get("with", {}).get("ref") == "main"
    runs = [s.get("run", "") for s in steps]
    assert any("tools/check_prod_build.py" in r for r in runs)
    assert not any("pip install" in r for r in runs), "stdlib only: nothing to install"
