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
