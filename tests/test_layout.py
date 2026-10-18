"""Tests for the branch layout (about, upstream/*, tapeout/*).

The layout writes branches with git pushes to a bare `file://` remote and switches the
default branch through the API. `SyncedFake` (tests/fakes.py) copies the remote's heads and
commits into the fake before each API write, so `set_default_branch` sees the pushed
`about`; a test that compares the fake with the remote calls `gh._sync` first. Final-state
assertions are made on the real remote. `record_pushes` logs the remote's pushes and the
default-branch switch in one sequence, to pin their order.
"""

import json
import os

import yaml
from fakes import SyncedFake, sh

from wafer_space_bench import git as git_module
from wafer_space_bench.catalog import Tapeout
from wafer_space_bench.cli import main
from wafer_space_bench.config import LocalConfig
from wafer_space_bench.copy import local_clone_path, run_copy
from wafer_space_bench.git import Git
from wafer_space_bench.github import GitHubError, RateLimited
from wafer_space_bench.layout import LayoutPlan, apply_layout, plan_layout, run_layouts
from wafer_space_bench.naming import RepoRef
from wafer_space_bench.scan import RepoPlan, write_plans

ORG = "wafer-space-bench"
TARGET = f"{ORG}/a--upstream"
TAG = "tinytapeout/ttgf0p2/tt_um_x"
NOW = "2026-10-17T00:00:00Z"
LATER = "2026-10-18T00:00:00Z"


class Events(list):
    def __call__(self, event, **fields):
        self.append((event, fields))


class Sleeps(list):
    def __call__(self, s):
        self.append(s)


def plan():
    return RepoPlan(
        RepoRef("github.com", "a", "upstream"), "a/upstream", "live", "a/upstream", "main", 1, "fork", TARGET
    )


def tapeouts(commit):
    return {
        "a/upstream": [
            Tapeout(
                "tinytapeout",
                "ttgf0p2",
                "tt_um_x",
                "P",
                "1x1",
                "digital",
                "Apache-2.0",
                commit,
                "",
                "a/upstream",
            )
        ]
    }


def cfg(tmp_path):
    return LocalConfig(work_dir=tmp_path / "work")


def setup(tmp_path, upstream, remote, *, extra_heads=(), tag=True, in_org=True):
    """Push the upstream fixture (plus `extra_heads` as name -> source branch) to the remote,
    with the annotated tapeout tag on ttgf0p2; register the bench repo in a SyncedFake."""
    path, shas = upstream
    if tag:
        sh(path, "tag", "-a", TAG, "-m", "taped out", shas["ttgf0p2"])
    for name, src in extra_heads:
        sh(path, "branch", name, src)
    sh(path, "push", "-q", remote, "refs/heads/*:refs/heads/*", "refs/tags/*:refs/tags/*")
    gh = SyncedFake(tmp_path / "remote.git")
    if in_org:
        gh.add_upstream(TARGET, branches=shas)
        gh._sync(TARGET)
    return gh, shas


def remote_heads(tmp_path):
    g = Git(tmp_path / "remote.git")
    return {h: g.rev_parse(f"refs/heads/{h}") for h in g.heads()}


def run(gh, tmp_path, remote, shas, *, now=NOW, ev=None, sleep=None, **kw):
    return apply_layout(
        plan(),
        tapeouts(shas["ttgf0p2"]),
        gh,
        cfg(tmp_path),
        ORG,
        remote_fmt=remote,
        now=now,
        log=ev if ev is not None else Events(),
        sleep=sleep if sleep is not None else Sleeps(),
        **kw,
    )


def writes(gh):
    return [c for c in gh.calls if c[0] in ("set_ref", "delete_ref", "set_default_branch")]


ZERO = "0" * 40


class OrderedFake(SyncedFake):
    """SyncedFake that appends `set_default_branch` to the push log `record_pushes` keeps."""

    def __init__(self, remote_path, log_path):
        super().__init__(remote_path)
        self.log_path = log_path

    def set_default_branch(self, full_name, branch):
        super().set_default_branch(full_name, branch)
        with open(self.log_path, "a") as f:
            f.write("set_default_branch\n")


def record_pushes(tmp_path):
    """A post-receive hook on the remote appending `push` and one `<old> <new> <ref>` line per
    updated ref for every push (up-to-date refs are not listed). Returns the log path."""
    log = tmp_path / "order.log"
    hook = tmp_path / "remote.git" / "hooks" / "post-receive"
    hook.write_text(f'#!/bin/sh\n{{ echo push; cat; }} >> "{log}"\n')
    hook.chmod(0o755)
    return log


def pushes(log):
    """The log as [("push", {ref: "create" | "update" | "delete"}) | ("set_default_branch",)]."""
    out = []
    for line in log.read_text().splitlines() if log.exists() else []:
        if line in ("push", "set_default_branch"):
            out.append((line, {}) if line == "push" else (line,))
        else:
            old, new, ref = line.split()
            out[-1][1][ref] = "create" if old == ZERO else "delete" if new == ZERO else "update"
    return out


# --- plan_layout -------------------------------------------------------------


def test_plan_layout_maps_original_branches():
    p = plan_layout(["main", "feature/x"], [])
    assert p == LayoutPlan(
        upstream={"main": "upstream/main", "feature/x": "upstream/feature/x"},
        tapeout=[],
        delete=["feature/x", "main"],
        conflicts=[],
    )


def test_plan_layout_skips_bench_branches():
    p = plan_layout(["about", "upstream/main", "tapeout/" + TAG, "main"], [TAG])
    assert p.upstream == {"main": "upstream/main"}
    assert p.delete == ["main"]


def test_plan_layout_tapeout_from_tags():
    tags = [
        TAG,
        "google/gf180mcu-mpw0/slot_1",
        "v1.0",
        "other/run/slot",
        "tinytapeout/ttgf0p2",
        "wafer.space/a/b/c",
    ]
    assert plan_layout(["main"], tags).tapeout == ["google/gf180mcu-mpw0/slot_1", TAG]


def test_layout_conflicts():
    assert plan_layout(["about", "main"], []).conflicts == ["about"]
    assert plan_layout(["x", "x/y"], []).conflicts == ["x", "x/y"]
    assert plan_layout(["about/x", "main"], []).conflicts == ["about/x"]  # the head, not "about"
    assert plan_layout(["dev/z", "main"], []).conflicts == ["dev/z"]
    assert plan_layout(["dev/z", "main"], [], upstream_dev=True).conflicts == []
    # an original `upstream` (or `tapeout`) branch collides with the namespace it would need
    assert plan_layout(["upstream", "main"], []).conflicts == ["upstream"]
    assert plan_layout(["tapeout", "main"], [TAG]).conflicts == ["tapeout"]
    assert plan_layout(["main", "feature/x"], [TAG]).conflicts == []


# --- apply_layout ------------------------------------------------------------


def test_apply_layout_end_to_end(tmp_path, upstream, remote):
    gh, shas = setup(tmp_path, upstream, remote)
    ev = Events()
    status = run(gh, tmp_path, remote, shas, ev=ev)
    assert status == "ok: 3 upstream, 1 tapeout"
    heads = remote_heads(tmp_path)
    assert set(heads) == {
        "about",
        "upstream/main",
        "upstream/feature/x",
        "upstream/ttgf0p2",
        f"tapeout/{TAG}",
    }
    for b in ("main", "feature/x", "ttgf0p2"):
        assert heads[f"upstream/{b}"] == shas[b]
    assert heads[f"tapeout/{TAG}"] == shas["ttgf0p2"]
    assert gh.default_branch[TARGET] == "about"
    gh._sync(TARGET)  # the pushes went to the remote only
    assert gh.branches[TARGET] == heads
    about = Git(tmp_path / "remote.git")
    data = yaml.safe_load(about.show("refs/heads/about", "bench.yaml"))
    assert data["branches"] == sorted(heads)
    assert data["generated"] == NOW
    assert "README.md" in sh(tmp_path / "remote.git", "ls-tree", "--name-only", "refs/heads/about").split()
    assert ("layout", {"target": TARGET, "status": status}) in ev


def test_apply_layout_upstream_dev_branch_then_ours_kept(tmp_path, upstream, remote):
    gh, shas = setup(tmp_path, upstream, remote, extra_heads=[("dev/w", "main")])
    assert run(gh, tmp_path, remote, shas) == "ok: 4 upstream, 1 tapeout"
    heads = remote_heads(tmp_path)
    assert heads["upstream/dev/w"] == shas["main"] and "dev/w" not in heads
    data = yaml.safe_load(Git(tmp_path / "remote.git").show("refs/heads/about", "bench.yaml"))
    assert "upstream/dev/w" in data["branches"] and "dev/w" not in data["branches"]

    # Once laid out, dev/* is ours: a re-run keeps it.
    sh(tmp_path / "remote.git", "branch", "dev/mine", "upstream/main")
    gh._sync(TARGET)
    assert run(gh, tmp_path, remote, shas, now=LATER) == "ok: 4 upstream, 1 tapeout"
    assert remote_heads(tmp_path)["dev/mine"] == shas["main"]


def test_apply_layout_sets_default_before_delete(tmp_path, upstream, remote):
    """about push, one push creating every branch, the default switch, one push deleting the
    originals; no ref is written through the refs API."""
    path, shas = upstream
    sh(path, "tag", "-a", TAG, "-m", "taped out", shas["ttgf0p2"])
    sh(path, "push", "-q", remote, "refs/heads/*:refs/heads/*", "refs/tags/*:refs/tags/*")
    log = record_pushes(tmp_path)
    gh = OrderedFake(tmp_path / "remote.git", log)
    gh.add_upstream(TARGET, branches=shas)
    assert run(gh, tmp_path, remote, shas) == "ok: 3 upstream, 1 tapeout"
    assert pushes(log) == [
        ("push", {"refs/heads/about": "create"}),
        (
            "push",
            {f"refs/heads/upstream/{b}": "create" for b in shas} | {f"refs/heads/tapeout/{TAG}": "create"},
        ),
        ("set_default_branch",),
        ("push", {f"refs/heads/{b}": "delete" for b in shas}),
    ]
    assert [c[0] for c in writes(gh)] == ["set_default_branch"]


def test_apply_layout_idempotent(tmp_path, upstream, remote):
    gh, shas = setup(tmp_path, upstream, remote)
    first = run(gh, tmp_path, remote, shas)
    before = remote_heads(tmp_path)
    n = len(gh.calls)
    log = record_pushes(tmp_path)
    second = run(gh, tmp_path, remote, shas, now=LATER)
    assert second == first
    assert writes(gh)[n:] == []  # no writes (the fake records only writes; reads are not checked)
    assert pushes(log) == []
    assert remote_heads(tmp_path) == before  # no new about commit for a timestamp-only change


def test_apply_layout_pending(tmp_path, upstream, remote):
    gh, shas = setup(tmp_path, upstream, remote, in_org=False)
    before = remote_heads(tmp_path)
    ev = Events()
    assert run(gh, tmp_path, remote, shas, ev=ev) == "pending"
    assert remote_heads(tmp_path) == before
    assert ev == [("layout", {"target": TARGET, "status": "pending"})]


def test_apply_layout_conflict_upstream_about(tmp_path, upstream, remote):
    gh, shas = setup(tmp_path, upstream, remote, extra_heads=[("about", "main")])
    before = remote_heads(tmp_path)
    status = run(gh, tmp_path, remote, shas)
    assert status == "conflict: about"
    assert writes(gh) == []
    assert remote_heads(tmp_path) == before
    assert not local_clone_path(cfg(tmp_path), TARGET).exists()  # nothing cloned


def test_apply_layout_conflict_stray_bench_names(tmp_path, upstream, remote):
    gh, shas = setup(
        tmp_path,
        upstream,
        remote,
        extra_heads=[
            ("upstream/gone", "main"),
            ("upstream/feature/x", "main"),
            ("tapeout/x/y/z", "main"),
        ],
    )
    before = remote_heads(tmp_path)
    status = run(gh, tmp_path, remote, shas)
    assert status == "conflict: tapeout/x/y/z, upstream/feature/x, upstream/gone"
    assert writes(gh) == []
    assert remote_heads(tmp_path) == before


def test_apply_layout_conflict_tapeout_branch_differs_from_tag(tmp_path, upstream, remote):
    gh, shas = setup(tmp_path, upstream, remote, extra_heads=[(f"tapeout/{TAG}", "main")])
    assert run(gh, tmp_path, remote, shas) == f"conflict: tapeout/{TAG}"
    assert writes(gh) == []


def test_apply_layout_resumes_partial_run(tmp_path, upstream, remote):
    """An earlier run pushed upstream/main and tapeout/* but stopped before the default switch."""
    gh, shas = setup(
        tmp_path, upstream, remote, extra_heads=[("upstream/main", "main"), (f"tapeout/{TAG}", "ttgf0p2")]
    )
    log = record_pushes(tmp_path)
    status = run(gh, tmp_path, remote, shas)
    assert status == "ok: 3 upstream, 1 tapeout"
    assert set(remote_heads(tmp_path)) == {
        "about", "upstream/main", "upstream/feature/x", "upstream/ttgf0p2", f"tapeout/{TAG}"
    }  # fmt: skip
    created = [r for p in pushes(log) if p[0] == "push" for r, op in p[1].items() if op == "create"]
    assert sorted(created) == [
        "refs/heads/about",
        "refs/heads/upstream/feature/x",
        "refs/heads/upstream/ttgf0p2",
    ]


class FailOnce(SyncedFake):
    def __init__(self, remote_path, method, exc):
        super().__init__(remote_path)
        self._fail = {method: exc}

    def set_default_branch(self, full_name, branch):
        if exc := self._fail.pop("set_default_branch", None):
            raise exc
        super().set_default_branch(full_name, branch)


def test_apply_layout_resumes_after_crash_before_default(tmp_path, upstream, remote):
    path, shas = upstream
    sh(path, "tag", "-a", TAG, "-m", "t", shas["ttgf0p2"])
    sh(path, "push", "-q", remote, "refs/heads/*:refs/heads/*", "refs/tags/*:refs/tags/*")
    gh = FailOnce(tmp_path / "remote.git", "set_default_branch", GitHubError("boom (HTTP 500)"))
    gh.add_upstream(TARGET, branches=shas)
    ev = Events()
    first = run(gh, tmp_path, remote, shas, ev=ev)
    assert first.startswith("error: ") and "boom" in first
    assert {"about", "main", "upstream/main"} <= set(remote_heads(tmp_path))  # half done
    assert run(gh, tmp_path, remote, shas) == "ok: 3 upstream, 1 tapeout"
    assert set(remote_heads(tmp_path)) == {
        "about", "upstream/main", "upstream/feature/x", "upstream/ttgf0p2", f"tapeout/{TAG}"
    }  # fmt: skip
    assert gh.default_branch[TARGET] == "about"


def test_apply_layout_resumes_upstream_dev_delete_after_crash(tmp_path, upstream, remote):
    """The delete push fails (here: `main` moved) after the default switch. The re-run, now
    laid out, still deletes the upstream `dev/w` original and leaves it out of `about`."""
    path, shas = upstream
    sh(path, "branch", "dev/w", shas["main"])
    sh(path, "push", "-q", remote, "refs/heads/*:refs/heads/*")
    gh = MoveAfterDefault(
        tmp_path / "remote.git", lambda g: g.update_ref("refs/heads/main", shas["feature/x"])
    )
    gh.add_upstream(TARGET, branches=shas)
    assert run(gh, tmp_path, remote, shas).startswith("error: ")
    assert gh.default_branch[TARGET] == "about" and "dev/w" in remote_heads(tmp_path)  # laid out, not deleted
    assert run(gh, tmp_path, remote, shas, now=LATER) == "ok: 4 upstream, 0 tapeout, 1 mismatch"
    heads = remote_heads(tmp_path)
    assert "dev/w" not in heads and heads["upstream/dev/w"] == shas["main"]
    data = yaml.safe_load(Git(tmp_path / "remote.git").show("refs/heads/about", "bench.yaml"))
    assert "dev/w" not in data["branches"]


def test_apply_layout_retries_rate_limited(tmp_path, upstream, remote):
    path, shas = upstream
    sh(path, "push", "-q", remote, "refs/heads/*:refs/heads/*")
    gh = FailOnce(tmp_path / "remote.git", "set_default_branch", RateLimited("too quickly"))
    gh.add_upstream(TARGET, branches=shas)
    ev, sleeps = Events(), Sleeps()
    assert run(gh, tmp_path, remote, shas, ev=ev, sleep=sleeps) == "ok: 3 upstream, 0 tapeout"
    assert sleeps == [60]
    assert [e for e, _ in ev].count("backoff") == 1


def test_apply_layout_never_moves_tapeout_branch(tmp_path, upstream, remote):
    gh, shas = setup(tmp_path, upstream, remote)
    run(gh, tmp_path, remote, shas)
    Git(tmp_path / "remote.git").update_ref(f"refs/heads/tapeout/{TAG}", shas["main"])
    ev = Events()
    assert run(gh, tmp_path, remote, shas, ev=ev) == "ok: 3 upstream, 1 tapeout, 1 mismatch"
    assert remote_heads(tmp_path)[f"tapeout/{TAG}"] == shas["main"]
    assert ("tapeout-mismatch", {"target": TARGET, "branch": f"tapeout/{TAG}",
                                 "branch_sha": shas["main"], "tag_sha": shas["ttgf0p2"]}) in ev  # fmt: skip


def test_apply_layout_new_upstream_branch_after_layout(tmp_path, upstream, remote):
    """A laid-out repo that gains an original branch (e.g. a half-finished delete) is finished."""
    gh, shas = setup(tmp_path, upstream, remote)
    run(gh, tmp_path, remote, shas)
    Git(tmp_path / "remote.git").update_ref("refs/heads/main", shas["main"])
    assert run(gh, tmp_path, remote, shas) == "ok: 3 upstream, 1 tapeout"
    assert "main" not in remote_heads(tmp_path)


def test_apply_layout_keeps_differing_original_after_layout(tmp_path, upstream, remote):
    """In a laid-out repo, an original `b` that differs from `upstream/b` is neither copied
    over `upstream/b` nor deleted: it is logged and left for a human."""
    gh, shas = setup(tmp_path, upstream, remote)
    run(gh, tmp_path, remote, shas)
    Git(tmp_path / "remote.git").update_ref("refs/heads/main", shas["feature/x"])
    ev = Events()
    assert run(gh, tmp_path, remote, shas, ev=ev) == "ok: 3 upstream, 1 tapeout, 1 mismatch"
    heads = remote_heads(tmp_path)
    assert heads["main"] == shas["feature/x"]
    assert heads["upstream/main"] == shas["main"]
    assert ("upstream-mismatch", {"target": TARGET, "branch": "main", "branch_sha": shas["feature/x"],
                                  "upstream_sha": shas["main"]}) in ev  # fmt: skip


def test_apply_layout_dry_run(tmp_path, upstream, remote):
    gh, shas = setup(tmp_path, upstream, remote)
    before = remote_heads(tmp_path)
    ev = Events()
    assert run(gh, tmp_path, remote, shas, ev=ev, dry_run=True) == "dry-run: 3 upstream, 1 tapeout"
    assert writes(gh) == []
    assert remote_heads(tmp_path) == before
    assert ev[-1][0] == "layout" and ev[-1][1]["status"] == "dry-run: 3 upstream, 1 tapeout"
    assert not local_clone_path(cfg(tmp_path), TARGET).exists()  # no clone, no fetch


def test_apply_layout_git_error_is_returned(tmp_path, upstream, remote):
    gh, _ = setup(tmp_path, upstream, remote)
    status = apply_layout(
        plan(), {}, gh, cfg(tmp_path), ORG, remote_fmt=(tmp_path / "nope.git").as_uri(), now=NOW,
        log=Events(), sleep=Sleeps(),
    )  # fmt: skip
    assert status.startswith("error: ")
    assert str(tmp_path) not in status


# --- CLI ---------------------------------------------------------------------


def test_cli_layout(tmp_path, upstream, remote, monkeypatch):
    gh, shas = setup(tmp_path, upstream, remote)
    other = RepoPlan(RepoRef("github.com", "b", "none"), "", "missing", "", "", 0, "none", f"{ORG}/b--none")
    pending = RepoPlan(
        RepoRef("github.com", "c", "x"), "c/x", "live", "c/x", "main", 1, "copy", f"{ORG}/c--x"
    )
    write_plans(tmp_path / "plans.csv", [plan(), other, pending])
    cat = tmp_path / "designs.csv"
    cat.write_text(
        "pool,shuttle,code,project,repo,commit,licence,kind,size\n"
        f"tt,tt-gf-0p2,tt_um_x,P,https://github.com/a/upstream,{shas['ttgf0p2']},Apache-2.0,digital,1x1\n"
    )
    monkeypatch.setattr("wafer_space_bench.layout.GhCli", lambda: gh)
    (tmp_path / "bench.toml").write_text(f'work_dir = "{tmp_path / "work"}"\n')
    log = tmp_path / "e.jsonl"
    base = ["--plans", str(tmp_path / "plans.csv"), "--catalog", str(cat), "--log", str(log),
            "--config", str(tmp_path / "bench.toml"), "--remote-fmt", remote]  # fmt: skip
    assert main([*base, "layout", "--dry-run"]) == 0
    assert writes(gh) == []
    assert main([*base, "layout", "--only", "A--Upstream", "--delay", "0"]) == 0
    events = [json.loads(line) for line in log.read_text().splitlines()]
    layout = [(e["target"], e["status"]) for e in events if e["event"] == "layout"]
    assert layout == [
        (TARGET, "dry-run: 3 upstream, 1 tapeout"),
        (f"{ORG}/c--x", "pending"),
        (TARGET, "ok: 3 upstream, 1 tapeout"),
    ]
    assert gh.default_branch[TARGET] == "about"


def test_cli_layout_exit_code_on_conflict(tmp_path, upstream, remote, monkeypatch):
    gh, _ = setup(tmp_path, upstream, remote, extra_heads=[("about", "main")])
    write_plans(tmp_path / "plans.csv", [plan()])
    cat = tmp_path / "designs.csv"
    cat.write_text("pool,shuttle,code,project,repo,commit,licence,kind,size\n")
    monkeypatch.setattr("wafer_space_bench.layout.GhCli", lambda: gh)
    base = ["--plans", str(tmp_path / "plans.csv"), "--catalog", str(cat), "--log", str(tmp_path / "e.jsonl"),
            "--config", str(tmp_path / "none.toml"), "--remote-fmt", remote]  # fmt: skip
    monkeypatch.chdir(tmp_path)
    assert main([*base, "layout"]) == 1


# --- review fixes --------------------------------------------------------------


def test_apply_layout_empty_repo_is_not_laid_out(tmp_path, upstream, remote):
    """A bench repo with no branches yet (copy push failed, fork not ready) is left alone, so
    a later copy still pushes its heads; then the layout runs normally."""
    _, shas = upstream
    gh = SyncedFake(tmp_path / "remote.git")
    gh.add_upstream(TARGET, branches={})
    ev = Events()
    assert run(gh, tmp_path, remote, shas, ev=ev) == "empty"
    assert writes(gh) == [] and remote_heads(tmp_path) == {}
    assert run_layouts([plan()], tapeouts(shas["ttgf0p2"]), gh, cfg(tmp_path), ORG, remote_fmt=remote,
                       log=Events(), now=lambda: NOW, sleep=Sleeps()) == 1  # fmt: skip
    copy = RepoPlan(RepoRef("github.com", "a", "upstream"), "a/upstream", "live", "x/root", "main", 1, "copy",
                    TARGET)  # fmt: skip
    mirror_cfg = LocalConfig(work_dir=tmp_path / "work", mirrors={"https://github.com/a/": tmp_path})
    assert run_copy(copy, tapeouts(shas["ttgf0p2"]), gh, mirror_cfg, ORG, remote, log=Events()) == "pushed"
    assert run(gh, tmp_path, remote, shas) == "ok: 3 upstream, 1 tapeout"
    assert set(remote_heads(tmp_path)) == {
        "about", "upstream/main", "upstream/feature/x", "upstream/ttgf0p2", f"tapeout/{TAG}"
    }  # fmt: skip


class MoveAfterDefault(SyncedFake):
    """Runs `act(remote Git)` right after the default branch switch (a concurrent push)."""

    def __init__(self, remote_path, act):
        super().__init__(remote_path)
        self._act = act

    def set_default_branch(self, full_name, branch):
        super().set_default_branch(full_name, branch)
        self._act(self.remote)


def test_apply_layout_lease_keeps_branch_moved_since_listing(tmp_path, upstream, remote):
    """`main` moves between the listing and the delete: the lease fails the atomic delete
    push, so no original is deleted and the run reports an error. A re-run (now laid out)
    deletes the others and leaves `main`, whose new commit is not under upstream/main."""
    path, shas = upstream
    sh(path, "push", "-q", remote, "refs/heads/*:refs/heads/*")
    gh = MoveAfterDefault(
        tmp_path / "remote.git", lambda g: g.update_ref("refs/heads/main", shas["feature/x"])
    )
    gh.add_upstream(TARGET, branches=shas)
    status = run(gh, tmp_path, remote, shas)
    assert status.startswith("error: git push") and "stale info" in status
    assert str(tmp_path) not in status
    heads = remote_heads(tmp_path)
    assert heads["main"] == shas["feature/x"]
    assert heads["feature/x"] == shas["feature/x"] and heads["ttgf0p2"] == shas["ttgf0p2"]  # atomic
    assert heads["upstream/main"] == shas["main"] and gh.default_branch[TARGET] == "about"

    ev = Events()
    assert run(gh, tmp_path, remote, shas, ev=ev) == "ok: 3 upstream, 0 tapeout, 1 mismatch"
    heads = remote_heads(tmp_path)
    assert heads["main"] == shas["feature/x"] and "feature/x" not in heads and "ttgf0p2" not in heads
    assert ("upstream-mismatch", {"target": TARGET, "branch": "main", "branch_sha": shas["feature/x"],
                                  "upstream_sha": shas["main"]}) in ev  # fmt: skip


def test_apply_layout_deletes_only_verified_copies(tmp_path, upstream, remote):
    """An `upstream/` copy that is gone by the delete push fails it: no original is deleted."""
    path, shas = upstream
    sh(path, "push", "-q", remote, "refs/heads/*:refs/heads/*")
    gh = MoveAfterDefault(tmp_path / "remote.git", lambda g: g.delete_ref("refs/heads/upstream/feature/x"))
    gh.add_upstream(TARGET, branches=shas)
    assert run(gh, tmp_path, remote, shas).startswith("error: ")
    heads = remote_heads(tmp_path)
    assert all(heads[b] == sha for b, sha in shas.items())


def mirror_snapshot(root):
    """Every ref, and every file's and directory's (mtime, size, link count), under bare repo `root`."""
    entries = {}
    for d, dirs, names in os.walk(root):
        for n in [*dirs, *names]:
            st = os.stat(os.path.join(d, n))
            entries[os.path.relpath(os.path.join(d, n), root)] = (st.st_mtime_ns, st.st_size, st.st_nlink)
    st = os.stat(root)
    entries["."] = (st.st_mtime_ns, st.st_size, st.st_nlink)
    return sh(root, "for-each-ref", "--format=%(refname) %(objectname)"), entries


def test_apply_layout_without_mirror_fetches_bench_repo_only(tmp_path, upstream, remote, monkeypatch):
    """No local mirror: no clone of the public upstream; an empty repo fetches the bench repo."""
    gh, shas = setup(tmp_path, upstream, remote)
    calls = []
    real_run = git_module._run

    def spy(args, cwd=None, **kw):
        calls.append(args)
        return real_run(args, cwd=cwd, **kw)

    monkeypatch.setattr(git_module, "_run", spy)
    ev = Events()
    assert run(gh, tmp_path, remote, shas, ev=ev) == "ok: 3 upstream, 1 tapeout"
    assert not [a for a in calls if a[0] == "clone"]
    assert not [e for e, _ in ev if e == "seed-failed"]
    assert not any("https://" in a for args in calls for a in args)
    clone = local_clone_path(cfg(tmp_path), TARGET)
    assert sh(clone, "rev-parse", "refs/bench/heads/main") == shas["main"]


def test_apply_layout_seeds_clone_from_local_mirror_read_only(tmp_path, upstream, remote, monkeypatch):
    """With a local mirror of upstream, the clone is seeded from it (copied, not hardlinked)
    and git never runs inside it: its refs and files are unchanged."""
    gh, shas = setup(tmp_path, upstream, remote)
    mirrors = tmp_path / "mirrors"
    mirror = mirrors / "a" / "upstream.git"
    sh(tmp_path, "clone", "-q", "--bare", str(upstream[0]), str(mirror))
    before = mirror_snapshot(mirror)
    cwds = []
    real_run = git_module._run

    def spy(args, cwd=None, **kw):
        cwds.append((cwd, args))
        return real_run(args, cwd=cwd, **kw)

    monkeypatch.setattr(git_module, "_run", spy)
    mcfg = LocalConfig(work_dir=tmp_path / "work", mirrors={"https://github.com/": mirrors})
    ev = Events()
    status = apply_layout(plan(), tapeouts(shas["ttgf0p2"]), gh, mcfg, ORG, remote_fmt=remote, now=NOW,
                          log=ev, sleep=Sleeps())  # fmt: skip
    assert status == "ok: 3 upstream, 1 tapeout"
    assert not [e for e, _ in ev if e == "seed-failed"]
    clone = local_clone_path(mcfg, TARGET)
    assert sh(clone, "config", "remote.origin.url") == str(mirror)  # seeded from the mirror
    assert sh(clone, "rev-parse", f"refs/bench/tags/{TAG}^{{commit}}") == shas["ttgf0p2"]
    assert mirror_snapshot(mirror) == before
    clones = [args for cwd, args in cwds if args[0] == "clone"]
    assert clones == [["clone", "--bare", "--quiet", "--no-hardlinks", str(mirror), f"{clone}.tmp"]]
    for cwd, args in cwds:
        assert cwd is None or mirror not in (cwd, *cwd.parents), args
        assert not any(a in ("-C", "--git-dir") or a.startswith("--git-dir=") for a in args), args


def test_apply_layout_laid_out_skips_df_clash(tmp_path, upstream, remote):
    """Once laid out, a new original `feature` cannot become upstream/feature next to
    upstream/feature/x: it is skipped, logged and counted, and not named in bench.yaml."""
    gh, shas = setup(tmp_path, upstream, remote)
    run(gh, tmp_path, remote, shas)
    Git(tmp_path / "remote.git").update_ref("refs/heads/feature", shas["main"])
    n = len(gh.calls)
    ev = Events()
    assert run(gh, tmp_path, remote, shas, ev=ev) == "ok: 3 upstream, 1 tapeout, 1 mismatch"
    assert writes(gh)[n:] == []
    assert remote_heads(tmp_path)["feature"] == shas["main"]
    assert ("branch-conflict", {"target": TARGET, "branch": "feature", "new": "upstream/feature"}) in ev
    data = yaml.safe_load(Git(tmp_path / "remote.git").show("refs/heads/about", "bench.yaml"))
    assert "upstream/feature" not in data["branches"]


def test_run_layouts_validates_org_before_any_write(tmp_path, upstream, remote):
    gh, _ = setup(tmp_path, upstream, remote)
    bad = RepoPlan(RepoRef("github.com", "b", "x"), "b/x", "live", "b/x", "main", 1, "fork", "other-org/b--x")
    try:
        run_layouts([plan(), bad], {}, gh, cfg(tmp_path), ORG, remote_fmt=remote, log=Events(),
                    now=lambda: NOW, sleep=Sleeps())  # fmt: skip
    except ValueError as e:
        assert "other-org/b--x" in str(e)
    else:
        raise AssertionError("expected ValueError")
    assert writes(gh) == []
