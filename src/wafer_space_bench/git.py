"""A thin wrapper over the `git` command for one (usually bare) local repository.

Every call runs with GIT_TERMINAL_PROMPT=0, so a clone of a deleted or private repo fails
fast instead of asking for credentials. Commits and tags are made as IDENTITY.
"""

import os
import subprocess
from pathlib import Path

NAME = "wafer-space-bench"
EMAIL = "bench@wafer.space"
IDENTITY = f"{NAME} <{EMAIL}>"

_CONFIG = [
    "-c", f"user.name={NAME}",
    "-c", f"user.email={EMAIL}",
    "-c", "commit.gpgSign=false",
    "-c", "tag.gpgSign=false",
]  # fmt: skip


class GitError(RuntimeError):
    """A git command failed. The message holds the git subcommand and stderr (truncated)."""


def _run(args: list[str], cwd: Path | None = None, stdin: str | None = None, check: bool = True) -> str:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    p = subprocess.run(
        ["git", *_CONFIG, *args], cwd=cwd, input=stdin, capture_output=True, text=True, env=env, check=False
    )
    if check and p.returncode != 0:
        # Only the subcommand and its options: other arguments may be local paths.
        shown = " ".join([args[0], *(a for a in args[1:] if a.startswith("-"))])
        raise GitError(f"git {shown}: {(p.stderr or '').strip()}"[:300])
    return p.stdout if p.returncode == 0 else ""


class Git:
    def __init__(self, path: Path):
        self.path = Path(path)

    @classmethod
    def clone_bare(cls, url: str, dest: Path) -> "Git":
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        _run(["clone", "--bare", "--quiet", url, str(dest)])
        return cls(dest)

    @classmethod
    def init_bare(cls, path: Path) -> "Git":
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        _run(["init", "--bare", "--quiet", str(path)])
        return cls(path)

    def _git(self, *args: str, stdin: str | None = None) -> str:
        return _run(list(args), cwd=self.path, stdin=stdin)

    def rev_parse(self, ref: str) -> str | None:
        """The commit `ref` names (tags are peeled), or None if it is absent."""
        out = _run(["rev-parse", "--verify", "-q", f"{ref}^{{commit}}"], cwd=self.path, check=False)
        return out.strip() or None

    def update_ref(self, ref: str, target: str) -> None:
        self._git("update-ref", ref, target)

    def delete_ref(self, ref: str) -> None:
        self._git("update-ref", "-d", ref)

    def _refs(self, prefix: str) -> list[str]:
        out = self._git("for-each-ref", "--format=%(refname)", prefix)
        return [r.removeprefix(prefix) for r in out.splitlines()]

    def heads(self) -> list[str]:
        return self._refs("refs/heads/")

    def tags(self) -> list[str]:
        return self._refs("refs/tags/")

    def hash_blob(self, text: str) -> str:
        return self._git("hash-object", "-w", "--stdin", stdin=text).strip()

    def mktree(self, entries: dict[str, str]) -> str:
        """A tree from {path: blob sha}; paths may contain `/` (subtrees are built)."""
        blobs: dict[str, str] = {}
        subdirs: dict[str, dict[str, str]] = {}
        for path, sha in entries.items():
            head, sep, rest = path.partition("/")
            if sep:
                subdirs.setdefault(head, {})[rest] = sha
            else:
                blobs[head] = sha
        lines = [f"100644 blob {sha}\t{name}" for name, sha in blobs.items()]
        lines += [f"040000 tree {self.mktree(sub)}\t{name}" for name, sub in subdirs.items()]
        return self._git("mktree", stdin="".join(line + "\n" for line in lines)).strip()

    def commit_tree(self, tree: str, parents: list[str], message: str) -> str:
        args = ["commit-tree", tree]
        for p in parents:
            args += ["-p", p]
        return self._git(*args, stdin=message).strip()

    def create_tag(self, name: str, target: str, message: str) -> None:
        """An annotated tag `name` on commit `target`."""
        self._git("tag", "-a", name, "-m", message, target)

    def fetch(self, remote: str, refspecs: list[str], *, prune: bool = False) -> None:
        self._git("fetch", "--quiet", *(["--prune"] if prune else []), remote, *refspecs)

    def push(self, remote: str, refspecs: list[str]) -> None:
        self._git("push", "--quiet", remote, *refspecs)
