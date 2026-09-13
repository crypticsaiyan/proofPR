# syntax=docker/dockerfile:1.7

# Sandbox image for real_bugs case real-019 (scrapy), pinned to the same Python
# version (`3.8.3`) BugsInPy recorded as working for this historical commit.
# Generated from eval/fetch_real_bugs.py's sibling tooling; see
# docs/TARGET_REPO.md for how real_bugs images are built. Same isolation
# posture as sandbox/Dockerfile: no network at run time, so every dependency
# this case's test suite needs is installed here, at build time, instead.

FROM python:3.8.3-slim-buster

RUN groupadd --system --gid 10002 runner \
 && useradd --system --uid 10002 --gid runner --home /work --create-home runner

# Twisted, cryptography, and lxml (scrapy's own dependencies) build C
# extensions at install time; the base image ships neither a compiler
# nor the headers they compile against. Debian buster is EOL, so its
# live mirrors are gone; point at the frozen snapshot the base image
# itself already names (commented out) and stop checking staleness on
# a repository that will never update again.
RUN sed -i -e 's|http://deb.debian.org/debian |http://snapshot.debian.org/archive/debian/20200607T000000Z |' \
           -e 's|http://security.debian.org/debian-security |http://snapshot.debian.org/archive/debian-security/20200607T000000Z |' \
      /etc/apt/sources.list \
 && echo 'Acquire::Check-Valid-Until "false";' > /etc/apt/apt.conf.d/99no-check-valid \
 && apt-get update \
 && apt-get install --no-install-recommends -y \
      build-essential libssl-dev libffi-dev libxml2-dev libxslt1-dev zlib1g-dev \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt \
 && rm -rf /root/.cache /tmp/requirements.txt

WORKDIR /work
USER runner

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_INPUT=1

ENTRYPOINT ["python", "-m", "pytest"]
