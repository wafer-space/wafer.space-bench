"""`wsbench settings`: desired values, diffing, idempotent apply, rulesets, workflow step."""

from pathlib import Path

import pytest
import yaml
from fakes import FakeGitHub
from test_privacy import BANNED

from wafer_space_bench.catalog import Tapeout
from wafer_space_bench.cli import main
from wafer_space_bench.github import GitHubError
from wafer_space_bench.index import index_rows
from wafer_space_bench.naming import RepoRef
from wafer_space_bench.scan import RepoPlan, write_plans
from wafer_space_bench.settings import (
    DesiredSettings,
    apply_settings,
    desired_settings,
    diff_settings,
    run_settings,
    topic_for,
)
from wafer_space_bench.sync import shard_of

ORG = "wafer-space-bench"
ROOT = Path(__file__).resolve().parent.parent
SITE = ROOT / "site"
RULES = [{"type": "deletion"}, {"type": "non_fast_forward"}, {"type": "update"}]
SETTINGS_WRITES = (
    "update_repo",
    "set_topics",
    "set_actions_enabled",
    "set_vulnerability_alerts",
    "put_ruleset",
)


class Events(list):
    def __call__(self, event, **fields):
        self.append((event, fields))


def plan(name="a/up", strategy="fork", current=None, host="github.com"):
    owner, repo = name.split("/")
    cur = (name if strategy != "restore" and host == "github.com" else "") if current is None else current
    return RepoPlan(
        RepoRef(host, owner, repo), cur, "live", cur, "main", 1, strategy, f"{ORG}/{owner}--{repo}"
    )


def tapeout(source_key="a/up", org="tinytapeout", run="ttgf0p2", slot="tt_um_x"):
    return Tapeout(org, run, slot, "P", "1x1", "digital", "MIT", "abc", "", source_key)


def org_repo(gh, p):
    gh.add_upstream(p.target, branches={"about": "c1"})


def writes(gh, since=0):
    return [c for c in gh.calls[since:] if c[0] in SETTINGS_WRITES]


def quiet():
    return {"sleep": lambda s: None, "log": Events()}


# --- desired values ------------------------------------------------------------


def test_desired_settings_fork_copy_restore():
    fork = desired_settings(
        plan("a/up", current="a/renamed"),
        {"a/up": [tapeout(), tapeout(slot="tt_um_y"), tapeout(org="wafer.space", run="ws-run1", slot="X")]},
    )
    assert fork.repo == {
        "description": "Benchmark copy of https://github.com/a/renamed: 3 tapeouts on "
        "tinytapeout/ttgf0p2, wafer.space/ws-run1",
        "homepage": "https://wafer-space-bench.github.io/#a--up",
        "has_issues": False,
        "has_projects": False,
        "has_wiki": False,
        "has_discussions": False,
        "has_pull_requests": False,
    }
    assert fork.topics == [
        "wafer-space-bench",
        "gf180mcu",
        "open-silicon",
        "fork",
        "tinytapeout",
        "wafer-space",
        "ttgf0p2",
        "ws-run1",
    ]
    assert fork.actions_enabled is False and fork.vulnerability_alerts is False

    copy = desired_settings(
        plan("g/lab", strategy="copy", host="gitlab.com"),
        {"g/lab": [tapeout("g/lab", org="google", run="gf180mcu-mpw0", slot="s1")]},
        index_url="https://example.org",
    )
    assert copy.repo["description"] == (
        "Benchmark copy of https://gitlab.com/g/lab: 1 tapeout on google/gf180mcu-mpw0"
    )
    assert copy.repo["homepage"] == "https://example.org/#g--lab"
    assert copy.topics == ["wafer-space-bench", "gf180mcu", "open-silicon", "copy", "google", "gf180mcu-mpw0"]

    restore = desired_settings(plan("d/gone", strategy="restore"), {"d/gone": [tapeout("d/gone")]})
    assert restore.repo["description"] == (
        "Benchmark copy of https://github.com/d/gone: 1 tapeout on tinytapeout/ttgf0p2 (upstream deleted)"
    )
    assert restore.topics[3] == "restore"

    for d in (fork, copy, restore):
        assert len(d.repo["description"]) <= 350
        assert not [b for b in BANNED if b in d.repo["description"] + d.repo["homepage"]]


def test_desired_description_is_capped():
    tos = [tapeout(run=f"run{i:03d}-with-a-long-name", slot=f"s{i}") for i in range(40)]
    d = desired_settings(plan(), {"a/up": tos})
    assert len(d.repo["description"]) <= 350
    assert d.repo["description"].startswith(
        "Benchmark copy of https://github.com/a/up: 40 tapeouts on 40 runs"
    )
    assert len(d.topics) <= 20


def test_topic_for_normalises():
    assert topic_for("wafer.space") == "wafer-space"
    assert topic_for("TinyTapeout") == "tinytapeout"
    assert topic_for("gf180mcu-mpw0") == "gf180mcu-mpw0"
    assert topic_for("a..b__c  d") == "a-b-c-d"
    assert topic_for(".x.") == "x"
    assert topic_for("y" * 80) == "y" * 50


# --- diff and apply -------------------------------------------------------------


def test_diff_settings_reports_only_differences():
    want = desired_settings(plan(), {"a/up": [tapeout()]})
    have = DesiredSettings(
        repo=want.repo | {"has_wiki": True, "homepage": None},
        topics=list(reversed(want.topics)),  # order does not matter
        actions_enabled=False,
        vulnerability_alerts=True,
        ruleset={"bench-frozen-branches": want.ruleset["bench-frozen-branches"] | {"id": 7}},
    )
    diff = diff_settings(have, want)
    assert set(diff) == {"has_wiki", "homepage", "vulnerability_alerts", "ruleset:bench-frozen-tags"}
    assert diff["has_wiki"] == (True, False)


def test_apply_settings_only_changes_diffs():
    gh, p = FakeGitHub(), plan()
    org_repo(gh, p)
    gh.settings[p.target]["has_issues"] = False  # already right: not sent
    tos = {"a/up": [tapeout()]}
    status = apply_settings(p, tos, gh, ORG, dry_run=False, **quiet())
    assert status.startswith("ok: ") and "has_pull_requests" in status
    patch, prs = [c[1][1] for c in gh.calls if c[0] == "update_repo"]
    assert "has_issues" not in patch and "has_pull_requests" not in patch
    assert patch["has_wiki"] is False
    assert prs == {"has_pull_requests": False}  # on its own, after the other fields
    assert gh.settings[p.target]["has_pull_requests"] is False
    assert gh.topics[p.target] == desired_settings(p, tos).topics
    assert gh.actions_enabled[p.target] is False and gh.vulnerability_alerts[p.target] is False
    assert set(gh.rulesets[p.target]) == {"bench-frozen-branches", "bench-frozen-tags"}

    n = len(gh.calls)
    assert apply_settings(p, tos, gh, ORG, dry_run=False, **quiet()) == "unchanged"
    assert writes(gh, n) == []


def test_apply_settings_dry_run_no_writes():
    gh, p = FakeGitHub(), plan()
    org_repo(gh, p)
    log = Events()
    status = apply_settings(p, {}, gh, ORG, dry_run=True, sleep=lambda s: None, log=log)
    assert status.startswith("dry-run: ") and "topics" in status and "ruleset:bench-frozen-tags" in status
    assert writes(gh) == []
    assert [e for e, _ in log] == ["settings-plan"]


def test_apply_settings_pending_when_absent():
    gh = FakeGitHub()
    assert apply_settings(plan(), {}, gh, ORG, dry_run=False, **quiet()) == "pending"
    assert writes(gh) == []


def test_apply_settings_error_is_returned():
    gh, p = FakeGitHub(), plan()
    org_repo(gh, p)

    def boom(full, fields):
        raise GitHubError("repos/x: Validation Failed (HTTP 422)")

    gh.update_repo = boom
    assert apply_settings(p, {}, gh, ORG, dry_run=False, **quiet()).startswith("error: ")


def test_pull_requests_left_alone_when_unsupported():
    gh, p, q = FakeGitHub(), plan("a/up"), plan("b/two")
    org_repo(gh, p)
    org_repo(gh, q)
    gh.pull_requests_supported = False
    log = Events()
    assert (
        run_settings(
            [p, q], {}, gh, ORG, only=None, shard=None, dry_run=False, delay=0, sleep=lambda s: None, log=log
        )
        == 0
    )
    assert all("has_pull_requests" not in c[1][1] for c in gh.calls if c[0] == "update_repo")
    assert [e for e, _ in log].count("pull-requests-unsupported") == 1


def _pr_run(gh, n=2):
    plans = [plan(f"o/r{i}") for i in range(n)]
    for p in plans:
        org_repo(gh, p)
    log = Events()
    failed = run_settings(
        plans, {}, gh, ORG, only=None, shard=None, dry_run=False, delay=0, sleep=lambda s: None, log=log
    )
    statuses = [f["status"] for e, f in log if e == "settings"]
    return failed, statuses, [e for e, _ in log], plans


def test_pull_requests_rejected_does_not_cancel_other_fields():
    gh = FakeGitHub()
    gh.pull_requests_rejected = True
    failed, statuses, events, plans = _pr_run(gh)
    assert failed == 0
    assert all(s.startswith("ok: ") and "has_pull_requests" not in s for s in statuses)
    assert all(gh.settings[p.target]["has_wiki"] is False for p in plans)  # the main PATCH went through
    assert events.count("pull-requests-update-failed") == 1  # tried once per run, not per repo
    prs_patches = [c for c in gh.calls if c[0] == "update_repo" and "has_pull_requests" in c[1][1]]
    assert len(prs_patches) == 1


def test_pull_requests_ignored_stops_trying_for_the_run():
    gh = FakeGitHub()
    gh.pull_requests_ignored = True
    failed, statuses, events, plans = _pr_run(gh, n=3)
    assert failed == 0 and events.count("pull-requests-ignored") == 1
    prs_patches = [c for c in gh.calls if c[0] == "update_repo" and "has_pull_requests" in c[1][1]]
    assert len(prs_patches) == 1
    # A later run in which everything else is set tries once, then is unchanged (no churn).
    n = len(gh.calls)
    failed, statuses, events, _ = _pr_run_again(gh, plans)
    assert statuses == ["unchanged", "unchanged", "unchanged"]
    assert len(writes(gh, n)) == 1 and events.count("pull-requests-ignored") == 1


def _pr_run_again(gh, plans):
    log = Events()
    failed = run_settings(
        plans, {}, gh, ORG, only=None, shard=None, dry_run=False, delay=0, sleep=lambda s: None, log=log
    )
    return failed, [f["status"] for e, f in log if e == "settings"], [e for e, _ in log], plans


def test_pull_requests_success_is_reported():
    gh = FakeGitHub()
    failed, statuses, events, plans = _pr_run(gh)
    assert failed == 0 and all("has_pull_requests" in s for s in statuses)
    assert "pull-requests-ignored" not in events and "pull-requests-update-failed" not in events
    n = len(gh.calls)
    assert _pr_run_again(gh, plans)[1] == ["unchanged", "unchanged"] and writes(gh, n) == []


def test_actions_managed_by_org_is_not_an_error():
    gh = FakeGitHub()
    gh.actions_org_managed = True
    failed, statuses, events, plans = _pr_run(gh, n=3)
    assert failed == 0
    assert all(s.startswith("ok: ") and "actions_enabled" not in s for s in statuses)
    assert events.count("actions-managed-by-org") == 1
    assert len([c for c in gh.calls if c[0] == "set_actions_enabled"]) == 1  # not retried per repo
    assert all(gh.topics[p.target] for p in plans)  # the rest was still applied


def test_actions_other_error_is_an_error():
    gh, p = FakeGitHub(), plan()
    org_repo(gh, p)

    def boom(full, enabled):
        raise GitHubError("repos/x/actions/permissions: Forbidden (HTTP 403)")

    gh.set_actions_enabled = boom
    assert apply_settings(p, {}, gh, ORG, dry_run=False, **quiet()).startswith("error: ")


def test_copy_creates_repos_with_the_settings_description():
    from wafer_space_bench.settings import bench_description

    p, tos = plan("g/lab", strategy="copy", host="gitlab.com"), {"g/lab": [tapeout("g/lab")]}
    assert desired_settings(p, tos).repo["description"] == bench_description(p, tos["g/lab"])


def test_apply_settings_writes_are_retried_when_rate_limited():
    gh, p = FakeGitHub(), plan()
    org_repo(gh, p)
    real, failures = gh.set_topics, [1]

    def flaky(full, names):
        if failures:
            failures.pop()
            from wafer_space_bench.github import RateLimited

            raise RateLimited("secondary rate limit")
        real(full, names)

    gh.set_topics = flaky
    log = Events()
    assert apply_settings(p, {}, gh, ORG, dry_run=False, sleep=lambda s: None, log=log).startswith("ok: ")
    assert [e for e, _ in log].count("backoff") == 1


# --- rulesets ---------------------------------------------------------------------


def test_frozen_rulesets_shape():
    d = desired_settings(plan(), {})
    branches, tags = d.ruleset["bench-frozen-branches"], d.ruleset["bench-frozen-tags"]
    assert branches == {
        "name": "bench-frozen-branches",
        "target": "branch",
        "enforcement": "active",
        "conditions": {"ref_name": {"include": ["refs/heads/tapeout/**"], "exclude": []}},
        "rules": RULES,
        "bypass_actors": [],
    }
    assert tags["target"] == "tag" and tags["rules"] == RULES and tags["bypass_actors"] == []
    assert tags["conditions"]["ref_name"]["include"] == [
        "refs/tags/wafer.space/**",
        "refs/tags/google/**",
        "refs/tags/tinytapeout/**",
    ]
    assert not any(r["type"] == "creation" for r in branches["rules"] + tags["rules"])

    # Enforced by the fake: creating tapeout/* and tapeout tags works, moving or deleting fails.
    gh, p = FakeGitHub(), plan()
    gh.add_upstream(p.target, commits=["c1", "c2"], branches={"about": "c1", "dev/x": "c1"})
    apply_settings(p, {}, gh, ORG, dry_run=False, **quiet())
    gh.set_ref(p.target, "heads/tapeout/tinytapeout/ttgf0p2/x", "c1")
    gh.create_tag(p.target, "tinytapeout/ttgf0p2/x", "c1", "m")
    with pytest.raises(GitHubError, match="rule"):
        gh.set_ref(p.target, "heads/tapeout/tinytapeout/ttgf0p2/x", "c2")
    with pytest.raises(GitHubError, match="rule"):
        gh.delete_ref(p.target, "heads/tapeout/tinytapeout/ttgf0p2/x")
    with pytest.raises(GitHubError, match="rule"):
        gh.delete_ref(p.target, "tags/tinytapeout/ttgf0p2/x")
    assert len(gh.refused) == 3
    gh.set_ref(p.target, "heads/dev/x", "c2")  # other branches are not frozen
    gh.delete_ref(p.target, "heads/dev/x")
    assert gh.branches[p.target]["tapeout/tinytapeout/ttgf0p2/x"] == "c1"


# --- run_settings -----------------------------------------------------------------


def test_run_settings_continues_after_error():
    gh = FakeGitHub()
    plans = [plan("a/one"), plan("b/two"), plan("c/three", strategy="none")]
    plans[2].target = ""
    for p in plans[:2]:
        org_repo(gh, p)
    real = gh.update_repo

    def fail_first(full, fields):
        if full.endswith("a--one"):
            raise GitHubError("repos/a: boom (HTTP 500)")
        real(full, fields)

    gh.update_repo = fail_first
    log, slept = Events(), []
    failed = run_settings(
        plans, {}, gh, ORG, only=None, shard=None, dry_run=False, delay=1, sleep=slept.append, log=log
    )
    assert failed == 1
    statuses = {f["target"]: f["status"] for e, f in log if e == "settings"}
    assert set(statuses) == {f"{ORG}/a--one", f"{ORG}/b--two"}
    assert statuses[f"{ORG}/a--one"].startswith("error: ")
    assert statuses[f"{ORG}/b--two"].startswith("ok: ")
    assert slept == [1]  # after the first repo (an error), not after the last


def test_run_settings_only_and_shard():
    gh = FakeGitHub()
    plans = [plan(f"o/r{i}") for i in range(12)]
    for p in plans:
        org_repo(gh, p)
    log = Events()
    run_settings(
        plans,
        {},
        gh,
        ORG,
        only=["o/r3", "o--r5"],
        shard=None,
        dry_run=True,
        delay=0,
        sleep=lambda s: None,
        log=log,
    )
    assert sorted(f["target"] for e, f in log if e == "settings") == [f"{ORG}/o--r3", f"{ORG}/o--r5"]
    seen = []
    for k in (1, 2, 3):
        log = Events()
        run_settings(
            plans, {}, gh, ORG, only=None, shard=(k, 3), dry_run=True, delay=0, sleep=lambda s: None, log=log
        )
        got = [f["target"] for e, f in log if e == "settings"]
        assert all(shard_of(t, 3) == k for t in got)
        seen += got
    assert sorted(seen) == sorted(p.target for p in plans)
    with pytest.raises(ValueError):
        run_settings(
            plans, {}, gh, ORG, only=None, shard=(4, 3), dry_run=True, delay=0, sleep=lambda s: None, log=log
        )


def test_cli_settings_dry_run(tmp_path, monkeypatch):
    plans = tmp_path / "plans.csv"
    write_plans(plans, [plan("a/up")])
    cat = tmp_path / "designs.csv"
    cat.write_text("pool,shuttle,code,project,repo,commit,licence,kind,size\n")
    gh = FakeGitHub()
    org_repo(gh, plan("a/up"))
    monkeypatch.setattr("wafer_space_bench.settings.GhCli", lambda: gh)
    base = ["--plans", str(plans), "--catalog", str(cat), "--log", str(tmp_path / "e.jsonl")]
    assert main([*base, "settings", "--dry-run", "--shard", "1/1", "--delay", "0"]) == 0
    assert writes(gh) == []
    assert main([*base, "settings", "--only", "nope/nope"]) == 2


# --- index anchors and workflow -----------------------------------------------------


def test_index_rows_have_anchor_ids():
    gh = FakeGitHub()
    none = plan("ws-run1/brwn", strategy="none")
    none.target = ""
    rows = index_rows([plan("a/up"), none], {}, gh, ORG)
    assert {r["name"]: r["anchor"] for r in rows} == {"a--up": "a--up", "brwn": ""}
    md = (SITE / "index.md").read_text()
    assert '<tr{% if repo.anchor != "" %} id="{{ repo.anchor | escape }}"{% endif %}>' in md
    # The homepage link targets the same anchor.
    assert desired_settings(plan("a/up"), {}).repo["homepage"].endswith("#" + rows[0]["anchor"])


def test_sync_workflow_runs_settings():
    wf = yaml.safe_load((ROOT / ".github" / "workflows" / "sync.yml").read_text())
    steps = wf["jobs"]["sync"]["steps"]
    names = [s.get("name") for s in steps]
    i, j = names.index("Sync"), names.index("Settings")
    assert j == i + 1
    step = steps[j]
    assert step["if"] == "${{ !cancelled() && steps.app-token.outcome == 'success' }}"
    assert "app-token" in [s.get("id") for s in steps[:i]]
    assert set(step["env"]) == {"GH_TOKEN", "ONLY", "DRY_RUN", "SHARD"}
    assert "${{" not in step["run"]
    assert 'settings "${args[@]}"' in step["run"] and '--shard "$SHARD/8"' in step["run"]
