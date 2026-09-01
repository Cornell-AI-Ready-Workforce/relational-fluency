FROM python:3.12-slim

WORKDIR /app

# Install dependencies first (layer caching): resolve deps from pyproject alone
# so a source-only change doesn't invalidate the dependency install layer.
# KEEP THIS LIST IN SYNC with [project.dependencies] in agents/pyproject.toml —
# the `--no-deps` install below will NOT pull anything added there but missing here.
COPY pyproject.toml ./
RUN pip install --no-cache-dir \
    "fastapi>=0.115" \
    "uvicorn[standard]>=0.30" \
    "pydantic>=2.7" \
    "anthropic>=0.40" \
    "python-dotenv>=1.0"
# Then copy the source and install the package without re-resolving deps.
COPY src ./src
RUN pip install --no-cache-dir --no-deps .

# Non-root user
RUN useradd -m appuser && mkdir -p /app/logs && chown -R appuser /app
USER appuser

ENV PYTHONUNBUFFERED=1 \
    STEERING_LOG_DIR=/app/logs \
    PORT=8100

EXPOSE 8100

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request,os;urllib.request.urlopen(f'http://127.0.0.1:{os.getenv(\"PORT\",\"8100\")}/health')" || exit 1

CMD ["sh", "-c", "uvicorn agents.director_actor.server:app --host 0.0.0.0 --port ${PORT}"]
