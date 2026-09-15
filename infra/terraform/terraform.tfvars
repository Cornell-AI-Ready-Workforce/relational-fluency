# Deployed release. Committed on purpose: the running version is then visible in
# git history, and a plain `tofu apply` cannot accidentally fall back to the
# non-existent :bootstrap placeholder.
#
# THE PIN IS THE TRUTH OR IT IS A ROLLBACK. This line read 3cf8496 while the
# live service was running cabc1dd — two releases newer. That is not a stale
# comment; it is a loaded gun, because the documented procedure for almost
# anything else in this directory ends in `tofu apply`. Run as written, against
# the value that was here, it rolls production back two releases, prints
# "Apply complete!", and leaves an ECS deployment that goes healthy. Every
# symptom after that is a missing feature in a build nobody believes is running.
#
# So: pinned to the truth, and the truth is now recorded beside it. The
# `deployed:` line below names the ECS task-definition revision this tag was
# checked against, and tests/test_terraform_persistence.py requires it to be
# present — change the tag and you have to say what you verified.
#
# To release a new build:
#   1. docker build --platform linux/amd64 -t $REPO:$SHA . && docker push
#   2. update container_image below to $SHA
#   3. re-verify and update the `deployed:` line (see the handover commands:
#      describe-services gives the running task definition revision)
#   4. commit, then apply
#
# deployed: relational-fluency-agent:38 verified 2026-09-12 (image tag cabc1dd)
container_image = "540586745717.dkr.ecr.us-east-1.amazonaws.com/relational-fluency/platform:cabc1dd"

# Two values this file deliberately does NOT set, so that an apply stops and
# asks rather than answering on a participant's behalf. Both are declared
# without a default (upstream_consent_version in ecs.tf,
# study_data_retention_days in storage_secrets.tf) and both have to come from
# the approved protocol, not from this repository:
#
#   upstream_consent_version  = "..."   # e.g. cornell-irb-2026-09-v3
#   study_data_retention_days = ...     # the number config/consent.yaml promises
#
# Fill them in here once the IRB answers, in the same commit — neither is a
# secret, and the running wave's consent version belonging to git history is a
# feature.

# survey_return_url has an empty default, which is NOT the same as being unset:
# the task definition sets SURVEY_RETURN_URL="" and server/app.py reads an empty
# string, so a participant who finishes the fourth encounter is simply not sent
# back to Qualtrics and the survey half of their response never completes.
# Nothing errors, on any surface. Set it before collection:
#
#   survey_return_url = "https://cornell.qualtrics.com/jfe/form/SV_...?..."
