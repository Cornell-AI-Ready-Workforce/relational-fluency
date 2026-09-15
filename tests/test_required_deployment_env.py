"""A variable the code REQUIRES must be declared where an operator would look.

The failure this pins is the worst kind this project has had, because it is
silent on every surface that exists to make things visible.
`server/storage.py` requires `UPSTREAM_CONSENT_VERSION` before it will record
any study consent — and that name appeared nowhere else in the repository: not
`.env.example` (which lists fifteen other variables), not the ECS task
definition, not the Dockerfile, not one page of `docs/`. Unset, every study
arrival is refused at `_consent_provenance`, `POST /api/consent` answers 404,
the voice socket closes 4403, `/health` answers 200, and runs keep being
created. The wave collects zero encounters, uniformly, from the first
participant onward, and nothing anywhere says so until somebody opens the
dataset.

`tests/test_task_definition_env.py` already pins the reverse direction — a
variable set in the task definition that no module reads. That test cannot
catch this one: the name was read by the code and set by nobody, which is the
direction that actually voided the wave.

So the rule here is general, not one variable's special case. Any module under
`server/` may declare

    REQUIRED_ENV = {"SOME_NAME": "one line saying what an operator puts in it"}

and every name in it must appear, uncommented, in `.env.example` and in the ECS
task definition. The next variable somebody makes mandatory is caught on the day
it is made mandatory, by the same test, without anyone remembering to come here.

The terraform and dotenv files are read as text, the way
`tests/test_task_definition_env.py` and `tests/test_infra_scenarios.py` read
them: terraform is not installed in this environment, and what is under test is
the literal name in the file.
"""

from __future__ import annotations

import ast
import importlib
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

SERVER_DIR = REPO_ROOT / "server"
ENV_EXAMPLE = REPO_ROOT / ".env.example"
ECS_TF = REPO_ROOT / "infra" / "terraform" / "ecs.tf"

_ENTRY = re.compile(
    r'\{\s*name\s*=\s*"([A-Z0-9_]+)"\s*,\s*(?:value|valueFrom)\s*=\s*([^\n}]+?)\s*\}')


def _declares_required_env(path: Path) -> bool:
    """Whether this module assigns REQUIRED_ENV at module level.

    Parsed rather than imported, so the discovery pass does not import the whole
    of server/ (voice sessions, boto3 clients, the realtime stack) to find out
    which two files it actually needs.
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError:  # not this test's business to report
        return False
    for node in tree.body:
        targets = (node.targets if isinstance(node, ast.Assign)
                   else [node.target] if isinstance(node, ast.AnnAssign) else [])
        if any(isinstance(t, ast.Name) and t.id == "REQUIRED_ENV" for t in targets):
            return True
    return False


def _required_env() -> list[tuple[str, str]]:
    """(module, variable name) for every REQUIRED_ENV declared under server/.

    The module IS imported once it is known to declare one, because the keys are
    written as constants (`UPSTREAM_CONSENT_VERSION_ENV`) rather than literals —
    naming the string twice is exactly how a rename would split the declaration
    from the code that reads it.
    """
    found: list[tuple[str, str]] = []
    for path in sorted(SERVER_DIR.rglob("*.py")):
        if not _declares_required_env(path):
            continue
        rel = path.relative_to(REPO_ROOT).with_suffix("")
        mod = importlib.import_module(".".join(rel.parts))
        declared = getattr(mod, "REQUIRED_ENV", None)
        assert isinstance(declared, dict) and declared, (
            f"{rel} declares REQUIRED_ENV but it is not a non-empty mapping of "
            f"name -> what an operator puts in it"
        )
        for name, why in declared.items():
            assert isinstance(name, str) and name.isupper(), (
                f"{rel}: REQUIRED_ENV key {name!r} is not an environment "
                f"variable name")
            assert isinstance(why, str) and why.strip(), (
                f"{rel}: REQUIRED_ENV[{name!r}] carries no explanation, so the "
                f"operator it is written for learns nothing from it")
            found.append((".".join(rel.parts), name))
    return found


REQUIRED = _required_env()
IDS = [f"{mod}:{name}" for mod, name in REQUIRED]


def test_something_declares_a_required_variable():
    """The parametrised tests below are vacuous if discovery finds nothing, and
    a parametrisation over an empty list is GREEN — the same trap conftest.py
    documents for the wave fixture. If REQUIRED_ENV is renamed or the
    declaration is deleted, this is the test that notices."""
    assert REQUIRED, (
        "no module under server/ declares REQUIRED_ENV. Either the declaration "
        "was removed, or this test's discovery no longer finds it — and the "
        "checks below are passing without checking anything"
    )


@pytest.mark.parametrize("mod,name", REQUIRED, ids=IDS)
def test_required_env_is_in_env_example(mod, name):
    """An operator setting up any deployment starts from .env.example.

    Uncommented, because a commented line is not a setting: `cp .env.example
    .env` has to produce a file that names every variable the code will refuse
    to run without, with the value blank and waiting rather than absent and
    forgotten. The value is deliberately NOT required to be non-empty here — a
    plausible-looking version string shipped in this file would be stamped on
    real participants' records by anyone who copied it without reading.
    """
    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    assert re.search(rf"(?m)^{re.escape(name)}=", text), (
        f"{name} is required by {mod} but is not an uncommented setting in "
        f".env.example, so every operator who starts there deploys without it"
    )


@pytest.mark.parametrize("mod,name", REQUIRED, ids=IDS)
def test_required_env_is_in_the_task_definition(mod, name):
    """And the deployment that actually serves participants is the ECS task."""
    body = ECS_TF.read_text(encoding="utf-8")
    declared = {n for n, _ in _ENTRY.findall(body)}
    assert name in declared, (
        f"{name} is required by {mod} but the ECS task definition never sets "
        f"it: the task comes up healthy and records nothing"
    )


@pytest.mark.parametrize("mod,name", REQUIRED, ids=IDS)
def test_required_env_is_documented_for_the_operator(mod, name):
    """Docs are the third surface, and the one a person reaches for when the
    deployment is already misbehaving. A name that appears only in code and in
    config is a name nobody can look up."""
    docs = [p for p in (REPO_ROOT / "docs").glob("*.md")] + [REPO_ROOT / "README.md"]
    named_in = [p.name for p in docs
                if name in p.read_text(encoding="utf-8")]
    assert named_in, (
        f"{name} is required by {mod} and no page of docs/ or README.md "
        f"mentions it"
    )


# --------------------------------------------------------------------------
# A placeholder is not a value
# --------------------------------------------------------------------------

def test_a_placeholder_consent_version_is_treated_as_unset(monkeypatch, caplog):
    """The likeliest way this gets "set" wrongly is by being copied.

    `[FILL IN: ...]` is config/consent.yaml's own convention and it travels: a
    tfvars template, a .env someone filled in halfway. Stamped on a record it is
    worse than the refusal it replaces, because the wave then carries consents
    naming a document that does not exist and no audit can recover which text
    was actually shown.
    """
    from server import storage

    for value in ("[FILL IN: the approved consent version]",
                  "<the Qualtrics consent version>",
                  "TBD", "todo", "changeme", "placeholder"):
        monkeypatch.setenv(storage.UPSTREAM_CONSENT_VERSION_ENV, value)
        assert storage.upstream_consent_version() == "", (
            f"{value!r} was accepted as the name of the approved consent text")
        assert storage.UPSTREAM_CONSENT_VERSION_ENV in storage.missing_required_env()

    monkeypatch.setenv(storage.UPSTREAM_CONSENT_VERSION_ENV, "cornell-irb-2026-09-v3")
    assert storage.upstream_consent_version() == "cornell-irb-2026-09-v3"
    assert storage.missing_required_env() == []


def test_missing_required_env_reports_the_unset_ones(monkeypatch):
    """What a boot preflight and /health publish: the names, so an operator is
    told which variable rather than that something is wrong."""
    from server import storage

    for name in storage.REQUIRED_ENV:
        monkeypatch.delenv(name, raising=False)
    assert sorted(storage.missing_required_env()) == sorted(storage.REQUIRED_ENV)


# --------------------------------------------------------------------------
# ...and a value IS a value
# --------------------------------------------------------------------------
#
# The rule above was widened until it fired on a correctly configured
# deployment, which is the failure it exists to prevent wearing the fix's
# clothes: a refusal here and an unset variable produce the same silent void —
# /api/consent 404, sockets 4403, a wave of zero encounters. So every test in
# this section is a positive control, and they are written first on purpose.

#: Strings the rule used to refuse and an operator may legitimately mean. Some
#: IRB wordings really are called "v0" or "test"; a guess about what somebody
#: meant is not worth a wave.
LEGITIMATE_CONSENT_VERSIONS = [
    "cornell-irb-2026-09-v3", "IRB-2026-1234-v2", "2026-09-consent",
    "protocol_0042_rev3", "v1.4.0-cornell", "protocol-0042-none-of-the-above",
    "v0", "v1", "1", "x", "test", "example", "temp", "default", "set", "value",
    "version",
]

#: What nobody means by anything, for any variable: still refused, everywhere.
GENUINE_PLACEHOLDERS = [
    "FILL IN", "fill_in", "[FILL IN: the approved consent version]",
    "changeme", "change-me", "TODO", "TBD", "xxx", "XXX", "placeholder",
    "<the Qualtrics consent version>", "<the version",
]


@pytest.mark.parametrize("value", LEGITIMATE_CONSENT_VERSIONS)
def test_a_correctly_configured_deployment_boots_clean(value, monkeypatch):
    """POSITIVE CONTROL. A deployment that named its consent wording is usable,
    and every surface says so.

    The denylist had grown to hold "v1", "v0", "1", "x", "test", "example",
    "temp", "default", "set", "value" and "version", and "v1" was the last of
    them still refused. An IRB wording genuinely called v0 or v1 was then
    refused, and the refusal is indistinguishable from never having set the
    variable: `POST /api/consent` answers 404, every voice socket
    closes 4403 and the wave records nothing, on a task that was configured
    right. That is a worse defect than the one the list was added to close.
    """
    from server import storage

    monkeypatch.setenv(storage.UPSTREAM_CONSENT_VERSION_ENV, value)
    assert not storage.is_placeholder_value(value)
    assert storage.upstream_consent_version() == value
    assert storage.missing_required_env() == []


@pytest.mark.parametrize("value", GENUINE_PLACEHOLDERS)
def test_a_genuine_placeholder_is_still_caught(value, monkeypatch):
    """The other side of the same control: narrowing the rule did not open it.
    A template marker copied out of a tfvars file is still refused, and the
    variable is still named on the surfaces an operator reads."""
    from server import storage

    monkeypatch.setenv(storage.UPSTREAM_CONSENT_VERSION_ENV, value)
    assert storage.is_placeholder_value(value)
    assert storage.upstream_consent_version() == ""
    assert storage.UPSTREAM_CONSENT_VERSION_ENV in storage.missing_required_env()


def test_a_genuinely_unset_variable_is_still_caught(monkeypatch):
    """And the original failure — the variable simply absent — is unchanged."""
    from server import storage

    monkeypatch.delenv(storage.UPSTREAM_CONSENT_VERSION_ENV, raising=False)
    assert storage.upstream_consent_version() == ""
    assert storage.UPSTREAM_CONSENT_VERSION_ENV in storage.missing_required_env()


# --------------------------------------------------------------------------
# The consent version's rule is not every variable's rule
# --------------------------------------------------------------------------

@pytest.fixture()
def foreign_required_env(monkeypatch):
    """A required variable declared by some other module, as the rule promises
    any module may. Injected into sys.modules the way a real one arrives."""
    import types

    module = types.ModuleType("server._scoped_env_probe")
    module.REQUIRED_ENV = {
        "RF_PROBE_PORT": "the port the sidecar listens on",
        "RF_PROBE_MIRROR": "1 or 0; whether to mirror uploads",
        "RF_PROBE_HEADERS": "extra request headers, as a JSON object",
    }
    monkeypatch.setitem(sys.modules, module.__name__, module)
    return module


@pytest.mark.parametrize("name,value", [
    ("RF_PROBE_PORT", "0"),
    ("RF_PROBE_MIRROR", "none"),
    ("RF_PROBE_HEADERS", '{"X-Study": "relational-fluency"}'),
])
def test_a_non_version_variable_is_judged_as_what_it_is(name, value,
                                                        foreign_required_env,
                                                        monkeypatch):
    """POSITIVE CONTROL for the scoping, and the reproduced defect.

    The consent version's denylist was promoted to the universal test for every
    variable any module makes required. A placeholder test written about version
    strings is not one about a port, a flag or a header block: with all three of
    these set correctly, all three were reported missing, the boot printed
    "record NOTHING" and /health answered `config.ok: false` — on a deployment
    with nothing wrong with it. "0" is a port. "none" is an answer to a flag.
    A JSON header block opens with a brace, which the bracket rule reads as an
    unclosed `[FILL IN`.
    """
    from server import storage

    for var in foreign_required_env.REQUIRED_ENV:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv(storage.UPSTREAM_CONSENT_VERSION_ENV, "cornell-irb-2026-09-v3")
    monkeypatch.setenv(name, value)

    assert name not in storage.missing_required_env(), (
        f"{name} is correctly set and was reported as missing, which on a real "
        f"deployment reads exactly like the misconfiguration it is not")


@pytest.mark.parametrize("value", ["FILL IN", "changeme", "TODO", "xxx"])
def test_a_placeholder_in_any_variable_is_still_caught(value,
                                                       foreign_required_env,
                                                       monkeypatch):
    """The scoping did not turn the general rule off. What nobody means by
    anything is still refused for a variable that is not the consent version —
    which is the whole reason the rule was made general in the first place."""
    from server import storage

    monkeypatch.setenv(storage.UPSTREAM_CONSENT_VERSION_ENV, "cornell-irb-2026-09-v3")
    monkeypatch.setenv("RF_PROBE_PORT", value)
    assert "RF_PROBE_PORT" in storage.missing_required_env()


# --------------------------------------------------------------------------
# One definition, two enforcers
# --------------------------------------------------------------------------

def _plan_rule_literal() -> str:
    """The regex string as infra/terraform/ecs.tf actually carries it.

    Read as text, with HCL's doubled backslashes undone — terraform is not
    installed here and what is under test is the literal in the file.
    """
    text = ECS_TF.read_text(encoding="utf-8")
    m = re.search(r'regex\("([^"]+)"', text)
    assert m, "no validation regex found in infra/terraform/ecs.tf"
    return m.group(1).replace("\\\\", "\\")


def test_the_plan_and_the_app_carry_the_same_rule():
    """One definition, in one place, that both sides read.

    They were two rules that happened to look alike, joined by a comment asking
    people to keep them in sync, and they drifted — the ONE way that costs a
    wave instead of an apply. `tofu plan` accepted "xxx" (its regex had no XXX
    branch), the apply succeeded, and `server/storage.py` then treated the value
    as unset: the task came up healthy, minted a run per arrival and recorded
    nothing. That is the original silent-void failure reproduced through the
    documented deployment path.

    Terraform cannot import Python and this repository will not run `tofu` in
    CI, so the copy is tested rather than trusted. The failure message carries
    the string to paste, because a test that says "these disagree" and leaves
    you to work out the escaping is a test people edit the other file to silence.
    """
    from server import storage

    expected = "(?i)" + storage.consent_version_rule_pattern()
    assert _plan_rule_literal() == expected, (
        "infra/terraform/ecs.tf no longer enforces the rule server/storage.py "
        "applies at runtime. Replace the regex literal in its validation block "
        "with this, backslashes already doubled for HCL:\n\n    "
        + expected.replace("\\", "\\\\")
    )


#: Every way this variable has actually been got wrong, plus the families the
#: two rules were measured to disagree about, plus the strings an operator
#: legitimately means.
CONFIG_CORPUS = LEGITIMATE_CONSENT_VERSIONS + GENUINE_PLACEHOLDERS + [
    "xxxxxxxx", "TODOnow", "TBDish", "placeholderish", "CHANGE ME", "FILL_IN",
    "[the version", "unknown", "none", "null", "nil", "nan", "n/a", "na",
    "n.a.", "tbc", "tba", "-", "--", ".", "?", "0", "asdf", "foo", "bar",
]


@pytest.mark.parametrize("value", CONFIG_CORPUS)
def test_the_plan_and_the_app_agree_on_every_value(value):
    """And they agree on behaviour, not only on bytes.

    Byte equality above is the mechanism; this is the promise. Either direction
    of disagreement is a defect, and they are not equally expensive: plan
    refuses / app accepts costs an apply, plan accepts / app treats as unset
    costs a wave. Measured before this change, thirty-three of these values ran
    the expensive way. Both counts have to be zero, because a rule that is
    enforced in two places and means two things is the drift that produced them.
    """
    from server import storage

    plan_refuses = bool(re.search(_plan_rule_literal(), value, re.I))
    app_refuses = storage.is_placeholder_value(value)
    assert plan_refuses == app_refuses, (
        f"{value!r}: the deployment plan "
        f"{'refuses' if plan_refuses else 'accepts'} it and the app "
        f"{'refuses' if app_refuses else 'accepts'} it"
        + ("" if plan_refuses else " — which is a task that plans, applies, "
                                   "comes up healthy and records nothing"))


def test_the_plan_rule_is_written_for_terraforms_regex_engine():
    """Terraform's regex is RE2, which has no backreferences and no lookaround,
    and a plan that cannot compile its own validation is a deployment nobody can
    make. Not a substitute for running `tofu validate`; it is what can be
    checked in a repository that does not have terraform installed."""
    from server import storage

    pattern = storage.consent_version_rule_pattern()
    for unsupported in ("(?=", "(?!", "(?<", "\\1", "\\b", "\\d", "\\w"):
        assert unsupported not in pattern, (
            f"{unsupported!r} in the shared rule: `tofu plan` will fail to "
            f"compile the copy of it in infra/terraform/ecs.tf")
    assert '"' not in pattern, "a double quote cannot survive the HCL string"
    assert "${" not in pattern, "HCL would read ${ as an interpolation"


# --------------------------------------------------------------------------
# The runtime half of the general rule
# --------------------------------------------------------------------------

def test_the_runtime_rule_sees_a_module_it_never_imported(tmp_path, monkeypatch):
    """The reproduced hole, and the one that made the machinery decorative.

    The promise is that any module under `server/` may declare REQUIRED_ENV and
    the next variable somebody makes mandatory is caught the day it is made
    mandatory. The runtime half read `sys.modules`, so it saw only what
    `server.app` imports at module level — and the consent path is not in that
    set: `server/consent_check.py`, `server/runs.py`, `server/qualtrics.py` and
    `server/identity.py` are all imported inside functions, to break the cycle
    through `server/storage.py`. Declared on the consent path, a required
    variable was found by the tests above and invisible to the boot preflight
    and to /health, and the silent void reproduced in full with every piece of
    this round's machinery installed.

    A fixture tree rather than a real module, because the point is that nothing
    imports it: the source is read, never executed.
    """
    from server import storage

    (tmp_path / "late_module.py").write_text(
        'SURVEY_ENV = "RF_PROBE_SURVEY"\n'
        'REQUIRED_ENV = {SURVEY_ENV: "the id of the approved Qualtrics survey"}\n',
        encoding="utf-8")
    monkeypatch.setattr(storage, "_SERVER_DIR", tmp_path)
    monkeypatch.delenv("RF_PROBE_SURVEY", raising=False)
    assert "server.late_module" not in sys.modules

    declared = storage._declared_required_env()
    assert declared.get("RF_PROBE_SURVEY") == (
        "the id of the approved Qualtrics survey"), (
        "a REQUIRED_ENV declared in a module nothing imports is invisible to "
        "the running server, so the wave it would void is silent again")
    assert "RF_PROBE_SURVEY" in storage.missing_required_env()

    monkeypatch.setenv("RF_PROBE_SURVEY", "SV_1a2b3c")
    assert "RF_PROBE_SURVEY" not in storage.missing_required_env()


def test_the_source_scan_reads_the_real_server_directory():
    """The test above monkeypatches where the scan looks, so this is what keeps
    it honest: the unpatched scan has to find this repository's own declaration
    in the file that makes it."""
    from server import storage

    found = storage._scan_required_env(storage._SERVER_DIR)
    assert found.get(storage.UPSTREAM_CONSENT_VERSION_ENV), (
        "the source scan no longer resolves server/storage.py's own "
        "REQUIRED_ENV, so it resolves nobody's")
    assert storage._SERVER_DIR == SERVER_DIR.resolve()


# --------------------------------------------------------------------------
# What the operator is actually told
# --------------------------------------------------------------------------

def test_the_warning_says_set_and_rejected_rather_than_missing(
        foreign_required_env, monkeypatch, capsys):
    """"Missing" was said of a variable that is present.

    An operator who has set UPSTREAM_CONSENT_VERSION and been told it is missing
    goes to look at the task definition, finds it there, and concludes the
    warning is wrong — when the thing to look at is the value they typed. The
    two states are told apart now, and neither line carries the value: the rule
    is general, the next variable it covers may hold a credential, and a log
    that has learned to print values prints that one too.
    """
    from server import app as appmod
    from server import storage

    secretish = "changeme-9f3a-not-a-real-token"
    monkeypatch.setenv(storage.UPSTREAM_CONSENT_VERSION_ENV, secretish)
    monkeypatch.delenv("RF_PROBE_PORT", raising=False)
    monkeypatch.setenv("RF_PROBE_MIRROR", "1")
    monkeypatch.setenv("RF_PROBE_HEADERS", "{}")

    # This preflight alone, not all three: the other two are real network calls
    # made with real credentials, and conftest's offline stand-in patches only a
    # server.app that was already imported when the fixture ran. That this check
    # is one of the three the boot runs is pinned in tests/test_cohort_integrity.
    appmod._check_required_env()
    out = capsys.readouterr().out

    present = next(l for l in out.splitlines()
                   if storage.UPSTREAM_CONSENT_VERSION_ENV in l)
    absent = next(l for l in out.splitlines() if "RF_PROBE_PORT" in l)
    assert "set" in present and "rejected" in present, present
    assert "not set" in absent, absent
    assert secretish not in out, (
        "the boot warning echoed the configured value; a required variable may "
        "hold a credential and this line goes to CloudWatch")


def test_the_operator_line_survives_for_every_module(foreign_required_env,
                                                     monkeypatch, capsys):
    """The whole payload of the general rule is "one line saying what an
    operator puts in it", and it was dropped for every variable except the one
    hardcoded: everything else printed "required by this server", which tells a
    person woken by a failing wave nothing they did not know."""
    from server import app as appmod
    from server import storage

    monkeypatch.setenv(storage.UPSTREAM_CONSENT_VERSION_ENV, "cornell-irb-2026-09-v3")
    monkeypatch.delenv("RF_PROBE_HEADERS", raising=False)
    monkeypatch.setenv("RF_PROBE_PORT", "8080")
    monkeypatch.setenv("RF_PROBE_MIRROR", "1")

    appmod._check_required_env()  # see the note in the test above
    out = capsys.readouterr().out

    assert "extra request headers, as a JSON object" in out, out
    assert "required by this server" not in out, out


def test_the_reason_never_reaches_the_published_json(monkeypatch):
    """/health publishes these names unauthenticated, and the contract that
    makes that safe is that they are NAMES and never values. Carrying the
    unset/rejected distinction to the boot warning must not smuggle anything
    into the payload a stranger can fetch."""
    import json

    from server import storage

    monkeypatch.setenv(storage.UPSTREAM_CONSENT_VERSION_ENV,
                       "[FILL IN: the approved consent version]")
    published = json.loads(json.dumps(storage.missing_required_env()))
    assert published == [storage.UPSTREAM_CONSENT_VERSION_ENV], published


def test_the_declaration_lookup_is_still_a_plain_mapping():
    """REQUIRED_ENV is read by the tests at the top of this file as a dict, and
    iterating it must keep meaning "what storage requires" rather than "what the
    whole server requires" — only a lookup answers for other modules."""
    from server import storage

    assert isinstance(storage.REQUIRED_ENV, dict)
    assert list(storage.REQUIRED_ENV) == [storage.UPSTREAM_CONSENT_VERSION_ENV]
    assert storage.REQUIRED_ENV[storage.UPSTREAM_CONSENT_VERSION_ENV]
    assert storage.REQUIRED_ENV.get("RF_NOT_DECLARED_ANYWHERE") is None
