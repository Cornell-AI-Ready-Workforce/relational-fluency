FROM python:3.12-slim

WORKDIR /app

# Install dependencies first (layer caching)
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir .

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
