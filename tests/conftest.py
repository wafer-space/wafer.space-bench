"""Shared fixtures: real git repos in tmp_path, isolated from the user's git config."""

import os
import subprocess

import pytest

ID = ["-c", "user.name=Test", "-c", "user.email=test@example.com", "-c", "commit.gpgSign=false"]


@pytest.fixture(autouse=True)
def _isolated_git(monkeypatch):
    """No user/system git config, and only file:// (local) transports: tests stay offline."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_ALLOW_PROTOCOL", "file")
    # CI runners have no derivable identity; direct `git tag -a` / `git commit` calls need one.
    for who in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{who}_NAME", "test")
        monkeypatch.setenv(f"GIT_{who}_EMAIL", "test@example.invalid")


def _git(path, *args) -> str:
    p = subprocess.run(["git", *ID, *args], cwd=path, capture_output=True, text=True, check=True)
    return p.stdout.strip()


@pytest.fixture
def upstream(tmp_path):
    """Non-bare repo with branches main, feature/x and ttgf0p2 (3 commits).

    Returns (path, {branch: sha})."""
    path = tmp_path / "upstream"
    path.mkdir()
    _git(path, "init", "-q", "-b", "main")
    shas = {}
    (path / "a.txt").write_text("one\n")
    _git(path, "add", "a.txt")
    _git(path, "commit", "-q", "-m", "one")
    shas["main"] = _git(path, "rev-parse", "HEAD")
    for branch in ("feature/x", "ttgf0p2"):
        _git(path, "checkout", "-q", "-b", branch, "main")
        (path / "a.txt").write_text(f"{branch}\n")
        _git(path, "commit", "-q", "-am", branch)
        shas[branch] = _git(path, "rev-parse", "HEAD")
    _git(path, "checkout", "-q", "main")
    return path, shas


@pytest.fixture
def remote(tmp_path):
    """Empty bare repo; returns its file:// URL."""
    path = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(path)], check=True)
    return path.as_uri()
