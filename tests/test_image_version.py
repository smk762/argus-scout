"""The published image reports its tag's version, not whatever git inside the build makes of the tree.

v0.1.0's image reported ``0.1.1.dev0+g69730fbd9.d<date>``: ``.dockerignore`` left the tracked ``.github`` out of the
build context, so hatch-vcs saw a dirty tree. The release now passes the tag in as ``VERSION``, checks the built image
reports it before pushing, and the context leaves out nothing git tracks.
"""

from __future__ import annotations

import fnmatch
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent


def _image_steps() -> list[dict]:
    workflow = yaml.safe_load((ROOT / ".github" / "workflows" / "release.yml").read_text())
    return workflow["jobs"]["publish-image"]["steps"]


def _build_step() -> dict:
    [step] = [s for s in _image_steps() if str(s.get("uses", "")).startswith("docker/build-push-action")]
    return step


def test_release_passes_the_tag_version_as_a_build_arg():
    build = _build_step()["with"]
    args = dict(line.split("=", 1) for line in str(build.get("build-args", "")).split("\n") if "=" in line)
    # metadata-action's version is the tag without the v; on a cortex dispatch the tag is the newest release's.
    assert args.get("VERSION", "").strip() == "${{ steps.meta.outputs.version }}", build.get("build-args")
    meta = next(s for s in _image_steps() if s.get("id") == "meta")
    assert "value=${{ needs.release-tag.outputs.tag }}" in meta["with"]["tags"]


def version_arg_problem(dockerfile: str) -> str | None:
    """Why ``dockerfile`` would build the wheel without ``VERSION``, or None. An ARG is scoped to its stage: one
    before the first FROM, or in an earlier stage, is empty in the stage that builds unless declared there again."""
    lines = re.sub(r"\\\n", " ", dockerfile).splitlines()
    build = next((i for i, line in enumerate(lines) if re.match(r"\s*RUN\s", line) and "uv build" in line), None)
    if build is None:
        return "no RUN builds the wheel"
    stage = max((i for i in range(build) if re.match(r"\s*FROM\s", lines[i], re.IGNORECASE)), default=None)
    if stage is None:
        return "the wheel is built outside any stage"
    if not any(re.match(r"\s*ARG\s+VERSION(=|\s|$)", line) for line in lines[stage + 1 : build]):
        return "ARG VERSION is not declared in the stage that builds the wheel, before the build"
    if 'SETUPTOOLS_SCM_PRETEND_VERSION="$VERSION"' not in lines[build]:
        return "the build RUN must hand VERSION to hatch-vcs"
    return None


def test_dockerfile_builds_the_wheel_with_the_version_arg():
    assert version_arg_problem((ROOT / "Dockerfile").read_text()) is None


def test_version_arg_check_respects_stage_scope():
    build = 'RUN SETUPTOOLS_SCM_PRETEND_VERSION="$VERSION" uv build --wheel\n'
    assert version_arg_problem(f"FROM python AS app\nARG VERSION\n{build}") is None
    assert version_arg_problem(f"ARG VERSION\nFROM python\n{build}") is not None  # global scope only
    assert version_arg_problem(f"FROM a\nARG VERSION\nFROM b\n{build}") is not None  # an earlier stage's
    assert version_arg_problem(f"FROM python\n{build}ARG VERSION\n") is not None  # declared after the build
    assert version_arg_problem("FROM python\nARG VERSION\nRUN uv build --wheel\n") is not None


def test_release_checks_the_image_version_before_pushing():
    steps = _image_steps()
    build = steps.index(_build_step())
    assert not _build_step()["with"].get("push"), "the build must not push before the version check"
    assert _build_step()["with"].get("load") is True
    check = next(i for i, s in enumerate(steps) if "argus-scout version" in str(s.get("run", "")))
    push = next(i for i, s in enumerate(steps) if "docker push" in str(s.get("run", "")))
    assert build < check < push


def _ignored(path: str, patterns: list[str]) -> bool:
    """Whether a ``.dockerignore`` pattern drops ``path`` or one of its parent directories (no ``!`` support)."""
    parts = path.split("/")
    prefixes = ["/".join(parts[: i + 1]) for i in range(len(parts))]
    for pattern in patterns:
        if pattern.startswith("**/"):
            tail = pattern[3:]
            if any(fnmatch.fnmatchcase(part, tail) for part in parts) or any(
                fnmatch.fnmatchcase(p, pattern) or fnmatch.fnmatchcase(p, tail) for p in prefixes
            ):
                return True
        elif any(fnmatch.fnmatchcase(p, pattern) for p in prefixes):
            return True
    return False


def test_dockerignore_drops_no_tracked_file():
    """Git inside the build sees a dropped tracked file as deleted, so the tree reads dirty."""
    if not shutil.which("git") or not (ROOT / ".git").exists():
        pytest.skip("not a git checkout")
    lines = (ROOT / ".dockerignore").read_text().splitlines()
    patterns = [line.strip().lstrip("/") for line in lines if line.strip() and not line.startswith("#")]
    assert not any(p.startswith("!") for p in patterns), "teach _ignored about ! exceptions"
    tracked = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True).stdout
    dropped = [path for path in tracked.splitlines() if _ignored(path, patterns)]
    assert not dropped, f".dockerignore leaves tracked files out of the build context: {dropped}"


def test_dockerignore_check_catches_a_dropped_directory():
    assert _ignored(".github/workflows/release.yml", [".github"])
    assert _ignored("src/pkg/__pycache__/x.pyc", ["**/__pycache__"])
    assert not _ignored("src/pkg/build.py", ["build"])
