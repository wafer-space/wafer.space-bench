"""Scan upstream repos on GitHub and plan how to mirror each into the bench org."""

import collections
import csv
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from wafer_space_bench.catalog import Design, load_designs, no_source_ref
from wafer_space_bench.events import EventLog
from wafer_space_bench.github import GhCli, GitHub, NotFound, RepoInfo
from wafer_space_bench.naming import RepoRef, bench_name, parse_repo_url

Status = Literal["live", "archived", "missing", "gitlab", "none"]
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


def none_plan(d: Design) -> RepoPlan:
    """The plan of a design with no known repository: reported, never created."""
    return RepoPlan(no_source_ref(d), "", "none", "", "", 0, "none", "")


def _org_repos(gh: GitHub, org: str) -> tuple[dict[str, RepoInfo], dict[str, str]]:
    """The org's repos by lowercased full name, and {lowercased network: full name} of the
    org's forks. The list endpoint has no fork source, so each fork is read with get_repo."""
    repos: dict[str, RepoInfo] = {}
    forks: dict[str, str] = {}
    for r in gh.list_repos(org):
        if r.fork:
            r = gh.get_repo(r.full_name)
            forks.setdefault(r.network.lower(), r.full_name)
        repos[r.full_name.lower()] = r
    return repos, forks


def plan_repos(
    designs: list[Design],
    gh: GitHub,
    *,
    org: str = "wafer-space-bench",
    progress=None,
    log: Callable[..., None] | None = None,
) -> list[RepoPlan]:
    """One plan per distinct source repo, sorted by `source.key`, then one `none` plan per
    design without a parseable repo (sorted by its key).

    What the org already holds wins: a bench repo that exists under the old or the current
    `bench_name` (any case) keeps its name, an upstream rename is only logged (`rename`),
    and the org's existing fork of a network stays that network's fork. Otherwise the first
    repo (by key) of each fork network is forked and later ones are copied; an existing
    plain repo stays a copy. GitLab repos are copied, deleted ones restored. Errors other
    than NotFound propagate.
    """
    if log is None:
        log = lambda event, **fields: None
    repos: dict[str, RepoRef] = {}
    commits: dict[str, set[str]] = collections.defaultdict(set)
    nones: list[RepoPlan] = []
    for d in designs:
        ref = parse_repo_url(d.repo)
        if ref is None:
            nones.append(none_plan(d))
            continue
        repos.setdefault(ref.key, ref)
        if d.commit:
            commits[ref.key].add(d.commit)

    in_org, org_forks = _org_repos(gh, org)

    def target_for(*full_names: str) -> tuple[str, RepoInfo | None]:
        """The existing bench repo of the first of `full_names` that has one, else the
        bench name of the first."""
        for full in full_names:
            r = in_org.get(f"{org}/{bench_name(full)}".lower())
            if r is not None:
                return r.full_name, r
        return f"{org}/{bench_name(full_names[0])}", None

    plans: list[RepoPlan] = []
    claimed: set[str] = set()
    keys = sorted(repos)
    for i, key in enumerate(keys, 1):
        src = repos[key]
        if src.host != "github.com":
            target, _ = target_for(src.full_name)
            plans.append(RepoPlan(src, "", "gitlab", "", "", 0, "copy", target))
        else:
            try:
                info = gh.get_repo(src.full_name)
            except NotFound:
                target, _ = target_for(src.full_name)
                plans.append(RepoPlan(src, "", "missing", "", "", 0, "restore", target))
            else:
                missing = [c for c in sorted(commits[key]) if gh.resolve_commit(info.full_name, c) is None]
                target, existing = target_for(info.full_name, src.full_name)
                if existing is not None and target.lower() != f"{org}/{bench_name(info.full_name)}".lower():
                    log("rename", source=src.full_name, current=info.full_name, target=target)
                net = info.network.lower()
                strategy: Strategy
                if net in org_forks:
                    strategy = "fork" if org_forks[net].lower() == target.lower() else "copy"
                elif existing is not None and not existing.fork:
                    strategy = "copy"
                else:
                    strategy = "copy" if net in claimed else "fork"
                if strategy == "fork":
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
                        target,
                        missing,
                    )
                )
        if progress and (i % 25 == 0 or i == len(keys)):
            progress(i, len(keys))
    return plans + sorted(nones, key=lambda p: p.source.key)


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
    events = EventLog(args.log)

    def log(event: str, **fields) -> None:
        events.write(event, **fields)
        print(event, *(f"{k}={v}" for k, v in fields.items()), flush=True)

    plans = plan_repos(
        designs,
        GhCli(),
        org=args.org,
        progress=lambda n, m: print(f"scanned {n}/{m}", flush=True),
        log=log,
    )
    write_plans(args.plans, plans)
    counts = collections.Counter(f"{p.status}/{p.strategy}" for p in plans)
    events.write("scan", repos=len(plans), counts=dict(sorted(counts.items())))
    for k, n in sorted(counts.items()):
        print(f"{k}: {n}")
    return 0


def add_subcommand(subparsers) -> None:
    p = subparsers.add_parser("scan", help="read-only scan of upstream repos; writes the plans CSV")
    p.set_defaults(handler=_scan)
