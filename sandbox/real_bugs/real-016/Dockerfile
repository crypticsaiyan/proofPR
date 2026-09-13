# syntax=docker/dockerfile:1.7

# Sandbox image for real_bugs case real-016 (fastapi), pinned to the same Python
# version (`3.8.3`) BugsInPy recorded as working for this historical commit.
# Generated from eval/fetch_real_bugs.py's sibling tooling; see
# docs/TARGET_REPO.md for how real_bugs images are built. Same isolation
# posture as sandbox/Dockerfile: no network at run time, so every dependency
# this case's test suite needs is installed here, at build time, instead.

FROM python:3.8.3-slim-buster

RUN groupadd --system --gid 10002 runner \
 && useradd --system --uid 10002 --gid runner --home /work --create-home runner

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt \
 && rm -rf /root/.cache /tmp/requirements.txt

WORKDIR /work
USER runner

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_INPUT=1

ENTRYPOINT ["python", "-m", "pytest"]
