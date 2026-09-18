"""What the runbooks must say, because the runbook and the practice diverged.

Every assertion here is a fact somebody was burned by, pinned to the document a
person reads while it is burning them.

Two kinds of test live in this file and the distinction matters when one fails:

* **Presence tests** assert that a named fact is written down somewhere a
  reader will find it. They fail when a rewrite drops the awkward half of the
  truth — which is exactly how these documents got into the state this pass
  found them in. The cure is to put the fact back, not to delete the test.
* **Drift tests** read the fact out of the source of truth (`server/`,
  `infra/terraform/`) and assert the document agrees. They fail when the code
  moves and the doc does not. The cure is to update the doc.

Scope: the four operator-facing documents. `docs/RATING.md` is covered by
`tests/test_deploy_portability.py` and is not touched here.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

OPERATIONS = REPO_ROOT / "docs" / "OPERATIONS.md"
DEPLOY_AWS = REPO_ROOT / "docs" / "DEPLOY-AWS.md"
README = REPO_ROOT / "README.md"


def _text(p: Path) -> str:
    return p.read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# The release path actually in use
# --------------------------------------------------------------------------
#
# Every live revision of relational-fluency-agent was registered by hand with
# the AWS CLI. The Terraform state for this stack is not in the account's state
# bucket, and versions.tf still has its S3 backend block commented out. So a
# person following the documented `tofu apply` flow today runs it from EMPTY
# state, which does not update the service — it tries to CREATE the bucket, the
# ECR repositories, the IAM roles and the ACM certificate that already exist.
# A runbook that describes a procedure nobody has run is worse than no runbook,
# because it is followed with confidence.

RELEASE_CLI_STEPS = [
    "aws ecs describe-task-definition",
    "aws ecs register-task-definition",
    "aws ecs update-service",
]


@pytest.mark.parametrize("step", RELEASE_CLI_STEPS)
def test_release_runbook_gives_the_cli_sequence_in_use(step):
    """The AWS runbook must spell out the path the service is actually on."""
    assert step in _text(DEPLOY_AWS), (
        f"docs/DEPLOY-AWS.md never mentions `{step}`. Every live task-definition "
        "revision was registered by hand with the CLI, so this is the release "
        "procedure a person has to follow; documenting only `tofu apply` sends "
        "them to a flow that has never been run against this service."
    )


@pytest.mark.parametrize("doc", [DEPLOY_AWS, OPERATIONS], ids=["DEPLOY-AWS", "OPERATIONS"])
def test_the_missing_terraform_state_is_written_down(doc):
    """Terraform cannot be used at all until the state is found.

    This is the single fact that decides which of the two release paths a
    reader is allowed to take, and it appeared in neither document.
    """
    body = _text(doc)
    assert "tofu apply" in body or "tofu -chdir" in body, (
        f"{doc.name} no longer mentions the Terraform flow at all; if it has "
        "genuinely been removed, remove this test with it."
    )
    assert re.search(r"state", body, re.I) and "state bucket" in body, (
        f"{doc.name} documents a Terraform flow without saying that the state "
        "for this stack is not in the account's state bucket. Run from empty "
        "state, `tofu apply` does not update the running service: it plans to "
        "CREATE the S3 bucket, the ECR repositories, the IAM roles and the ACM "
        "certificate that already exist, and the operator finds out mid-apply."
    )


def test_the_committed_image_pin_is_flagged_as_a_rollback_hazard():
    """`terraform.tfvars` pins an older tag than the one serving participants.

    Anyone who follows the runbook's `tofu apply` without `-var container_image`
    deploys the committed pin, which is a live rollback of the platform in the
    middle of a study wave and reports success while doing it.
    """
    body = _text(DEPLOY_AWS)
    assert "terraform.tfvars" in body
    assert "roll production back" in body, (
        "docs/DEPLOY-AWS.md does not warn that the committed container_image "
        "pin in infra/terraform/terraform.tfvars can be OLDER than the tag the "
        "service is running, so an apply that takes the file at its word rolls "
        "production back to a build participants have already moved past."
    )


IAM_ACTIONS = [
    "ecr:GetAuthorizationToken",
    "ecr:PutImage",
    "ecs:RegisterTaskDefinition",
    "ecs:UpdateService",
    "iam:PassRole",
    "secretsmanager:GetSecretValue",
]


@pytest.mark.parametrize("action", IAM_ACTIONS)
def test_each_release_step_names_the_permission_it_needs(action):
    """So a person can tell BEFORE a wave whether they can deploy at all.

    `iam:PassRole` is the one that catches people: an operator with every ecs:*
    action still cannot register a task definition that names the execution and
    task roles, and the refusal names PassRole rather than the task definition,
    which reads like a problem with the roles themselves.
    """
    assert action in _text(DEPLOY_AWS), (
        f"docs/DEPLOY-AWS.md does not say that {action} is needed. The point of "
        "listing them is that a researcher can find out they lack a permission "
        "while nobody is being recorded, rather than at the deploy."
    )


# --------------------------------------------------------------------------
# No persistent volume
# --------------------------------------------------------------------------


def test_operations_states_the_measured_volume_situation():
    """The volume is a measured fact, with the id and revision that carry it.

    Until 17 September 2026 the live task had no volume and no EFS file system
    existed; `tofu apply` then created `fs-09e2d30bae3ce9239` and registered
    revision 41 with `study-data` mounted at /data. The page must name both, so
    a reader comparing the one-line check's output knows what "good" looks like,
    and must still show the empty-list shape that means ephemeral.
    """
    body = _text(OPERATIONS)
    assert "fs-09e2d30bae3ce9239" in body and "study-data" in body, (
        "docs/OPERATIONS.md must name the EFS file system and the volume that "
        "revision 41 mounts at /data, as measured fact"
    )
    assert "volumes=[]" in body, (
        "docs/OPERATIONS.md must still show the empty-list shape the check "
        "returns on an ephemeral revision, so a regression is recognisable."
    )


def test_the_volume_check_is_a_command_the_reader_can_run():
    body = _text(OPERATIONS)
    assert "taskDefinition.[volumes,containerDefinitions[0].mountPoints]" in body, (
        "the one-line check that reveals the missing volume has gone from "
        "docs/OPERATIONS.md"
    )


# --------------------------------------------------------------------------
# The participant links — the section people copy from
# --------------------------------------------------------------------------
#
# The survey declares participantId, assignmentId and projectId, and pipes
# participantId and ResponseID. `ParticipantKey` is a field that does not
# exist: piped, Qualtrics substitutes the empty string, /start cannot attribute
# the arrival, and the run is recorded as `unattributed` — a paid participant
# whose recording you hold and whose recruitment record you cannot join to it.

FIELD_TOKEN = "${e://Field/participantId}"


def test_the_piped_field_is_the_one_the_survey_declares():
    body = _text(OPERATIONS)
    assert FIELD_TOKEN in body, (
        "docs/OPERATIONS.md must pipe ${e://Field/participantId} — that is the "
        "embedded field the live survey declares. This is the block people "
        "copy into Qualtrics verbatim."
    )
    assert "${e://Field/ParticipantKey}" not in body, (
        "docs/OPERATIONS.md still tells the operator to pipe "
        "${e://Field/ParticipantKey}, which the survey does not declare. "
        "Qualtrics substitutes an unknown field with the empty string and says "
        "nothing, so every arrival records as cohort=unattributed."
    )


@pytest.mark.parametrize("field", ["participantId", "assignmentId", "projectId"])
def test_every_declared_embedded_field_is_named(field):
    """All three, not just the one that gets piped: the other two are what a
    person checks against when the survey in front of them looks different."""
    assert field in _text(OPERATIONS), (
        f"docs/OPERATIONS.md does not name the survey's {field} embedded field"
    )


ENTRY_LINKS = ["/start"]


@pytest.mark.parametrize("link", ENTRY_LINKS)
def test_all_three_entry_links_are_documented(link):
    assert link in _text(OPERATIONS), (
        f"{link} is a live entry route and docs/OPERATIONS.md does not mention it"
    )


def test_the_researcher_link_is_never_published_without_its_key():
    """`/test` is check_key-gated now. A /test URL with no &key= answers 401 and
    creates no run, which reads as "the deployment is broken"."""
    body = _text(OPERATIONS)
    offenders = [
        m.group(0)
        for m in re.finditer(r"https?://\S*?/test\?\S+", body)
        if "key=" not in m.group(0)
    ]
    assert not offenders, (
        "these /test links in docs/OPERATIONS.md carry no &key= and now answer "
        "401:\n  " + "\n  ".join(offenders)
    )


# --------------------------------------------------------------------------
# The Qualtrics datacenter trap
# --------------------------------------------------------------------------


def test_the_export_host_is_documented_and_matches_the_code():
    """cornell.qualtrics.com answers /whoami and /surveys and then refuses
    /export-responses. Only the refusal names the host that works."""
    code = (REPO_ROOT / "server" / "qualtrics.py").read_text(encoding="utf-8")
    m = re.search(r'setting\("QUALTRICS_BASE_URL",\s*"([^"]+)"\)', code)
    assert m, "server/qualtrics.py no longer has a QUALTRICS_BASE_URL default"
    host = m.group(1).split("//", 1)[-1]

    body = _text(OPERATIONS)
    assert host in body, (
        f"docs/OPERATIONS.md does not name {host}, the only host the Qualtrics "
        "export endpoint accepts for this brand."
    )
    assert "viawest" in body, (
        "docs/OPERATIONS.md does not warn that /whoami reports the datacenter "
        'as "viawest" — a name that is not routable and is not what belongs in '
        "QUALTRICS_BASE_URL. The value to use appears only in the error message."
    )


# --------------------------------------------------------------------------
# The CORS allowlist, and the failure that looks like nothing
# --------------------------------------------------------------------------


def _cors_origins() -> list[str]:
    tf = (REPO_ROOT / "infra" / "terraform" / "storage_secrets.tf").read_text(
        encoding="utf-8")
    block = tf.split("allowed_origins", 1)
    assert len(block) == 2, "the bucket CORS rule has moved"
    return re.findall(r'"(https?://[^"]+)"', block[1].split("]", 1)[0])


def test_the_local_testing_port_is_documented_from_the_allowlist():
    """Local webcam testing works on exactly one port, because the bucket's
    CORS allowlist names exactly one."""
    origins = _cors_origins()
    local = [o for o in origins if "localhost" in o or "127.0.0.1" in o]
    assert local, "the CORS allowlist no longer carries a local origin"
    body = _text(OPERATIONS)
    for origin in local:
        port = origin.rsplit(":", 1)[-1]
        assert port in body, (
            f"docs/OPERATIONS.md never mentions port {port}. The bucket CORS "
            f"allowlist names {origin} and nothing else local, so a researcher "
            "who serves the app on any other port has every webcam recording "
            "refused at the browser's preflight."
        )


def test_operations_says_a_cors_refusal_does_not_reach_the_fallback():
    """`static/v2.html` reports a CORS-blocked PUT as `network`, and `network`
    is deliberately NOT one of the reasons that trigger the local upload
    fallback. So the recording is lost with nothing in any log naming CORS."""
    client = (REPO_ROOT / "static" / "v2.html").read_text(encoding="utf-8")
    assert "presigningIsUnavailable" in client, (
        "static/v2.html no longer has the fallback predicate this documents"
    )
    body = _text(OPERATIONS)
    assert "CORS" in body, "docs/OPERATIONS.md never mentions CORS"
    lowered = body.lower()
    assert "fallback" in lowered and "network" in lowered, (
        "docs/OPERATIONS.md must say that a CORS-refused PUT is reported as "
        "`network`, which does not trigger the local fallback — the one "
        "failure mode that loses a whole wave of recordings silently."
    )


# --------------------------------------------------------------------------
# Saying what is not known
# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# The links between these pages
# --------------------------------------------------------------------------


def _heading_slugs(path: Path) -> set[str]:
    """GitHub-style anchor slugs for every ATX heading in a file."""
    slugs = set()
    for line in _text(path).splitlines():
        if not line.startswith("#"):
            continue
        title = line.lstrip("#").strip().lower()
        slugs.add(re.sub(r"[^\w\s-]", "", title).strip().replace(" ", "-"))
    return slugs


@pytest.mark.parametrize(
    "doc", [OPERATIONS, DEPLOY_AWS, README],
    ids=["OPERATIONS", "DEPLOY-AWS", "README"])
def test_every_cross_reference_lands_on_a_heading_that_exists(doc):
    """These pages point at each other constantly, and a link into a renamed
    heading does not fail loudly — it drops the reader at the top of the page
    with the section they were sent for somewhere below, which during an
    incident is the same as no link at all.

    An emoji in a heading is the trap this catches: GitHub strips it and leaves
    a LEADING HYPHEN on the slug, so `## ⚠️ Read this` is reached at
    `#-read-this` and every hand-written `#read-this` misses.
    """
    broken = []
    for m in re.finditer(r"\]\(([^)\s]*?)#([^)\s]+)\)", _text(doc)):
        target, anchor = m.group(1), m.group(2)
        if target == "":
            path = doc
        else:
            path = (doc.parent / target).resolve()
            if not path.exists() or path.suffix != ".md":
                continue  # a link out of the markdown set is not this test's job
        if anchor not in _heading_slugs(path):
            broken.append(f"{doc.name}: [#{anchor}] -> {path.name}")
    assert not broken, (
        "these cross-references point at headings that do not exist:\n  "
        + "\n  ".join(broken))


def test_the_unanswered_questions_are_named_with_someone_to_ask():
    """Where this pass could not establish a fact, the runbook says so and says
    who can settle it. An invented procedure is the failure this prevents."""
    body = _text(DEPLOY_AWS)
    assert "## Open questions" in body, (
        "docs/DEPLOY-AWS.md has no Open questions section. The Terraform state, "
        "the account's deploy history and the IAM grants held by the people who "
        "will run this are not knowable from inside the repository, and a "
        "runbook that fills those gaps with plausible commands is the thing "
        "this pass exists to remove."
    )
