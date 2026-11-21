# syntax=docker/dockerfile:1
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

# hatch-vcs derives the version from git history, so the build context must
# include .git (keep it out of .dockerignore for this image) and the image must
# have the git binary -- python:*-slim does not ship one, and without it the
# install below fails with "unable to detect version".
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

COPY . .

# Every extra that backs a built-in channel goes in the image, so a channels: entry the config enables never
# fails inside it with "install argus-scout[...]" (tests/test_dockerfile.py keeps this list complete).
RUN uv pip install --system --no-cache ".[server,snapcast,mask]"

# Mount a config directory at /config (scout.yaml, its .env, and the files it names). Relative paths in
# scout.yaml resolve against it, so the default store (scout.db) lands there too and survives restarts.
# Point SCOUT_CONFIG elsewhere to use another file. `serve` refuses to start without a valid config.
ENV SCOUT_CONFIG=/config/scout.yaml
VOLUME ["/config"]

EXPOSE 8400
# Bound off loopback inside the container: set server.read_auth: true unless only trusted hosts reach the port
# (serve warns when it is off).
CMD ["argus-scout", "serve", "--host", "0.0.0.0", "--port", "8400"]

