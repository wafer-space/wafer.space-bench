from pathlib import Path

import pytest

from wafer_space_bench.cli import build_parser, main


def test_help_lists_subcommands(capsys):
    with pytest.raises(SystemExit) as e:
        main(["--help"])
    assert e.value.code == 0
    assert "wsbench" in capsys.readouterr().out


def test_global_option_defaults():
    args = build_parser().parse_args([])
    assert args.org == "wafer-space-bench"
    assert args.catalog == Path("data/designs.csv")
    assert args.plans == Path("data/bench_repos.csv")
    assert args.log == Path("work/events.jsonl")
    assert args.config == Path("bench.local.toml")
    assert args.remote_fmt == "git@github.com:{full}.git"


def test_global_options_override():
    args = build_parser().parse_args(["--org", "x", "--remote-fmt", "https://h/{full}"])
    assert args.org == "x"
    assert args.remote_fmt == "https://h/{full}"


def test_no_subcommand_prints_help_and_returns_2(capsys):
    assert main([]) == 2
    assert "usage: wsbench" in capsys.readouterr().out


# --- final review: --only that matches nothing (T6) ------------------------------


class NoGitHub:
    def __getattr__(self, name):
        raise AssertionError(f"GitHub must not be called ({name})")


def _plans_cli(tmp_path, monkeypatch):
    from wafer_space_bench.naming import RepoRef
    from wafer_space_bench.scan import RepoPlan, write_plans

    org = "wafer-space-bench"
    plans = [
        RepoPlan(RepoRef("github.com", "a", "f"), "a/f", "live", "a/f", "main", 1, "fork", f"{org}/a--f"),
        RepoPlan(RepoRef("github.com", "b", "c"), "b/c", "live", "a/f", "main", 1, "copy", f"{org}/b--c"),
    ]
    write_plans(tmp_path / "plans.csv", plans)
    (tmp_path / "designs.csv").write_text("pool,shuttle,code,project,repo,commit,licence,kind,size\n")
    for mod in ("fork", "copy", "layout", "sync"):
        monkeypatch.setattr(f"wafer_space_bench.{mod}.GhCli", NoGitHub)
    monkeypatch.chdir(tmp_path)
    return ["--plans", str(tmp_path / "plans.csv"), "--catalog", str(tmp_path / "designs.csv"),
            "--log", str(tmp_path / "e.jsonl"), "--config", str(tmp_path / "none.toml")]  # fmt: skip


@pytest.mark.parametrize(
    "cmd",
    [
        ["fork", "--only", "nope"],
        ["fork", "--only", "b--c"],  # a copy plan: nothing for fork to do
        ["copy", "--only", "nope"],
        ["copy", "--only", "a--f"],
        ["layout", "--only", "nope"],
        ["sync", "--only", "nope"],
        ["sync", "--only", "nope", "--shard", "1/8"],
    ],
)
def test_only_matching_nothing_warns_and_exits_2(tmp_path, monkeypatch, capsys, cmd):
    base = _plans_cli(tmp_path, monkeypatch)
    assert main([*base, *cmd]) == 2
    err = capsys.readouterr().err
    assert "--only matched no" in err and cmd[0] in err
    assert not (tmp_path / "e.jsonl").exists()


def test_sync_only_outside_this_shard_is_not_an_error(tmp_path, monkeypatch):
    """Every shard gets the same --only; shards without the repo have nothing to do."""
    from wafer_space_bench.sync import shard_of

    base = _plans_cli(tmp_path, monkeypatch)
    other = shard_of("wafer-space-bench/a--f", 8) % 8 + 1  # any shard but the repo's own
    assert main([*base, "sync", "--only", "a--f", "--shard", f"{other}/8"]) == 0
