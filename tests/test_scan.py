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
