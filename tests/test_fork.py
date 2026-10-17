import json

import pytest
from fakes import FakeGitHub

from wafer_space_bench.catalog import Design, tapeouts_by_repo
from wafer_space_bench.cli import main
from wafer_space_bench.fork import ensure_fork, ensure_tags, run_fork, tag_message
from wafer_space_bench.github import GitHubError, RateLimited
from wafer_space_bench.naming import COMMIT_SOURCE, RepoRef
from wafer_space_bench.scan import RepoPlan, write_plans

ORG = "wafer-space-bench"
SHA = "a" * 40
SHA2 = "b" * 40


class Sleeps(list):
    def __call__(self, s):
        self.append(s)


class Events(list):
    def __call__(self, event, **fields):
        self.append((event, fields))


def plan(full="a/one", strategy="fork"):
    owner, name = full.split("/")
    return RepoPlan(
        RepoRef("github.com", owner, name), full, "live", full, "main", 1, strategy,
        f"{ORG}/{owner}--{name}",
    )  # fmt: skip


def design(repo, code="1", commit=SHA, shuttle="ws-run1"):
    return Design("wafer.space", shuttle, code, "P", repo, commit, "Apache-2.0", "digital", "1x1")


def upstream(gh, full="a/one", **kw):
    gh.add_upstream(full, branches={"main": SHA}, commits=[SHA2], **kw)


def test_fork_backoff_then_success():
    gh, sleeps, ev = FakeGitHub(), Sleeps(), Events()
    upstream(gh)
    gh.rate_limit_next(2)
    assert ensure_fork(gh, plan(), ORG, sleep=sleeps, log=ev) == "created"
    assert sleeps[:2] == [60, 120]
    assert [e for e, _ in ev].count("backoff") == 2
    assert f"{ORG}/a--one" in gh.repos


def test_fork_backoff_caps_at_960():
    gh, sleeps = FakeGitHub(), Sleeps()
    upstream(gh)
    gh.rate_limit_next(6)
    ensure_fork(gh, plan(), ORG, sleep=sleeps, log=Events())
    assert sleeps[:6] == [60, 120, 240, 480, 960, 960]


def test_fork_other_error_not_retried():
    class Boom(FakeGitHub):
        def fork(self, *a):
            self.calls.append(("fork", a))
            raise GitHubError("nope")

    gh, sleeps = Boom(), Sleeps()
    upstream(gh)
    with pytest.raises(GitHubError):
        ensure_fork(gh, plan(), ORG, sleep=sleeps, log=Events())
    assert len(gh.calls) == 1 and sleeps == []


def test_fork_exists_is_noop():
    gh = FakeGitHub()
    upstream(gh)
    ensure_fork(gh, plan(), ORG, sleep=Sleeps(), log=Events())
    n = len(gh.calls)
    # target compared case-insensitively
    p = plan()
    p.target = p.target.upper()
    assert ensure_fork(gh, p, ORG, sleep=Sleeps(), log=Events()) == "exists"
    assert len(gh.calls) == n


def test_fork_waits_until_ready():
    gh, sleeps = FakeGitHub(), Sleeps()
    upstream(gh)
    gh.ready_after(3)
    assert ensure_fork(gh, plan(), ORG, sleep=sleeps, log=Events()) == "created"
    assert sleeps == [2, 2, 2]


def test_fork_not_ready_still_created_and_logged():
    gh, sleeps, ev = FakeGitHub(), Sleeps(), Events()
    upstream(gh)
    gh.ready_after(100)
    assert ensure_fork(gh, plan(), ORG, sleep=sleeps, log=ev, ready_polls=4) == "created"
    assert sleeps == [2, 2, 2]  # between polls only
    assert "fork-not-ready" in [e for e, _ in ev]


def test_fork_name_collision_raises():
    gh2 = FakeGitHub()
    upstream(gh2, "a/one")
    upstream(gh2, "c/two", network="a/one")
    ensure_fork(gh2, plan("a/one"), ORG, sleep=Sleeps(), log=Events())
    with pytest.raises(GitHubError, match="fork network already has"):
        ensure_fork(gh2, plan("c/two"), ORG, sleep=Sleeps(), log=Events())


def tapeouts(*pairs):
    ds = [design("https://github.com/a/one", code=c, commit=sha) for c, sha in pairs]
    return tapeouts_by_repo(ds)["a/one"]


def forked(gh):
    upstream(gh)
    ensure_fork(gh, plan(), ORG, sleep=Sleeps(), log=Events())
    return f"{ORG}/a--one"


def test_tags_created_then_exists():
    gh = FakeGitHub()
    full = forked(gh)
    ts = tapeouts(("1", SHA), ("2", SHA2))
    r1 = ensure_tags(gh, full, ts, "https://github.com/a/one", Events())
    assert set(r1.values()) == {"created"} and len(r1) == 2
    assert gh.tags[full]["wafer.space/ws-run1/1"] == SHA
    r2 = ensure_tags(gh, full, ts, "https://github.com/a/one", Events())
    assert set(r2.values()) == {"exists"}
    assert [c for c, _ in gh.calls].count("create_tag") == 2


def test_tags_missing_commit():
    gh, ev = FakeGitHub(), Events()
    full = forked(gh)
    ts = tapeouts(("1", "c" * 40), ("2", SHA2), ("3", ""))
    r = ensure_tags(gh, full, ts, "https://github.com/a/one", ev)
    assert r == {
        "wafer.space/ws-run1/1": "missing-commit",
        "wafer.space/ws-run1/2": "created",
        "wafer.space/ws-run1/3": "missing-commit",
    }
    assert [f["status"] for e, f in ev if e == "tag"] == ["missing-commit", "created", "missing-commit"]


def test_tag_message_has_no_local_paths():
    (t,) = tapeouts(("1", SHA))
    msg = tag_message(t, "https://github.com/a/one")
    for banned in ["/back" + "ups", "/ho" + "me/", "big-" + "storage", "~/git" + "hub", "/tmp/" + "claude"]:
        assert banned not in msg
    assert "https://github.com/a/one" in msg and COMMIT_SOURCE[t.org] in msg
    assert msg == (
        f"Taped out as {t.tag}\n\nSource: https://github.com/a/one\nCommit source: {COMMIT_SOURCE[t.org]}\n"
    )
    assert "Source: https://github.com/a/one (restored)\n" in tag_message(
        t, "https://github.com/a/one", "restored"
    )


def three():
    gh = FakeGitHub()
    for n in ("a/one", "b/two", "c/three"):
        upstream(gh, n)
    plans = [plan(n) for n in ("a/one", "b/two", "c/three")]
    ds = [design(f"https://github.com/{n}") for n in ("a/one", "b/two", "c/three")]
    return gh, plans, tapeouts_by_repo(ds)


def test_run_fork_resumes():
    gh, plans, tos = three()
    bad = {"armed": True}
    real = gh.fork

    def fork(src, org, name):
        if bad["armed"] and name == "b--two":
            gh.calls.append(("fork-failed", (src,)))
            raise GitHubError("boom")
        return real(src, org, name)

    gh.fork = fork
    sleeps, ev = Sleeps(), Events()
    assert run_fork(plans, tos, gh, ORG, sleep=sleeps, log=ev) == 1
    assert sleeps == [20, 20]
    bad["armed"] = False
    sleeps2 = Sleeps()
    assert run_fork(plans, tos, gh, ORG, sleep=sleeps2, log=ev) == 0
    assert sleeps2 == [20]
    forks = [c for c in gh.calls if c[0] == "fork"]
    assert len(forks) == 3 and len({c[1][2] for c in forks}) == 3
    assert sum(1 for r in gh.repos if r.startswith(ORG)) == 3
    assert all(gh.tags[f"{ORG}/{n}"] for n in ("a--one", "b--two", "c--three"))
    fork_events = [f for e, f in ev if e == "fork"]
    assert fork_events[1]["status"].startswith("error: ") and fork_events[0]["n"] == "1/3"


def test_run_fork_skips_non_fork_plans():
    gh, plans, tos = three()
    plans[1].strategy = "copy"
    assert run_fork(plans, tos, gh, ORG, sleep=Sleeps(), log=Events()) == 0
    assert f"{ORG}/b--two" not in gh.repos


def test_cli_fork_only_filter_and_exit_code(tmp_path, monkeypatch):
    gh, plans, _ = three()
    write_plans(tmp_path / "plans.csv", plans)
    cat = tmp_path / "designs.csv"
    cat.write_text(
        "pool,shuttle,code,project,repo,commit,licence,kind,size\n"
        + "".join(
            f"wafer.space,ws-run1,{i},P,https://github.com/{n},{SHA},Apache-2.0,digital,1x1\n"
            for i, n in enumerate(("a/one", "b/two", "c/three"))
        )
    )
    monkeypatch.setattr("wafer_space_bench.fork.GhCli", lambda: gh)
    rc = main(
        ["--plans", str(tmp_path / "plans.csv"), "--catalog", str(cat), "--log", str(tmp_path / "e.jsonl"),
         "fork", "--only", "B/Two", "--delay", "0"]
    )  # fmt: skip
    assert rc == 0
    assert [r for r in gh.repos if r.startswith(ORG)] == [f"{ORG}/b--two"]
    events = [json.loads(line) for line in (tmp_path / "e.jsonl").read_text().splitlines()]
    assert any(e["event"] == "fork" for e in events)


def test_tags_retry_when_rate_limited():
    class Limited(FakeGitHub):
        n = 1

        def create_tag(self, *a):
            if self.n:
                self.n -= 1
                raise RateLimited("slow down")
            super().create_tag(*a)

    gh, sleeps, ev = Limited(), Sleeps(), Events()
    full = forked(gh)
    r = ensure_tags(gh, full, tapeouts(("1", SHA)), "https://github.com/a/one", ev, sleep=sleeps)
    assert r == {"wafer.space/ws-run1/1": "created"}
    assert sleeps == [60] and "backoff" in [e for e, _ in ev]


def test_run_fork_one_event_and_delay_when_tagging_fails():
    class BadTag(FakeGitHub):
        def create_tag(self, *a):
            raise GitHubError("tag boom")

    gh = BadTag()
    upstream(gh)
    sleeps, ev = Sleeps(), Events()
    tos = tapeouts_by_repo([design("https://github.com/a/one")])
    assert run_fork([plan()], tos, gh, ORG, sleep=sleeps, log=ev) == 1
    fork_events = [f for e, f in ev if e == "fork"]
    assert len(fork_events) == 1 and fork_events[0]["status"].startswith("error: ")
    assert sleeps == [20]


def test_run_fork_not_ready_skips_tags_and_fails():
    gh, sleeps, ev = FakeGitHub(), Sleeps(), Events()
    upstream(gh)
    gh.ready_after(1000)
    tos = tapeouts_by_repo([design("https://github.com/a/one")])
    assert run_fork([plan()], tos, gh, ORG, sleep=sleeps, log=ev) == 1
    assert [e for e, _ in ev if e == "tag"] == []
    (fe,) = [f for e, f in ev if e == "fork"]
    assert fe["status"] == "created (not ready; re-run to tag)"
    assert sleeps[-1] == 20
    # re-run: fork exists, tags get created
    ev2 = Events()
    assert run_fork([plan()], tos, gh, ORG, sleep=Sleeps(), log=ev2) == 0
    assert gh.tags[f"{ORG}/a--one"]


def test_run_fork_renamed_upstream_uses_original_key_and_new_url():
    gh = FakeGitHub()
    gh.add_upstream("old/name", branches={"main": SHA}, renamed_to="new/name")
    p = plan("new/name")
    p.source = RepoRef("github.com", "old", "name")
    tos = tapeouts_by_repo([design("https://github.com/old/name")])
    assert run_fork([p], tos, gh, ORG, sleep=Sleeps(), log=Events()) == 0
    msg = next(c[1][3] for c in gh.calls if c[0] == "create_tag")
    assert "Source: https://github.com/new/name\n" in msg
    assert gh.tags[f"{ORG}/new--name"]["wafer.space/ws-run1/1"] == SHA
