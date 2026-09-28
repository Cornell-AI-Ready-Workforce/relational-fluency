"""The bug-report form asks for what places a report against a build.

For four days from 2026-09-24 production ran a rolled-back image and the
issues filed then had to be matched to a build by timestamp afterwards. The
form (.github/ISSUE_TEMPLATE/bug_report.yml) makes the build, the session id
and the time with its zone required, and its scenario list is held to the
scenario bank so a new form cannot be missing from it.
"""
from __future__ import annotations

from pathlib import Path

import yaml

from server import scenarios_v3

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = ROOT / ".github" / "ISSUE_TEMPLATE"


def _form():
    return yaml.safe_load((TEMPLATES / "bug_report.yml").read_text(encoding="utf-8"))


def _fields():
    return {b["id"]: b for b in _form()["body"] if b.get("type") != "markdown"}


def test_the_form_is_a_valid_issue_form_shape():
    form = _form()
    assert form["name"] and form["description"] and form["body"]
    ids = [b["id"] for b in form["body"] if b.get("type") != "markdown"]
    assert len(ids) == len(set(ids)), "issue-form ids must be unique"
    for b in form["body"]:
        assert b["type"] in {"markdown", "input", "textarea", "dropdown", "checkboxes"}
        if b["type"] != "markdown":
            assert b["attributes"]["label"], b["id"]
        if b["type"] == "dropdown":
            opts = b["attributes"]["options"]
            assert opts and len(opts) == len(set(opts))


def test_everything_that_places_a_report_is_required():
    fields = _fields()
    for fid in ("build", "session", "scenario", "environment", "when",
                "what_happened", "expected"):
        assert fid in fields, f"the form no longer asks for {fid}"
        assert fields[fid].get("validations", {}).get("required") is True, (
            f"{fid} is optional; a report without it cannot be placed against a build")


def test_the_build_field_says_where_the_build_is_shown():
    text = str(_fields()["build"]["attributes"])
    assert "build" in text and "/health" in text


def test_the_time_field_asks_for_the_time_zone():
    assert "time zone" in _fields()["when"]["attributes"]["label"].lower()


def test_every_study_scenario_is_offered():
    opts = set(_fields()["scenario"]["attributes"]["options"])
    missing = set(scenarios_v3.available()) - opts
    assert not missing, f"the form's scenario list is missing {sorted(missing)}"


def test_the_form_warns_that_the_repository_is_public_and_links_carry_the_key():
    intro = " ".join(b["attributes"]["value"] for b in _form()["body"] if b["type"] == "markdown")
    assert "public" in intro and "key=" in intro


def test_blank_issues_stay_available():
    cfg = yaml.safe_load((TEMPLATES / "config.yml").read_text(encoding="utf-8"))
    assert cfg["blank_issues_enabled"] is True
