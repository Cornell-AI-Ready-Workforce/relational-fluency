"""Is production running the build main pins?

Run daily by .github/workflows/prod-build-drift.yml, and by hand whenever you
want the answer:

    python tools/check_prod_build.py
    python tools/check_prod_build.py --url https://rf.ai-ready-workforce.ai.cornell.edu

It reads the container_image tag pinned in infra/terraform/terraform.tfvars
(the checkout it runs in; the workflow checks out main) and GET /health's
top-level "build" (server/build_info.py), and exits non-zero, loudly, unless
they name the same commit.

WHY. On 2026-09-24 a `tofu apply` from a stale branch, whose terraform.tfvars
still pinned ca77c2f, replaced 4798e64 in production. The apply printed
"Apply complete!", the deployment went healthy, and for four days testers
filed issues against a build nobody believed was running. Every guard at the
time was a thing a person had to remember to run. This one runs every day on
its own, needs no credentials (/health is public, which is also why "build" is
a bare commit id and nothing more), and turns that silent state into a red
workflow with the two commits named.

What a failure means, in the order to suspect it:
  * production reports a build older than the pin: main's pin moved and the
    apply has not happened yet (a deploy is pending), or the apply happened
    from something other than main (the 2026-09-24 rollback);
  * production reports no build at all: the image predates BUILD_SHA, or was
    built without `--build-arg BUILD_SHA`. 4798e64, live when this was
    written, predates it, so this check fails until the next deploy;
  * /health unreachable: production is down, or the network is.

Exit codes: 0 same build; 1 different or unknown build; 2 could not check.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Optional, Tuple

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import pinned_image  # noqa: E402

PROD_URL = "https://rf.ai-ready-workforce.ai.cornell.edu"

# The image serving production when BUILD_SHA was introduced. Named in the
# null-build message because "reports no build" is the EXPECTED answer until
# the first deploy of an image built with the argument, and a red check that
# nobody can tell is expected is a red check people learn to ignore.
LAST_IMAGE_WITHOUT_BUILD_SHA = "4798e64"

OK, DRIFT, UNCHECKED = 0, 1, 2


def fetch_health(url: str, *, attempts: int = 3, timeout: float = 20.0,
                 opener: Callable = urllib.request.urlopen) -> dict:
    """GET <url>/health as JSON. Retried: a single dropped connection from a
    GitHub runner is not an answer about production."""
    last: Optional[Exception] = None
    for i in range(attempts):
        try:
            req = urllib.request.Request(
                url.rstrip("/") + "/health",
                headers={"User-Agent": "rf-prod-build-drift/1", "Accept": "application/json"})
            with opener(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, ValueError) as exc:
            last = exc
            if i + 1 < attempts:
                time.sleep(2 * (i + 1))
    raise RuntimeError(f"could not read {url.rstrip('/')}/health: {last}")


def compare(health: dict, pin: pinned_image.Pin) -> Tuple[int, str]:
    """(exit code, message) for one /health body against one pin."""
    if "build" not in health or health.get("build") is None:
        where = ("has no \"build\" key" if "build" not in health
                 else "reports \"build\": null")
        return DRIFT, (
            f"production /health {where}, so nothing says which image is serving "
            f"participants. main pins {pin.tag}.\n"
            f"The image live when BUILD_SHA was introduced "
            f"({LAST_IMAGE_WITHOUT_BUILD_SHA}) predates it and reports no build "
            f"until the next deploy of an image built with --build-arg BUILD_SHA; this "
            f"failure is expected until then. If that deploy has happened, the "
            f"image was built without the argument (see the Dockerfile header) "
            f"and has to be rebuilt from a new commit, because ECR tags are "
            f"immutable.\n"
            f"Until then the running image is visible only with AWS credentials:\n"
            f"  aws ecs describe-task-definition --task-definition \"$(aws ecs "
            f"describe-services --cluster relational-fluency --services platform "
            f"--query 'services[0].taskDefinition' --output text)\" "
            f"--query 'taskDefinition.containerDefinitions[0].image' --output text")
    build = str(health.get("build"))
    if pinned_image.same_commit(build, pin.tag):
        return OK, f"production is running {build}, the build main pins ({pin.tag})."
    return DRIFT, (
        f"production is running {build}, but main pins {pin.tag}.\n"
        f"Either main's pin moved and the apply has not happened yet (a deploy is "
        f"pending: run tools/deploy.sh from an up-to-date main), or production "
        f"was applied from something other than main, which is how 2026-09-24's "
        f"four-day rollback happened. Find out which before the next session is "
        f"collected: `git log --oneline -1 {build}` and "
        f"`git merge-base --is-ancestor {build} {pin.tag}` say whether production "
        f"is behind main's pin or off it.")


def _annotate(level: str, title: str, message: str) -> None:
    """A GitHub Actions annotation, so the reason is on the run page and in
    the notification rather than three clicks into a log."""
    if os.environ.get("GITHUB_ACTIONS") != "true":
        return
    flat = message.replace("%", "%25").replace("\r", "").replace("\n", "%0A")
    print(f"::{level} title={title}::{flat}")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8", newline="") as fh:
            fh.write(f"### {title}\n\n```\n{message}\n```\n")


def main(argv=None, *, fetch: Callable[[str], dict] = fetch_health) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--url", default=os.environ.get("RF_URL", PROD_URL),
                    help=f"server to ask (default {PROD_URL})")
    ap.add_argument("--tfvars", type=Path, default=pinned_image.TFVARS,
                    help="terraform.tfvars to read the pin from")
    args = ap.parse_args(argv)

    try:
        pin = pinned_image.read(args.tfvars)
    except (OSError, pinned_image.PinError) as exc:
        msg = f"cannot read the pinned image from {args.tfvars}: {exc}"
        print(msg, file=sys.stderr)
        _annotate("error", "Pinned image unreadable", msg)
        return UNCHECKED
    try:
        health = fetch(args.url)
    except RuntimeError as exc:
        msg = f"{exc}\nProduction is down or unreachable; main pins {pin.tag}."
        print(msg, file=sys.stderr)
        _annotate("error", "Production /health unreachable", msg)
        return UNCHECKED
    if not isinstance(health, dict):
        msg = f"{args.url}/health did not answer a JSON object; main pins {pin.tag}."
        print(msg, file=sys.stderr)
        _annotate("error", "Production /health unreadable", msg)
        return UNCHECKED

    code, msg = compare(health, pin)
    if code == OK:
        print(msg)
    else:
        print("PRODUCTION BUILD DRIFT\n" + msg, file=sys.stderr)
        _annotate("error", "Production is not running main's pinned build", msg)
    return code


if __name__ == "__main__":
    sys.exit(main())
