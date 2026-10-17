"""A thin wrapper over the `git` command for one (usually bare) local repository.

Every call runs with GIT_TERMINAL_PROMPT=0, so a clone of a deleted or private repo fails
fast instead of asking for credentials. Commits and tags are made as IDENTITY.
"""

import os
import subprocess
from pathlib import Path
from urllib.parse import unquote, urlparse

NAME = "wafer.space Robotic Helper"
EMAIL = "robot@wafer.space"
IDENTITY = f"{NAME} <{EMAIL}>"

_CONFIG = [
    "-c", f"user.name={NAME}",
    "-c", f"user.email={EMAIL}",
    "-c", "commit.gpgSign=false",
    "-c", "tag.gpgSign=false",
]  # fmt: skip


class GitError(RuntimeError):
    """A git command failed. The message holds the git subcommand and stderr (truncated)."""


def git_env() -> dict[str, str]:
    """The environment for every git call: never prompt, and always commit as IDENTITY."""
    env = {
        **os.environ,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_AUTHOR_NAME": NAME,
        "GIT_AUTHOR_EMAIL": EMAIL,
        "GIT_COMMITTER_NAME": NAME,
        "GIT_COMMITTER_EMAIL": EMAIL,
    }
    env.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes")
    return env


def _redactions(args: list[str], cwd: Path | None) -> list[str]:
    """Non-option arguments containing `/` (paths, URLs, refs) and the repo path, longest first; for a
    file:// URL also its plain path, which is how git reports it."""
    out = [a for a in args[1:] if "/" in a and not a.startswith("-")]
    out += [unquote(urlparse(a).path) for a in out if a.startswith("file://")]
    if cwd is not None:
        out += [str(cwd), str(Path(cwd).resolve())]
    return sorted({a for a in out if a}, key=len, reverse=True)


def _run(args: list[str], cwd: Path | None = None, stdin: str | None = None, check: bool = True) -> str:
    p = subprocess.run(
        ["git", *_CONFIG, *args],
        cwd=cwd,
        input=stdin,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        env=git_env(),
        check=False,
    )
    if check and p.returncode != 0:
        # Only the subcommand and its options; arguments (local paths, URLs) are redacted
        # from stderr too, since error text can end up in shared event logs.
        shown = " ".join([args[0], *(a for a in args[1:] if a.startswith("-"))])
        err = (p.stderr or "").strip()
        for a in _redactions(args, cwd):
            err = err.replace(a, "<arg>")
        raise GitError(f"git {shown}: {err}"[:300])
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

    def show(self, ref: str, path: str) -> str | None:
        """The text of file `path` at `ref`, or None if either is absent."""
        out = _run(["show", f"{ref}:{path}"], cwd=self.path, check=False)
        return out if out else None

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
        """A tree from {path: blob sha}. Every entry is a regular file (mode 100644): no
        executables, symlinks or submodules. Paths may contain `/`; subtrees are built."""
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

    @staticmethod
    def ls_remote(url: str) -> dict[str, str]:
        """{ref: sha} of the heads and tags at `url`, downloading no objects. Annotated
        tags map to the commit they peel to."""
        out = _run(["ls-remote", "--heads", "--tags", url])
        refs: dict[str, str] = {}
        peeled: dict[str, str] = {}
        for line in out.splitlines():
            sha, _, ref = line.partition("\t")
            if ref.endswith("^{}"):
                peeled[ref.removesuffix("^{}")] = sha
            elif ref:
                refs[ref] = sha
        return {**refs, **{r: s for r, s in peeled.items() if r in refs}}

    def fetch(
        self,
        remote: str,
        refspecs: list[str],
        *,
        prune: bool = False,
        no_tags: bool = False,
        depth: int | None = None,
    ) -> None:
        opts = ["--prune"] if prune else []
        opts += ["--no-tags"] if no_tags else []
        opts += [f"--depth={depth}"] if depth else []
        self._git("fetch", "--quiet", *opts, remote, *refspecs)

    def push(self, remote: str, refspecs: list[str], *, atomic: bool = False) -> None:
        """Push `refspecs`; `atomic` updates either every ref or none."""
        self._git("push", "--quiet", *(["--atomic"] if atomic else []), remote, *refspecs)
