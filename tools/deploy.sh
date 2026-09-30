#!/usr/bin/env bash
# Plan a production deploy, but only from a state that can be trusted.
#
#   tools/deploy.sh                          # checks, then `tofu plan -out tfplan.bin`
#   tools/deploy.sh --allow-active-sessions  # plan even though someone is mid-encounter
#   tools/deploy.sh --allow-rollback         # plan a deliberate move to an OLDER build
#
# It never applies. When every check passes it prints the plan summary, with
# the image change set apart, and the one command that applies exactly that
# plan. The apply stays a separate, deliberate step a person types.
#
# WHY. On 2026-09-24 somebody ran `tofu apply` from a branch whose
# terraform.tfvars still pinned an older image. Terraform did what it was
# told: it replaced 4798e64 with ca77c2f, printed "Apply complete!", and the
# deployment went healthy. Production ran the old build for four days and
# nobody could see it. Nothing was wrong with Terraform or with the pin on
# main; the apply simply ran from a checkout that was not main. So this script
# refuses to plan unless:
#
#   1. the working tree is clean: what is planned is what is committed
#      (untracked sim reports under tools/sim/reports/ excepted, see below),
#      and no *.auto.tfvars or TF_CLI_ARGS can override the committed pin;
#   2. `origin` is the canonical GitHub repository (not a fork, a mirror or a
#      clone of a local copy, any of which can be behind it), and after
#      `git fetch`, HEAD is exactly origin/main: not behind it (the
#      2026-09-24 case), not ahead of it (unreviewed), not beside it;
#   3. the container_image tag pinned in infra/terraform/terraform.tfvars is a
#      commit reachable from origin/main, and exists in ECR (read-only
#      `aws ecr describe-images`), so the plan cannot point the service at an
#      image that is not there;
#   4. the service runs one task (read-only `aws ecs describe-services`),
#      because /health answers for one task only, and production's GET
#      /health says active_sessions is 0, because a rollout cuts every
#      encounter on the old task about two minutes in (docs/OPERATIONS.md,
#      "Before every deploy"); and, where /health reports the running build,
#      the pin is not OLDER than it and it is a commit this checkout knows
#      (one it cannot place is refused like a rollback);
#   5. and it WARNS, without refusing, when tools/sim/reports/<tag>.json is
#      missing or did not pass (tools/sim/check.py, the pre-deploy sim check).
#
# After the plan, the image change is checked once more against the plan itself
# (tools/deploy_guard.py plan-summary): the plan must deploy the pinned image
# and must not move production to an older commit than it runs. That second
# look matters today, because the images production ran while this was
# written (4798e64, then 0066b10) predate BUILD_SHA and their /health names no
# build; the plan's "before" image is then the only record of what runs.
#
# Untracked files under tools/sim/reports/ do not count as a dirty tree: the
# runbook runs the sim check on the pinned commit, which writes its report
# there, and a report is evidence about the plan, not input to it. Commit it
# afterwards so the record of what was checked is in git.
#
# bash 3.2 or later (macOS's /bin/bash), GNU or BSD userland. On Windows, run
# it from Git Bash or WSL. Needs git, aws (read-only), curl, tofu and python.
#
# Exit status: 0 planned (or nothing to change); 1 refused; 2 bad usage.

set -euo pipefail

PROD_URL="${RF_URL:-https://rf.ai-ready-workforce.ai.cornell.edu}"
ECS_CLUSTER="relational-fluency"
ECS_SERVICE="platform"
TF_DIR="infra/terraform"
PLAN_FILE="tfplan.bin"

ALLOW_ACTIVE=0
ALLOW_ROLLBACK=0
WARNINGS=0
PLANNED=0

usage() { sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'; }

while [ $# -gt 0 ]; do
  case "$1" in
    --allow-active-sessions) ALLOW_ACTIVE=1 ;;
    --allow-rollback) ALLOW_ROLLBACK=1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

refuse() {
  printf '\nREFUSED: %s\n' "$1" >&2
  shift
  for line in "$@"; do printf '  %s\n' "$line" >&2; done
  if [ "$PLANNED" = 1 ]; then
    printf '\nNothing was applied, and no plan file is left to apply.\n' >&2
  else
    printf '\nNothing was planned. Nothing was applied.\n' >&2
  fi
  exit 1
}
step() { printf '\n== %s\n' "$*"; }
ok() { printf '  ok: %s\n' "$*"; }
warn() { printf '  WARNING: %s\n' "$*" >&2; WARNINGS=$((WARNINGS + 1)); }

ROOT=$(git rev-parse --show-toplevel 2>/dev/null) || refuse "not inside a git checkout"
cd "$ROOT"

# An old plan file must not survive a failed or refused run: `apply tfplan.bin`
# would apply whatever it holds. So it goes before the first check, not just
# before the plan: a run refused below ("BEHIND origin/main", "encounter(s) in
# progress") says nothing was planned, and a plan an earlier run made and
# nobody applied would otherwise still be there, applying past every check
# this run failed. tfplan.bin is gitignored, so check 1 cannot see it either.
rm -f "$TF_DIR/$PLAN_FILE"

# The interpreter for tools/deploy_guard.py: $PYTHON, else the project venv,
# else whatever python is on PATH. Standard library only, so any 3.8+ works.
if [ -z "${PYTHON:-}" ]; then
  if [ -x .venv/bin/python ]; then PYTHON=.venv/bin/python
  elif command -v python3 >/dev/null 2>&1; then PYTHON=python3
  else PYTHON=python; fi
fi
# No .pyc files: the guard must not write into the tree it has just called clean.
guard() { PYTHONDONTWRITEBYTECODE=1 "$PYTHON" tools/deploy_guard.py "$@"; }

for tool in git aws curl tofu "$PYTHON"; do
  command -v "$tool" >/dev/null 2>&1 || refuse "$tool is not on PATH"
done

# --- 1. what is planned is what is committed -------------------------------
step "Working tree"
dirty=$(git status --porcelain --untracked-files=all | grep -v '^?? tools/sim/reports/' || true)
if [ -n "$dirty" ]; then
  refuse "the working tree is not clean" \
    "A plan made here would include changes nobody reviewed. Commit, stash or" \
    "remove them first:" "$dirty"
fi
# Terraform reads these on top of terraform.tfvars, so either could replace
# the committed pin without a single tracked file changing.
auto=$(find "$TF_DIR" -maxdepth 1 \( -name '*.auto.tfvars' -o -name '*.auto.tfvars.json' \) 2>/dev/null || true)
[ -z "$auto" ] || refuse "Terraform would also load these, over the committed pin:" "$auto"
if [ -n "${TF_CLI_ARGS:-}${TF_CLI_ARGS_plan:-}" ]; then
  refuse "TF_CLI_ARGS / TF_CLI_ARGS_plan is set in this shell" \
    "It can pass -var container_image=... to the plan behind the committed pin." \
    "unset TF_CLI_ARGS TF_CLI_ARGS_plan"
fi
ok "clean"

# --- 2. HEAD is origin/main --------------------------------------------------
step "Is this checkout main, as it is on GitHub?"
# "HEAD is origin/main" is only worth something when origin is the repository
# production is released from. The repository is public: a fork, a mirror or a
# clone of somebody's local copy passes against its own main, which can be
# behind GitHub's and pin an older image, and then the rollback check below
# cannot see the newer commit production runs either.
origin_url=$(git remote get-url origin 2>/dev/null) \
  || refuse "this checkout has no remote named origin" \
       "Clone https://github.com/Cornell-AI-Ready-Workforce/relational-fluency.git and run this from there."
canonical=$(guard origin "$origin_url") \
  || refuse "origin is $origin_url, not the canonical repository ($canonical)" \
       "Production is released from that repository's main. A fork, a mirror or a clone of" \
       "a local copy can be behind it, and then HEAD matching its main proves nothing." \
       "git remote set-url origin https://$canonical.git (or work from a clone of it)."
ok "origin is $origin_url"
git fetch --quiet origin main || refuse "git fetch origin main failed" \
  "Without it there is no way to know whether this checkout is behind main."
head=$(git rev-parse HEAD)
main=$(git rev-parse origin/main)
if [ "$head" != "$main" ]; then
  if git merge-base --is-ancestor HEAD origin/main; then
    behind=$(git rev-list --count HEAD..origin/main)
    refuse "this checkout is $behind commit(s) BEHIND origin/main" \
      "This is the 2026-09-24 incident: a stale checkout's terraform.tfvars pinned" \
      "an older image, and applying it rolled production back for four days." \
      "git switch main && git pull --ff-only, then run this again."
  elif git merge-base --is-ancestor origin/main HEAD; then
    ahead=$(git rev-list --count origin/main..HEAD)
    refuse "this checkout is $ahead commit(s) AHEAD of origin/main" \
      "Production is deployed from main only, after review: merge the change first."
  else
    refuse "this checkout has diverged from origin/main" \
      "HEAD $(git rev-parse --short HEAD) is neither behind nor ahead of origin/main $(git rev-parse --short origin/main)."
  fi
fi
ok "HEAD is origin/main ($(git rev-parse --short HEAD))"

# --- 3. the pin ---------------------------------------------------------------
step "The pinned image"
pin_line=$(guard pin) || refuse "cannot read container_image from $TF_DIR/terraform.tfvars"
IFS=$'\t' read -r IMAGE REGISTRY REGION REPOSITORY TAG <<<"$pin_line"
printf '  %s\n' "$IMAGE"
git cat-file -e "${TAG}^{commit}" 2>/dev/null \
  || refuse "the pinned tag $TAG is not a commit in this repository" \
       "The tag is the short SHA the image was built from; a tag that names no" \
       "commit connects the running image to no source."
git merge-base --is-ancestor "$TAG" origin/main \
  || refuse "the pinned tag $TAG is not reachable from origin/main" \
       "An image built from a branch that was never merged is not a release."
if probe=$(aws ecr describe-images --region "$REGION" --registry-id "$REGISTRY" \
             --repository-name "$REPOSITORY" --image-ids imageTag="$TAG" \
             --query 'imageDetails[0].imagePushedAt' --output text 2>&1); then
  ok "$REPOSITORY:$TAG exists in ECR (pushed $probe)"
else
  case "$probe" in
    *ImageNotFoundException*)
      refuse "$REPOSITORY:$TAG is not in ECR" \
        "Build and push it first (the build-platform-image workflow, or docs/DEPLOY-AWS.md" \
        "section 3); planning now would point the service at an image that is not there." ;;
    *)
      refuse "could not tell whether $REPOSITORY:$TAG is in ECR" "$probe" \
        "(ecr:DescribeImages on your identity? aws sts get-caller-identity?)" ;;
  esac
fi

# --- 4. production ------------------------------------------------------------
step "Production ($PROD_URL)"
# How many tasks /health could be answered by, first. active_sessions is the
# session registry of ONE process, and the ALB's lb_cookie stickiness sends a
# cookieless request to one task picked at random. The service is meant to run
# two during collection (variables.tf), Terraform ignores a manual scale-up
# (ecs.tf ignore_changes) and never scales back, and a rollout runs old and new
# side by side: with two, "0" can be the idle task while the other holds a live
# encounter, the very case this check exists to refuse.
if tasks=$(aws ecs describe-services --region "$REGION" --cluster "$ECS_CLUSTER" \
             --services "$ECS_SERVICE" --query 'services[0].[runningCount,desiredCount]' \
             --output text 2>&1) \
   && read -r RUNNING DESIRED <<<"$tasks" \
   && [[ "$RUNNING" =~ ^[0-9]+$ && "$DESIRED" =~ ^[0-9]+$ ]]; then
  MOST=$RUNNING; [ "$DESIRED" -gt "$MOST" ] && MOST=$DESIRED
  if [ "$MOST" -gt 1 ]; then
    if [ "$ALLOW_ACTIVE" = 1 ]; then
      warn "the service runs $MOST tasks (running $RUNNING, desired $DESIRED); /health answers for one of them, so its active_sessions below covers one task only (--allow-active-sessions)"
    else
      refuse "the service runs $MOST tasks (running $RUNNING, desired $DESIRED), and /health answers for one of them" \
        "active_sessions is one task's count, and a request with no cookie reaches one task" \
        "at random: \"0\" can be the idle one while another holds a live encounter." \
        "After a collection burst, scale back to one and wait for running 1:" \
        "  aws ecs update-service --region $REGION --cluster $ECS_CLUSTER --service $ECS_SERVICE --desired-count 1" \
        "During a rollout, wait for it to finish. Or pass --allow-active-sessions."
    fi
  else
    ok "one task running (running $RUNNING, desired $DESIRED): /health speaks for all of it"
  fi
else
  if [ "$ALLOW_ACTIVE" = 1 ]; then
    warn "could not read how many tasks $ECS_SERVICE runs, so /health may speak for only one of them (--allow-active-sessions)"
  else
    refuse "could not read how many tasks $ECS_SERVICE runs from ECS" "${tasks:-}" \
      "/health answers for one task, so its active_sessions means nothing without the count." \
      "(ecs:DescribeServices on your identity? aws sts get-caller-identity?)"
  fi
fi
PROD_BUILD=""
if health=$(curl -fsS --max-time 20 "$PROD_URL/health" 2>&1) \
   && parsed=$(printf '%s' "$health" | guard health -); then
  IFS=$'\t' read -r ACTIVE PROD_BUILD <<<"$parsed"
  if [ "$ACTIVE" != "0" ]; then
    if [ "$ALLOW_ACTIVE" = 1 ]; then
      warn "$ACTIVE encounter(s) in progress; a rollout will cut them about two minutes in (--allow-active-sessions)"
    else
      refuse "$ACTIVE encounter(s) are in progress on production" \
        "A rollout retires the old task about two minutes after the new one is healthy," \
        "and every conversation on it is cut (Fargate caps the stop timeout at 120 s)." \
        "Wait until /health says active_sessions 0, or pass --allow-active-sessions."
    fi
  else
    ok "active_sessions 0"
  fi
else
  if [ "$ALLOW_ACTIVE" = 1 ]; then
    warn "could not read $PROD_URL/health, so whether anyone is mid-encounter is unknown (--allow-active-sessions)"
  else
    refuse "could not read $PROD_URL/health" "${health:-}" \
      "Whether anyone is mid-encounter is unknown. If production is down and this" \
      "deploy is the fix, pass --allow-active-sessions."
  fi
fi
if [ -n "$PROD_BUILD" ]; then
  IFS=$'\t' read -r REL MOVED <<<"$(guard relation "$PROD_BUILD" "$TAG")"
  case "$REL" in
    same) ok "production already runs $PROD_BUILD, the pinned build" ;;
    forward) ok "production runs $PROD_BUILD; the pin is $MOVED commit(s) newer" ;;
    rollback)
      if [ "$ALLOW_ROLLBACK" = 1 ]; then
        warn "the pin $TAG is $MOVED commit(s) OLDER than production's $PROD_BUILD (--allow-rollback)"
      else
        refuse "the pin $TAG is $MOVED commit(s) OLDER than the $PROD_BUILD production runs" \
          "Applying this would roll production back. If that is the intent (an emergency" \
          "rollback, pinned on main by PR), pass --allow-rollback."
      fi ;;
    sideways) warn "production runs $PROD_BUILD, which is not on the pinned commit's history (a branch build?)" ;;
    *)
      # Treated as a rollback. Production running a commit this checkout
      # cannot place is what a stale remote looks like from inside it: the
      # pin may well be OLDER than what runs.
      if [ "$ALLOW_ROLLBACK" = 1 ]; then
        warn "production runs $PROD_BUILD, which this repository does not know (--allow-rollback)"
      else
        refuse "production runs $PROD_BUILD, which this repository does not know" \
          "So whether the pin $TAG is newer or older than it cannot be told, and a checkout" \
          "that has never seen a newer build is exactly how a rollback looks from inside it." \
          "git fetch origin, and check origin is the canonical repository; if production" \
          "really runs a build this repository will never have, pass --allow-rollback."
      fi ;;
  esac
else
  warn "production /health names no build (the image predates BUILD_SHA); the plan's image diff below is the only record of what runs"
fi

# --- 5. the sim check -----------------------------------------------------------
step "Pre-deploy sim check (tools/sim/reports/$TAG.json)"
sim=$(guard sim-report "$TAG")
case "$sim" in
  pass) ok "the sim check passed for $TAG" ;;
  missing) warn "no sim report for $TAG. Run: python -m tools.sim.check (tools/sim/README.md)" ;;
  *) warn "the sim check did not pass for $TAG: ${sim#fail: }" ;;
esac

# --- the plan ---------------------------------------------------------------------
step "tofu plan"
# Again, for a plan file anything wrote while the checks ran.
rm -f "$TF_DIR/$PLAN_FILE"
PLANNED=1
set +e
tofu -chdir="$TF_DIR" plan -input=false -detailed-exitcode -out="$PLAN_FILE"
rc=$?
set -e
case "$rc" in
  0)
    rm -f "$TF_DIR/$PLAN_FILE"
    printf '\nNo changes: production already matches main. Nothing to apply.\n'
    exit 0 ;;
  2) : ;;
  *)
    rm -f "$TF_DIR/$PLAN_FILE"
    refuse "tofu plan failed (exit $rc)" \
      "First run on this machine? tofu -chdir=$TF_DIR init" ;;
esac

step "What this plan does"
plan_json=$(tofu -chdir="$TF_DIR" show -json "$PLAN_FILE") \
  || { rm -f "$TF_DIR/$PLAN_FILE"; refuse "tofu show -json $PLAN_FILE failed"; }
extra=""
[ "$ALLOW_ROLLBACK" = 1 ] && extra="--allow-rollback"
set +e
printf '%s' "$plan_json" | guard plan-summary - --pinned "$IMAGE" $extra
src=$?
set -e
case "$src" in
  0) : ;;
  3) rm -f "$TF_DIR/$PLAN_FILE"
     refuse "the plan deploys an image other than the pinned $IMAGE" \
       "Something is overriding terraform.tfvars. The plan file has been deleted." ;;
  4) rm -f "$TF_DIR/$PLAN_FILE"
     refuse "the plan moves production to an OLDER build than it runs" \
       "The plan file has been deleted. If this rollback is intended, pin it on main by" \
       "PR and run again with --allow-rollback." ;;
  5) rm -f "$TF_DIR/$PLAN_FILE"
     refuse "the plan replaces an image whose commit this checkout cannot place" \
       "It may be NEWER than the pin (a stale remote looks exactly like this). The plan" \
       "file has been deleted. git fetch origin and run again; if production really runs" \
       "a build this repository will never have, pass --allow-rollback." ;;
  *) rm -f "$TF_DIR/$PLAN_FILE"; refuse "could not summarize the plan (exit $src)" ;;
esac

printf '\n'
[ "$WARNINGS" = 0 ] || printf '%s warning(s) above. Read them before applying.\n\n' "$WARNINGS"
cat <<EOF
Planned, not applied. To apply exactly this plan, between collection sessions:

    tofu -chdir=$TF_DIR apply $PLAN_FILE

Then wait for the rollout and check the build that answers:

    aws ecs describe-services --cluster relational-fluency --services platform --query 'services[0].deployments[0].rolloutState' --output text
    curl -s $PROD_URL/health      # "build" should read $TAG
EOF
