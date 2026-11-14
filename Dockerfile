# Two stages: the first builds wheels (it needs a compiler), the second only installs them.
FROM python:3.13-slim AS build
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /src
RUN apt-get update && apt-get install -y --no-install-recommends build-essential && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml README.md ./
COPY callagent ./callagent
# The PostgreSQL driver is part of the image: PostgreSQL is what a deployment runs on.
RUN pip wheel --wheel-dir /wheels ".[postgres]"

FROM python:3.13-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DATA_DIR=/app/data
# pg_dump and pg_restore, for `callagent db backup` against PostgreSQL. curl, for the health check.
RUN apt-get update && apt-get install -y --no-install-recommends postgresql-client curl && rm -rf /var/lib/apt/lists/* \
    && useradd --system --create-home --uid 10001 callagent \
    && mkdir -p /app/data && chown -R callagent:callagent /app
COPY --from=build /wheels /wheels
RUN pip install --no-index --find-links /wheels "callagent[postgres]" && rm -rf /wheels
WORKDIR /app
USER callagent
VOLUME ["/app/data"]
EXPOSE 8000
# Ready means: the database answers, its schema is this release's, the worker runs.
HEALTHCHECK --interval=15s --timeout=6s --start-period=40s --retries=3 CMD curl -fsS http://127.0.0.1:8000/health/ready || exit 1
# The server waits for calls in progress when it is told to stop (SHUTDOWN_DRAIN_SECONDS);
# give it longer than that before killing it: `docker stop -t 200`, or stop_grace_period below.
STOPSIGNAL SIGTERM
CMD ["callagent", "serve", "--host", "0.0.0.0", "--port", "8000"]
