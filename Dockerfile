# Build for Fargate with:
#
#     docker build --platform linux/amd64 -t $REPO:$SHA .
#
# `--platform linux/amd64` is not optional on an Apple Silicon Mac, which is the
# most likely researcher laptop. python:3.12-slim is a multi-arch manifest, so
# an arm64 build succeeds silently, pushes, and is then pinned by terraform —
# and the ECS task definition declares no runtime_platform, so Fargate defaults
# to X86_64/LINUX and the task dies on start with an exec format error. That
# surfaces as a 503 from the ALB and a crash loop, which reads like an
# application bug. Recovery is worse than the failure: ECR tags are IMMUTABLE
# and the tag is the short git SHA, so the same commit cannot be re-pushed
# correctly — someone has to invent a new commit to get a deployable tag.
#
# 3.12 is deliberate and is the version CI treats as production. The code also
# runs on 3.11 and on 3.13+ (see requirements.txt for the range and for what
# changes on 3.13), but the image does not float.
FROM python:3.12-slim

# System deps — none beyond what python:slim ships; everything in pure Python.

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOST=0.0.0.0 \
    PORT=8080 \
    DATA_DIR=/data

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Application code (scenarios + config travel with the image; data is mounted).
COPY server ./server
COPY static ./static
COPY scenarios ./scenarios
COPY config ./config

# The ESCI item bank. server/esci.py resolves it as
# <repo root>/studies/study1/qualtrics/esci_construct4_items.csv and _load()
# raises RuntimeError when it is missing — deliberately, because a half-loaded
# instrument is worse than none. That import happens lazily, inside the rating
# routes, so the image built fine and `import server.app` passed while every
# /rate, /api/raters, /api/ratings and /api/reliability request in the deployed
# service would 500 on the missing file. Phase 2 cannot run without this path in
# the image. (Keep .dockerignore's allowlist in step with this COPY.)
COPY studies/study1/qualtrics ./studies/study1/qualtrics

# The volume gets mounted here. mkdir is just for first-boot when there is no
# volume yet (local docker run, etc.).
RUN mkdir -p /data

# Drop root: run as an unprivileged user. A code-execution bug in the app then
# yields an ordinary uid, not root over /app and the mounted /data volume.
# Pin uid 1000 to match the EFS access point (infra/terraform/ecs.tf), which
# owns the mounted /data as 1000:1000 so this non-root user can write to it.
RUN useradd -m -u 1000 appuser && chown -R appuser /app /data
USER appuser

EXPOSE 8080

CMD ["python", "-m", "server.app"]
