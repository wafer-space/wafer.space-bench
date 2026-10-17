import subprocess

import pytest

from wafer_space_bench.git import IDENTITY, Git, GitError, git_env


def ls_tree(path, ref, *args):
    p = subprocess.run(
        ["git", "ls-tree", *args, "--name-only", ref], cwd=path, capture_output=True, text=True, check=True
    )
    return p.stdout.split()


def test_mktree_commit_tree_orphan(tmp_path):
    g = Git.init_bare(tmp_path / "r.git")
    tree = g.mktree({"README.md": g.hash_blob("# hi\n"), "bench.yaml": g.hash_blob("a: 1\n")})
    c = g.commit_tree(tree, [], "about")
    assert ls_tree(g.path, c) == ["README.md", "bench.yaml"]
    parents = subprocess.run(
        ["git", "rev-list", "--parents", "-n1", c], cwd=g.path, capture_output=True, text=True, check=True
    ).stdout.split()
    assert parents == [c]  # no parents
    who = subprocess.run(
        ["git", "log", "-1", "--format=%an <%ae>|%cn <%ce>", c],
        cwd=g.path,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert who == f"{IDENTITY}|{IDENTITY}"


def test_mktree_nested_paths(tmp_path):
    g = Git.init_bare(tmp_path / "r.git")
    tree = g.mktree({"README.md": g.hash_blob("x"), ".github/workflows/ci.yml": g.hash_blob("y")})
    c = g.commit_tree(tree, [], "m")
    assert ls_tree(g.path, c, "-r") == [".github/workflows/ci.yml", "README.md"]


def test_clone_refs_tags_and_update(tmp_path, upstream):
    path, shas = upstream
    g = Git.clone_bare(str(path), tmp_path / "c.git")
    assert sorted(g.heads()) == ["feature/x", "main", "ttgf0p2"]
    assert g.tags() == []
    assert g.rev_parse("refs/heads/main") == shas["main"]
    assert g.rev_parse(shas["main"][:10]) == shas["main"]
    assert g.rev_parse("refs/heads/nope") is None
    assert g.rev_parse("f" * 40) is None
    g.create_tag("a/b/c", shas["ttgf0p2"], "msg")
    assert g.tags() == ["a/b/c"]
    assert g.rev_parse("refs/tags/a/b/c") == shas["ttgf0p2"]  # peeled to the commit
    g.update_ref("refs/heads/upstream/main", shas["main"])
    assert "upstream/main" in g.heads()
    g.delete_ref("refs/heads/upstream/main")
    assert "upstream/main" not in g.heads()


def test_fetch_and_push(tmp_path, upstream, remote):
    path, _ = upstream
    g = Git.init_bare(tmp_path / "e.git")
    g.fetch(str(path), ["+refs/heads/*:refs/heads/*"])
    assert sorted(g.heads()) == ["feature/x", "main", "ttgf0p2"]
    g.push(remote, ["refs/heads/main:refs/heads/main"])
    assert Git(tmp_path / "remote.git").heads() == ["main"]


def test_git_error_has_command_and_stderr(tmp_path):
    g = Git.init_bare(tmp_path / "r.git")
    with pytest.raises(GitError, match="git update-ref") as e:
        g.update_ref("refs/heads/x", "f" * 40)
    assert len(str(e.value)) < 400


def test_clone_missing_fails_fast(tmp_path):
    with pytest.raises(GitError):
        Git.clone_bare((tmp_path / "missing").as_uri(), tmp_path / "c.git")
    assert not (tmp_path / "c.git").exists()


def test_git_error_redacts_paths(tmp_path):
    with pytest.raises(GitError) as e:
        Git.clone_bare((tmp_path / "missing").as_uri(), tmp_path / "c.git")
    assert str(tmp_path) not in str(e.value) and "<arg>" in str(e.value)
    g = Git.init_bare(tmp_path / "r.git")
    with pytest.raises(GitError) as e:
        g.fetch(str(tmp_path / "missing2"), ["+refs/heads/*:refs/heads/*"])
    assert str(tmp_path) not in str(e.value)


def test_fetch_no_tags(tmp_path, upstream):
    path, shas = upstream
    subprocess.run(["git", "tag", "v1", shas["main"]], cwd=path, check=True)
    g = Git.init_bare(tmp_path / "e.git")
    g.fetch(str(path), ["+refs/heads/*:refs/heads/*"], no_tags=True)
    assert g.tags() == [] and "main" in g.heads()
    g.fetch(str(path), ["+refs/heads/*:refs/heads/*"])
    assert g.tags() == ["v1"]  # auto-followed without no_tags


def test_identity_not_overridden_by_env(tmp_path, monkeypatch):
    for v in ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME"):
        monkeypatch.setenv(v, "Someone")
    for v in ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL"):
        monkeypatch.setenv(v, "x@example.com")
    g = Git.init_bare(tmp_path / "r.git")
    c = g.commit_tree(g.mktree({"a": g.hash_blob("a")}), [], "m")
    who = subprocess.run(
        ["git", "log", "-1", "--format=%an <%ae>|%cn <%ce>", c], cwd=g.path, capture_output=True, text=True,
        check=True,
    ).stdout.strip()  # fmt: skip
    assert who == f"{IDENTITY}|{IDENTITY}"


def test_git_env_batch_ssh(monkeypatch):
    monkeypatch.delenv("GIT_SSH_COMMAND", raising=False)
    env = git_env()
    assert env["GIT_SSH_COMMAND"] == "ssh -o BatchMode=yes" and env["GIT_TERMINAL_PROMPT"] == "0"
    monkeypatch.setenv("GIT_SSH_COMMAND", "ssh -i key")
    assert git_env()["GIT_SSH_COMMAND"] == "ssh -i key"


def test_non_utf8_output_does_not_raise(tmp_path, upstream):
    path, shas = upstream
    g = Git.clone_bare(str(path), tmp_path / "c.git")
    subprocess.run(
        [b"git", b"update-ref", b"refs/heads/caf\xe9", shas["main"].encode()], cwd=g.path, check=True
    )
    assert len(g.heads()) == 4


def test_show_reads_blob_or_none(tmp_path):
    g = Git.init_bare(tmp_path / "r.git")
    c = g.commit_tree(g.mktree({"a.txt": g.hash_blob("hello\n")}), [], "m")
    g.update_ref("refs/heads/x", c)
    assert g.show("refs/heads/x", "a.txt") == "hello\n"
    assert g.show("refs/heads/x", "missing") is None
    assert g.show("refs/heads/nope", "a.txt") is None
