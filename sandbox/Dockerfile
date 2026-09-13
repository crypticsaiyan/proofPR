# syntax=docker/dockerfile:1.7

# Pinned runner image for model-written tests and patches.
#
# This image never receives a credential. Containers run with --network none,
# a read-only root filesystem, all capabilities dropped, and no new privileges.
# See AGENTS.md section 5.

# Pinned by digest in M1 so sandbox results are reproducible. Resolve with:
#   docker buildx imagetools inspect python:3.12-slim-bookworm
# then replace the tag below with image@sha256:<digest>.
FROM python:3.14-slim-bookworm

RUN groupadd --system --gid 10002 runner \
 && useradd --system --uid 10002 --gid runner --home /work --create-home runner

# Test dependencies are installed at image build time so the container itself
# never needs network access.
RUN pip install --no-cache-dir \
      "pytest==8.3.4" \
      "pytest-timeout==2.3.1" \
      "coverage==7.6.10" \
 && rm -rf /root/.cache

WORKDIR /work
USER runner

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_INPUT=1

ENTRYPOINT ["python", "-m", "pytest"]
