import json
import time

from fakes import FakeGitHub

from wafer_space_bench.catalog import Design
from wafer_space_bench.cli import main
from wafer_space_bench.events import EventLog
from wafer_space_bench.scan import plan_repos, read_plans, write_plans

TEMPLATE = "wafer-space/gf180mcu-project-template"
SHA = "a" * 40


def design(repo: str, commit: str = "", code: str = "1") -> Design:
    return Design("wafer.space", "ws-run1", code, "P", repo, commit, "Apache-2.0", "digital", "1x1")


def by_source(plans):
    return {p.source.full_name: p for p in plans}


def test_event_log_appends_json_lines_with_injected_clock(tmp_path):
    log = EventLog(tmp_path / "sub" / "events.jsonl", clock=lambda: time.gmtime(0))
    log.write("scan", repos=3)
    log.write("other")
    lines = (tmp_path / "sub" / "events.jsonl").read_text().splitlines()
    assert json.loads(lines[0]) == {"time": "1970-01-01T00:00:00Z", "event": "scan", "repos": 3}
    assert json.loads(lines[1])["event"] == "other"


def test_plan_first_in_network_forks_rest_copy():
    gh = FakeGitHub()
    gh.add_upstream(TEMPLATE)
    gh.add_upstream("b/two", network=TEMPLATE)
    gh.add_upstream("a/one", network=TEMPLATE)
    plans = plan_repos([design("https://github.com/b/two"), design("https://github.com/a/one")], gh)
    assert [p.source.key for p in plans] == ["a/one", "b/two"]
    assert [p.strategy for p in plans] == ["fork", "copy"]
    assert plans[0].network == TEMPLATE
    assert plans[0].target == "wafer-space-bench/a--one"
    assert plans[0].status == "live"


def test_plan_gitlab_is_copy():
    gh = FakeGitHub()
    (p,) = plan_repos([design("https://gitlab.com/x/y")], gh)
    assert (p.status, p.strategy, p.target) == ("gitlab", "copy", "wafer-space-bench/x--y")
    assert gh.calls == []


def test_plan_deleted_is_restore():
    gh = FakeGitHub()
    gh.add_upstream("gone/repo", deleted=True)
    (p,) = plan_repos([design("https://github.com/gone/repo")], gh)
    assert (p.status, p.strategy, p.current_name) == ("missing", "restore", "")
    assert p.target == "wafer-space-bench/gone--repo"


def test_plan_renamed_repo():
    gh = FakeGitHub()
    gh.add_upstream("joannec34/teenytpu", renamed_to="1oketto/teenytpu")
    (p,) = plan_repos([design("https://github.com/joannec34/teenytpu")], gh)
    assert p.target == "wafer-space-bench/1oketto--teenytpu"
    assert p.source.full_name == "joannec34/teenytpu"
    assert p.current_name == "1oketto/teenytpu"


def test_plan_archived_status_and_custom_org():
    gh = FakeGitHub()
    gh.add_upstream("a/b", archived=True)
    (p,) = plan_repos([design("https://github.com/a/b")], gh, org="o")
    assert (p.status, p.strategy, p.target) == ("archived", "fork", "o/a--b")


def test_plan_missing_commit_listed():
    gh = FakeGitHub()
    gh.add_upstream("a/b", commits=[SHA])
    ds = [design("https://github.com/a/b", SHA, "1"), design("https://github.com/a/b", "c" * 40, "2")]
    (p,) = plan_repos(ds, gh)
    assert p.missing_commits == ["c" * 40]
    assert p.strategy == "fork"


def test_plans_round_trip(tmp_path):
    gh = FakeGitHub()
    gh.add_upstream("a/b", commits=[SHA])
    gh.add_upstream("c/d", deleted=True)
    ds = [
        design("https://github.com/a/b", "c" * 40),
        design("https://github.com/c/d"),
        design("https://gitlab.com/x/y"),
    ]
    plans = plan_repos(ds, gh)
    write_plans(tmp_path / "p.csv", plans)
    assert read_plans(tmp_path / "p.csv") == plans


def test_scan_cli_writes_plans_and_event(tmp_path, monkeypatch):
    import wafer_space_bench.scan as scan_mod

    gh = FakeGitHub()
    gh.add_upstream("a/b")
    monkeypatch.setattr(scan_mod, "GhCli", lambda: gh)
    cat = tmp_path / "designs.csv"
    cat.write_text(
        "pool,shuttle,code,project,repo,commit,licence,kind,size\n"
        "wafer.space,ws-run1,1,P,https://github.com/a/b,,MIT,digital,1x1\n"
    )
    plans, log = tmp_path / "plans.csv", tmp_path / "w" / "ev.jsonl"
    rc = main(["--catalog", str(cat), "--plans", str(plans), "--log", str(log), "scan"])
    assert rc == 0
    assert len(read_plans(plans)) == 1
    ev = json.loads(log.read_text().splitlines()[0])
    assert ev["event"] == "scan" and ev["counts"] == {"live/fork": 1}


# --- final review: existing org repos (I2) ---------------------------------------

ORG = "wafer-space-bench"


class Events(list):
    def __call__(self, event, **fields):
        self.append((event, fields))


def test_plan_existing_org_fork_stays_the_fork():
    """A new catalog repo sorting earlier in an already-forked network becomes the copy."""
    gh = FakeGitHub()
    gh.add_upstream(TEMPLATE)
    gh.add_upstream("b/two", network=TEMPLATE)
    gh.add_upstream("a/one", network=TEMPLATE)
    assert gh.fork("b/two", ORG, "b--two") == f"{ORG}/b--two"
    plans = by_source(
        plan_repos([design("https://github.com/b/two"), design("https://github.com/a/one")], gh)
    )
    assert (plans["b/two"].strategy, plans["b/two"].target) == ("fork", f"{ORG}/b--two")
    assert (plans["a/one"].strategy, plans["a/one"].target) == ("copy", f"{ORG}/a--one")


def test_plan_renamed_upstream_keeps_existing_bench_repo():
    gh = FakeGitHub()
    gh.add_upstream("old/name", renamed_to="new/name")
    gh.fork("new/name", ORG, "OLD--Name")  # forked before the rename; another spelling
    ev = Events()
    (p,) = plan_repos([design("https://github.com/old/name")], gh, log=ev)
    assert (p.current_name, p.strategy, p.target) == ("new/name", "fork", f"{ORG}/OLD--Name")
    assert ev == [("rename", {"source": "old/name", "current": "new/name", "target": f"{ORG}/OLD--Name"})]


def test_plan_existing_bench_repo_found_by_current_name_case_insensitively():
    gh = FakeGitHub()
    gh.add_upstream("a/b")
    gh.fork("a/b", ORG, "A--B")
    ev = Events()
    (p,) = plan_repos([design("https://github.com/a/b")], gh, log=ev)
    assert (p.strategy, p.target) == ("fork", f"{ORG}/A--B")
    assert ev == []


def test_plan_existing_plain_copy_stays_a_copy():
    """A pushed copy is not a fork: it keeps strategy copy and does not claim the network."""
    gh = FakeGitHub()
    gh.add_upstream(TEMPLATE)
    gh.add_upstream("a/one", network=TEMPLATE)
    gh.add_upstream("b/two", network=TEMPLATE)
    gh.create_repo(ORG, "a--one", "d")
    plans = by_source(
        plan_repos([design("https://github.com/a/one"), design("https://github.com/b/two")], gh)
    )
    assert plans["a/one"].strategy == "copy"
    assert plans["b/two"].strategy == "fork"


# --- final review: designs without a repository (I4) -----------------------------


def test_plan_design_without_repo_is_none():
    gh = FakeGitHub()
    gh.add_upstream("a/b")
    ds = [
        design("", code="BRWN"),
        design("(slot repo)", code="0003"),
        design("https://github.com/a/b"),
    ]
    plans = plan_repos(ds, gh)
    assert [(p.source.key, p.strategy, p.status, p.target) for p in plans] == [
        ("a/b", "fork", "live", f"{ORG}/a--b"),
        ("ws-run1/0003", "none", "none", ""),
        ("ws-run1/brwn", "none", "none", ""),
    ]


def test_none_plans_round_trip(tmp_path):
    plans = plan_repos([design("", code="BRWN"), design("https://gitlab.com/x/y")], FakeGitHub())
    write_plans(tmp_path / "p.csv", plans)
    assert read_plans(tmp_path / "p.csv") == plans


def test_committed_plans_have_one_none_row_per_design_without_repo():
    from pathlib import Path

    from wafer_space_bench.catalog import load_designs
    from wafer_space_bench.naming import parse_repo_url

    root = Path(__file__).resolve().parent.parent
    none = {p.source.name for p in read_plans(root / "data" / "bench_repos.csv") if p.strategy == "none"}
    codes = {d.code for d in load_designs(root / "data" / "designs.csv") if parse_repo_url(d.repo) is None}
    assert none == codes == {"BRWN", "MOSB", "BTX1", "FC01", "TQVE", "0002", "0003"}


def test_other_commands_skip_none_plans(tmp_path):
    from wafer_space_bench.config import LocalConfig
    from wafer_space_bench.copy import run_copies
    from wafer_space_bench.fork import plan_matches, run_fork
    from wafer_space_bench.layout import run_layouts
    from wafer_space_bench.sync import run_sync

    (p,) = plan_repos([design("", code="BRWN")], FakeGitHub())
    gh, ev, cfg = FakeGitHub(), Events(), LocalConfig(tmp_path / "work")
    assert plan_matches(p, {"ws-run1/brwn"}) and not plan_matches(p, {""})
    assert run_fork([p], {}, gh, ORG, log=ev, sleep=lambda s: None) == 0
    assert run_copies([p], {}, gh, cfg, ORG, "unused", log=ev, sleep=lambda s: None) == 0
    assert run_layouts([p], {}, gh, cfg, ORG, remote_fmt="unused", log=ev, now=lambda: "t") == 0
    assert run_sync([p], {}, gh, cfg, ORG, remote_fmt="unused", now="t", log=ev, sleep=lambda s: None) == 0
    assert gh.calls == [] and ev == [] and not (tmp_path / "work").exists()
