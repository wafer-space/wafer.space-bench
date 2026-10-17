"""In-memory stand-in for GitHub, implementing `wafer_space_bench.github.GitHub`.

Models what the tooling relies on:
- Networks: every repo belongs to one network (named by its root repo), and each network
  has a set of commits. Any repo can reference any commit in its network, and only those.
- One repo per network per org: a second fork into the same org returns the existing
  fork; a name taken by an unrelated repo gets a `-1` suffix.
- Renames: the old name keeps working and `get_repo` reports the new name.
- Deleted repos raise `NotFound`.
- Rate limits on creations (`rate_limit_next`) and slow fork readiness (`ready_after`).

Inspection state is keyed by each repo's current full name.
"""

from collections.abc import Iterable

from wafer_space_bench.github import GitHubError, NotFound, RateLimited, RepoInfo


class FakeGitHub:
    def __init__(self) -> None:
        self.repos: dict[str, RepoInfo] = {}
        self.tags: dict[str, dict[str, str]] = {}
        self.branches: dict[str, dict[str, str]] = {}
        self.files: dict[tuple[str, str, str], str] = {}
        self.default_branch: dict[str, str] = {}
        self.dispatches: list[tuple[str, str]] = []
        self.calls: list[tuple[str, tuple]] = []
        self.networks: dict[str, set[str]] = {}  # network root -> commits
        self._names: dict[str, str] = {}  # lowercased name (old or current) -> current name
        self._deleted: set[str] = set()
        self._rate_limited = 0
        self._ready_after = 0
        self._not_ready: dict[str, int] = {}

    # --- test setup ---------------------------------------------------------

    def add_upstream(
        self,
        full: str,
        network: str | None = None,
        commits: Iterable[str] = (),
        branches: dict[str, str] | None = None,
        deleted: bool = False,
        renamed_to: str | None = None,
        archived: bool = False,
    ) -> None:
        """Add a repo outside the bench org. `network` names an existing (or new) network
        root, making this repo a fork in it; None makes it the root of its own network."""
        name = renamed_to or full
        net = network or name
        branches = dict(branches or {})
        self.networks.setdefault(net, set()).update([*commits, *branches.values()])
        self._names[full.lower()] = name
        self._names[name.lower()] = name
        default = "main" if "main" in branches or not branches else next(iter(branches))
        self._put(name, net, fork=net != name, default=default, archived=archived, branches=branches)
        if deleted:
            self._deleted.add(name)

    def add_commits(self, full: str, shas: Iterable[str]) -> None:
        """Tell the fake that commits were pushed to `full` (by real git, elsewhere)."""
        self.networks[self.repos[self._resolve(full)].network].update(shas)

    def rate_limit_next(self, n: int) -> None:
        """The next `n` creations (fork, create_repo) raise `RateLimited`."""
        self._rate_limited = n

    def ready_after(self, n: int) -> None:
        """`branches_visible` is False for the first `n` polls of each later fork."""
        self._ready_after = n

    # --- internals ----------------------------------------------------------

    def _put(self, name, net, *, fork, default, archived=False, branches=None) -> None:
        self.repos[name] = RepoInfo(name, fork, net, default, archived, 0)
        self._names[name.lower()] = name
        self.branches[name] = dict(branches or {})
        self.tags[name] = {}
        self.default_branch[name] = default

    def _resolve(self, full: str) -> str:
        name = self._names.get(full.lower())
        if name is None or name in self._deleted:
            raise NotFound(f"repos/{full}: Not Found (HTTP 404)")
        return name

    def _record(self, method: str, *args) -> None:
        self.calls.append((method, args))

    def _creation(self) -> None:
        if self._rate_limited > 0:
            self._rate_limited -= 1
            raise RateLimited("was submitted too quickly (HTTP 403)")

    def _check_sha(self, name: str, sha: str) -> None:
        if sha not in self.networks[self.repos[name].network]:
            raise GitHubError(f"repos/{name}: Object does not exist (HTTP 422)")

    def _split_ref(self, ref: str) -> tuple[str, str]:
        kind, _, short = ref.partition("/")
        if kind not in ("heads", "tags") or not short:
            raise GitHubError(f"unsupported ref {ref!r} (fake models heads/ and tags/ only)")
        return kind, short

    def _refs(self, name: str, kind: str) -> dict[str, str]:
        return self.branches[name] if kind == "heads" else self.tags[name]

    # --- GitHub Protocol ----------------------------------------------------

    def get_repo(self, full_name: str) -> RepoInfo:
        return self.repos[self._resolve(full_name)]

    def resolve_commit(self, full_name: str, sha: str) -> str | None:
        name = self._resolve(full_name)
        hits = [c for c in self.networks[self.repos[name].network] if c.startswith(sha.lower())]
        return hits[0] if len(hits) == 1 else None

    def fork(self, source_full_name: str, org: str, name: str) -> str:
        self._record("fork", source_full_name, org, name)
        src = self._resolve(source_full_name)
        self._creation()
        net = self.repos[src].network
        for n, r in self.repos.items():  # GitHub returns the org's existing fork in the network
            if r.network == net and n.split("/")[0].lower() == org.lower() and n not in self._deleted:
                return n
        full, i = f"{org}/{name}", 0
        while full.lower() in self._names:  # name taken by an unrelated repo: GitHub adds -1, -2, ...
            i += 1
            full = f"{org}/{name}-{i}"
        self._put(full, net, fork=True, default=self.repos[src].default_branch, branches=self.branches[src])
        self._not_ready[full] = self._ready_after
        return full

    def branches_visible(self, full_name: str) -> bool:
        name = self._resolve(full_name)
        if self._not_ready.get(name, 0) > 0:
            self._not_ready[name] -= 1
            return False
        return bool(self.branches[name])

    def get_tag(self, full_name: str, tag: str) -> str | None:
        return self.tags[self._resolve(full_name)].get(tag)

    def create_tag(self, full_name: str, tag: str, sha: str, message: str) -> None:
        self._record("create_tag", full_name, tag, sha, message)
        name = self._resolve(full_name)
        self._check_sha(name, sha)
        if tag in self.tags[name]:
            raise GitHubError(f"repos/{name}/git/refs: Reference already exists (HTTP 422)")
        self.tags[name][tag] = sha

    def create_repo(self, org: str, name: str, description: str) -> None:
        self._record("create_repo", org, name, description)
        self._creation()
        full = f"{org}/{name}"
        if full.lower() in self._names:
            raise GitHubError(f"orgs/{org}/repos: name already exists on this account (HTTP 422)")
        self.networks[full] = set()
        self._put(full, full, fork=False, default="main")

    def set_default_branch(self, full_name: str, branch: str) -> None:
        self._record("set_default_branch", full_name, branch)
        name = self._resolve(full_name)
        if branch not in self.branches[name]:
            raise GitHubError(f"repos/{name}: Validation Failed (HTTP 422)")
        r = self.repos[name]
        self.repos[name] = RepoInfo(r.full_name, r.fork, r.network, branch, r.archived, r.size_kb)
        self.default_branch[name] = branch

    def list_repos(self, org: str) -> list[RepoInfo]:
        """Like the real list endpoint, `network` is each repo's own name here."""
        return [
            RepoInfo(r.full_name, r.fork, r.full_name, r.default_branch, r.archived, r.size_kb)
            for n, r in self.repos.items()
            if n.split("/")[0].lower() == org.lower() and n not in self._deleted
        ]

    def list_branches(self, full_name: str) -> dict[str, str]:
        return dict(self.branches[self._resolve(full_name)])

    def set_ref(self, full_name: str, ref: str, sha: str) -> None:
        self._record("set_ref", full_name, ref, sha)
        name = self._resolve(full_name)
        kind, short = self._split_ref(ref)
        self._check_sha(name, sha)
        self._refs(name, kind)[short] = sha

    def delete_ref(self, full_name: str, ref: str) -> None:
        self._record("delete_ref", full_name, ref)
        name = self._resolve(full_name)
        kind, short = self._split_ref(ref)
        if kind == "heads" and short == self.default_branch[name]:
            raise GitHubError(f"repos/{name}/git/refs/{ref}: cannot delete the default branch (HTTP 422)")
        refs = self._refs(name, kind)
        if short not in refs:
            raise GitHubError(f"repos/{name}/git/refs/{ref}: Reference does not exist (HTTP 422)")
        del refs[short]

    def get_file(self, full_name: str, path: str, ref: str) -> str | None:
        return self.files.get((self._resolve(full_name), ref, path))

    def dispatch(self, full_name: str, event_type: str) -> None:
        self._record("dispatch", full_name, event_type)
        self.dispatches.append((self._resolve(full_name), event_type))
