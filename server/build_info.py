"""Which build is serving: the commit the running image was built from.

WHY this exists. On 2026-09-24 a `tofu apply` from a stale branch, whose
terraform.tfvars still pinned an older image, rolled production back from
4798e64 to ca77c2f. It said "Apply complete!", the deployment went healthy, and
for four days testers filed issues against a build nobody knew was running:
nothing a person could see, on the page or on /health, named the build. The
only way to tell was `aws ecs describe-tasks`, which no tester has.

So the image carries its own commit. The Dockerfile takes `--build-arg
BUILD_SHA=<short sha>` and bakes it into the environment as BUILD_SHA; every
documented build (the GitHub workflow, docs/DEPLOY-AWS.md, docs/OPERATIONS.md)
passes it, with the same value it tags the image with. This module is the one
reader, and the answer is published three ways:

* GET /health, top-level "build": what the drift check
  (tools/check_prod_build.py) compares with the tag pinned in main's
  terraform.tfvars, and what an operator reads after a deploy;
* GET /api/run/config "build": what the participant page shows as a small
  build tag, so a bug report can say which build it was filed against;
* llm.provenance()["build"]: on realtime_session_started and so on every
  encounter record, so an analyst can split the archive by the code that
  served it rather than inferring it from dates and pipeline_version.

None means "not known", never a guess. A local checkout has no BUILD_SHA and
reports None; so does every image built before this module existed (4798e64
and 0066b10, the two production ran while it was being written, are two).
Nothing here falls back to `git rev-parse`: a checkout with local edits is not
the commit its HEAD names, and the one thing this value may not do is claim a
build that is not running.

Read from the process environment, never through llm.setting(). That accessor
lets the repository's .env win over the environment, which is right for the
gateway key and wrong here: the build is a property of the image, set by
`docker build`, and a checkout's .env must not be able to rename a running
image. (server/app.py's load_dotenv() can still fill an UNSET BUILD_SHA from a
local .env; nothing documented puts one there, and the image has no .env at
all, because .dockerignore excludes it.)
"""
from __future__ import annotations

import os
import re
from typing import Optional

# A git commit id, abbreviated or full. Anything else (a branch name, "latest",
# an empty ARG) is refused rather than published: /health is public, and a
# value that is not a commit cannot be checked against anything, which is the
# whole point of having it.
_SHA = re.compile(r"[0-9a-f]{7,40}")


def build_sha() -> Optional[str]:
    """The short commit the running image was built from, or None if unknown.

    Read per call, not cached at import: it is one os.environ lookup, and a
    test (or a future embedding) that sets the variable after import should
    see it.
    """
    raw = (os.environ.get("BUILD_SHA") or "").strip().lower()
    return raw if _SHA.fullmatch(raw) else None
