import json
import subprocess
from pathlib import Path

import pytest
from fakes import FakeGitHub
from test_privacy import BANNED

from wafer_space_bench.catalog import Tapeout
from wafer_space_bench.cli import main
from wafer_space_bench.config import LocalConfig, load_config, resolve_clone_url
from wafer_space_bench.copy import RestoreSource, copy_description, restore_sources, run_copy
from wafer_space_bench.git import Git
from wafer_space_bench.naming import RepoRef
from wafer_space_bench.scan import RepoPlan, write_plans

ORG = "wafer-space-bench"
TAG = "tinytapeout/ttgf0p2/tt_um_x"
DATA = Path(__file__).resolve().parent.parent / "data"


class Events(list):
    def __call__(self, event, **fields):
        self.append((event, fields))


class Sleeps(list):
    def __call__(self, s):
        self.append(s)


def tapeout(commit, slot="tt_um_x"):
    return Tapeout(
        "tinytapeout", "ttgf0p2", slot, "P", "1x1", "digital", "Apache-2.0", commit, "", "a/upstream"
    )


def copy_plan(owner="a", name="upstream", strategy="copy", host="github.com", network="x/root"):
    status = {"copy": "live", "restore": "missing"}[strategy]
    if host != "github.com":
        status = "gitlab"
    current = f"{owner}/{name}" if strategy == "copy" and host == "github.com" else ""
    return RepoPlan(
        RepoRef(host, owner, name), current, status, network if current else "", "main", 1, strategy,
        f"{ORG}/{owner}--{name}",
    )  # fmt: skip


def cfg_for(tmp_path):
    """Mirrors map https://github.com/a/ onto tmp_path, so a/upstream is the upstream fixture."""
    return LocalConfig(work_dir=tmp_path / "work", mirrors={"https://github.com/a/": tmp_path})


def remote_refs(tmp_path):
    g = Git(tmp_path / "remote.git")
    return sorted(g.heads()), g.tags()


def tag_type(tmp_path, tag):
    return subprocess.run(
        ["git", "cat-file", "-t", f"refs/tags/{tag}"],
        cwd=tmp_path / "remote.git",
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def test_copy_pushes_branches_and_tags(tmp_path, upstream, remote):
    _, shas = upstream
    gh, ev = FakeGitHub(), Events()
    tos = {"a/upstream": [tapeout(shas["ttgf0p2"]), tapeout("c" * 40, slot="gone"), tapeout("", slot="e")]}
    status = run_copy(copy_plan(), tos, gh, cfg_for(tmp_path), ORG, remote, log=ev)
    assert status == "pushed"
    heads, tags = remote_refs(tmp_path)
    assert heads == ["feature/x", "main", "ttgf0p2"]
    assert tags == [TAG]
    assert tag_type(tmp_path, TAG) == "tag"  # annotated
    assert Git(tmp_path / "remote.git").rev_parse(f"refs/tags/{TAG}") == shas["ttgf0p2"]
    assert f"{ORG}/a--upstream" in gh.repos
    (desc,) = [c[1][2] for c in gh.calls if c[0] == "create_repo"]
    assert desc == copy_description(copy_plan())
    missing = [f["tag"] for e, f in ev if e == "missing-commit"]
    assert missing == ["tinytapeout/ttgf0p2/gone", "tinytapeout/ttgf0p2/e"]
    (ce,) = [f for e, f in ev if e == "copy"]
    assert ce["status"] == "pushed" and ce["from"] == "local mirror"
    assert str(tmp_path) not in json.dumps(ev)
    # local bare clone lives under work_dir/mirrors
    assert (tmp_path / "work" / "mirrors" / "a--upstream.git").is_dir()


def test_copy_tag_message(tmp_path, upstream, remote):
    _, shas = upstream
    run_copy(copy_plan(), {"a/upstream": [tapeout(shas["main"])]}, FakeGitHub(), cfg_for(tmp_path), ORG,
             remote, log=Events())  # fmt: skip
    msg = subprocess.run(
        ["git", "tag", "-l", "--format=%(contents)", TAG], cwd=tmp_path / "remote.git", capture_output=True, check=True,
        text=True,
    ).stdout  # fmt: skip
    assert "Source: https://github.com/a/upstream (shares the fork network of x/root)" in msg
    assert str(tmp_path) not in msg


def test_copy_idempotent(tmp_path, upstream, remote):
    _, shas = upstream
    gh = FakeGitHub()
    tos = {"a/upstream": [tapeout(shas["ttgf0p2"])]}
    cfg = cfg_for(tmp_path)
    assert run_copy(copy_plan(), tos, gh, cfg, ORG, remote, log=Events()) == "pushed"
    first = remote_refs(tmp_path)
    assert run_copy(copy_plan(), tos, gh, cfg, ORG, remote, log=Events()) == "pushed"
    assert remote_refs(tmp_path) == first
    assert [c[0] for c in gh.calls].count("create_repo") == 1


def test_copy_reuses_clone_and_fetches_new_branches(tmp_path, upstream, remote):
    path, _ = upstream
    gh, cfg = FakeGitHub(), cfg_for(tmp_path)
    run_copy(copy_plan(), {}, gh, cfg, ORG, remote, log=Events())
    subprocess.run(["git", "branch", "new", "main"], cwd=path, check=True)
    subprocess.run(["git", "branch", "-D", "feature/x"], cwd=path, check=True, capture_output=True)
    assert run_copy(copy_plan(), {}, gh, cfg, ORG, remote, log=Events()) == "pushed"
    assert "new" in remote_refs(tmp_path)[0]
    local = Git(tmp_path / "work" / "mirrors" / "a--upstream.git")
    assert "feature/x" not in local.heads()  # pruned locally


def test_copy_create_repo_backs_off(tmp_path, upstream, remote):
    gh, sleeps, ev = FakeGitHub(), Sleeps(), Events()
    gh.rate_limit_next(2)
    status = run_copy(copy_plan(), {}, gh, cfg_for(tmp_path), ORG, remote, log=ev, sleep=sleeps)
    assert status == "pushed" and sleeps == [60, 120]
    assert [e for e, _ in ev].count("backoff") == 2


def test_copy_laid_out_repo_pushes_tags_only(tmp_path, upstream, remote):
    _, shas = upstream
    gh = FakeGitHub()
    gh.create_repo(ORG, "a--upstream", "d")
    full = f"{ORG}/a--upstream"
    gh.branches[full]["about"] = "0" * 40
    gh.set_default_branch(full, "about")
    tos = {"a/upstream": [tapeout(shas["ttgf0p2"])]}
    status = run_copy(copy_plan(), tos, gh, cfg_for(tmp_path), ORG, remote, log=Events())
    assert status == "exists (laid out)"
    assert remote_refs(tmp_path) == ([], [TAG])
    assert [c[0] for c in gh.calls].count("create_repo") == 1  # only the setup one


def test_copy_dry_run_does_nothing(tmp_path, upstream, remote):
    gh, ev = FakeGitHub(), Events()
    status = run_copy(copy_plan(), {}, gh, cfg_for(tmp_path), ORG, remote, log=ev, dry_run=True)
    assert status == "dry-run: local mirror"
    assert gh.calls == [] and remote_refs(tmp_path) == ([], [])
    assert not (tmp_path / "work").exists()
    assert str(tmp_path) not in json.dumps(ev)


def test_copy_clone_failure_is_error(tmp_path, remote):
    gh = FakeGitHub()
    status = run_copy(copy_plan(), {}, gh, cfg_for(tmp_path), ORG, remote, log=Events())
    assert status.startswith("error: ")  # no mirror, and https is not an allowed transport here
    assert gh.calls == []


def test_copy_rejects_other_strategies(tmp_path, remote):
    p = copy_plan()
    p.strategy = "fork"
    with pytest.raises(ValueError):
        run_copy(p, {}, FakeGitHub(), cfg_for(tmp_path), ORG, remote, log=Events())


def test_resolve_clone_url_prefers_local_mirror(tmp_path):
    (tmp_path / "gh" / "o" / "r.git").mkdir(parents=True)
    (tmp_path / "gs" / "x" / "slot-1").mkdir(parents=True)
    cfg = LocalConfig(
        tmp_path,
        {
            "https://github.com/": tmp_path / "gh",
            "https://github.com/other/": tmp_path / "nothing",
            "https://foss-eda-tools.googlesource.com/": tmp_path / "gs",
        },
    )
    assert resolve_clone_url("https://github.com/o/r", cfg) == str(tmp_path / "gh" / "o" / "r.git")
    assert resolve_clone_url("https://github.com/o/r.git", cfg) == str(tmp_path / "gh" / "o" / "r.git")
    assert resolve_clone_url("https://foss-eda-tools.googlesource.com/x/slot-1/", cfg) == str(
        tmp_path / "gs" / "x" / "slot-1"
    )


def test_resolve_clone_url_falls_back_to_public(tmp_path):
    cfg = LocalConfig(tmp_path, {"https://github.com/": tmp_path / "gh"})
    assert resolve_clone_url("https://github.com/o/r", cfg) == "https://github.com/o/r"
    assert resolve_clone_url("https://gitlab.com/o/r", cfg) == "https://gitlab.com/o/r"
    assert resolve_clone_url("https://github.com/o/r", LocalConfig(tmp_path, {})) == "https://github.com/o/r"


def test_copy_description_public():
    cases = [
        (copy_plan(), "shares the fork network of x/root"),
        (copy_plan(host="gitlab.com"), "hosted on GitLab"),
        (copy_plan(strategy="restore"), "upstream repository was deleted"),
    ]
    for p, why in cases:
        d = copy_description(p)
        assert d == f"Copy of {p.source.url} for the wafer.space GF180 benchmark ({why})"
        assert not [b for b in BANNED if b in d]


def test_copy_none_reports_no_source(tmp_path, remote):
    """A deleted GitHub repo with no local mirror and no restore source: nothing to copy."""
    gh, ev = FakeGitHub(), Events()
    p = copy_plan("gone", "repo", strategy="restore")
    status = run_copy(p, {}, gh, LocalConfig(tmp_path / "work", {}), ORG, remote, log=ev, sources={})
    assert status == "no source"
    assert gh.calls == [] and remote_refs(tmp_path) == ([], [])
    (ce,) = [f for e, f in ev if e == "copy"]
    assert ce["status"] == "no source"


def test_restore_deleted_without_mirror_is_no_source(tmp_path, remote):
    gh = FakeGitHub()
    p = copy_plan("gone", "repo", strategy="restore")
    src = {"gone/repo": RestoreSource((tmp_path / "missing").as_uri(), "nowhere")}
    status = run_copy(p, {}, gh, LocalConfig(tmp_path / "work", {}), ORG, remote, log=Events(), sources=src)
    assert status == "no source"
    assert gh.calls == []


def test_restore_from_restore_source(tmp_path, upstream, remote):
    path, shas = upstream
    gh = FakeGitHub()
    p = copy_plan("gone", "repo", strategy="restore")
    src = {"gone/repo": RestoreSource("https://foss-eda-tools.googlesource.com/s/slot-001", "the slot")}
    (tmp_path / "gs" / "s").mkdir(parents=True)
    path.rename(tmp_path / "gs" / "s" / "slot-001")
    cfg = LocalConfig(tmp_path / "work", {"https://foss-eda-tools.googlesource.com/": tmp_path / "gs"})
    tos = {"gone/repo": [tapeout(shas["main"])]}
    assert run_copy(p, tos, gh, cfg, ORG, remote, log=Events(), sources=src) == "pushed"
    assert remote_refs(tmp_path)[1] == [TAG]


def test_restore_sources_file():
    srcs = restore_sources(DATA / "restore_sources.toml")
    assert len(srcs) == 4
    base = "https://foss-eda-tools.googlesource.com/third_party/shuttle/gf180mcu/"
    assert srcs["0616ygh/rioschip2"].public_url == base + "mpw-000/slot-018"
    assert srcs["abdulmoizsheikh1/sha-256-with-clock-multiplexing"].public_url == base + "mpw-000/slot-032"
    assert srcs["mdagois/tiny_user_project_xls_lfsr"].public_url == base + "mpw-000/slot-034"
    assert srcs["ashbir/sensor-harness-gf180"].public_url == base + "mpw-18h1/slot-004"
    for s in srcs.values():
        assert s.description == f"the shuttle slot repository {s.public_url}"


def test_load_config(tmp_path):
    assert load_config(tmp_path / "missing.toml") == LocalConfig(Path("work"), {})
    f = tmp_path / "c.toml"
    f.write_text('work_dir = "w"\n[mirrors]\n"https://github.com/" = "/m/gh/"\n')
    assert load_config(f) == LocalConfig(Path("w"), {"https://github.com/": Path("/m/gh/")})


def test_example_config_loads():
    root = DATA.parent
    cfg = load_config(root / "bench.local.toml.example")
    assert cfg.work_dir == Path("work")
    assert set(cfg.mirrors) == {"https://github.com/", "https://foss-eda-tools.googlesource.com/"}


def test_cli_copy_dry_run_only_and_exit_code(tmp_path, monkeypatch):
    plans = [copy_plan("a", "one"), copy_plan("b", "two", strategy="restore"), copy_plan("c", "three")]
    plans[2].strategy = "fork"
    write_plans(tmp_path / "plans.csv", plans)
    cat = tmp_path / "designs.csv"
    cat.write_text("pool,shuttle,code,project,repo,commit,licence,kind,size\n")
    gh = FakeGitHub()
    monkeypatch.setattr("wafer_space_bench.copy.GhCli", lambda: gh)
    log = tmp_path / "e.jsonl"
    base = ["--plans", str(tmp_path / "plans.csv"), "--catalog", str(cat), "--log", str(log),
            "--config", str(tmp_path / "none.toml")]  # fmt: skip
    srcs = ["--restore-sources", str(DATA / "restore_sources.toml")]
    assert main([*base, "copy", "--dry-run", *srcs]) == 0
    events = [json.loads(line) for line in log.read_text().splitlines()]
    assert [(e["target"], e["status"]) for e in events if e["event"] == "copy"] == [
        (f"{ORG}/a--one", "dry-run: public"),
        (f"{ORG}/b--two", "dry-run: public"),
    ]
    log.unlink()
    monkeypatch.chdir(tmp_path)
    # a real (offline) run of a/one fails to clone: exit code 1
    assert main([*base, "copy", "--only", "A--One", *srcs]) == 1
    events = [json.loads(line) for line in log.read_text().splitlines()]
    assert [e["target"] for e in events if e["event"] == "copy"] == [f"{ORG}/a--one"]
