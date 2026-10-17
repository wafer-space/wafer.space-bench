"""Tests for the branch layout (about, upstream/*, tapeout/*).

FakeGitHub and the real git remote are kept consistent by `SyncedFake` (tests/fakes.py):
its `set_ref`, `delete_ref` and `set_default_branch` also apply the change to the bare
`file://` remote (git update-ref / symbolic-ref), and first copy the remote's heads and
commits into the fake. `ls-remote` and the `about` push therefore see exactly what the API
calls did, and the final-state assertions are made on the real remote.
"""

import json

import yaml
from fakes import SyncedFake, sh

from wafer_space_bench.catalog import Tapeout
from wafer_space_bench.cli import main
from wafer_space_bench.config import LocalConfig
from wafer_space_bench.copy import run_copy
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
    assert gh.branches[TARGET] == heads  # the fake and the real remote agree
    about = Git(tmp_path / "remote.git")
    data = yaml.safe_load(about.show("refs/heads/about", "bench.yaml"))
    assert data["branches"] == sorted(heads)
    assert data["generated"] == NOW
    assert "README.md" in sh(tmp_path / "remote.git", "ls-tree", "--name-only", "refs/heads/about").split()
    assert ("layout", {"target": TARGET, "status": status}) in ev


def test_apply_layout_sets_default_before_delete(tmp_path, upstream, remote):
    gh, shas = setup(tmp_path, upstream, remote)
    run(gh, tmp_path, remote, shas)
    names = [c[0] for c in writes(gh)]
    default = names.index("set_default_branch")
    deletes = [i for i, n in enumerate(names) if n == "delete_ref"]
    sets = [i for i, n in enumerate(names) if n == "set_ref"]
    assert len(deletes) == 3 and len(sets) == 4
    assert max(sets) < default < min(deletes)


def test_apply_layout_idempotent(tmp_path, upstream, remote):
    gh, shas = setup(tmp_path, upstream, remote)
    first = run(gh, tmp_path, remote, shas)
    before = remote_heads(tmp_path)
    n = len(gh.calls)
    second = run(gh, tmp_path, remote, shas, now=LATER)
    assert second == first
    assert writes(gh)[n:] == []  # no writes (the fake records only writes; reads are not checked)
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


def test_apply_layout_conflict_stray_bench_names(tmp_path, upstream, remote):
    gh, shas = setup(
        tmp_path,
        upstream,
        remote,
        extra_heads=[
            ("upstream/gone", "main"),
            ("upstream/feature/x", "main"),
            ("tapeout/x/y/z", "main"),
            ("dev/w", "main"),
        ],
    )
    before = remote_heads(tmp_path)
    status = run(gh, tmp_path, remote, shas)
    assert status == "conflict: dev/w, tapeout/x/y/z, upstream/feature/x, upstream/gone"
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
    status = run(gh, tmp_path, remote, shas)
    assert status == "ok: 3 upstream, 1 tapeout"
    assert set(remote_heads(tmp_path)) == {
        "about", "upstream/main", "upstream/feature/x", "upstream/ttgf0p2", f"tapeout/{TAG}"
    }  # fmt: skip
    assert ("set_ref", (TARGET, "heads/upstream/main", shas["main"])) not in gh.calls  # already right


class FailOnce(SyncedFake):
    def __init__(self, remote_path, method, exc):
        super().__init__(remote_path)
        self._fail = {method: exc}

    def set_default_branch(self, full_name, branch):
        if exc := self._fail.pop("set_default_branch", None):
            raise exc
        super().set_default_branch(full_name, branch)

    def set_ref(self, full_name, ref, sha):
        if exc := self._fail.pop("set_ref", None):
            raise exc
        super().set_ref(full_name, ref, sha)


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


def test_apply_layout_retries_rate_limited(tmp_path, upstream, remote):
    path, shas = upstream
    sh(path, "push", "-q", remote, "refs/heads/*:refs/heads/*")
    gh = FailOnce(tmp_path / "remote.git", "set_ref", RateLimited("too quickly"))
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
    gh, _ = setup(tmp_path, upstream, remote, extra_heads=[("dev/w", "main")])
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
    """Moves `main` on the remote right after the default branch switch (a concurrent push)."""

    def __init__(self, remote_path, sha):
        super().__init__(remote_path)
        self._sha = sha

    def set_default_branch(self, full_name, branch):
        super().set_default_branch(full_name, branch)
        self.remote.update_ref("refs/heads/main", self._sha)


def test_apply_layout_skips_delete_of_branch_moved_since_listing(tmp_path, upstream, remote):
    path, shas = upstream
    sh(path, "push", "-q", remote, "refs/heads/*:refs/heads/*")
    gh = MoveAfterDefault(tmp_path / "remote.git", shas["feature/x"])
    gh.add_upstream(TARGET, branches=shas)
    ev = Events()
    assert run(gh, tmp_path, remote, shas, ev=ev) == "ok: 3 upstream, 0 tapeout"
    heads = remote_heads(tmp_path)
    assert heads["main"] == shas["feature/x"]  # kept: its new commit is not under upstream/main
    assert heads["upstream/main"] == shas["main"]
    assert "feature/x" not in heads and "ttgf0p2" not in heads
    assert ("delete-skipped", {"target": TARGET, "branch": "main"}) in ev


class LosesSetRef(SyncedFake):
    """Accepts set_ref for `ref` but the branch never appears on the remote."""

    def __init__(self, remote_path, ref):
        super().__init__(remote_path)
        self._lost = ref

    def set_ref(self, full_name, ref, sha):
        if ref == self._lost:
            self._record("set_ref", full_name, ref, sha)
            return
        super().set_ref(full_name, ref, sha)


def test_apply_layout_deletes_only_verified_copies(tmp_path, upstream, remote):
    path, shas = upstream
    sh(path, "push", "-q", remote, "refs/heads/*:refs/heads/*")
    gh = LosesSetRef(tmp_path / "remote.git", "heads/upstream/feature/x")
    gh.add_upstream(TARGET, branches=shas)
    ev = Events()
    run(gh, tmp_path, remote, shas, ev=ev)
    heads = remote_heads(tmp_path)
    assert heads["feature/x"] == shas["feature/x"]  # its copy is missing: not deleted
    assert "main" not in heads and "ttgf0p2" not in heads
    assert ("delete-skipped", {"target": TARGET, "branch": "feature/x"}) in ev


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
