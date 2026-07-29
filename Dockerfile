# Build:  docker build -t arcadia/notification-service:local .
#
# The context is this repository and nothing else: the shared plumbing is vendored under
# app/platform, so there is no sibling checkout to arrange.
#
# Two stages, because compiling asyncpg needs a toolchain that has no business shipping
# in the runtime image. The wheels are built once and copied.

FROM python:3.12-slim AS build

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /build

# Dependencies before source, so a code-only change reuses this layer.
COPY pyproject.toml ./
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --upgrade pip \
 && /opt/venv/bin/pip install .

# --- runtime -------------------------------------------------------------

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH"

# A dedicated unprivileged user. Running as root in a container buys nothing and turns a
# container escape into a host compromise.
RUN groupadd --system --gid 65532 arcadia \
 && useradd --system --uid 65532 --gid arcadia --no-create-home arcadia

COPY --from=build /opt/venv /opt/venv

WORKDIR /srv
COPY app ./app
COPY migrations ./migrations

USER 65532:65532

ARG VERSION=dev
ENV SERVICE_VERSION=${VERSION}

# 8086 serves REST plus /metrics, /livez and /readyz.
EXPOSE 8086

# Python is in the image, so the check needs no extra tooling. It calls /readyz rather
# than /livez: a container that cannot reach its database should report unhealthy, while
# /livez deliberately checks nothing. start-period covers migrations, which run at boot
# before the listener opens.
HEALTHCHECK --interval=15s --timeout=5s --start-period=40s --retries=3 \
  CMD ["python", "-c", "import os,urllib.request,sys; port=os.environ.get('HTTP_PORT','8086'); sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{port}/readyz', timeout=3).status==200 else 1)"]

# One worker per container. Scaling is the orchestrator's job, and a second worker inside
# the container would double the database pool and run five more Kafka consumers — each
# duplicate a member of the same group, so it would add rebalancing rather than throughput.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${HTTP_PORT:-8086} --no-access-log"]
