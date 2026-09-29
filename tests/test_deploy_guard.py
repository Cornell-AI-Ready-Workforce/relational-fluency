"""tools/deploy.sh refuses to plan from a state that cannot be trusted.

The incident: on 2026-09-24 a `tofu apply` from a checkout behind main, whose
terraform.tfvars still pinned ca77c2f, replaced 4798e64 in production and said
"Apply complete!". These tests run the real script against a throwaway git
repository (a bare "origin" and a clone of it) with `aws`, `curl` and `tofu`
replaced by stubs on PATH, and hold each refusal: dirty, behind, ahead, a pin
that is not on main or not in ECR, someone mid-encounter, a pin older than
production, and a plan that would roll the image back. And the one success:
current, clean, idle, which plans, prints the image change and the apply
command, and never applies.

The parsing half (tools/deploy_guard.py) is also tested directly.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from tools import deploy_guard as G

ROOT = Path(__file__).resolve().parent.parent
IMG = "540586745717.dkr.ecr.us-east-1.amazonaws.com/relational-fluency/platform"

bash_only = pytest.mark.skipif(
    sys.platform == "win32" or not shutil.which("bash") or not shutil.which("git"),
    reason="tools/deploy.sh is bash for macOS and Linux; Windows operators run it "
           "from Git Bash or WSL (docs/DEPLOY-AWS.md)")


# --- the parsing half ----------------------------------------------------------------

def _td(image):
    return json.dumps([{"name": "platform", "image": image, "essential": True}])


def _plan(before_tag, after_tag, extra=()):
    return {"format_version": "1.2", "resource_changes": [
        {"address": "aws_ecs_task_definition.agent", "type": "aws_ecs_task_definition",
         "change": {"actions": ["delete", "create"],
                    "before": {"container_definitions": _td(f"{IMG}:{before_tag}") if before_tag else None},
                    "after": {"container_definitions": _td(f"{IMG}:{after_tag}")}}},
        {"address": "aws_ecs_service.agent", "type": "aws_ecs_service",
         "change": {"actions": ["update"], "before": {}, "after": {}}},
        {"address": "aws_s3_bucket.study_data", "type": "aws_s3_bucket",
         "change": {"actions": ["no-op"]}},
        *extra,
    ]}


def test_the_plan_summary_counts_like_tofu_and_finds_the_image():
    s = G.summarize_plan(_plan("ca77c2f", "4798e64", extra=[
        {"address": "aws_iam_role.x", "type": "aws_iam_role", "change": {"actions": ["delete"]}},
        {"address": "aws_efs_file_system.y", "type": "aws_efs_file_system",
         "change": {"actions": ["create"]}}]))
    # replace = 1 add + 1 destroy, as tofu prints it; no-op is not a change
    assert s["counts"] == {"add": 2, "change": 1, "destroy": 2}
    assert [c["address"] for c in s["changes"]] == [
        "aws_ecs_task_definition.agent", "aws_ecs_service.agent", "aws_iam_role.x",
        "aws_efs_file_system.y"]
    assert s["images"] == [{"address": "aws_ecs_task_definition.agent", "container": "platform",
                            "before": f"{IMG}:ca77c2f", "after": f"{IMG}:4798e64"}]
    code, text = G.render_plan(s, f"{IMG}:4798e64")
    assert "Plan: 2 to add, 1 to change, 2 to destroy." in text
    assert "IMAGE CHANGE  ca77c2f  ->  4798e64" in text
    assert "DESTROYS 1 resource(s) outright: aws_iam_role.x" in text


def test_a_plan_that_leaves_the_image_alone_says_so():
    s = G.summarize_plan(_plan("4798e64", "4798e64"))
    assert s["images"] == []
    code, text = G.render_plan(s, f"{IMG}:4798e64")
    assert code == 0 and "IMAGE: unchanged" in text


def test_a_plan_deploying_anything_but_the_pin_is_refused():
    s = G.summarize_plan(_plan("4798e64", "ca77c2f"))
    code, text = G.render_plan(s, f"{IMG}:5093dcd")
    assert code == G.WRONG_IMAGE and "NOT THE PIN" in text


def test_colour_only_when_asked():
    s = G.summarize_plan(_plan("ca77c2f", "4798e64"))
    assert "\033[" not in G.render_plan(s, f"{IMG}:4798e64")[1]
    assert "\033[1;33m" in G.render_plan(s, f"{IMG}:4798e64", color=True)[1]


@pytest.mark.parametrize("url", [
    "https://github.com/Cornell-AI-Ready-Workforce/relational-fluency.git",
    "https://github.com/Cornell-AI-Ready-Workforce/relational-fluency",
    "https://github.com/cornell-ai-ready-workforce/Relational-Fluency/",
    "git@github.com:Cornell-AI-Ready-Workforce/relational-fluency.git",
    "ssh://git@github.com/Cornell-AI-Ready-Workforce/relational-fluency.git",
    "https://jl3369@github.com/Cornell-AI-Ready-Workforce/relational-fluency.git",
])
def test_the_canonical_repository_in_any_spelling(url):
    assert G.remote_names_repo(url, G.CANONICAL_REPO)


@pytest.mark.parametrize("url", [
    "https://github.com/jl3369/relational-fluency.git",                    # a fork
    "https://github.com/Cornell-AI-Ready-Workforce/relational-fluency-old",
    "https://gitlab.com/Cornell-AI-Ready-Workforce/relational-fluency.git",
    "/Users/someone/relational_fluency",                                   # a clone of a clone
    "",
])
def test_anything_else_is_not_it(url):
    assert not G.remote_names_repo(url, G.CANONICAL_REPO)


def test_a_plan_replacing_an_image_this_checkout_cannot_place_is_refused():
    """Review of 850b08e: 'unknown' printed 'cannot tell how these relate' and
    exited 0, and "production runs a commit I cannot place" is what a stale
    remote looks like."""
    s = G.summarize_plan(_plan("fedcba9", "4798e64"))
    code, text = G.render_plan(s, f"{IMG}:4798e64")
    assert code == G.UNKNOWN and "CANNOT PLACE fedcba9" in text and "NEWER" in text
    code, _ = G.render_plan(s, f"{IMG}:4798e64", allow_rollback=True)
    assert code == 0


@pytest.mark.parametrize("body,want", [
    ('{"active_sessions": 0, "build": "4798e64"}', (0, "4798e64")),
    ('{"active_sessions": 2, "build": null}', (2, "")),
    ('{"active_sessions": 0}', (0, "")),
])
def test_health_parse(body, want):
    assert G.parse_health(body) == want


@pytest.mark.parametrize("body", ['{"status": "ok"}', '[]', '{"active_sessions": "0"}'])
def test_a_health_body_without_an_integer_session_count_is_refused(body):
    with pytest.raises(ValueError):
        G.parse_health(body)


def test_sim_report_status(tmp_path):
    assert G.sim_report_status("4798e64", tmp_path) == "missing"
    (tmp_path / "4798e64.json").write_text(json.dumps({"build": "4798e64", "passed": True}),
                                           encoding="utf-8")
    assert G.sim_report_status("4798e64", tmp_path) == "pass"
    (tmp_path / "4798e64.json").write_text(json.dumps(
        {"build": "4798e64", "passed": False, "failures": ["S2A: phantom_turns 4 against limit 1"]}),
        encoding="utf-8")
    assert G.sim_report_status("4798e64", tmp_path) == "fail: S2A: phantom_turns 4 against limit 1"
    (tmp_path / "4798e64.json").write_text(json.dumps({"build": "ca77c2f", "passed": True}),
                                           encoding="utf-8")
    assert G.sim_report_status("4798e64", tmp_path).startswith("fail: 4798e64.json is a report for build")
    (tmp_path / "4798e64.json").write_text("{", encoding="utf-8")
    assert "unreadable" in G.sim_report_status("4798e64", tmp_path)


def test_a_dirty_report_is_never_the_builds_report(tmp_path):
    """tools/sim/check.py names a report from uncommitted code <sha>-dirty."""
    (tmp_path / "4798e64-dirty.json").write_text(
        json.dumps({"build": "4798e64-dirty", "passed": True}), encoding="utf-8")
    assert G.sim_report_status("4798e64", tmp_path) == "missing"


# --- the script, against a throwaway repository ---------------------------------------

STUBS = {
    "aws": """
        echo "aws $*" >> "$STUB_LOG"
        case " $* " in
          *" ecs describe-services "*)
            case "${STUB_ECS:-1 1}" in
              denied) echo "An error occurred (AccessDeniedException) when calling the DescribeServices operation" >&2; exit 254 ;;
              *) printf '%s\\t%s\\n' ${STUB_ECS:-1 1}; exit 0 ;;
            esac ;;
        esac
        case "${STUB_ECR:-present}" in
          present) echo "1790276635.436"; exit 0 ;;
          absent) echo "An error occurred (ImageNotFoundException) when calling the DescribeImages operation: The image does not exist" >&2; exit 254 ;;
          *) echo "An error occurred (AccessDeniedException) when calling the DescribeImages operation" >&2; exit 254 ;;
        esac
        """,
    "curl": """
        echo "curl $*" >> "$STUB_LOG"
        h=${STUB_HEALTH:-}
        if [ "$h" = down ]; then echo "curl: (7) Failed to connect" >&2; exit 7; fi
        [ -n "$h" ] || h='{"active_sessions": 0, "build": null}'
        printf '%s' "$h"
        """,
    "tofu": """
        echo "tofu $*" >> "$STUB_LOG"
        dir=.
        for a in "$@"; do case "$a" in -chdir=*) dir="${a#-chdir=}" ;; esac; done
        case " $* " in
          *" apply "*) echo "the stub refuses: deploy.sh must never apply" >&2; exit 99 ;;
          *" plan "*) [ "${STUB_PLAN_RC:-2}" = 1 ] || : > "$dir/tfplan.bin"; exit "${STUB_PLAN_RC:-2}" ;;
          *" show "*) cat "$STUB_PLAN_JSON" ;;
        esac
        """,
}


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                          text=True, encoding="utf-8").stdout.strip()


@pytest.fixture()
def world(tmp_path, monkeypatch):
    """origin (bare) and a clone of it: c0 initial, c1 the build, c2 pins c1."""
    for k, v in {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
                 "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
                 "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}.items():
        monkeypatch.setenv(k, v)
    origin, work, stubs = tmp_path / "origin.git", tmp_path / "work", tmp_path / "stubs"
    subprocess.run(["git", "init", "-q", "--bare", "--initial-branch=main", str(origin)], check=True)
    subprocess.run(["git", "clone", "-q", str(origin), str(work)], check=True, capture_output=True)
    _git(work, "checkout", "-q", "-b", "main")
    (work / "README").write_text("x\n", encoding="utf-8")
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "c0")
    c0 = _git(work, "rev-parse", "--short=7", "HEAD")
    (work / "tools").mkdir()
    for f in ("deploy.sh", "deploy_guard.py", "pinned_image.py"):
        shutil.copy2(ROOT / "tools" / f, work / "tools" / f)
    tf = work / "infra" / "terraform"
    tf.mkdir(parents=True)
    (tf / "terraform.tfvars").write_text(f'container_image = "{IMG}:bootstrap"\n', encoding="utf-8")
    (work / ".gitignore").write_text("infra/terraform/tfplan.bin\n", encoding="utf-8")
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "c1 build")
    c1 = _git(work, "rev-parse", "--short=7", "HEAD")
    (tf / "terraform.tfvars").write_text(f'container_image = "{IMG}:{c1}"\n', encoding="utf-8")
    _git(work, "commit", "-q", "-am", "c2 pin c1")
    c2 = _git(work, "rev-parse", "--short=7", "HEAD")
    _git(work, "push", "-q", "origin", "main")
    _git(work, "branch", "-q", "--set-upstream-to=origin/main")

    stubs.mkdir()
    for name, body in STUBS.items():
        p = stubs / name
        p.write_text("#!/usr/bin/env bash\n" + textwrap.dedent(body), encoding="utf-8")
        p.chmod(0o755)
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps(_plan(c0, c1)), encoding="utf-8")
    log = tmp_path / "stub.log"
    log.write_text("", encoding="utf-8")
    env = dict(os.environ, PATH=f"{stubs}{os.pathsep}{os.environ['PATH']}",
               PYTHON=sys.executable, STUB_LOG=str(log), STUB_PLAN_JSON=str(plan), NO_COLOR="1",
               # The bare origin stands in for the canonical GitHub repository.
               RF_DEPLOY_CANONICAL_REMOTE=str(origin))
    for k in ("TF_CLI_ARGS", "TF_CLI_ARGS_plan", "STUB_ECR", "STUB_ECS", "STUB_HEALTH",
              "STUB_PLAN_RC"):
        env.pop(k, None)

    class W:
        pass

    w = W()
    w.origin, w.work, w.env, w.log, w.plan, w.tmp = origin, work, env, log, plan, tmp_path
    w.c0, w.c1, w.c2 = c0, c1, c2

    def run(*args, **env_over):
        e = dict(w.env, **env_over)
        r = subprocess.run(["bash", "tools/deploy.sh", *args], cwd=work, env=e,
                           capture_output=True, text=True, encoding="utf-8", timeout=120)
        r.out = r.stdout + r.stderr
        r.calls = log.read_text(encoding="utf-8")
        return r

    w.run = run
    return w


@bash_only
def test_current_clean_and_idle_plans_and_prints_the_apply_command(world):
    r = world.run()
    assert r.returncode == 0, r.out
    assert f"IMAGE CHANGE  {world.c0}  ->  {world.c1}" in r.out
    assert "forward, 1 commit(s) newer" in r.out
    assert "tofu -chdir=infra/terraform apply tfplan.bin" in r.out
    assert "plan -input=false -detailed-exitcode -out=tfplan.bin" in r.calls
    assert " apply" not in r.calls.replace("show -json", "")
    assert (world.work / "infra" / "terraform" / "tfplan.bin").exists()
    # The ECR probe is read-only and names the pinned repository and tag.
    assert f"ecr describe-images --region us-east-1 --registry-id 540586745717 " \
           f"--repository-name relational-fluency/platform --image-ids imageTag={world.c1}" in r.calls


@bash_only
def test_a_dirty_tree_is_refused_before_anything_is_asked(world):
    (world.work / "README").write_text("edited\n", encoding="utf-8")
    r = world.run()
    assert r.returncode == 1 and "the working tree is not clean" in r.out
    assert r.calls == "", "nothing may be called on a dirty tree"


@bash_only
def test_an_untracked_file_is_dirty_but_an_untracked_sim_report_is_not(world):
    (world.work / "notes.txt").write_text("x\n", encoding="utf-8")
    assert "not clean" in world.run().out
    (world.work / "notes.txt").unlink()
    reports = world.work / "tools" / "sim" / "reports"
    reports.mkdir(parents=True)
    (reports / f"{world.c1}.json").write_text(
        json.dumps({"build": world.c1, "passed": True}), encoding="utf-8")
    r = world.run()
    assert r.returncode == 0, r.out
    assert f"the sim check passed for {world.c1}" in r.out


@bash_only
def test_a_checkout_behind_main_is_refused(world):
    """The 2026-09-24 case."""
    other = world.tmp / "other"
    subprocess.run(["git", "clone", "-q", str(world.origin), str(other)], check=True,
                   capture_output=True)
    (other / "README").write_text("newer\n", encoding="utf-8")
    _git(other, "commit", "-q", "-am", "c3 on main")
    _git(other, "push", "-q", "origin", "main")
    r = world.run()
    assert r.returncode == 1 and "1 commit(s) BEHIND origin/main" in r.out, r.out
    assert "tofu" not in r.calls


@bash_only
def test_a_checkout_ahead_of_main_is_refused(world):
    (world.work / "README").write_text("unreviewed\n", encoding="utf-8")
    _git(world.work, "commit", "-q", "-am", "local only")
    r = world.run()
    assert r.returncode == 1 and "AHEAD of origin/main" in r.out


@bash_only
def test_a_checkout_whose_origin_is_not_the_canonical_repository_is_refused(world):
    """A fork, a mirror or a clone of a local copy passes "HEAD is
    origin/main" against its own main, which can be behind GitHub's."""
    r = world.run(RF_DEPLOY_CANONICAL_REMOTE="")
    assert r.returncode == 1 and "not the canonical repository" in r.out, r.out
    assert "github.com/Cornell-AI-Ready-Workforce/relational-fluency" in r.out
    assert "tofu" not in r.calls and "aws" not in r.calls


@bash_only
def test_a_production_build_this_checkout_cannot_place_is_refused(world):
    """Production runs a newer main build a stale remote never fetched."""
    unknown = '{"active_sessions": 0, "build": "fedcba9"}'
    r = world.run(STUB_HEALTH=unknown)
    assert r.returncode == 1 and "does not know" in r.out, r.out
    assert "tofu" not in r.calls
    r = world.run("--allow-rollback", STUB_HEALTH=unknown)
    assert r.returncode == 0 and "WARNING: production runs fedcba9" in r.out, r.out


@bash_only
def test_a_plan_replacing_an_image_this_checkout_cannot_place_is_refused_and_deleted(world):
    world.plan.write_text(json.dumps(_plan("fedcba9", world.c1)), encoding="utf-8")
    r = world.run()
    assert r.returncode == 1 and "cannot place" in r.out, r.out
    assert not (world.work / "infra" / "terraform" / "tfplan.bin").exists()
    assert "apply tfplan.bin" not in r.stdout
    r = world.run("--allow-rollback")
    assert r.returncode == 0, r.out


@bash_only
def test_a_pin_that_is_not_on_main_is_refused(world):
    _git(world.work, "checkout", "-q", "-b", "side")
    (world.work / "README").write_text("side\n", encoding="utf-8")
    _git(world.work, "commit", "-q", "-am", "side build")
    side = _git(world.work, "rev-parse", "--short=7", "HEAD")
    _git(world.work, "checkout", "-q", "main")
    tfvars = world.work / "infra" / "terraform" / "terraform.tfvars"
    tfvars.write_text(f'container_image = "{IMG}:{side}"\n', encoding="utf-8")
    _git(world.work, "commit", "-q", "-am", "pin a branch build")
    _git(world.work, "push", "-q", "origin", "main")
    # The side commit exists in this clone, so the tag names a real commit,
    # but no commit on origin/main descends from it.
    r = world.run()
    assert r.returncode == 1 and "not reachable from origin/main" in r.out, r.out


@bash_only
@pytest.mark.parametrize("ecr,words", [("absent", "is not in ECR"),
                                       ("denied", "could not tell whether")])
def test_a_pin_ecr_cannot_confirm_is_refused(world, ecr, words):
    r = world.run(STUB_ECR=ecr)
    assert r.returncode == 1 and words in r.out
    assert "tofu" not in r.calls


@bash_only
def test_someone_mid_encounter_is_refused_unless_overridden(world):
    busy = '{"active_sessions": 2, "build": null}'
    r = world.run(STUB_HEALTH=busy)
    assert r.returncode == 1 and "2 encounter(s) are in progress" in r.out
    assert "tofu" not in r.calls
    r = world.run("--allow-active-sessions", STUB_HEALTH=busy)
    assert r.returncode == 0 and "WARNING: 2 encounter(s) in progress" in r.out


@bash_only
@pytest.mark.parametrize("ecs", ["2 2", "2 1", "1 2"])
def test_more_than_one_task_is_refused_because_health_counts_one(world, ecs):
    """active_sessions is one process's registry, and the ALB's lb_cookie
    stickiness sends a cookieless /health to one task at random. With two
    (desired_count "2 during collection"; Terraform ignores a manual
    scale-up, and a rollout runs old and new side by side), "0" can be the
    idle task while the other holds a live encounter."""
    r = world.run(STUB_ECS=ecs)
    assert r.returncode == 1 and "/health answers for one of them" in r.out, r.out
    assert "tofu" not in r.calls
    assert "ecs describe-services --region us-east-1 --cluster relational-fluency " \
           "--services platform" in r.calls, "the count is read, read-only, from ECS"
    r = world.run("--allow-active-sessions", STUB_ECS=ecs)
    assert r.returncode == 0, r.out
    assert "WARNING: the service runs 2 tasks" in r.out, r.out


@bash_only
def test_a_task_count_ecs_will_not_give_is_refused_unless_overridden(world):
    r = world.run(STUB_ECS="denied")
    assert r.returncode == 1 and "could not read how many tasks" in r.out, r.out
    r = world.run("--allow-active-sessions", STUB_ECS="denied")
    assert r.returncode == 0 and "WARNING: could not read how many tasks" in r.out, r.out


@bash_only
def test_one_task_is_what_health_covers(world):
    r = world.run(STUB_ECS="1 1")
    assert r.returncode == 0, r.out
    assert "one task running" in r.out


@bash_only
def test_unreachable_health_is_refused_unless_overridden(world):
    r = world.run(STUB_HEALTH="down")
    assert r.returncode == 1 and "could not read" in r.out
    r = world.run("--allow-active-sessions", STUB_HEALTH="down")
    assert r.returncode == 0 and "whether anyone is mid-encounter is unknown" in r.out


@bash_only
def test_a_pin_older_than_the_build_production_reports_is_refused(world):
    """Once images carry BUILD_SHA, the rollback is visible before the plan."""
    r = world.run(STUB_HEALTH=f'{{"active_sessions": 0, "build": "{world.c2}"}}')
    assert r.returncode == 1 and "OLDER than the" in r.out, r.out
    assert "tofu" not in r.calls


@bash_only
def test_a_plan_that_rolls_the_image_back_is_refused_and_deleted(world):
    """Today's case: production's /health names no build, so only the plan's
    before-image says what runs."""
    world.plan.write_text(json.dumps(_plan(world.c2, world.c1)), encoding="utf-8")
    r = world.run()
    assert r.returncode == 1 and "ROLLBACK" in r.out and "OLDER build" in r.out, r.out
    assert not (world.work / "infra" / "terraform" / "tfplan.bin").exists()
    assert "apply tfplan.bin" not in r.stdout
    r = world.run("--allow-rollback")
    assert r.returncode == 0 and "ROLLBACK" in r.out


@bash_only
@pytest.mark.parametrize("refusal", ["active_sessions", "dirty"])
def test_an_old_plan_does_not_survive_a_refused_run(world, refusal):
    """Monday's plan, not applied; Tuesday's run is refused and says nothing
    was planned. `apply tfplan.bin`, still in shell history and the runbook,
    would then apply Monday's plan past every check Tuesday's run failed."""
    r = world.run()
    assert r.returncode == 0, r.out
    plan = world.work / "infra" / "terraform" / "tfplan.bin"
    plan.write_text("MONDAY", encoding="utf-8")
    if refusal == "dirty":
        (world.work / "README").write_text("edited\n", encoding="utf-8")
        r = world.run()
    else:
        r = world.run(STUB_HEALTH='{"active_sessions": 2, "build": null}')
    assert r.returncode == 1 and "Nothing was planned" in r.out, r.out
    assert not plan.exists(), "a refused run left the previous plan ready to apply"


@bash_only
def test_no_sim_report_warns_but_does_not_refuse(world):
    r = world.run()
    assert r.returncode == 0
    assert f"WARNING: no sim report for {world.c1}" in r.out


@bash_only
def test_an_override_of_the_pin_in_the_shell_is_refused(world):
    r = world.run(TF_CLI_ARGS_plan=f"-var container_image={IMG}:{world.c0}")
    assert r.returncode == 1 and "TF_CLI_ARGS" in r.out
    (world.work / "infra" / "terraform" / "x.auto.tfvars").write_text("", encoding="utf-8")
    r = world.run()
    assert r.returncode == 1


@bash_only
def test_no_changes_is_a_clean_exit_with_nothing_to_apply(world):
    r = world.run(STUB_PLAN_RC="0")
    assert r.returncode == 0 and "Nothing to apply" in r.out
    assert "apply tfplan.bin" not in r.out
    assert not (world.work / "infra" / "terraform" / "tfplan.bin").exists()


@bash_only
def test_a_failed_plan_is_refused(world):
    r = world.run(STUB_PLAN_RC="1")
    assert r.returncode == 1 and "tofu plan failed" in r.out


def test_the_script_is_committed_executable_and_lf():
    p = ROOT / "tools" / "deploy.sh"
    raw = p.read_bytes()
    assert raw.startswith(b"#!/usr/bin/env bash\n") and b"\r" not in raw
    if shutil.which("git"):
        mode = subprocess.run(["git", "ls-files", "-s", "tools/deploy.sh"], cwd=ROOT,
                              capture_output=True, text=True).stdout.split(" ")[0]
        assert mode in ("", "100755"), f"tools/deploy.sh is committed as {mode}, not executable"


def test_the_script_never_applies():
    """The apply is a separate, deliberate step a person types."""
    code = [ln for ln in (ROOT / "tools" / "deploy.sh").read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.lstrip().startswith("#")]
    runs = [ln for ln in code if "tofu" in ln and "apply" in ln]
    assert runs and all(ln.strip().startswith(("tofu -chdir=$TF_DIR apply $PLAN_FILE",))
                        for ln in runs), runs
