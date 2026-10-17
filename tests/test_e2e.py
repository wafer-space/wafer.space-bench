"""End to end on SyncedFake: scan -> fork -> copy -> layout -> sync -> index, then again.

Every bench repo is a real bare `file://` remote under `remotes/`, created when the fake
forks or creates it, so every git operation (ls-remote, fetch, push) and every API write
lands in one consistent place. Upstreams live in the fake (branches, network commits) and,
for copies, as bare repos under a local `mirrors` prefix.
"""

import shutil
import subprocess
from pathlib import Path

import pytest
from fakes import SyncedFake, sh

from wafer_space_bench.catalog import Design, tapeouts_by_repo
from wafer_space_bench.config import LocalConfig
from wafer_space_bench.copy import run_copies
from wafer_space_bench.fork import run_fork
from wafer_space_bench.git import Git
from wafer_space_bench.index import index_rows
from wafer_space_bench.layout import run_layouts
from wafer_space_bench.scan import plan_repos
from wafer_space_bench.settings import run_settings
from wafer_space_bench.sync import INDEX_REPO, run_sync

ORG = "wafer-space-bench"
NOW = "2026-10-17T00:00:00Z"
WRITES = ("set_ref", "delete_ref", "set_default_branch", "create_tag", "create_repo", "fork", "dispatch")
SETTINGS_WRITES = (
    "update_repo",
    "set_topics",
    "set_actions_enabled",
    "set_vulnerability_alerts",
    "put_ruleset",
)

# A pre-receive hook that does to git pushes what the bench-frozen rulesets do on GitHub:
# a tapeout branch or tapeout tag may be created, never updated or deleted.
FROZEN_HOOK = """#!/bin/sh
zero=0000000000000000000000000000000000000000
status=0
while read old new ref; do
  case "$ref" in
    refs/heads/tapeout/*|refs/tags/wafer.space/*|refs/tags/google/*|refs/tags/tinytapeout/*)
      if [ "$old" != "$zero" ]; then
        echo "refused by bench-frozen: $ref" >&2
        status=1
      fi;;
  esac
done
exit $status
"""


class Events(list):
    def __call__(self, event, **fields):
        self.append((event, fields))


class OrgFake(SyncedFake):
    """SyncedFake whose org repos get a bare remote at `root/<full>.git` when created: a fork
    gets the upstream's branches (pushed from `sources[upstream]`), a new repo starts empty.
    `get_file` reads the remote, as the contents API reads the repo."""

    def __init__(self, root: Path, sources: dict[str, Path], frozen: bool = False):
        super().__init__()
        self.root, self.sources, self.frozen = root, sources, frozen

    def _new_remote(self, full: str) -> Path:
        path = self.root / f"{full}.git"
        path.parent.mkdir(parents=True, exist_ok=True)
        sh(self.root, "init", "-q", "--bare", str(path))
        if self.frozen:  # pushes obey the rulesets too, not just API writes
            hook = path / "hooks" / "pre-receive"
            hook.write_text(FROZEN_HOOK)
            hook.chmod(0o755)
        return path

    def fork(self, source_full_name, org, name):
        known = set(self.repos)
        full = super().fork(source_full_name, org, name)
        if full not in known:
            path = self._new_remote(full)
            sh(self.sources[source_full_name], "push", "-q", path.as_uri(), "refs/heads/*:refs/heads/*")
            self.link(full, path)
            self._sync(full)
        return full

    def create_repo(self, org, name, description):
        super().create_repo(org, name, description)
        full = f"{org}/{name}"
        self.link(full, self._new_remote(full))

    def get_file(self, full_name, path, ref):
        name = self._resolve(full_name)
        if name not in self.remotes:
            return super().get_file(full_name, path, ref)
        return self._git_of(name).show(f"refs/heads/{ref}", path)


def design(repo, code, commit, shuttle="tt-gf-0p2"):
    return Design("tinytapeout", shuttle, code, f"P {code}", repo, commit, "Apache-2.0", "digital", "1x1")


def all_refs(root: Path) -> dict[str, str]:
    """Every ref of every bench remote: {"<remote>:<ref>": sha}."""
    out = {}
    for path in sorted(root.rglob("*.git")):
        listing = sh(path, "for-each-ref", "--format=%(refname) %(objectname)")
        for line in listing.splitlines():
            ref, sha = line.split()
            out[f"{path.relative_to(root)}:{ref}"] = sha
    return out


def writes(gh, since):
    return [c for c in gh.calls[since:] if c[0] in WRITES + SETTINGS_WRITES]


def test_end_to_end_then_second_pass_writes_nothing(tmp_path, upstream):
    scenario(tmp_path, upstream, rulesets=False)


def test_layout_and_sync_work_under_rulesets(tmp_path, upstream):
    """The same scenario with `wsbench settings` run right after fork and copy, so the
    bench-frozen rulesets are in force (in the fake, and as a hook on every remote) for
    layout and sync. Nothing is refused, and a second pass writes nothing either."""
    gh = scenario(tmp_path, upstream, rulesets=True)
    assert gh.refused == []
    # The hook is live: moving a tapeout branch by a push is refused, as on GitHub.
    upath, shas = upstream
    one = tmp_path / "remotes" / ORG / "a--one.git"
    branch = "refs/heads/tapeout/tinytapeout/ttgf0p2/tt_um_one"
    with pytest.raises(subprocess.CalledProcessError):
        sh(upath, "push", "-q", "--force", one.as_uri(), f"{shas['main']}:{branch}")
    assert Git(one).rev_parse(branch) == shas["ttgf0p2"]
    frozen = {r: set(rs) for r, rs in gh.rulesets.items() if rs}
    assert frozen == {
        f"{ORG}/{name}": {"bench-frozen-branches", "bench-frozen-tags"}
        for name in ("a--one", "b--two", "0--zero")
    }


def scenario(tmp_path, upstream, *, rulesets):
    upath, shas = upstream
    root, mirrors = tmp_path / "remotes", tmp_path / "mirrors"
    for full in ("b/two", "0/zero"):  # copies are fetched from these local mirrors
        sh(tmp_path, "clone", "-q", "--bare", str(upath), str(mirrors / f"{full}.git"))
    gh = OrgFake(root, {"a/one": upath}, frozen=rulesets)
    gh.add_upstream("a/one", branches=shas)
    gh.add_upstream("b/two", network="a/one", branches=shas)
    gh.add_upstream(INDEX_REPO)
    cfg = LocalConfig(work_dir=tmp_path / "work", mirrors={"https://github.com/": mirrors})
    fmt = root.as_uri() + "/{full}.git"
    designs = [
        design("https://github.com/a/one", "tt_um_one", shas["ttgf0p2"]),
        design("https://github.com/b/two", "tt_um_two", shas["feature/x"]),
        design("", "BRWN", "", shuttle="ws-run1"),
    ]
    quiet = {"log": Events(), "sleep": lambda s: None}

    def full_pass(plans):
        """fork, copy, layout, sync, index; asserts nothing failed. Returns the index rows."""
        tos = tapeouts_by_repo(designs)
        assert run_fork(plans, tos, gh, ORG, delay=0, **quiet) == 0
        assert run_copies(plans, tos, gh, cfg, ORG, fmt, sources={}, delay=0, **quiet) == 0
        if rulesets:
            assert (
                run_settings(plans, tos, gh, ORG, only=None, shard=None, dry_run=False, delay=0, **quiet) == 0
            )
        assert run_layouts(plans, tos, gh, cfg, ORG, remote_fmt=fmt, now=lambda: NOW, delay=0, **quiet) == 0
        assert run_sync(plans, tos, gh, cfg, ORG, remote_fmt=fmt, now=NOW, delay=0, **quiet) == 0
        return index_rows(plans, tapeouts_by_repo(designs, include_none=True), gh, ORG)

    # --- first pass from a fresh org
    plans = plan_repos(designs, gh, org=ORG)
    assert [(p.source.key, p.strategy) for p in plans] == [
        ("a/one", "fork"),
        ("b/two", "copy"),
        ("ws-run1/brwn", "none"),
    ]
    rows = {r["name"]: r["state"] for r in full_pass(plans)}
    assert rows == {"a--one": "laid-out", "b--two": "laid-out", "P BRWN": "no-source"}
    one, two = Git(root / ORG / "a--one.git"), Git(root / ORG / "b--two.git")
    for g, tag, sha in ((one, "tinytapeout/ttgf0p2/tt_um_one", shas["ttgf0p2"]),
                        (two, "tinytapeout/ttgf0p2/tt_um_two", shas["feature/x"])):  # fmt: skip
        assert g.rev_parse(f"refs/tags/{tag}") == g.rev_parse(f"refs/heads/tapeout/{tag}") == sha
        assert {h for h in g.heads() if not h.startswith("tapeout/")} == {
            "about",
            *(f"upstream/{b}" for b in shas),
        }
        assert all(g.rev_parse(f"refs/heads/upstream/{b}") == s for b, s in shas.items())
        assert sh(g.path, "symbolic-ref", "HEAD") == "refs/heads/about"
        assert g.show("refs/heads/about", "bench.yaml") is not None

    # --- a second full pass writes nothing, through the API or git
    n, before = len(gh.calls), all_refs(root)
    full_pass(plans)
    assert writes(gh, n) == [] and all_refs(root) == before

    # --- copy from a deleted work/ (I1): tapeout tags are fetched back, not remade
    shutil.rmtree(tmp_path / "work")
    n = len(gh.calls)
    tos = tapeouts_by_repo(designs)
    assert run_copies(plans, tos, gh, cfg, ORG, fmt, sources={}, delay=0, **quiet) == 0
    assert writes(gh, n) == [] and all_refs(root) == before

    # --- a re-scan after a catalog repo is added to the already-forked network (I2)
    gh.add_upstream("0/zero", network="a/one", branches=shas)
    designs.append(design("https://github.com/0/zero", "tt_um_zero", shas["main"]))
    plans = plan_repos(designs, gh, org=ORG)
    assert [(p.source.key, p.strategy, p.target) for p in plans] == [
        ("0/zero", "copy", f"{ORG}/0--zero"),
        ("a/one", "fork", f"{ORG}/a--one"),
        ("b/two", "copy", f"{ORG}/b--two"),
        ("ws-run1/brwn", "none", ""),
    ]
    rows = {r["name"]: r["state"] for r in full_pass(plans)}
    assert rows == {"0--zero": "laid-out", "a--one": "laid-out", "b--two": "laid-out", "P BRWN": "no-source"}
    assert [c for c in gh.calls if c[0] == "fork"] == [("fork", ("a/one", ORG, "a--one"))]

    n, before = len(gh.calls), all_refs(root)
    full_pass(plans)
    assert writes(gh, n) == [] and all_refs(root) == before
    return gh
