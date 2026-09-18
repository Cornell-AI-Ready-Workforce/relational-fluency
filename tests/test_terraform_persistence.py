"""The deployment must have somewhere to put the study, and keep it.

This file exists because of the worst finding on the platform, and the reason it
went unnoticed for four task-definition revisions is worth stating plainly: the
Terraform in this repository *already described* a persistent volume. An EFS
filesystem, an access point, mount targets, a volume block, a mountPoint and
DATA_DIR=/data were all written down in infra/terraform/ecs.tf. None of it was
ever applied. Every live revision (35-38) carries `volumes: []`, no mount
points, and no DATA_DIR, so server/storage.py fell back to its `<repo>/data`
default -- the container filesystem -- and every run file, participant record,
transcript, WAV and the SQLite index was written to a disk that the next deploy
destroys. Nothing errored. /health stayed green. Only S3 (webcam video)
survived, which is why the loss reads as "some data is there".

So the defect was never "the code is missing". It was "the code was never true
of the deployment, and nothing in the repository could tell the difference".
That is the gap these tests close, as far as a test can close it: they read the
Terraform and fail if the task definition *would* come up without a volume, a
mount point, DATA_DIR, or any of the four variables the live task lacks. They
cannot prove an apply happened -- no offline test can -- but they can make the
description internally consistent, so the only remaining failure is "nobody
ran it", which is a question an operator can answer in one command
(`aws ecs describe-task-definition ... --query 'taskDefinition.volumes'`).

tests/test_task_definition_env.py pins the reverse direction: a name set in the
task definition that no module reads. It could not catch this one, because the
absent thing was absent from the *deployment*, and DATA_DIR was absent from the
task definition while being read by the code -- the direction that cost the
records.

Terraform is not installed in this environment and these tests are not a plan.
They read the HCL as text, the way tests/test_task_definition_env.py,
tests/test_required_deployment_env.py and tests/test_infra_scenarios.py already
do: what is under test is the literal declaration in the file, and a test that
needed a toolchain would be a test that never runs.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TF_DIR = REPO_ROOT / "infra" / "terraform"
ECS_TF = TF_DIR / "ecs.tf"
TFVARS = TF_DIR / "terraform.tfvars"
DOCKERFILE = REPO_ROOT / "Dockerfile"

#: Where the app is told to write. One string, used by the volume assertions and
#: by the DATA_DIR assertion, so a change to one that is not a change to the
#: other is what fails rather than what ships.
MOUNT_PATH = "/data"


# --------------------------------------------------------------------------
# Reading HCL as text, with balanced braces rather than line-at-a-time regex
# --------------------------------------------------------------------------

def _all_tf() -> str:
    """Every .tf file concatenated. Terraform itself has no file boundaries --
    a resource may move between files without changing a plan -- so a test that
    grepped one file would fail the day someone tidied, which teaches people
    that the test is noise."""
    return "\n".join(
        p.read_text(encoding="utf-8") for p in sorted(TF_DIR.glob("*.tf"))
    )


def _block_at(body: str, start: int, opener: str = "{", closer: str = "}") -> str:
    """The text from the first `opener` at or after `start` to its match."""
    i = body.index(opener, start)
    depth = 0
    for j in range(i, len(body)):
        if body[j] == opener:
            depth += 1
        elif body[j] == closer:
            depth -= 1
            if depth == 0:
                return body[i:j + 1]
    raise AssertionError(f"unterminated {opener}...{closer} block in infra/terraform")


def _find_block(kind: str, *labels: str, body: str | None = None) -> str:
    """The body of e.g. `resource "aws_efs_file_system" "study" { ... }`."""
    body = _all_tf() if body is None else body
    head = re.compile(
        rf'(?m)^\s*{re.escape(kind)}\s+'
        + r"\s+".join(f'"{re.escape(lbl)}"' for lbl in labels)
        + r"\s*\{"
    )
    m = head.search(body)
    assert m, f'no {kind} {" ".join(labels)!r} declared under infra/terraform'
    return _block_at(body, m.start())


def _has_block(kind: str, *labels: str) -> bool:
    try:
        _find_block(kind, *labels)
    except AssertionError:
        return False
    return True


def _container_definition() -> str:
    body = ECS_TF.read_text(encoding="utf-8")
    return _block_at(body, body.index("container_definitions = jsonencode("),
                     "[", "]")


def _list_in(body: str, key: str) -> str:
    return _block_at(body, body.index(f"{key} = ["), "[", "]")


_ENTRY = re.compile(
    r'\{\s*name\s*=\s*"([A-Z0-9_]+)"\s*,\s*(?:value|valueFrom)\s*=\s*([^\n}]+?)\s*\}')


@pytest.fixture(scope="module")
def task_env() -> dict[str, str]:
    return dict(_ENTRY.findall(_list_in(_container_definition(), "environment")))


@pytest.fixture(scope="module")
def task_definition() -> str:
    return _find_block("resource", "aws_ecs_task_definition", "agent")


# --------------------------------------------------------------------------
# 1. The volume exists, the container mounts it, and the app writes into it
# --------------------------------------------------------------------------
#
# Three declarations in two places have to agree for a single byte to survive a
# deploy, and each of them is independently silent when wrong:
#
#   volume {}       absent -> ECS accepts the task definition, mountPoints is
#                   rejected... only if a mountPoint names it. Absent from BOTH
#                   (the live revisions) the task simply runs without storage.
#   mountPoints     absent -> the filesystem exists, is mounted by nothing, and
#                   shows 0 bytes forever while the app writes happily to /app.
#   DATA_DIR        absent -> storage.py resolves <repo>/data. The EFS mount is
#                   there, correct, empty, and beside the real write path.
#
# So they are tested as one fact, not three.

def test_the_task_definition_declares_a_persistent_volume(task_definition):
    volume = _block_at(task_definition, task_definition.index("volume {"))
    assert "efs_volume_configuration" in volume, (
        "the task definition's volume is not EFS-backed. A bind mount or an "
        "undeclared volume is the container filesystem under another name, and "
        "Fargate destroys it on every deploy -- which is exactly the state "
        "revisions 35-38 shipped in"
    )
    assert "aws_efs_file_system.study.id" in volume, (
        "the volume does not point at the study filesystem declared in this "
        "same file")


def test_the_container_mounts_that_volume(task_definition):
    container = _container_definition()
    assert "mountPoints" in container, (
        "the container declares no mountPoints, so the EFS volume is attached "
        "to the task and reachable by nothing inside it")
    mounts = _list_in(container, "mountPoints")

    volume = _block_at(task_definition, task_definition.index("volume {"))
    m = re.search(r'name\s*=\s*"([^"]+)"', volume)
    assert m, "the volume block has no name"
    volume_name = m.group(1)

    assert re.search(rf'sourceVolume\s*=\s*"{re.escape(volume_name)}"', mounts), (
        f"the mountPoint does not name the declared volume {volume_name!r}. "
        f"ECS rejects that at RegisterTaskDefinition, so this one at least "
        f"fails loudly -- but only for whoever runs the apply")
    assert re.search(rf'containerPath\s*=\s*"{re.escape(MOUNT_PATH)}"', mounts), (
        f"the volume is not mounted at {MOUNT_PATH}")
    assert re.search(r"readOnly\s*=\s*false", mounts), (
        "the study volume is mounted read-only; the app cannot write a record "
        "to it")


def test_data_dir_is_set_and_names_the_mount(task_env):
    """The line that turns a mounted filesystem into the one the app uses.

    server/storage.py:57 is `Path(os.environ.get("DATA_DIR", str(ROOT/"data")))`.
    Unset, the fallback is a path inside the image. The Dockerfile sets
    ENV DATA_DIR=/data, which is why this looked fine -- but the Dockerfile is
    not the deployment's configuration, and an image rebuilt without that line,
    or a task definition that overrides it, moves every record silently.
    """
    assert "DATA_DIR" in task_env, (
        "the task definition never sets DATA_DIR, so the app writes to the "
        "container filesystem and the mounted volume sits empty beside it")
    assert task_env["DATA_DIR"] == f'"{MOUNT_PATH}"', (
        f"DATA_DIR is {task_env['DATA_DIR']}, not the {MOUNT_PATH} the volume "
        f"is mounted at")


def test_the_dockerfile_and_the_task_definition_agree_on_the_data_dir():
    """Two places set DATA_DIR and only one of them is the deployment.

    They are allowed to both exist -- the Dockerfile's value is what a local
    `docker run` gets -- but they must not disagree, because the task
    definition wins and the Dockerfile is what a reader finds first.
    """
    m = re.search(r"(?m)^\s*DATA_DIR=(\S+)", DOCKERFILE.read_text(encoding="utf-8"))
    assert m, "the Dockerfile no longer sets DATA_DIR"
    assert m.group(1) == MOUNT_PATH, (
        f"Dockerfile sets DATA_DIR={m.group(1)} but the volume mounts at "
        f"{MOUNT_PATH}")


# --------------------------------------------------------------------------
# 2. The filesystem the volume points at
# --------------------------------------------------------------------------

def test_the_study_filesystem_is_encrypted_at_rest():
    """Audio of identifiable participants and their transcripts. The IRB record
    says encrypted at rest; this is the line that makes that true."""
    fs = _find_block("resource", "aws_efs_file_system", "study")
    assert re.search(r"(?m)^\s*encrypted\s*=\s*true", fs), (
        "the study filesystem is not encrypted at rest")
    assert "kms_key_id" in fs, (
        "the filesystem falls back to the AWS-managed EFS key rather than the "
        "study key, so recordings on disk are not under the key the protocol "
        "names")


def test_in_transit_encryption_on_the_mount(task_definition):
    volume = _block_at(task_definition, task_definition.index("volume {"))
    assert re.search(r'transit_encryption\s*=\s*"ENABLED"', volume), (
        "NFS traffic between the task and the filesystem is unencrypted; "
        "every transcript and WAV crosses the VPC in clear")


def test_the_access_point_owns_its_root_as_the_container_user():
    """A raw EFS root is root:root and the container is uid 1000 (Dockerfile).

    Without the access point the mount succeeds and the first write returns
    EACCES -- which surfaces as a 500 on the first participant, not as a
    deployment failure.
    """
    ap = _find_block("resource", "aws_efs_access_point", "study")
    uids = re.findall(r"(?m)^\s*(?:owner_)?uid\s*=\s*(\d+)", ap)
    assert uids, "the access point sets no POSIX uid"
    assert set(uids) == {"1000"}, (
        f"the access point's uids are {sorted(set(uids))}; the container runs "
        f"as uid 1000 (Dockerfile `useradd -m -u 1000 appuser`) and cannot "
        f"write to a root directory it does not own")

    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    assert re.search(r"useradd\s+.*-u\s+1000\b", dockerfile), (
        "the Dockerfile no longer pins the app user to uid 1000, so the access "
        "point's ownership is aimed at a user that does not exist")


def test_the_mount_is_authorized_through_the_access_point(task_definition):
    volume = _block_at(task_definition, task_definition.index("volume {"))
    assert "access_point_id" in volume, (
        "the volume mounts the filesystem root instead of the access point, so "
        "the POSIX ownership above does not apply and writes fail EACCES")
    assert re.search(r'iam\s*=\s*"ENABLED"', volume), (
        "IAM authorization is off, so any task in the VPC that can reach 2049 "
        "can mount the study filesystem")
    policy = _find_block("data", "aws_iam_policy_document", "task_efs")
    for action in ("elasticfilesystem:ClientMount", "elasticfilesystem:ClientWrite"):
        assert action in policy, (
            f"IAM authorization is ENABLED on the mount but the task role has "
            f"no {action}: the task cannot mount /data at all, new tasks never "
            f"go healthy, and with minimum_healthy_percent = 100 the deploy "
            f"hangs while the old task keeps serving")


def test_nfs_is_reachable_only_from_the_platform_task():
    sg = _find_block("resource", "aws_security_group", "efs")
    assert "cidr_blocks" not in sg, (
        "the EFS security group admits a CIDR range. Port 2049 is an "
        "unauthenticated filesystem to anything that can route to it; the only "
        "thing that should reach it is the task security group")
    assert "aws_security_group.agent.id" in sg, (
        "NFS ingress does not name the platform task's security group")
    assert re.search(r"(?m)^\s*from_port\s*=\s*2049", sg), (
        "the EFS security group does not open 2049, so the mount times out and "
        "the task never starts")


def test_mount_targets_cover_the_subnets_the_service_actually_runs_in():
    """A filesystem with no mount target in the task's AZ is unreachable.

    The service places tasks across both private subnets, so both need one --
    and they must be the *same* expression, not two lists that happen to agree
    today.
    """
    mt = _find_block("resource", "aws_efs_mount_target", "study")
    svc = _find_block("resource", "aws_ecs_service", "agent")
    assert "module.vpc.private_subnets" in mt, (
        "the mount targets are not placed in the VPC's private subnets")
    assert "module.vpc.private_subnets" in svc, (
        "the service no longer runs in the private subnets the mount targets "
        "were placed in; a task in an AZ with no mount target cannot mount "
        "/data and never becomes healthy")


def test_mount_targets_do_not_iterate_over_values_that_are_unknown_at_plan_time():
    """The bug that proves this code was never applied.

    `for_each = toset(module.vpc.private_subnets)` iterates over subnet IDs that
    do not exist until the VPC is created, and Terraform refuses to build a plan
    whose resource *addresses* depend on apply-time values:

        Invalid for_each argument ... depends on resource attributes that
        cannot be determined until apply

    On a fresh apply -- which is the only kind this stack has, since its state
    is not in the account's state bucket -- that is a hard stop before a single
    resource is created. `count` is fine, because the number of private subnets
    is known from the literal list in network.tf even when the IDs are not.
    """
    mt = _find_block("resource", "aws_efs_mount_target", "study")
    assert "for_each" not in mt, (
        "aws_efs_mount_target.study uses for_each over computed subnet IDs. "
        "Terraform cannot plan that on a fresh apply -- use count with "
        "length(module.vpc.private_subnets), whose value is known from "
        "network.tf's literal CIDR list")
    assert re.search(r"(?m)^\s*count\s*=", mt), (
        "the mount targets iterate over neither count nor for_each, so only "
        "one AZ gets a mount target and tasks in the other cannot start")


def test_throughput_does_not_depend_on_burst_credits():
    """A fresh EFS in the default `bursting` mode earns throughput in
    proportion to how much it stores, and this filesystem stores almost
    nothing -- a few MB of JSON and WAVs per participant.

    That is the wrong shape for the workload: a collection session writes audio
    from several concurrent encounters, drains the credit balance, and EFS then
    throttles writes to the baseline. The app does not report a slow write; it
    reports nothing, and the operator sees encounters that take longer and
    finish. `elastic` prices per byte moved and has no credit balance to
    exhaust.
    """
    fs = _find_block("resource", "aws_efs_file_system", "study")
    m = re.search(r'throughput_mode\s*=\s*"([^"]+)"', fs)
    assert m, (
        "the filesystem does not set throughput_mode, so it takes the default "
        "`bursting` and a wave can throttle itself at the worst moment")
    assert m.group(1) != "bursting", (
        "throughput_mode is explicitly `bursting`; see the docstring")


def test_the_study_filesystem_is_backed_up():
    """EFS automatic backups are ON for a filesystem made in the console and
    OFF for one made through the API, which is what Terraform uses.

    This filesystem is about to become the only copy of every run record,
    participant file, transcript and the SQLite index -- S3 holds webcam video
    and nothing else. An `rm -rf` in a debugging session, or a wrong path in an
    export script, is then unrecoverable in a study that cannot re-run its
    participants.
    """
    assert _has_block("resource", "aws_efs_backup_policy", "study"), (
        "no aws_efs_backup_policy: the filesystem holding every study record "
        "has no backup, because Terraform-created EFS defaults to none")
    policy = _find_block("resource", "aws_efs_backup_policy", "study")
    assert re.search(r'status\s*=\s*"ENABLED"', policy), (
        "the backup policy is declared but not ENABLED")


def test_the_study_data_cannot_be_removed_by_an_apply():
    """`tofu destroy`, or any change that forces replacement, takes the
    participants with it. prevent_destroy turns that into a plan-time error.

    This matters more here than it normally would: the state for this stack is
    not in the account's state bucket, so the first person to run `tofu apply`
    is running it from empty state against resources that already exist. A
    mistake in that situation is not "recreate it" -- it is 21 pilot recordings
    and every consent record.
    """
    for kind, name in (
        ("aws_efs_file_system", "study"),
        ("aws_s3_bucket", "study_data"),
        ("aws_kms_key", "study"),
    ):
        block = _find_block("resource", kind, name)
        assert re.search(r"prevent_destroy\s*=\s*true", block), (
            f"{kind}.{name} has no `lifecycle {{ prevent_destroy = true }}`, so "
            f"a destroy or a forced replacement silently takes the study data "
            f"with it")


# --------------------------------------------------------------------------
# 3. The four variables the live task lacks
# --------------------------------------------------------------------------

def test_credentials_still_come_from_secrets_manager():
    """Adding environment entries is how a secret becomes a plaintext one."""
    container = _container_definition()
    secrets = dict(_ENTRY.findall(_list_in(container, "secrets")))
    env = dict(_ENTRY.findall(_list_in(container, "environment")))
    for name in ("ANTHROPIC_API_KEY", "SESSION_KEY"):
        assert name in secrets, f"{name} is no longer injected from Secrets Manager"
        assert "aws_secretsmanager_secret" in secrets[name], (
            f"{name}'s valueFrom is not a Secrets Manager ARN")
        assert name not in env, (
            f"{name} is a plaintext environment entry; it is then readable in "
            f"the console, in describe-task-definition, and in CloudTrail")


# --------------------------------------------------------------------------
# 4. Retention: the promise, and the copies the promise forgets
# --------------------------------------------------------------------------

def test_retention_expires_noncurrent_versions_too():
    """The trap that makes a retention rule look done and leave everything.

    Versioning is ENABLED on this bucket. An `expiration` rule on a versioned
    bucket does not delete the object -- it writes a delete marker and makes the
    previous version noncurrent, where it stays, billed and readable, forever.
    A study that told its IRB "deleted after N days" would be keeping every
    byte, and `aws s3 ls` would show an empty prefix.
    """
    lc = _find_block("resource", "aws_s3_bucket_lifecycle_configuration",
                     "study_data")
    assert "noncurrent_version_expiration" in lc, (
        "the lifecycle rules expire current versions only. Versioning is "
        "ENABLED (aws_s3_bucket_versioning.study_data), so every superseded "
        "version survives the retention period in full")
    assert "expired_object_delete_marker" in lc, (
        "nothing cleans up the delete markers left behind once every version "
        "under them has expired; they accumulate and slow every LIST")
    assert "abort_incomplete_multipart_upload" in lc, (
        "a webcam upload that dies mid-PUT leaves multipart parts that no LIST "
        "shows and no retention rule reaches, billed indefinitely")


def test_the_retention_period_is_the_variable_everywhere_it_appears():
    """One number, one source. A rule that hardcoded 365 beside a variable that
    says 730 is a discrepancy nobody reads HCL closely enough to see."""
    lc = _find_block("resource", "aws_s3_bucket_lifecycle_configuration",
                     "study_data")
    days = re.findall(r"(?m)^\s*days\s*=\s*(\S+)", lc)
    assert days, "no expiration period is set on any rule"
    hardcoded = [d for d in days if d.isdigit()]
    assert not hardcoded, (
        f"the retention period is written as a literal ({hardcoded}) rather "
        f"than var.study_data_retention_days, so the IRB's number and the "
        f"bucket's can drift apart silently")


# --------------------------------------------------------------------------
# 5. The pin, which is a statement about production
# --------------------------------------------------------------------------

def test_the_pinned_image_is_an_immutable_release_tag():
    """ECR here is IMMUTABLE and the tag is the short git SHA on purpose: the
    record has to be able to say which code served a given encounter. `latest`
    or `bootstrap` in this file would make that unanswerable."""
    text = TFVARS.read_text(encoding="utf-8")
    m = re.search(r'(?m)^\s*container_image\s*=\s*"([^"]+)"', text)
    assert m, "terraform.tfvars does not pin container_image"
    tag = m.group(1).rsplit(":", 1)[-1]
    assert tag not in ("latest", "bootstrap", ""), (
        f"container_image is pinned to the mutable tag {tag!r}")
    assert re.fullmatch(r"[0-9a-f]{7,40}", tag), (
        f"container_image tag {tag!r} is not a git SHA, so nothing connects the "
        f"running build to a commit")


def test_the_pin_records_which_deployment_it_describes():
    """The defect this file had: it pinned 3cf8496 while cabc1dd was deployed,
    so an operator following the runbook rolled production back two releases
    and got a success message for it.

    An offline test cannot know what is deployed. What it can require is that
    the file state which task-definition revision the pin corresponds to, so
    the next person to change the tag has to say what they checked, and the
    drift is visible to a reader instead of only to production.
    """
    text = TFVARS.read_text(encoding="utf-8")
    m = re.search(r"(?im)^#\s*deployed:\s*relational-fluency-agent:(\d+)\b", text)
    assert m, (
        "terraform.tfvars does not record which task-definition revision its "
        "container_image pin was checked against. Add a line of the form\n"
        "    # deployed: relational-fluency-agent:<revision> verified <date>\n"
        "and update it in the same commit as the tag -- an unverified pin is a "
        "rollback with a success message")
