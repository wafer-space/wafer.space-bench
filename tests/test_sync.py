"""Tests for `wsbench sync`: mirroring upstream branches, tapeouts, `about`, the workflow.

Bench repos are real bare `file://` remotes kept consistent with the fake by `SyncedFake`
(tests/fakes.py), linked per repo. A fork's upstream lives in the fake only (its branch
list); its commits are pushed to the bench remote under `refs/network/*`, standing in for
the objects GitHub shares across a fork network. A copy's upstream is the real `upstream`
fixture, reached through a `mirrors` prefix in the local config.
"""

import argparse
import json
from pathlib import Path

import pytest
import yaml
from fakes import FakeGitHub, SyncedFake, sh

from wafer_space_bench.catalog import Tapeout
from wafer_space_bench.cli import main
from wafer_space_bench.config import LocalConfig
from wafer_space_bench.copy import copy_why, run_copy
from wafer_space_bench.fork import tag_message
from wafer_space_bench.git import Git
from wafer_space_bench.github import GitHubError
from wafer_space_bench.layout import apply_layout
from wafer_space_bench.naming import RepoRef
from wafer_space_bench.scan import RepoPlan, write_plans
from wafer_space_bench.sync import (
    INDEX_REPO,
    MirrorPlan,
    parse_shard,
    plan_mirror,
    run_sync,
    shard_of,
    sync_repo,
)

ORG = "wafer-space-bench"
UP = "a/upstream"
TARGET = f"{ORG}/a--upstream"
TAG = "tinytapeout/ttgf0p2/tt_um_x"
TAG2 = "google/gf180mcu-mpw0/slot_1"
NOW = "2026-10-17T00:00:00Z"
INDEX = f"{ORG}/{ORG}.github.io"
WORKFLOW = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "sync.yml"
WRITES = ("set_ref", "delete_ref", "set_default_branch", "create_tag", "create_repo", "fork", "dispatch")


class Events(list):
    def __call__(self, event, **fields):
        self.append((event, fields))


class Sleeps(list):
    def __call__(self, s):
        self.append(s)


def mkplan(up=UP, target=TARGET, strategy="fork", host="github.com", network=None):
    owner, name = up.split("/")
    return RepoPlan(RepoRef(host, owner, name), up, "live", network or up, "main", 1, strategy, target)


def tapeout(tag, commit, key=UP):
    org, run, slot = tag.split("/")
    return Tapeout(org, run, slot, "P", "1x1", "digital", "Apache-2.0", commit, "", key)


def cfg(tmp_path, **kw):
    return LocalConfig(work_dir=tmp_path / "work", **kw)


def heads_of(path):
    g = Git(path)
    return {h: g.rev_parse(f"refs/heads/{h}") for h in g.heads()}


def tags_of(path):
    g = Git(path)
    return {t: g.rev_parse(f"refs/tags/{t}") for t in g.tags()}


def writes(gh, since=0):
    return [c for c in gh.calls[since:] if c[0] in WRITES]


def remote_path(remote_url):
    return Path(remote_url.removeprefix("file://"))


def bench_fork(tmp_path, upstream, path, *, gh=None, up=UP, target=TARGET, strategy="fork", layout=True):
    """A fork bench repo at bare `path` (created if needed): the upstream fixture's heads and
    the tapeout tag pushed to it, registered in `gh` in the network of `up`, laid out."""
    upath, shas = upstream
    if not sh(upath, "tag", "-l", TAG):
        sh(upath, "tag", "-a", TAG, "-m", "taped out", shas["ttgf0p2"])
    if not path.exists():
        sh(tmp_path, "init", "-q", "--bare", str(path))
    sh(upath, "push", "-q", path.as_uri(), "refs/heads/*:refs/heads/*", "refs/tags/*:refs/tags/*")
    gh = gh or SyncedFake()
    gh.add_upstream(up, branches=shas)
    gh.add_upstream(target, network=up, branches=shas)
    gh.link(target, path)
    gh._sync(target)
    if layout:
        p = mkplan(up, target, strategy)
        status = apply_layout(p, {up: [tapeout(TAG, shas["ttgf0p2"])]}, gh, cfg(tmp_path), ORG,
                              remote_fmt=path.as_uri(), now=NOW, log=Events(), sleep=Sleeps())  # fmt: skip
        assert status.startswith("ok:"), status
    return gh


def change_upstream(upstream):
    """Upstream gains branch `new`, force-pushes `main` (amended) and deletes `feature/x`."""
    upath, _ = upstream
    sh(upath, "checkout", "-q", "-b", "new", "main")
    (upath / "b.txt").write_text("new\n")
    sh(upath, "add", "b.txt")
    sh(upath, "commit", "-q", "-m", "new")
    sh(upath, "checkout", "-q", "main")
    sh(upath, "commit", "-q", "--amend", "-m", "rewritten")
    sh(upath, "branch", "-D", "feature/x")
    return heads_of(upath / ".git")


def publish(gh, upstream, up, paths):
    """Tell the fake about upstream's branches and put its commits in the bench remotes' network."""
    upath, _ = upstream
    heads = heads_of(upath / ".git")
    for p in paths:
        sh(upath, "push", "-q", "-f", p.as_uri(), "refs/heads/*:refs/network/*")
    gh.branches[up] = dict(heads)
    gh.add_commits(up, heads.values())
    return heads


def sync(gh, tmp_path, path, shas, *, plan=None, tos=None, ev=None, conf=None, **kw):
    return sync_repo(
        plan or mkplan(),
        tos if tos is not None else {UP: [tapeout(TAG, shas["ttgf0p2"])]},
        gh,
        conf or cfg(tmp_path),
        ORG,
        remote_fmt=path.as_uri(),
        now=NOW,
        log=ev if ev is not None else Events(),
        sleep=Sleeps(),
        **kw,
    )


def upstream_part(heads):
    return {b.removeprefix("upstream/"): s for b, s in heads.items() if b.startswith("upstream/")}


# --- plan_mirror ---------------------------------------------------------------


def test_plan_mirror_create_update_delete():
    current = {"upstream/main": "A", "upstream/old": "X", "about": "Y", f"tapeout/{TAG}": "Z"}
    assert plan_mirror({"main": "B", "new": "C"}, current) == MirrorPlan(
        set={"upstream/main": "B", "upstream/new": "C"}, delete=["upstream/old"]
    )


def test_plan_mirror_omits_unchanged():
    assert plan_mirror({"main": "A"}, {"upstream/main": "A"}) == MirrorPlan(set={}, delete=[])


def test_plan_mirror_only_emits_upstream_names():
    upstream = {"about": "1", "tapeout/x/y/z": "2", "dev/w": "3", "upstream/q": "4", "main": "5"}
    current = {"about": "9", "dev/w": "9", "tapeout/a/b/c": "9", "main": "9", "upstream/gone": "9"}
    mp = plan_mirror(upstream, current)
    assert all(b.startswith("upstream/") for b in [*mp.set, *mp.delete])
    assert set(mp.set) == {f"upstream/{b}" for b in upstream}
    assert mp.delete == ["upstream/gone"]


# --- sync_repo: forks ------------------------------------------------------------


def test_sync_fork_uses_refs_api(tmp_path, upstream, remote):
    path = remote_path(remote)
    gh = bench_fork(tmp_path, upstream, path)
    new = change_upstream(upstream)
    publish(gh, upstream, UP, [path])
    n = len(gh.calls)
    status = sync(gh, tmp_path, path, upstream[1])
    assert status == "ok: +1 ~1 -1, about"
    assert upstream_part(gh.branches[TARGET]) == new
    assert upstream_part(heads_of(path)) == new
    assert gh.list_branches(TARGET) == heads_of(path)  # read re-syncs after the git push of about
    names = [c[0] for c in writes(gh, n)]
    assert names == ["delete_ref", "set_ref", "set_ref"]
    data = yaml.safe_load(Git(path).show("refs/heads/about", "bench.yaml"))
    assert data["branches"] == sorted(heads_of(path))


def test_sync_never_touches_tapeout_or_dev(tmp_path, upstream, remote):
    path = remote_path(remote)
    gh = bench_fork(tmp_path, upstream, path)
    upath, shas = upstream
    Git(path).update_ref("refs/heads/dev/w", shas["main"])
    sh(upath, "branch", "-D", "ttgf0p2")  # the branch the tapeout came from
    change_upstream(upstream)
    publish(gh, upstream, UP, [path])
    keep = lambda heads: {b: s for b, s in heads.items() if b.startswith(("tapeout/", "dev/"))}
    before, before_tags, fake_tags = keep(heads_of(path)), tags_of(path), dict(gh.tags[TARGET])
    n = len(gh.calls)
    assert sync(gh, tmp_path, path, shas).startswith("ok: ")
    assert keep(heads_of(path)) == before and set(before) == {f"tapeout/{TAG}", "dev/w"}
    assert tags_of(path) == before_tags and gh.tags[TARGET] == fake_tags
    assert "upstream/ttgf0p2" not in heads_of(path)
    touched = {c[1][1] for c in writes(gh, n)}
    assert touched == {"heads/upstream/main", "heads/upstream/new", "heads/upstream/feature/x",
                       "heads/upstream/ttgf0p2"}  # fmt: skip


def test_sync_upstream_missing_changes_nothing(tmp_path, upstream, remote):
    path = remote_path(remote)
    gh = bench_fork(tmp_path, upstream, path)
    gh._deleted.add(UP)
    before, n = heads_of(path), len(gh.calls)
    ev = Events()
    assert sync(gh, tmp_path, path, upstream[1], ev=ev) == "upstream-missing"
    assert writes(gh, n) == []
    assert heads_of(path) == before
    assert any(e == "upstream-missing" for e, _ in ev)


def test_sync_upstream_with_no_branches_changes_nothing(tmp_path, upstream, remote):
    """An empty branch listing is not mirrored as "delete everything"."""
    path = remote_path(remote)
    gh = bench_fork(tmp_path, upstream, path)
    gh.branches[UP] = {}
    before, n = heads_of(path), len(gh.calls)
    assert sync(gh, tmp_path, path, upstream[1]) == "upstream-missing"
    assert writes(gh, n) == [] and heads_of(path) == before


def test_sync_restore_skipped(tmp_path, upstream, remote):
    path = remote_path(remote)
    gh = bench_fork(tmp_path, upstream, path, strategy="restore")
    change_upstream(upstream)
    publish(gh, upstream, UP, [path])
    before, n = heads_of(path), len(gh.calls)
    assert sync(gh, tmp_path, path, upstream[1], plan=mkplan(strategy="restore")) == "skipped: restore"
    assert writes(gh, n) == [] and heads_of(path) == before


def test_sync_pending(tmp_path, upstream, remote):
    gh = SyncedFake()
    assert sync(gh, tmp_path, remote_path(remote), upstream[1]) == "pending"
    assert writes(gh) == []


def test_sync_not_laid_out_runs_layout(tmp_path, upstream, remote):
    path = remote_path(remote)
    gh = bench_fork(tmp_path, upstream, path, layout=False)
    change_upstream(upstream)
    publish(gh, upstream, UP, [path])
    assert sync(gh, tmp_path, path, upstream[1]) == "laid-out"
    assert gh.default_branch[TARGET] == "about"
    assert upstream_part(heads_of(path)) == upstream[1]  # mirroring waits for the next run


def test_sync_about_only_on_change(tmp_path, upstream, remote):
    path = remote_path(remote)
    gh = bench_fork(tmp_path, upstream, path)
    change_upstream(upstream)
    publish(gh, upstream, UP, [path])
    assert sync(gh, tmp_path, path, upstream[1]) == "ok: +1 ~1 -1, about"
    before, n = heads_of(path), len(gh.calls)
    about_log = sh(path, "rev-list", "--count", "refs/heads/about")
    assert sync(gh, tmp_path, path, upstream[1]) == "unchanged"
    assert writes(gh, n) == []
    assert heads_of(path) == before
    assert sh(path, "rev-list", "--count", "refs/heads/about") == about_log


def test_sync_only_sha_change_keeps_about(tmp_path, upstream, remote):
    """A force-push changes no branch name, so `about` gets no new commit."""
    path = remote_path(remote)
    gh = bench_fork(tmp_path, upstream, path)
    upath, shas = upstream
    sh(upath, "commit", "-q", "--amend", "-m", "rewritten")
    publish(gh, upstream, UP, [path])
    about = heads_of(path)["about"]
    assert sync(gh, tmp_path, path, shas) == "ok: +0 ~1 -0"
    assert heads_of(path)["about"] == about


def test_sync_adds_new_tapeout_tags(tmp_path, upstream, remote):
    path = remote_path(remote)
    gh = bench_fork(tmp_path, upstream, path)
    shas = upstream[1]
    tos = {UP: [tapeout(TAG, shas["ttgf0p2"]), tapeout(TAG2, shas["main"][:12])]}
    ev = Events()
    assert sync(gh, tmp_path, path, shas, tos=tos, ev=ev) == "ok: +0 ~0 -0, about, 1 tags"
    assert gh.tags[TARGET][TAG2] == shas["main"]
    assert tags_of(path)[TAG2] == shas["main"]
    assert heads_of(path)[f"tapeout/{TAG2}"] == shas["main"]
    data = yaml.safe_load(Git(path).show("refs/heads/about", "bench.yaml"))
    assert [t["tag"] for t in data["tapeouts"]] == [TAG, TAG2]
    assert f"tapeout/{TAG2}" in data["branches"]
    assert sync(gh, tmp_path, path, shas, tos=tos) == "unchanged"


def test_sync_never_moves_tapeout_branch(tmp_path, upstream, remote):
    path = remote_path(remote)
    gh = bench_fork(tmp_path, upstream, path)
    shas = upstream[1]
    Git(path).update_ref(f"refs/heads/tapeout/{TAG}", shas["main"])
    ev, n = Events(), len(gh.calls)
    assert sync(gh, tmp_path, path, shas, ev=ev) == "unchanged"
    assert writes(gh, n) == []
    assert heads_of(path)[f"tapeout/{TAG}"] == shas["main"]
    assert ("tapeout-mismatch", {"target": TARGET, "branch": f"tapeout/{TAG}",
                                 "branch_sha": shas["main"], "tag_sha": shas["ttgf0p2"]}) in ev  # fmt: skip


def test_sync_dry_run_changes_nothing(tmp_path, upstream, remote):
    path = remote_path(remote)
    gh = bench_fork(tmp_path, upstream, path)
    change_upstream(upstream)
    publish(gh, upstream, UP, [path])
    before, n = heads_of(path), len(gh.calls)
    ev = Events()
    assert sync(gh, tmp_path, path, upstream[1], dry_run=True, ev=ev) == "dry-run: +1 ~1 -1"
    assert writes(gh, n) == [] and heads_of(path) == before
    assert ("mirror-plan", {"target": TARGET, "add": 1, "update": 1, "delete": 1}) in ev


def test_sync_dry_run_needs_layout(tmp_path, upstream, remote):
    path = remote_path(remote)
    gh = bench_fork(tmp_path, upstream, path, layout=False)
    before = heads_of(path)
    assert sync(gh, tmp_path, path, upstream[1], dry_run=True) == "dry-run: needs layout"
    assert writes(gh) == [] and heads_of(path) == before


def test_sync_rename_to_nested_branch(tmp_path, upstream, remote):
    """Upstream renames `feature/x` to `feature/x/y`: the delete goes first, so no D/F clash."""
    path = remote_path(remote)
    gh = bench_fork(tmp_path, upstream, path)
    upath, shas = upstream
    sh(upath, "branch", "-m", "feature/x", "feature/x/y")
    new = publish(gh, upstream, UP, [path])
    assert sync(gh, tmp_path, path, shas) == "ok: +1 ~0 -1, about"
    assert upstream_part(heads_of(path)) == new


# --- sync_repo: copies -----------------------------------------------------------


def bench_copy(tmp_path, upstream, path, host="github.com", extra=()):
    """A laid-out copy bench repo at bare `path`, made by `run_copy` from the upstream fixture."""
    upath, shas = upstream
    sh(upath, "tag", "-a", TAG, "-m", "taped out", shas["ttgf0p2"])
    gh = SyncedFake()
    if host == "github.com":
        gh.add_upstream(UP, branches=shas)
    gh.add_upstream(TARGET, branches={})
    gh.link(TARGET, path)
    p = mkplan(strategy="copy", host=host, network="x/root")
    conf = cfg(tmp_path, mirrors={f"https://{host}/a/": tmp_path})
    tos = {UP: [tapeout(TAG, shas["ttgf0p2"]), *extra]}
    assert run_copy(p, tos, gh, conf, ORG, path.as_uri(), log=Events()) == "pushed"
    status = apply_layout(
        p, tos, gh, conf, ORG, remote_fmt=path.as_uri(), now=NOW, log=Events(), sleep=Sleeps()
    )
    assert status.startswith("ok:"), status
    return gh, p, conf


def test_sync_copy_fetches_and_pushes(tmp_path, upstream, remote):
    path = remote_path(remote)
    gh, p, conf = bench_copy(tmp_path, upstream, path)
    upath, shas = upstream
    (upath / "a.txt").write_text("two\n")
    sh(upath, "commit", "-q", "-am", "two")
    new = change_upstream(upstream)
    n = len(gh.calls)
    assert sync(gh, tmp_path, path, shas, plan=p, conf=conf) == "ok: +1 ~1 -1, about"
    heads = heads_of(path)
    assert heads["upstream/main"] == new["main"] != shas["main"]
    assert upstream_part(heads) == new
    assert gh.list_branches(TARGET) == heads
    assert tags_of(path) == {TAG: shas["ttgf0p2"]}  # upstream tags are not mirrored
    assert [c[0] for c in writes(gh, n)] == []  # objects and refs went by git push
    assert sync(gh, tmp_path, path, shas, plan=p, conf=conf) == "unchanged"


def tag_messages(gh, path):
    """{tag: message} of the tags sync created (fake calls) and of all tags on the remote."""
    created = {c[1][1]: c[1][3] for c in gh.calls if c[0] == "create_tag"}
    out = sh(path, "for-each-ref", "--format=%(refname:short)%00%(contents)%01", "refs/tags")
    remote = dict(r.strip("\n").split("\0", 1) for r in out.split("\1") if r.strip())
    return created, remote


def test_sync_copy_tag_message_matches_copy(tmp_path, upstream, remote):
    """Tags are permanent: one sync creates for a copy has exactly Task 7's message form."""
    path = remote_path(remote)
    shas = upstream[1]
    t3 = tapeout("wafer.space/ws-run1/slot_9", shas["feature/x"])  # tagged by run_copy (Task 7)
    gh, p, conf = bench_copy(tmp_path, upstream, path, extra=[t3])
    t2 = tapeout(TAG2, shas["main"])  # tagged by sync
    tos = {UP: [tapeout(TAG, shas["ttgf0p2"]), t3, t2]}
    assert sync(gh, tmp_path, path, shas, plan=p, conf=conf, tos=tos).endswith(", 1 tags")
    created, on_remote = tag_messages(gh, path)
    assert list(created) == [TAG2]
    assert created[TAG2] == tag_message(t2, p.source.url, copy_why(p))
    assert on_remote[t3.tag].strip() == tag_message(t3, p.source.url, copy_why(p)).strip()
    assert on_remote[TAG2].strip() == created[TAG2].strip()


def test_sync_fork_tag_message_matches_fork(tmp_path, upstream, remote):
    path = remote_path(remote)
    gh = bench_fork(tmp_path, upstream, path)
    shas = upstream[1]
    t2 = tapeout(TAG2, shas["main"])
    sync(gh, tmp_path, path, shas, tos={UP: [tapeout(TAG, shas["ttgf0p2"]), t2]})
    created, _ = tag_messages(gh, path)
    assert created == {TAG2: tag_message(t2, f"https://github.com/{UP}")}  # as run_fork makes it


def test_sync_gitlab_copy(tmp_path, upstream, remote):
    path = remote_path(remote)
    gh, p, conf = bench_copy(tmp_path, upstream, path, host="gitlab.com")
    shas = upstream[1]
    new = change_upstream(upstream)
    assert sync(gh, tmp_path, path, shas, plan=p, conf=conf) == "ok: +1 ~1 -1, about"
    assert upstream_part(heads_of(path)) == new


def test_sync_gitlab_copy_fetch_error(tmp_path, upstream, remote):
    path = remote_path(remote)
    gh, p, conf = bench_copy(tmp_path, upstream, path, host="gitlab.com")
    upath, shas = upstream
    before, n = heads_of(path), len(gh.calls)
    upath.rename(tmp_path / "gone")
    status = sync(gh, tmp_path, path, shas, plan=p, conf=conf)
    assert status.startswith("error: ") and str(tmp_path) not in status
    assert writes(gh, n) == [] and heads_of(path) == before


# --- run_sync --------------------------------------------------------------------


class FailSetRef(SyncedFake):
    """set_ref on `target` fails once `armed` (after the bench repo is set up)."""

    def __init__(self, target):
        super().__init__()
        self._target = target
        self.armed = False

    def set_ref(self, full_name, ref, sha):
        if self.armed and full_name == self._target:
            raise GitHubError("boom (HTTP 500)")
        super().set_ref(full_name, ref, sha)


def three_benches(tmp_path, upstream, gh):
    names = [(f"a/up{i}", f"{ORG}/a--up{i}") for i in (1, 2, 3)]
    root = tmp_path / "remotes"
    paths = []
    for up, target in names:
        path = root / f"{target}.git"
        path.parent.mkdir(parents=True, exist_ok=True)
        bench_fork(tmp_path, upstream, path, gh=gh, up=up, target=target)
        paths.append(path)
    gh.add_upstream(INDEX)
    plans = [mkplan(up, target) for up, target in names]
    shas = upstream[1]
    tos = {up: [tapeout(TAG, shas["ttgf0p2"], up)] for up, _ in names}
    return plans, tos, paths, root.as_uri() + "/{full}.git"


def test_run_sync_continues_after_error_and_dispatches_once(tmp_path, upstream):
    gh = FailSetRef(f"{ORG}/a--up1")
    plans, tos, paths, fmt = three_benches(tmp_path, upstream, gh)
    gh.armed = True
    change_upstream(upstream)
    for up, p in zip(["a/up1", "a/up2", "a/up3"], paths, strict=True):
        new = publish(gh, upstream, up, [p])
    ev, sleeps = Events(), Sleeps()
    failed = run_sync(plans, tos, gh, cfg(tmp_path), ORG, only=None, dry_run=False, remote_fmt=fmt,
                      now=NOW, log=ev, sleep=sleeps)  # fmt: skip
    assert failed == 1
    statuses = [f["status"] for e, f in ev if e == "sync"]
    assert statuses[0].startswith("error: ") and "boom" in statuses[0]
    assert statuses[1:] == ["ok: +1 ~1 -1, about"] * 2
    assert [f["n"] for e, f in ev if e == "sync"] == ["1/3", "2/3", "3/3"]
    assert upstream_part(heads_of(paths[2])) == new
    assert gh.dispatches == [(INDEX, "bench-updated")]
    assert sleeps == [2, 2]  # after repos that wrote, not after the last


def test_run_sync_no_change_no_dispatch(tmp_path, upstream):
    gh = SyncedFake()
    plans, tos, _, fmt = three_benches(tmp_path, upstream, gh)
    n = len(gh.calls)
    failed = run_sync(plans, tos, gh, cfg(tmp_path), ORG, only=None, dry_run=False, remote_fmt=fmt,
                      now=NOW, log=Events(), sleep=Sleeps())  # fmt: skip
    assert failed == 0
    assert writes(gh, n) == [] and gh.dispatches == []


def test_run_sync_only_and_dry_run(tmp_path, upstream):
    gh = SyncedFake()
    plans, tos, paths, fmt = three_benches(tmp_path, upstream, gh)
    change_upstream(upstream)
    for up, p in zip(["a/up1", "a/up2", "a/up3"], paths, strict=True):
        publish(gh, upstream, up, [p])
    ev, n = Events(), len(gh.calls)
    failed = run_sync(plans, tos, gh, cfg(tmp_path), ORG, only=["A--UP2"], dry_run=True, remote_fmt=fmt,
                      now=NOW, log=ev, sleep=Sleeps())  # fmt: skip
    assert failed == 0
    assert [(f["target"], f["status"]) for e, f in ev if e == "sync"] == [
        (f"{ORG}/a--up2", "dry-run: +1 ~1 -1")
    ]
    assert writes(gh, n) == [] and gh.dispatches == []


def pending_plans(k):
    return [mkplan(f"o/r{i:02d}", f"{ORG}/o--r{i:02d}") for i in range(k)]


def synced_targets(plans, gh=None, **kw):
    ev = Events()
    failed = run_sync(plans, {}, gh or FakeGitHub(), LocalConfig(), ORG, remote_fmt="unused", now=NOW, log=ev,
                      sleep=Sleeps(), **kw)  # fmt: skip
    return failed, [f["target"] for e, f in ev if e == "sync"], ev


@pytest.mark.parametrize("n", [1, 2, 3, 8])
def test_run_sync_shards_partition_plans(n):
    plans = pending_plans(11)
    every = {p.target for p in plans}
    shards = [set(synced_targets(list(reversed(plans)), shard=(k, n))[1]) for k in range(1, n + 1)]
    assert set().union(*shards) == every
    assert sum(len(s) for s in shards) == len(every)  # pairwise disjoint
    want = [p.target for p in plans if shard_of(p.target, 3) == 2]
    assert synced_targets(plans, shard=(2, 3))[1] == want


def test_shard_is_a_stable_hash_of_the_target():
    """Adding or removing a plan never moves another plan to a different shard (its
    mirror cache is per shard), and the shard ignores the target's case."""
    import zlib

    assert shard_of(f"{ORG}/O--R01", 8) == shard_of(f"{ORG}/o--r01", 8)
    assert shard_of(f"{ORG}/o--r01", 8) == zlib.crc32(f"{ORG}/o--r01".encode()) % 8 + 1
    plans = pending_plans(20)
    before = {k: synced_targets(plans[1:], shard=(k, 4))[1] for k in range(1, 5)}
    after = {k: synced_targets(plans, shard=(k, 4))[1] for k in range(1, 5)}
    for k in range(1, 5):
        assert [t for t in after[k] if t != plans[0].target] == before[k]


def test_run_sync_shard_after_only():
    plans = pending_plans(6)
    only = ["o/r01", "o/r02", "o/r04", "o/r05"]
    for k in (1, 2):
        want = [f"{ORG}/o--r{i:02d}" for i in (1, 2, 4, 5) if shard_of(f"{ORG}/o--r{i:02d}", 2) == k]
        assert synced_targets(plans, only=only, shard=(k, 2))[1] == want


def test_parse_shard():
    assert parse_shard("3/8") == (3, 8)
    assert parse_shard("1/1") == (1, 1)
    for bad in ("0/8", "9/8", "3", "a/8", "3/b", "-1/8", "1/0", ""):
        with pytest.raises(argparse.ArgumentTypeError):
            parse_shard(bad)
    with pytest.raises(ValueError):
        synced_targets(pending_plans(2), shard=(0, 8))


def test_cli_sync_rejects_bad_shard(tmp_path):
    with pytest.raises(SystemExit) as e:
        main(["--plans", str(tmp_path / "p.csv"), "sync", "--shard", "9/8"])
    assert e.value.code == 2


class Explodes(FakeGitHub):
    def get_repo(self, full_name):
        if full_name.endswith("r01"):
            raise RuntimeError("boom")
        return super().get_repo(full_name)


def test_run_sync_unexpected_exception_continues():
    failed, targets, ev = synced_targets(pending_plans(3), gh=Explodes())
    assert failed == 1
    assert len(targets) == 3
    assert [f["status"] for e, f in ev if e == "sync"] == ["pending", "error: RuntimeError: boom", "pending"]


# --- CLI and workflow ----------------------------------------------------------


def test_cli_sync(tmp_path, upstream, remote, monkeypatch):
    path = remote_path(remote)
    gh = bench_fork(tmp_path, upstream, path)
    gh.add_upstream(INDEX)
    change_upstream(upstream)
    publish(gh, upstream, UP, [path])
    shas = upstream[1]
    write_plans(tmp_path / "plans.csv", [mkplan()])
    cat = tmp_path / "designs.csv"
    cat.write_text(
        "pool,shuttle,code,project,repo,commit,licence,kind,size\n"
        f"tt,tt-gf-0p2,tt_um_x,P,https://github.com/a/upstream,{shas['ttgf0p2']},Apache-2.0,digital,1x1\n"
    )
    monkeypatch.setattr("wafer_space_bench.sync.GhCli", lambda: gh)
    (tmp_path / "bench.toml").write_text(f'work_dir = "{tmp_path / "work"}"\n')
    log = tmp_path / "e.jsonl"
    base = ["--plans", str(tmp_path / "plans.csv"), "--catalog", str(cat), "--log", str(log),
            "--config", str(tmp_path / "bench.toml"), "--remote-fmt", remote]  # fmt: skip
    assert main([*base, "sync", "--dry-run"]) == 0
    assert main([*base, "sync", "--only", "a/upstream", "--delay", "0"]) == 0
    events = [json.loads(line) for line in log.read_text().splitlines()]
    assert [(e["target"], e["status"]) for e in events if e["event"] == "sync"] == [
        (TARGET, "dry-run: +1 ~1 -1"),
        (TARGET, "ok: +1 ~1 -1, about"),
    ]
    assert gh.dispatches == [(INDEX, "bench-updated")]


def test_cli_sync_exit_code_on_error(tmp_path, upstream, remote, monkeypatch):
    path = remote_path(remote)
    gh = FailSetRef(TARGET)
    bench_fork(tmp_path, upstream, path, gh=gh)
    gh.armed = True
    change_upstream(upstream)
    publish(gh, upstream, UP, [path])
    write_plans(tmp_path / "plans.csv", [mkplan()])
    cat = tmp_path / "designs.csv"
    cat.write_text("pool,shuttle,code,project,repo,commit,licence,kind,size\n")
    monkeypatch.setattr("wafer_space_bench.sync.GhCli", lambda: gh)
    base = ["--plans", str(tmp_path / "plans.csv"), "--catalog", str(cat), "--log", str(tmp_path / "e.jsonl"),
            "--config", str(tmp_path / "none.toml"), "--remote-fmt", remote]  # fmt: skip
    monkeypatch.chdir(tmp_path)
    assert main([*base, "sync", "--delay", "0"]) == 1


def _workflow():
    return yaml.safe_load(WORKFLOW.read_text())


def test_sync_workflow_inputs_via_env():
    wf = _workflow()
    runs = [s["run"] for job in wf["jobs"].values() for s in job["steps"] if "run" in s]
    assert runs and all("${{" not in r for r in runs)
    step = next(s for job in wf["jobs"].values() for s in job["steps"] if "wsbench" in s.get("run", ""))
    assert step["env"]["ONLY"] == "${{ inputs.only }}"
    assert step["env"]["DRY_RUN"] == "${{ inputs.dry_run }}"


def test_sync_workflow_shape():
    wf = _workflow()
    on = wf[True] if True in wf else wf["on"]  # YAML 1.1 reads a bare `on` key as True
    assert on["schedule"] == [{"cron": "17 3 * * *"}]
    inputs = on["workflow_dispatch"]["inputs"]
    assert inputs["only"]["type"] == "string" and inputs["only"]["default"] == ""
    assert inputs["dry_run"]["type"] == "boolean" and inputs["dry_run"]["default"] is False
    assert wf["concurrency"] == {"group": "sync", "cancel-in-progress": False}
    assert wf["permissions"] == {"contents": "read"}
    assert set(wf["jobs"]) == {"sync", "notify"}
    job = wf["jobs"]["sync"]
    assert job["timeout-minutes"] == 60
    assert job["strategy"] == {
        "fail-fast": False,
        "max-parallel": 2,
        "matrix": {"shard": [1, 2, 3, 4, 5, 6, 7, 8]},
    }
    uses = [s.get("uses", "") for s in job["steps"]]
    assert uses[0].startswith("actions/create-github-app-token@")
    assert job["steps"][0]["with"] == {
        "app-id": "${{ vars.BENCH_APP_ID }}",
        "private-key": "${{ secrets.BENCH_APP_PRIVATE_KEY }}",
        "owner": "wafer-space-bench",
    }
    key = "mirrors-${{ matrix.shard }}-${{ github.run_id }}"
    restore = next(s for s in job["steps"] if s.get("uses") == "actions/cache/restore@v6")
    assert restore["with"] == {
        "path": "work/mirrors",
        "key": key,
        "restore-keys": "mirrors-${{ matrix.shard }}-",
    }
    save = next(s for s in job["steps"] if s.get("uses") == "actions/cache/save@v6")
    assert save["if"] == "always()" and save["with"] == {"path": "work/mirrors", "key": key}
    assert not any(u.startswith("actions/cache@") for u in uses)  # it would skip saving when sync exits 1
    upload = next(s for s in job["steps"] if s.get("uses") == "actions/upload-artifact@v7")
    assert upload["if"] == "always()" and "work/events.jsonl" in upload["with"]["path"].split()
    assert "${{ matrix.shard }}" in upload["with"]["name"]
    step = next(s for s in job["steps"] if "wsbench" in s.get("run", ""))
    run = step["run"]
    assert "--remote-fmt 'https://github.com/{full}.git'" in run and "--log work/events.jsonl" in run
    assert step["env"]["SHARD"] == "${{ matrix.shard }}" and '--shard "$SHARD/8"' in run
    assert '"${#names[@]}" -gt 0' in run  # a whitespace-only `only` adds no bare --only


# --- final review: one index dispatch per sync run (I3) -------------------------


def test_run_sync_no_dispatch_writes_changed_marker(tmp_path, upstream):
    gh = SyncedFake()
    plans, tos, paths, fmt = three_benches(tmp_path, upstream, gh)
    marker = tmp_path / "out" / "changed"
    kw = {"only": None, "dry_run": False, "remote_fmt": fmt, "now": NOW, "sleep": Sleeps()}
    assert run_sync(plans, tos, gh, cfg(tmp_path), ORG, log=Events(), dispatch=False, changed_file=marker,
                    **kw) == 0  # fmt: skip
    assert not marker.exists()  # nothing changed
    change_upstream(upstream)
    for up, p in zip(["a/up1", "a/up2", "a/up3"], paths, strict=True):
        publish(gh, upstream, up, [p])
    assert run_sync(plans, tos, gh, cfg(tmp_path), ORG, log=Events(), dispatch=False, changed_file=marker,
                    **kw) == 0  # fmt: skip
    assert marker.exists()
    assert gh.dispatches == []


def test_run_sync_dry_run_writes_no_marker(tmp_path, upstream):
    gh = SyncedFake()
    plans, tos, paths, fmt = three_benches(tmp_path, upstream, gh)
    change_upstream(upstream)
    for up, p in zip(["a/up1", "a/up2", "a/up3"], paths, strict=True):
        publish(gh, upstream, up, [p])
    marker = tmp_path / "changed"
    run_sync(plans, tos, gh, cfg(tmp_path), ORG, dry_run=True, remote_fmt=fmt, now=NOW, log=Events(),
             sleep=Sleeps(), dispatch=False, changed_file=marker)  # fmt: skip
    assert not marker.exists()


def test_cli_sync_no_dispatch_and_changed_file(tmp_path, upstream, remote, monkeypatch):
    path = remote_path(remote)
    gh = bench_fork(tmp_path, upstream, path)
    gh.add_upstream(INDEX)
    change_upstream(upstream)
    publish(gh, upstream, UP, [path])
    write_plans(tmp_path / "plans.csv", [mkplan()])
    cat = tmp_path / "designs.csv"
    cat.write_text("pool,shuttle,code,project,repo,commit,licence,kind,size\n")
    monkeypatch.setattr("wafer_space_bench.sync.GhCli", lambda: gh)
    base = ["--plans", str(tmp_path / "plans.csv"), "--catalog", str(cat), "--log", str(tmp_path / "e.jsonl"),
            "--config", str(tmp_path / "none.toml"), "--remote-fmt", remote]  # fmt: skip
    monkeypatch.chdir(tmp_path)
    marker = tmp_path / "work" / "changed"
    assert main([*base, "sync", "--delay", "0", "--no-dispatch", "--changed-file", str(marker)]) == 0
    assert marker.exists() and gh.dispatches == []


def test_sync_workflow_dispatches_once_from_a_notify_job():
    wf = _workflow()
    sync_job, notify = wf["jobs"]["sync"], wf["jobs"]["notify"]
    run = next(s for s in sync_job["steps"] if "wsbench" in s.get("run", ""))["run"]
    assert "--no-dispatch" in run and "--changed-file work/changed" in run
    upload = next(s for s in sync_job["steps"] if s.get("uses", "").startswith("actions/upload-artifact@"))
    assert upload["with"]["path"].split() == ["work/events.jsonl", "work/changed"]
    assert notify["needs"] == "sync" and notify["if"] == "always()"
    steps = notify["steps"]
    download = next(s for s in steps if s.get("uses", "").startswith("actions/download-artifact@"))
    assert download["with"]["pattern"] == "sync-events-*"
    token = next(s for s in steps if s.get("uses", "").startswith("actions/create-github-app-token@"))
    assert token["with"]["owner"] == "wafer-space-bench"
    dispatch = next(s for s in steps if "dispatches" in s.get("run", ""))
    assert dispatch["env"]["GH_TOKEN"] == "${{ steps.app-token.outputs.token }}"
    assert (
        f"repos/{INDEX_REPO}/dispatches" in dispatch["run"] and "event_type=bench-updated" in dispatch["run"]
    )
    assert dispatch["if"] == token["if"] == "steps.changed.outputs.any == 'true'"
    runs = [s["run"] for s in steps if "run" in s]
    assert all("${{" not in r for r in runs)
