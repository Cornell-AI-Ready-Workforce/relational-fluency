# Build for Fargate with:
#
#     docker build --platform linux/amd64 --build-arg BUILD_SHA=$SHA -t $REPO:$SHA .
#
# `--build-arg BUILD_SHA=$SHA`, with the same $SHA as the tag, is how the
# running image knows which commit it is (see the ARG below and
# server/build_info.py). Leave it out and the image still works, but /health,
# the participant page and every encounter record report build null, and the
# daily drift check (.github/workflows/prod-build-drift.yml) fails on it.
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

# Application code (scenarios travel with the image; data is mounted).
COPY server ./server
COPY static ./static
COPY scenarios ./scenarios

# The volume gets mounted here. mkdir is just for first-boot when there is no
# volume yet (local docker run, etc.).
RUN mkdir -p /data

# Drop root: run as an unprivileged user. A code-execution bug in the app then
# yields an ordinary uid, not root over /app and the mounted /data volume.
# Pin uid 1000 to match the EFS access point (infra/terraform/ecs.tf), which
# owns the mounted /data as 1000:1000 so this non-root user can write to it.
RUN useradd -m -u 1000 appuser && chown -R appuser /app /data
USER appuser

# The commit this image is built from, published on /health, on
# /api/run/config (the page's build tag) and in every encounter's provenance
# (server/build_info.py). Declared here, after the pip and COPY layers, because
# an ARG invalidates the cache from its first use onward and this value changes
# on every build. Empty when --build-arg is not given, which build_sha() reads
# as "unknown" rather than as a build.
ARG BUILD_SHA=""
ENV BUILD_SHA=$BUILD_SHA

EXPOSE 8080

CMD ["python", "-m", "server.app"]
