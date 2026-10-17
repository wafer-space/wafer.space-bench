"""The GitHub boundary: a Protocol, a `gh api` implementation, and its error types.

Every GitHub call the tooling makes goes through `GitHub`. Tests use the in-memory
`FakeGitHub` in tests/fakes.py, which implements the same Protocol.
"""

import base64
import json
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable
from urllib.parse import quote


class GitHubError(Exception):
    """A GitHub API call failed. The message holds only the command and gh's stderr."""


class NotFound(GitHubError):
    """HTTP 404: the repo, ref, commit or file does not exist (or is not visible)."""


class RateLimited(GitHubError):
    """GitHub's primary or secondary rate limit refused the call; back off and retry."""


@dataclass(frozen=True)
class RepoInfo:
    full_name: str  # current name; GitHub follows renames
    fork: bool
    network: str  # full name of the network root (the fork source), else full_name
    default_branch: str
    archived: bool
    size_kb: int


@runtime_checkable
class GitHub(Protocol):
    def get_repo(self, full_name: str) -> RepoInfo: ...
    def resolve_commit(self, full_name: str, sha: str) -> str | None: ...
    def fork(self, source_full_name: str, org: str, name: str) -> str:
        """Fork into `org` as `name`; returns the resulting repo's full_name.

        Like GitHub, if `org` already has a repo in the source's fork network this
        returns that existing repo and creates nothing, and a name taken by an unrelated
        repo gets a suffix (`name-1`). Callers must compare the result with the request.
        """
        ...

    def branches_visible(self, full_name: str) -> bool: ...
    def get_tag(self, full_name: str, tag: str) -> str | None:
        """The commit SHA the tag points at (annotated tags are peeled); None if absent."""
        ...

    def create_tag(self, full_name: str, tag: str, sha: str, message: str) -> None: ...
    def create_repo(self, org: str, name: str, description: str) -> None: ...
    def set_default_branch(self, full_name: str, branch: str) -> None: ...
    def list_repos(self, org: str) -> list[RepoInfo]: ...
    def list_branches(self, full_name: str) -> dict[str, str]: ...
    def set_ref(self, full_name: str, ref: str, sha: str) -> None: ...
    def delete_ref(self, full_name: str, ref: str) -> None: ...
    def get_file(self, full_name: str, path: str, ref: str) -> str | None: ...
    def dispatch(self, full_name: str, event_type: str) -> None: ...


def _q(s: str) -> str:
    """URL-quote a ref or path for the URL path (keeps `/`, escapes `#`, `%`, `?`)."""
    return quote(s, safe="/")


def _classify(cmd: list[str], stderr: str) -> GitHubError:
    msg = f"{' '.join(cmd[:5])}: {stderr}"
    low = stderr.lower()
    if (
        "too quickly" in low
        or "secondary rate limit" in low
        or "rate limit exceeded" in low  # primary limit: "API rate limit exceeded for ..."
        or "HTTP 429" in stderr
    ):
        return RateLimited(msg)
    if "HTTP 404" in stderr:
        return NotFound(msg)
    return GitHubError(msg)


def _repo_info(r: dict[str, Any]) -> RepoInfo:
    return RepoInfo(
        full_name=r["full_name"],
        fork=bool(r.get("fork", False)),
        network=(r.get("source") or {}).get("full_name", r["full_name"]),
        default_branch=r.get("default_branch", ""),
        archived=bool(r.get("archived", False)),
        size_kb=int(r.get("size", 0)),
    )


class GhCli:
    """`GitHub` implemented with the `gh api` command; `runner` is swappable for tests."""

    def __init__(self, runner: Callable[..., subprocess.CompletedProcess] = subprocess.run):
        self._run = runner

    def _api(
        self,
        endpoint: str,
        method: str = "GET",
        fields: dict[str, str | bool] | None = None,
        paginate: bool = False,
    ) -> Any:
        cmd = ["gh", "api", "-X", method, endpoint]
        for k, v in (fields or {}).items():
            if isinstance(v, bool):
                cmd += ["-F", f"{k}={str(v).lower()}"]
            else:
                cmd += ["-f", f"{k}={v}"]
        if paginate:
            cmd += ["--paginate", "--slurp"]
        p = self._run(cmd, capture_output=True, text=True)
        if p.returncode != 0:
            raise _classify(cmd, (p.stderr or "").strip())
        return json.loads(p.stdout) if p.stdout.strip() else {}

    def get_repo(self, full_name: str) -> RepoInfo:
        return _repo_info(self._api(f"repos/{full_name}"))

    def resolve_commit(self, full_name: str, sha: str) -> str | None:
        try:
            return self._api(f"repos/{full_name}/commits/{sha}")["sha"]
        except NotFound:
            return None
        except GitHubError as e:
            if "HTTP 422" in str(e) or "HTTP 409" in str(e):  # 409: empty repository
                return None
            raise

    def fork(self, source_full_name: str, org: str, name: str) -> str:
        return self._api(
            f"repos/{source_full_name}/forks",
            "POST",
            {"organization": org, "name": name, "default_branch_only": False},
        )["full_name"]

    def branches_visible(self, full_name: str) -> bool:
        """False while a new fork is not ready yet: no branches, 404 or 409 (empty repo)."""
        try:
            return bool(self._api(f"repos/{full_name}/branches?per_page=1"))
        except NotFound:
            return False
        except GitHubError as e:
            if "HTTP 409" in str(e):
                return False
            raise

    def get_tag(self, full_name: str, tag: str) -> str | None:
        try:
            obj = self._api(f"repos/{full_name}/git/ref/tags/{_q(tag)}")["object"]
        except NotFound:
            return None
        except GitHubError as e:
            if "HTTP 409" in str(e):  # empty repository
                return None
            raise
        if obj["type"] == "tag":  # annotated: peel the tag object to its commit
            obj = self._api(f"repos/{full_name}/git/tags/{obj['sha']}")["object"]
        return obj["sha"]

    def create_tag(self, full_name: str, tag: str, sha: str, message: str) -> None:
        obj = self._api(
            f"repos/{full_name}/git/tags",
            "POST",
            {"tag": tag, "message": message, "object": sha, "type": "commit"},
        )
        self._api(f"repos/{full_name}/git/refs", "POST", {"ref": f"refs/tags/{tag}", "sha": obj["sha"]})

    def create_repo(self, org: str, name: str, description: str) -> None:
        self._api(f"orgs/{org}/repos", "POST", {"name": name, "description": description, "private": False})

    def set_default_branch(self, full_name: str, branch: str) -> None:
        self._api(f"repos/{full_name}", "PATCH", {"default_branch": branch})

    def list_repos(self, org: str) -> list[RepoInfo]:
        """Every repo in `org`. The list endpoint carries no `source`, so `network` is
        each repo's own full_name here; call `get_repo` when the real network matters."""
        pages = self._api(f"orgs/{org}/repos?per_page=100", paginate=True)
        return [_repo_info(r) for page in pages for r in page]

    def list_branches(self, full_name: str) -> dict[str, str]:
        pages = self._api(f"repos/{full_name}/branches?per_page=100", paginate=True)
        return {b["name"]: b["commit"]["sha"] for page in pages for b in page}

    def set_ref(self, full_name: str, ref: str, sha: str) -> None:
        try:
            self._api(f"repos/{full_name}/git/refs/{_q(ref)}", "PATCH", {"sha": sha, "force": True})
        except GitHubError as e:
            if "Reference does not exist" not in str(e):
                raise
            self._api(f"repos/{full_name}/git/refs", "POST", {"ref": f"refs/{ref}", "sha": sha})

    def delete_ref(self, full_name: str, ref: str) -> None:
        self._api(f"repos/{full_name}/git/refs/{_q(ref)}", "DELETE")

    def get_file(self, full_name: str, path: str, ref: str) -> str | None:
        try:
            r = self._api(f"repos/{full_name}/contents/{_q(path)}?ref={_q(ref)}")
        except NotFound:
            return None
        return base64.b64decode(r["content"]).decode()

    def dispatch(self, full_name: str, event_type: str) -> None:
        self._api(f"repos/{full_name}/dispatches", "POST", {"event_type": event_type})
