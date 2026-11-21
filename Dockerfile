# syntax=docker/dockerfile:1
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

# hatch-vcs derives the version from git history, so the build context must
# include .git (keep it out of .dockerignore for this image) and the image must
# have the git binary -- python:*-slim does not ship one, and without it the
# install below fails with "unable to detect version". Nothing tracked may be
# left out of the context either (.dockerignore): git would see it deleted, call
# the tree dirty and version the image 0.1.1.dev0+g<sha>.d<date> on the v0.1.0 tag.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

COPY . .

# Every extra that backs a built-in channel goes in the image, so a channels: entry the config enables never
# fails inside it with "install argus-scout[...]" (tests/test_dockerfile.py keeps this list complete).
#
# The release passes the tag (without the v) as VERSION, so the image reports it whatever the tree looks like; a
# local `docker build` leaves it empty and git decides. The variable is set only for building this wheel: hatch-vcs
# ignores the per-package SETUPTOOLS_SCM_PRETEND_VERSION_FOR_ARGUS_SCOUT, and the generic one would also version any
# dependency built from source (tests/test_image_version.py keeps this wired to the release workflow).
ARG VERSION=""
RUN if [ -n "$VERSION" ]; then export SETUPTOOLS_SCM_PRETEND_VERSION="$VERSION"; fi \
    && uv build --wheel --out-dir /tmp/dist \
    && uv pip install --system --no-cache "$(ls /tmp/dist/argus_scout-*.whl)[server,snapcast,mask]" \
    && rm -rf /tmp/dist

# Mount a config directory at /config (scout.yaml, its .env, and the files it names). Relative paths in
# scout.yaml resolve against it, so the default store (scout.db) lands there too and survives restarts.
# Point SCOUT_CONFIG elsewhere to use another file. `serve` refuses to start without a valid config.
ENV SCOUT_CONFIG=/config/scout.yaml
VOLUME ["/config"]

EXPOSE 8400
# Bound off loopback inside the container: set server.read_auth: true unless only trusted hosts reach the port
# (serve warns when it is off).
CMD ["argus-scout", "serve", "--host", "0.0.0.0", "--port", "8400"]

