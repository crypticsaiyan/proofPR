# syntax=docker/dockerfile:1.7
#
# Buildable with plain `docker build`; no buildx or cache mounts required, so the
# image can be reproduced on any host that has Docker at all.

# --- builder ------------------------------------------------------------------
FROM python:3.14-slim-bookworm AS builder

COPY --from=ghcr.io/astral-sh/uv:0.5.29 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dependencies first so the layer caches independently of source changes.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# README and LICENSE are referenced by the package metadata, so the project
# install needs them present.
COPY README.md LICENSE ./
COPY src/ ./src/
COPY config/ ./config/
RUN uv sync --frozen --no-dev

# --- runtime ------------------------------------------------------------------
FROM python:3.14-slim-bookworm AS runtime

LABEL org.opencontainers.image.title="proofpr" \
      org.opencontainers.image.description="Proof-carrying pull requests from chat bug reports" \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.source="https://github.com/OWNER/proofpr"

RUN groupadd --system --gid 10001 proofpr \
 && useradd --system --uid 10001 --gid proofpr --home /app --no-create-home proofpr

WORKDIR /app

COPY --from=builder --chown=proofpr:proofpr /app /app

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PROOFPR_DB_PATH=/data/proofpr.db

# The ledger is the only state, and it must outlive the container.
VOLUME ["/data"]

USER proofpr
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3).status == 200 else 1)"

ENTRYPOINT ["uvicorn", "proofpr.api:app", "--host", "0.0.0.0", "--port", "8080"]
