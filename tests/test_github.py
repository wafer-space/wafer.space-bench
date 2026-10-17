import base64
import json
import subprocess

import pytest
from fakes import FakeGitHub

from wafer_space_bench.github import GhCli, GitHub, GitHubError, NotFound, RateLimited, RepoInfo

A = "a" * 40
B = "b" * 40
C = "c" * 40


class Stub:
    """Replays canned gh responses and records the commands it was given."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.cmds = []

    def __call__(self, cmd, **kw):
        self.cmds.append(cmd)
        rc, out, err = self.responses.pop(0)
        if not isinstance(out, str):
            out = json.dumps(out)
        return subprocess.CompletedProcess(cmd, rc, out, err)


def ok(out=""):
    return (0, out, "")


def fail(err):
    return (1, "", err)


# --- GhCli -----------------------------------------------------------------


def test_ghcli_fork_args():
    stub = Stub(ok({"full_name": "o/a--b"}))
    GhCli(runner=stub).fork("a/b", "o", "a--b")
    assert stub.cmds == [
        [
            "gh",
            "api",
            "-X",
            "POST",
            "repos/a/b/forks",
            "-f",
            "organization=o",
            "-f",
            "name=a--b",
            "-F",
            "default_branch_only=false",
        ]
    ]


def test_ghcli_rate_limited():
    stub = Stub(fail("gh: was submitted too quickly (HTTP 403)"))
    with pytest.raises(RateLimited):
        GhCli(runner=stub).fork("a/b", "o", "a--b")


def test_ghcli_secondary_rate_limit():
    stub = Stub(fail("gh: You have exceeded a secondary rate limit (HTTP 403)"))
    with pytest.raises(RateLimited):
        GhCli(runner=stub).create_repo("o", "x", "d")


def test_ghcli_not_found():
    stub = Stub(fail("gh: Not Found (HTTP 404)"))
    with pytest.raises(NotFound):
        GhCli(runner=stub).get_repo("a/b")


def test_ghcli_other_error_is_github_error_with_stderr():
    stub = Stub(fail("gh: Validation Failed (HTTP 422)"))
    with pytest.raises(GitHubError) as e:
        GhCli(runner=stub).set_default_branch("a/b", "main")
    assert not isinstance(e.value, (NotFound, RateLimited))
    assert "Validation Failed (HTTP 422)" in str(e.value)


def test_ghcli_get_repo_follows_source():
    resp = {
        "full_name": "o/a--b",
        "fork": True,
        "source": {"full_name": "root/b"},
        "default_branch": "main",
        "archived": False,
        "size": 1234,
    }
    stub = Stub(ok(resp))
    info = GhCli(runner=stub).get_repo("o/a--b")
    assert stub.cmds[0][-1] == "repos/o/a--b"
    assert info == RepoInfo("o/a--b", True, "root/b", "main", False, 1234)


def test_ghcli_get_repo_without_source_is_own_network():
    resp = {"full_name": "new/x", "fork": False, "default_branch": "dev", "archived": True, "size": 5}
    info = GhCli(runner=Stub(ok(resp))).get_repo("old/x")
    assert info == RepoInfo("new/x", False, "new/x", "dev", True, 5)


def test_ghcli_resolve_commit():
    gh = GhCli(
        runner=Stub(ok({"sha": A}), fail("gh: Not Found (HTTP 404)"), fail("gh: No commit (HTTP 422)"))
    )
    assert gh.resolve_commit("a/b", "aaaa") == A
    assert gh.resolve_commit("a/b", "dead") is None
    assert gh.resolve_commit("a/b", "zz") is None


def test_ghcli_branches_visible():
    stub = Stub(ok([]), ok([{"name": "main"}]))
    gh = GhCli(runner=stub)
    assert gh.branches_visible("o/x") is False
    assert gh.branches_visible("o/x") is True
    assert stub.cmds[0][-1] == "repos/o/x/branches?per_page=1"


def test_ghcli_get_tag():
    stub = Stub(ok({"object": {"sha": B, "type": "tag"}}), fail("gh: Not Found (HTTP 404)"))
    gh = GhCli(runner=stub)
    assert gh.get_tag("o/x", "tapeout/ws-run1/d") == B
    assert stub.cmds[0][-1] == "repos/o/x/git/ref/tags/tapeout/ws-run1/d"
    assert gh.get_tag("o/x", "tapeout/ws-run1/d") is None


def test_ghcli_create_tag_makes_object_then_ref():
    stub = Stub(ok({"sha": B}), ok({"ref": "refs/tags/t/1"}))
    GhCli(runner=stub).create_tag("o/x", "t/1", A, "msg")
    assert stub.cmds == [
        [
            "gh",
            "api",
            "-X",
            "POST",
            "repos/o/x/git/tags",
            "-f",
            "tag=t/1",
            "-f",
            "message=msg",
            "-f",
            f"object={A}",
            "-f",
            "type=commit",
        ],
        ["gh", "api", "-X", "POST", "repos/o/x/git/refs", "-f", "ref=refs/tags/t/1", "-f", f"sha={B}"],
    ]


def test_ghcli_create_repo_and_default_branch():
    stub = Stub(ok({}), ok({}))
    gh = GhCli(runner=stub)
    gh.create_repo("o", "x", "desc")
    gh.set_default_branch("o/x", "upstream/main")
    assert stub.cmds == [
        [
            "gh",
            "api",
            "-X",
            "POST",
            "orgs/o/repos",
            "-f",
            "name=x",
            "-f",
            "description=desc",
            "-F",
            "private=false",
        ],
        ["gh", "api", "-X", "PATCH", "repos/o/x", "-f", "default_branch=upstream/main"],
    ]


def test_ghcli_list_repos_paginates():
    page1 = [{"full_name": "o/a", "fork": True, "default_branch": "main", "archived": False, "size": 1}]
    page2 = [{"full_name": "o/b", "fork": False, "default_branch": "dev", "archived": False, "size": 2}]
    stub = Stub(ok([page1, page2]))
    repos = GhCli(runner=stub).list_repos("o")
    assert stub.cmds[0] == ["gh", "api", "-X", "GET", "orgs/o/repos?per_page=100", "--paginate", "--slurp"]
    assert repos == [
        RepoInfo("o/a", True, "o/a", "main", False, 1),
        RepoInfo("o/b", False, "o/b", "dev", False, 2),
    ]


def test_ghcli_list_branches_paginates():
    page1 = [{"name": f"b{i}", "commit": {"sha": A}} for i in range(100)]
    page2 = [{"name": "main", "commit": {"sha": B}}]
    stub = Stub(ok([page1, page2]))
    branches = GhCli(runner=stub).list_branches("o/x")
    assert stub.cmds[0] == [
        "gh",
        "api",
        "-X",
        "GET",
        "repos/o/x/branches?per_page=100",
        "--paginate",
        "--slurp",
    ]
    assert len(branches) == 101
    assert branches["main"] == B and branches["b99"] == A


def test_ghcli_set_ref_updates_existing():
    stub = Stub(ok({}))
    GhCli(runner=stub).set_ref("o/x", "heads/upstream/main", A)
    assert stub.cmds == [
        [
            "gh",
            "api",
            "-X",
            "PATCH",
            "repos/o/x/git/refs/heads/upstream/main",
            "-f",
            f"sha={A}",
            "-F",
            "force=true",
        ]
    ]


def test_ghcli_set_ref_updates_then_creates():
    stub = Stub(fail("gh: Reference does not exist (HTTP 422)"), ok({}))
    GhCli(runner=stub).set_ref("o/x", "heads/upstream/main", A)
    assert stub.cmds[1] == [
        "gh",
        "api",
        "-X",
        "POST",
        "repos/o/x/git/refs",
        "-f",
        "ref=refs/heads/upstream/main",
        "-f",
        f"sha={A}",
    ]


def test_ghcli_set_ref_other_error_propagates():
    stub = Stub(fail("gh: Object does not exist (HTTP 422)"))
    with pytest.raises(GitHubError):
        GhCli(runner=stub).set_ref("o/x", "heads/x", A)
    assert len(stub.cmds) == 1


def test_ghcli_delete_ref_and_dispatch():
    stub = Stub(ok(""), ok(""))
    gh = GhCli(runner=stub)
    gh.delete_ref("o/x", "heads/old")
    gh.dispatch("o/x", "sync")
    assert stub.cmds == [
        ["gh", "api", "-X", "DELETE", "repos/o/x/git/refs/heads/old"],
        ["gh", "api", "-X", "POST", "repos/o/x/dispatches", "-f", "event_type=sync"],
    ]


def test_ghcli_get_file_decodes_base64():
    text = "design: foo\nrun: ws-run1\n"
    content = base64.encodebytes(text.encode()).decode()  # GitHub wraps lines
    stub = Stub(ok({"content": content, "encoding": "base64"}), fail("gh: Not Found (HTTP 404)"))
    gh = GhCli(runner=stub)
    assert gh.get_file("o/x", "info.yaml", "about") == text
    assert stub.cmds[0][-1] == "repos/o/x/contents/info.yaml?ref=about"
    assert gh.get_file("o/x", "missing", "about") is None


# --- FakeGitHub contract ----------------------------------------------------


@pytest.fixture(params=[FakeGitHub])
def gh(request):
    return request.param()


def test_fake_implements_protocol():
    assert isinstance(FakeGitHub(), GitHub)
    assert isinstance(GhCli(), GitHub)


def test_fake_one_fork_per_network(gh):
    gh.add_upstream("root/x", commits=[A], branches={"main": A})
    gh.add_upstream("someone/x", network="root/x")
    gh.fork("root/x", "o", "root--x")
    with pytest.raises(GitHubError, match="already forked in network"):
        gh.fork("someone/x", "o", "someone--x")


def test_fake_fork_copies_branches_and_network(gh):
    gh.add_upstream("root/x", commits=[A], branches={"main": A})
    gh.add_upstream("someone/x", network="root/x", branches={"main": B, "dev": C})
    gh.fork("someone/x", "o", "someone--x")
    info = gh.get_repo("o/someone--x")
    assert info.fork and info.network == "root/x" and info.default_branch == "main"
    assert gh.branches["o/someone--x"] == {"main": B, "dev": C}
    assert gh.resolve_commit("o/someone--x", "aaaa") == A
    assert gh.calls == [("fork", ("someone/x", "o", "someone--x"))]


def test_fake_rate_limit_then_ok(gh):
    gh.add_upstream("root/x", branches={"main": A})
    gh.rate_limit_next(2)
    for _ in range(2):
        with pytest.raises(RateLimited):
            gh.fork("root/x", "o", "root--x")
    gh.fork("root/x", "o", "root--x")
    assert gh.get_repo("o/root--x").fork
    assert [c[0] for c in gh.calls] == ["fork", "fork", "fork"]


def test_fake_ready_after(gh):
    gh.add_upstream("root/x", branches={"main": A})
    gh.ready_after(2)
    gh.fork("root/x", "o", "root--x")
    assert [gh.branches_visible("o/root--x") for _ in range(3)] == [False, False, True]


def test_fake_renamed_repo_reports_current_name(gh):
    gh.add_upstream("old/x", renamed_to="new/x", branches={"main": A})
    assert gh.get_repo("old/x").full_name == "new/x"
    assert gh.list_branches("old/x") == gh.list_branches("new/x") == {"main": A}


def test_fake_deleted_repo_not_found(gh):
    gh.add_upstream("gone/x", deleted=True)
    with pytest.raises(NotFound):
        gh.get_repo("gone/x")
    with pytest.raises(NotFound):
        gh.fork("gone/x", "o", "gone--x")


def test_fake_set_ref_rejects_foreign_sha(gh):
    gh.add_upstream("root/x", branches={"main": A})
    gh.add_upstream("other/y", branches={"main": B})
    gh.fork("root/x", "o", "root--x")
    with pytest.raises(GitHubError):
        gh.set_ref("o/root--x", "heads/upstream/main", B)
    with pytest.raises(GitHubError):
        gh.create_tag("o/root--x", "tapeout/r/d", B, "m")
    gh.set_ref("o/root--x", "heads/upstream/main", A)
    gh.add_commits("o/root--x", [C])
    gh.set_ref("o/root--x", "heads/upstream/main", C)
    assert gh.branches["o/root--x"]["upstream/main"] == C


def test_fake_cannot_delete_default_branch(gh):
    gh.add_upstream("root/x", branches={"main": A, "dev": A})
    gh.fork("root/x", "o", "root--x")
    with pytest.raises(GitHubError):
        gh.delete_ref("o/root--x", "heads/main")
    gh.delete_ref("o/root--x", "heads/dev")
    assert gh.branches["o/root--x"] == {"main": A}
    assert ("delete_ref", ("o/root--x", "heads/main")) in gh.calls


def test_fake_tags(gh):
    gh.add_upstream("root/x", branches={"main": A})
    gh.fork("root/x", "o", "root--x")
    assert gh.get_tag("o/root--x", "tapeout/r/d") is None
    gh.create_tag("o/root--x", "tapeout/r/d", A, "m")
    assert gh.get_tag("o/root--x", "tapeout/r/d") is not None
    assert gh.tags["o/root--x"] == {"tapeout/r/d": A}
    with pytest.raises(GitHubError):
        gh.create_tag("o/root--x", "tapeout/r/d", A, "m")


def test_fake_create_repo_list_and_misc(gh):
    gh.add_upstream("gitlab-mirror/x", branches={"main": A})
    gh.create_repo("o", "gl--x", "copy")
    assert gh.list_branches("o/gl--x") == {}
    with pytest.raises(GitHubError):
        gh.set_ref("o/gl--x", "heads/main", A)  # different network
    gh.add_commits("o/gl--x", [A])
    gh.set_ref("o/gl--x", "heads/upstream/main", A)
    gh.set_default_branch("o/gl--x", "upstream/main")
    assert gh.get_repo("o/gl--x").default_branch == "upstream/main"
    assert gh.default_branch["o/gl--x"] == "upstream/main"
    with pytest.raises(GitHubError):
        gh.set_default_branch("o/gl--x", "nope")
    assert [r.full_name for r in gh.list_repos("o")] == ["o/gl--x"]
    gh.files[("o/gl--x", "about", "info.yaml")] = "x: 1\n"
    assert gh.get_file("o/gl--x", "info.yaml", "about") == "x: 1\n"
    assert gh.get_file("o/gl--x", "nope", "about") is None
    gh.dispatch("o/gl--x", "sync")
    assert gh.dispatches == [("o/gl--x", "sync")]
    assert gh.resolve_commit("o/gl--x", "ffff") is None
    assert [c[0] for c in gh.calls] == [
        "create_repo",
        "set_ref",
        "set_ref",
        "set_default_branch",
        "set_default_branch",
        "dispatch",
    ]
