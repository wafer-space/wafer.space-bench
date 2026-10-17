"""In-memory stand-in for GitHub, implementing `wafer_space_bench.github.GitHub`.

Models what the tooling relies on:
- Networks: every repo belongs to one network (named by its root repo), and each network
  has a set of commits. Any repo can reference any commit in its network, and only those.
- One repo per network per org: a second fork into the same org returns the existing
  fork; a name taken by an unrelated repo gets a `-1` suffix.
- Renames: the old name keeps working and `get_repo` reports the new name.
- Deleted repos raise `NotFound`.
- Rate limits on creations (`rate_limit_next`) and slow fork readiness (`ready_after`).
- Repository settings (`settings`, `topics`, `actions_enabled`, `vulnerability_alerts`,
  `rulesets`). Active rulesets are enforced on API ref writes: a matching ref may not be
  created (`creation`), moved (`update`, `non_fast_forward`) or deleted (`deletion`);
  each refused write is appended to `refused`. `pull_requests_supported = False` models
  an API that does not report (or accept) `has_pull_requests`; `pull_requests_rejected`
  one that reports it but refuses to set it (HTTP 422), `pull_requests_ignored` one that
  accepts the PATCH but keeps the old value. `actions_org_managed` makes setting Actions
  fail with HTTP 409, as when the org policy decides.

Inspection state is keyed by each repo's current full name.
"""

import re
import subprocess
from collections.abc import Iterable
from pathlib import Path

from wafer_space_bench.git import Git
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
        self.settings: dict[str, dict] = {}
        self.topics: dict[str, list[str]] = {}
        self.actions_enabled: dict[str, bool] = {}
        self.vulnerability_alerts: dict[str, bool] = {}
        self.rulesets: dict[str, dict[str, dict]] = {}  # repo -> ruleset name -> ruleset
        self.refused: list[tuple[str, str, str]] = []  # (repo, ref, operation)
        self.pull_requests_supported = True
        self.pull_requests_rejected = False
        self.pull_requests_ignored = False
        self.actions_org_managed = False

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

    def _put(self, name, net, *, fork, default, archived=False, branches=None, description="") -> None:
        self.repos[name] = RepoInfo(name, fork, net, default, archived, 0)
        self._names[name.lower()] = name
        self.branches[name] = dict(branches or {})
        self.tags[name] = {}
        self.default_branch[name] = default
        # GitHub's defaults for a new public repo (alerts on, as an org may set).
        self.settings[name] = {
            "description": description or None,
            "homepage": None,
            "has_issues": True,
            "has_projects": True,
            "has_wiki": True,
            "has_discussions": False,
            "has_pull_requests": True,
        }
        self.topics[name] = []
        self.actions_enabled[name] = True
        self.vulnerability_alerts[name] = True
        self.rulesets[name] = {}

    def _check_rules(self, name: str, ref: str, op: str) -> None:
        """Refuse `op` ("creation", "update" or "deletion") of `ref` (heads/x, tags/x) if an
        active ruleset of the repo covers it."""
        kind, _ = self._split_ref(ref)
        full_ref = f"refs/{ref}"
        blocking = {
            "creation": {"creation"},
            "update": {"update", "non_fast_forward"},
            "deletion": {"deletion"},
        }
        for rs in self.rulesets[name].values():
            if rs["enforcement"] != "active" or rs["target"] != ("branch" if kind == "heads" else "tag"):
                continue
            cond = rs["conditions"]["ref_name"]
            if not any(_glob(p, full_ref) for p in cond["include"]) or any(
                _glob(p, full_ref) for p in cond.get("exclude", [])
            ):
                continue
            if blocking[op] & {r["type"] for r in rs["rules"]}:
                self.refused.append((name, ref, op))
                raise GitHubError(f"repos/{name}/git/refs: Repository rule violations found (HTTP 422)")

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
        self._check_rules(name, f"tags/{tag}", "creation")
        self.tags[name][tag] = sha

    def create_repo(self, org: str, name: str, description: str) -> None:
        self._record("create_repo", org, name, description)
        self._creation()
        full = f"{org}/{name}"
        if full.lower() in self._names:
            raise GitHubError(f"orgs/{org}/repos: name already exists on this account (HTTP 422)")
        self.networks[full] = set()
        self._put(full, full, fork=False, default="main", description=description)

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
        self._check_rules(name, ref, "update" if short in self._refs(name, kind) else "creation")
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
        self._check_rules(name, ref, "deletion")
        del refs[short]

    def get_file(self, full_name: str, path: str, ref: str) -> str | None:
        return self.files.get((self._resolve(full_name), ref, path))

    def dispatch(self, full_name: str, event_type: str) -> None:
        self._record("dispatch", full_name, event_type)
        self.dispatches.append((self._resolve(full_name), event_type))

    # --- repository settings --------------------------------------------------

    def get_repo_settings(self, full_name: str) -> dict:
        got = dict(self.settings[self._resolve(full_name)])
        if not self.pull_requests_supported:
            got["has_pull_requests"] = None
        return got

    def update_repo(self, full_name: str, fields: dict) -> None:
        self._record("update_repo", full_name, dict(fields))
        name = self._resolve(full_name)
        unknown = set(fields) - set(self.settings[name])
        prs = "has_pull_requests" in fields
        if unknown or (prs and (not self.pull_requests_supported or self.pull_requests_rejected)):
            raise GitHubError(f"repos/{name}: Validation Failed: {sorted(fields)} (HTTP 422)")
        if prs and self.pull_requests_ignored:
            fields = {k: v for k, v in fields.items() if k != "has_pull_requests"}
        self.settings[name].update(fields)

    def get_topics(self, full_name: str) -> list[str]:
        return list(self.topics[self._resolve(full_name)])

    def set_topics(self, full_name: str, names: list[str]) -> None:
        self._record("set_topics", full_name, list(names))
        bad = [n for n in names if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,49}", n)]
        if bad or len(names) > 20:
            raise GitHubError(f"repos/{full_name}/topics: Invalid topics {bad} (HTTP 422)")
        self.topics[self._resolve(full_name)] = list(names)

    def get_actions_enabled(self, full_name: str) -> bool:
        return self.actions_enabled[self._resolve(full_name)]

    def set_actions_enabled(self, full_name: str, enabled: bool) -> None:
        self._record("set_actions_enabled", full_name, enabled)
        name = self._resolve(full_name)
        if self.actions_org_managed:
            raise GitHubError(f"repos/{name}/actions/permissions: Conflict (HTTP 409)")
        self.actions_enabled[name] = enabled

    def get_vulnerability_alerts(self, full_name: str) -> bool:
        return self.vulnerability_alerts[self._resolve(full_name)]

    def set_vulnerability_alerts(self, full_name: str, enabled: bool) -> None:
        self._record("set_vulnerability_alerts", full_name, enabled)
        self.vulnerability_alerts[self._resolve(full_name)] = enabled

    def list_rulesets(self, full_name: str) -> list[dict]:
        return [dict(r) for r in self.rulesets[self._resolve(full_name)].values()]

    def put_ruleset(self, full_name: str, ruleset: dict) -> None:
        self._record("put_ruleset", full_name, ruleset["name"])
        name = self._resolve(full_name)
        if ruleset["target"] not in ("branch", "tag") or not all(
            p.startswith("refs/") for p in ruleset["conditions"]["ref_name"]["include"]
        ):
            raise GitHubError(f"repos/{name}/rulesets: Validation Failed (HTTP 422)")
        old = self.rulesets[name].get(ruleset["name"])
        rid = old["id"] if old else 1 + sum(len(r) for r in self.rulesets.values())
        self.rulesets[name][ruleset["name"]] = {"id": rid, "source_type": "Repository"} | ruleset


def _glob(pattern: str, ref: str) -> bool:
    """fnmatch as rulesets use it: `**` matches across `/`, `*` within one segment."""
    rx = "".join(
        ".*" if part == "**" else "[^/]*" if part == "*" else re.escape(part)
        for part in re.split(r"(\*\*|\*)", pattern)
    )
    return re.fullmatch(rx, ref) is not None


def sh(cwd, *args) -> str:
    """Run git in `cwd` (with a test identity); its stripped stdout."""
    return subprocess.run(
        ["git", "-c", "user.name=T", "-c", "user.email=t@e", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


class SyncedFake(FakeGitHub):
    """FakeGitHub kept consistent with real bare `file://` remotes.

    Writes (`set_ref`, `delete_ref`, `set_default_branch`, `create_tag`) first copy the
    remote's heads, tags and commits into the fake, then apply the change to both, so git
    (`ls-remote`, fetch, the `about` push) sees exactly what the API calls did and final
    state can be asserted on the real remote. `remote_path` is the remote of every repo not
    given its own with `link`. Reads (`list_branches`, `get_tag`) re-sync only linked repos,
    so a repo modelled in the fake alone (an upstream) keeps what the test gives it.
    """

    def __init__(self, remote_path: Path | None = None):
        super().__init__()
        self.remote = Git(remote_path) if remote_path is not None else None
        self.remotes: dict[str, Git] = {}

    def link(self, full: str, remote_path: Path) -> None:
        self.remotes[self._resolve(full)] = Git(remote_path)

    def _git_of(self, name: str) -> Git:
        return self.remotes.get(name) or self.remote

    def _sync(self, full):
        name = self._resolve(full)
        g = self._git_of(name)
        self.branches[name] = {h: g.rev_parse(f"refs/heads/{h}") for h in g.heads()}
        self.tags[name] = {t: g.rev_parse(f"refs/tags/{t}") for t in g.tags()}
        self.add_commits(name, sh(g.path, "rev-list", "--all").split())

    def _read_sync(self, full):
        if self._resolve(full) in self.remotes:
            self._sync(full)

    def list_branches(self, full_name):
        self._read_sync(full_name)
        return super().list_branches(full_name)

    def get_tag(self, full_name, tag):
        self._read_sync(full_name)
        return super().get_tag(full_name, tag)

    def set_ref(self, full_name, ref, sha):
        self._sync(full_name)
        super().set_ref(full_name, ref, sha)
        self._git_of(self._resolve(full_name)).update_ref(f"refs/{ref}", sha)

    def delete_ref(self, full_name, ref):
        self._sync(full_name)
        super().delete_ref(full_name, ref)
        self._git_of(self._resolve(full_name)).delete_ref(f"refs/{ref}")

    def set_default_branch(self, full_name, branch):
        self._sync(full_name)
        super().set_default_branch(full_name, branch)
        sh(self._git_of(self._resolve(full_name)).path, "symbolic-ref", "HEAD", f"refs/heads/{branch}")

    def create_tag(self, full_name, tag, sha, message):
        self._sync(full_name)
        super().create_tag(full_name, tag, sha, message)
        sh(self._git_of(self._resolve(full_name)).path, "tag", "-a", tag, "-m", message, sha)
