# Deployed release. Committed on purpose: the running version is then visible in
# git history, and a plain `tofu apply` cannot accidentally fall back to the
# non-existent :bootstrap placeholder.
#
# THE PIN IS THE TRUTH OR IT IS A ROLLBACK. This line read 3cf8496 while the
# live service was running cabc1dd — two releases newer. It happened a second
# time: cabc1dd was pinned here while production had moved on to df1ab83.
# That is not a stale comment; it is a loaded gun, because the documented
# procedure for almost
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
# deployed: relational-fluency-agent:0 UNVERIFIED AFTER THE 2026-09-15 MERGE.
#   Revision 0 does not exist. It is here so this line cannot be misread as a
#   verification, while still satisfying tests/test_terraform_persistence.py,
#   which requires the pin to name a revision. What is actually known:
#     - the tag below was moved cabc1dd -> df1ab83 during the merge of
#       origin/main (d1f3dfc, "Pin platform image df1ab83"), because df1ab83 is
#       the build production is running;
#     - the last time anyone ran describe-services and wrote the answer down was
#       relational-fluency-agent:38, 2026-09-12, against image tag cabc1dd — two
#       releases behind the tag below.
#   BEFORE THE NEXT `tofu apply`: run the handover describe-services command,
#   replace the 0 with the real running revision and today's date, and commit
#   that in the same change. Applying against an unverified pin is exactly the
#   rollback-with-a-success-message this block exists to prevent.
container_image = "540586745717.dkr.ecr.us-east-1.amazonaws.com/relational-fluency/platform:df1ab83"

# Live voice model. Native-audio route (nto.gemini-live-2.5-flash is being
# deprecated); verified for 1:1 and group rooms on 5a45420, 2026-09-08.
#
# THIS VALUE AND server/voice/realtime.py REALTIME_FAMILIES MOVE TOGETHER. The
# server resolves REALTIME_MODEL to a family row to decide input sample rate,
# autofire wait, turn-detection, whether text conversation items are accepted
# and whether room members get tools. Setting a model with no row of its own
# falls back to the plain-gemini row, which feeds native-audio 16 kHz input —
# the documented permanent-silence failure (session accepted, no transcription,
# no reply, no error). The native-audio row was added in the same merge that
# set this line; do not change one without the other.
#
# The fallback for the deprecation is `gpt-realtime-2.1`, which also has a row.
actor_model = "nto.gemini-live-2.5-flash-native-audio"

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
