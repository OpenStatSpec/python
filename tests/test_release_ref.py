from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys


VERIFY_RELEASE_REF = Path(__file__).parents[1] / ".github" / "verify_release_ref.py"


def _git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        stdout=subprocess.PIPE,
        text=True,
    )
    return completed.stdout.strip()


def _repository(tmp_path: Path) -> Path:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init", "--quiet")
    _git(repository, "config", "user.name", "Release Test")
    _git(repository, "config", "user.email", "release@example.invalid")
    (repository / "pyproject.toml").write_text(
        '[project]\nname = "openstatspec"\nversion = "0.2.0"\n',
        encoding="utf-8",
    )
    _git(repository, "add", "pyproject.toml")
    _git(repository, "commit", "--quiet", "-m", "fixture")
    return repository


def _verify(repository: Path, release_tag: str) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["RELEASE_TAG"] = release_tag
    return subprocess.run(
        [sys.executable, str(VERIFY_RELEASE_REF)],
        cwd=repository,
        env=environment,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def test_release_ref_accepts_annotated_version_tag_at_head(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _git(repository, "tag", "-a", "v0.2.0", "-m", "release")

    assert _verify(repository, "v0.2.0").returncode == 0


def test_release_ref_rejects_branch_with_version_name(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _git(repository, "branch", "v0.2.0")

    result = _verify(repository, "v0.2.0")

    assert result.returncode == 1
    assert "does not exist" in result.stderr


def test_release_ref_rejects_commit_sha_input(tmp_path: Path) -> None:
    repository = _repository(tmp_path)

    result = _verify(repository, _git(repository, "rev-parse", "HEAD"))

    assert result.returncode == 1
    assert "does not match package version" in result.stderr


def test_release_ref_rejects_missing_version_tag(tmp_path: Path) -> None:
    repository = _repository(tmp_path)

    result = _verify(repository, "v0.2.0")

    assert result.returncode == 1
    assert "does not exist" in result.stderr


def test_release_ref_rejects_tag_targeting_another_commit(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _git(repository, "tag", "-a", "v0.2.0", "-m", "release")
    (repository / "README.md").write_text("later commit\n", encoding="utf-8")
    _git(repository, "add", "README.md")
    _git(repository, "commit", "--quiet", "-m", "later")

    result = _verify(repository, "v0.2.0")

    assert result.returncode == 1
    assert "not HEAD" in result.stderr
