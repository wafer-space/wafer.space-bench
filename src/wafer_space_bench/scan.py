"""Scan upstream repos on GitHub and plan how to mirror each into the bench org."""

import collections
import csv
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from wafer_space_bench.catalog import Design, load_designs
from wafer_space_bench.events import EventLog
from wafer_space_bench.github import GhCli, GitHub, NotFound
from wafer_space_bench.naming import RepoRef, bench_name, parse_repo_url

Status = Literal["live", "archived", "missing", "gitlab"]
Strategy = Literal["fork", "copy", "restore", "none"]

COLUMNS = [
    "source",
    "current_name",
    "status",
    "network",
    "default_branch",
    "size_kb",
    "strategy",
    "target",
    "missing_commits",
]


@dataclass
class RepoPlan:
    source: RepoRef
    current_name: str
    status: Status
    network: str
    default_branch: str
    size_kb: int
    strategy: Strategy
    target: str
    missing_commits: list[str] = field(default_factory=list)


def plan_repos(
    designs: list[Design], gh: GitHub, *, org: str = "wafer-space-bench", progress=None
) -> list[RepoPlan]:
    """One plan per distinct source repo, sorted by `source.key`.

    The first repo (in that order) of each fork network is forked; later ones in the same
    network are copied. GitLab repos are copied, deleted ones restored. Errors other than
    NotFound propagate.
    """
    repos: dict[str, RepoRef] = {}
    commits: dict[str, set[str]] = collections.defaultdict(set)
    for d in designs:
        ref = parse_repo_url(d.repo)
        if ref is None:
            continue
        repos.setdefault(ref.key, ref)
        if d.commit:
            commits[ref.key].add(d.commit)

    plans: list[RepoPlan] = []
    claimed: set[str] = set()
    keys = sorted(repos)
    for i, key in enumerate(keys, 1):
        src = repos[key]
        if src.host != "github.com":
            plans.append(RepoPlan(src, "", "gitlab", "", "", 0, "copy", f"{org}/{bench_name(src.full_name)}"))
        else:
            try:
                info = gh.get_repo(src.full_name)
            except NotFound:
                plans.append(
                    RepoPlan(src, "", "missing", "", "", 0, "restore", f"{org}/{bench_name(src.full_name)}")
                )
            else:
                missing = [c for c in sorted(commits[key]) if gh.resolve_commit(info.full_name, c) is None]
                net = info.network.lower()
                strategy: Strategy = "copy" if net in claimed else "fork"
                claimed.add(net)
                plans.append(
                    RepoPlan(
                        src,
                        info.full_name,
                        "archived" if info.archived else "live",
                        info.network,
                        info.default_branch,
                        info.size_kb,
                        strategy,
                        f"{org}/{bench_name(info.full_name)}",
                        missing,
                    )
                )
        if progress and (i % 25 == 0 or i == len(keys)):
            progress(i, len(keys))
    return plans


def write_plans(path: Path, plans: list[RepoPlan]) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        for p in plans:
            w.writerow(
                {
                    "source": f"{p.source.host}/{p.source.full_name}",
                    "current_name": p.current_name,
                    "status": p.status,
                    "network": p.network,
                    "default_branch": p.default_branch,
                    "size_kb": p.size_kb,
                    "strategy": p.strategy,
                    "target": p.target,
                    "missing_commits": " ".join(p.missing_commits),
                }
            )


def read_plans(path: Path) -> list[RepoPlan]:
    with open(path, newline="") as f:
        return [
            RepoPlan(
                source=RepoRef(*r["source"].split("/", 2)),
                current_name=r["current_name"],
                status=r["status"],
                network=r["network"],
                default_branch=r["default_branch"],
                size_kb=int(r["size_kb"] or 0),
                strategy=r["strategy"],
                target=r["target"],
                missing_commits=r["missing_commits"].split(),
            )
            for r in csv.DictReader(f)
        ]


def _scan(args) -> int:
    designs = load_designs(args.catalog)
    plans = plan_repos(
        designs, GhCli(), org=args.org, progress=lambda n, m: print(f"scanned {n}/{m}", flush=True)
    )
    write_plans(args.plans, plans)
    counts = collections.Counter(f"{p.status}/{p.strategy}" for p in plans)
    EventLog(args.log).write("scan", repos=len(plans), counts=dict(sorted(counts.items())))
    for k, n in sorted(counts.items()):
        print(f"{k}: {n}")
    return 0


def add_subcommand(subparsers) -> None:
    p = subparsers.add_parser("scan", help="read-only scan of upstream repos; writes the plans CSV")
    p.set_defaults(handler=_scan)
