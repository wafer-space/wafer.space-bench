"""Command line entry point: ``wsbench <command>``."""

import argparse
from pathlib import Path

from wafer_space_bench import copy, fork, layout, scan

# Later tasks add modules here; each exposes ``add_subcommand(subparsers)``.
# Handler contract: add_subcommand must call ``parser.set_defaults(handler=fn)``, where
# ``fn(args)`` returns an int exit code (or None, meaning 0).
SUBCOMMAND_MODULES: tuple = (scan, fork, copy, layout)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="wsbench", description=__doc__)
    parser.add_argument("--org", default="wafer-space-bench", help="GitHub organisation holding the copies")
    parser.add_argument("--catalog", type=Path, default=Path("data/designs.csv"), help="design catalog CSV")
    parser.add_argument(
        "--plans", type=Path, default=Path("data/bench_repos.csv"), help="bench repo plans CSV"
    )
    parser.add_argument("--log", type=Path, default=Path("work/events.jsonl"), help="event log (JSON lines)")
    parser.add_argument("--config", type=Path, default=Path("bench.local.toml"), help="local config file")
    parser.add_argument(
        "--remote-fmt",
        default="git@github.com:{full}.git",
        help="git remote URL template; {full} is owner/name",
    )
    sub = parser.add_subparsers(dest="command", title="commands")
    for module in SUBCOMMAND_MODULES:
        module.add_subcommand(sub)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handler = getattr(args, "handler", None)
    if handler is None:
        parser.print_help()
        return 2
    return handler(args) or 0


if __name__ == "__main__":
    raise SystemExit(main())
