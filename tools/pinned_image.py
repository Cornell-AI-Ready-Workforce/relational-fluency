"""The container_image pinned in infra/terraform/terraform.tfvars, parsed once.

Three tools ask what that line says, and they must not disagree about it:
tools/deploy.sh (through tools/deploy_guard.py) before a plan,
tools/check_prod_build.py when it compares production with main, and
tools/sim/check.py when it names the build a report is for. A regex copied
into each would drift the first time the line's shape changes, and the drift
would show up as one tool approving a pin another cannot read.

The shape is the one tests/test_terraform_persistence.py already requires:

    container_image = "<account>.dkr.ecr.<region>.amazonaws.com/<repository>:<tag>"

with the tag a git commit id, because ECR here is IMMUTABLE and the tag is the
short SHA the image was built from.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
TFVARS = REPO_ROOT / "infra" / "terraform" / "terraform.tfvars"

_LINE = re.compile(r'(?m)^\s*container_image\s*=\s*"([^"]*)"')
_ECR = re.compile(
    r"^(?P<registry>\d{12})\.dkr\.ecr\.(?P<region>[a-z0-9-]+)\.amazonaws\.com/"
    r"(?P<repository>[a-z0-9._/-]+):(?P<tag>[^:@/\s]+)$")
_SHA = re.compile(r"[0-9a-f]{7,40}")


class PinError(ValueError):
    """The pin is missing or is not an ECR image tagged with a commit."""


@dataclass(frozen=True)
class Pin:
    image: str
    registry: str
    region: str
    repository: str
    tag: str


def parse(text: str) -> Pin:
    """The pin in terraform.tfvars text, or PinError saying what is wrong."""
    found = _LINE.findall(text)
    if not found:
        raise PinError("terraform.tfvars does not set container_image")
    if len(found) > 1:
        # Terraform takes the last assignment; a reader taking the first would
        # check one image and deploy another.
        raise PinError(f"terraform.tfvars sets container_image {len(found)} times")
    image = found[0].strip()
    m = _ECR.match(image)
    if not m:
        raise PinError(f"container_image {image!r} is not an ECR image reference "
                       f"of the form <account>.dkr.ecr.<region>.amazonaws.com/<repo>:<tag>")
    tag = m.group("tag")
    if not _SHA.fullmatch(tag):
        raise PinError(f"container_image tag {tag!r} is not a git commit id, so "
                       f"nothing connects the pinned image to source")
    return Pin(image=image, registry=m.group("registry"), region=m.group("region"),
               repository=m.group("repository"), tag=tag)


def read(path: Optional[Path] = None) -> Pin:
    return parse(Path(path or TFVARS).read_text(encoding="utf-8"))


def same_commit(a: Optional[str], b: Optional[str]) -> bool:
    """Do two commit ids name the same commit, allowing one to be abbreviated?

    `git rev-parse --short` is 7 characters until the repository grows an
    ambiguity and then silently longer, and the workflow slices GITHUB_SHA to 7.
    A build reported at 8 characters against a pin written at 7 is the same
    build, so compare on the shorter length. Both must be at least 7 long.
    """
    if not a or not b:
        return False
    a, b = a.strip().lower(), b.strip().lower()
    if not (_SHA.fullmatch(a) and _SHA.fullmatch(b)):
        return False
    n = min(len(a), len(b))
    return a[:n] == b[:n]
