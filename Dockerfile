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
