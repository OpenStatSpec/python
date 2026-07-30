"""Fail closed unless the requested release tag names HEAD and the package version."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tomllib


def _git(*arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if completed.returncode:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise RuntimeError(detail or f"git {' '.join(arguments)} failed")
    return completed.stdout.strip()


def verify_release_ref() -> None:
    release_tag = os.environ.get("RELEASE_TAG", "")
    version = str(
        tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    )
    expected_tag = f"v{version}"
    if release_tag != expected_tag:
        raise RuntimeError(
            f"release tag {release_tag!r} does not match package version {version!r}"
        )

    tag_ref = f"refs/tags/{release_tag}"
    exists = subprocess.run(
        ["git", "show-ref", "--verify", "--quiet", tag_ref],
        check=False,
    )
    if exists.returncode:
        raise RuntimeError(f"release tag ref {tag_ref!r} does not exist")

    tag_commit = _git("rev-parse", "--verify", f"{tag_ref}^{{commit}}")
    head_commit = _git("rev-parse", "--verify", "HEAD^{commit}")
    if tag_commit != head_commit:
        raise RuntimeError(
            f"release tag {release_tag!r} resolves to {tag_commit}, not HEAD {head_commit}"
        )


def main() -> int:
    try:
        verify_release_ref()
    except (KeyError, OSError, RuntimeError, tomllib.TOMLDecodeError) as error:
        print(f"release ref verification failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
