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
# To release a new build (docs/OPERATIONS.md, "Releasing a build"):
#   1. docker build --platform linux/amd64 --build-arg BUILD_SHA=$SHA -t $REPO:$SHA . && docker push
#      (or the build-platform-image workflow, which passes BUILD_SHA itself)
#   2. by PR: update container_image below to $SHA, and re-verify and update
#      the `deployed:` line (describe-services gives the running revision)
#   3. after it merges: python -m tools.sim.check, then tools/deploy.sh from an
#      up-to-date main; it refuses to plan from anywhere else
#   4. tofu -chdir=infra/terraform apply tfplan.bin, then check /health "build"
#
# deployed: relational-fluency-agent:54 carries 9b3dc3e, verified 2026-10-02
#   (describe-services, rollout COMPLETED; /health build 9b3dc3e); this tag
#   (ccd4678, main with #78) is built with BUILD_SHA and pushed,
#   awaiting the apply that registers 55.
container_image = "540586745717.dkr.ecr.us-east-1.amazonaws.com/relational-fluency/platform:ccd4678"

# Live voice model. gpt-realtime-2.1 since 2026-09-18: the Gemini live routes
# are being deprecated and the native-audio one is losing sessions to a gateway-side Vertex credentials error.
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
actor_model = "gpt-realtime-2.1"

# Retention: study_data_retention_days defaults to 0 = no expiration rule
# (PI decision, 2026-09-17: recordings are kept until deleted by hand). To
# expire recordings instead, set a positive number of days here, in a commit:
#
#   study_data_retention_days = ...

# survey_return_url has an empty default, which is NOT the same as being unset:
# the task definition sets SURVEY_RETURN_URL="" and server/app.py reads an empty
# string, so a participant who finishes the fourth encounter is simply not sent
# back to Qualtrics and the survey half of their response never completes.
# Nothing errors, on any surface. Set it before collection:
#
#   survey_return_url = "https://cornell.qualtrics.com/jfe/form/SV_...?..."

# Director (routing + stage directions). 3.1-flash-lite answers a routing call in
# about 1 s warm; 2.5-flash took 2.6 to 5.5 s in the same test (2026-09-18), and
# that call sits on the critical path of every group turn.
# nto.gemini-3.5-flash-lite since 2026-09-23 (was nto.gemini-3.1-flash-lite);
# see docs/model-benchmark-2026-09-23.md. Recorded per encounter as
# provenance.director_model.
director_model            = "nto.gemini-3.5-flash-lite"
analysis_db_allowed_cidrs = ["128.84.125.179/32"]

# The study-wide code a participant enters in Qualtrics after finishing all four
# encounters. Its VALUE lives in Secrets Manager (relational-fluency/survey-
# completion-code), never here: this repo is public. Put the value first, then
# set this to true (docs/OPERATIONS.md, "The survey completion code").
survey_completion_code_enabled = true
