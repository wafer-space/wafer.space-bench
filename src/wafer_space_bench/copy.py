"""Copy or restore upstream repos that cannot be GitHub forks.

- copy: a second repo of a fork network the org already forks, or a GitLab repo.
- restore: the upstream was deleted; the source is a public shuttle slot repository
  (data/restore_sources.toml) or a local mirror of the original URL.

Each repo is bare-cloned (or re-fetched) into `work_dir/mirrors/<bench name>.git`, its
taped-out commits get annotated tags, and branches and tags are pushed to a new plain
public repo in the org. Idempotent.
"""

import time
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from wafer_space_bench.catalog import Tapeout, load_designs, tapeouts_by_repo
from wafer_space_bench.config import LocalConfig, load_config, resolve_clone_url
from wafer_space_bench.events import EventLog
from wafer_space_bench.fork import Log, plan_matches, retry_rate_limited, tag_message
from wafer_space_bench.git import Git, GitError
from wafer_space_bench.github import GhCli, GitHub, GitHubError, NotFound
from wafer_space_bench.scan import RepoPlan, read_plans

STRATEGIES = ("copy", "restore")


@dataclass(frozen=True)
class RestoreSource:
    public_url: str
    description: str


def restore_sources(path: Path) -> dict[str, RestoreSource]:
    """Restore sources keyed by RepoRef.key (lowercased owner/name)."""
    with open(path, "rb") as f:
        data = tomllib.load(f)
    return {k.lower(): RestoreSource(v["public_url"], v["description"]) for k, v in data.items()}


def _why(plan: RepoPlan) -> str:
    if plan.strategy == "restore":
        return "upstream repository was deleted"
    if plan.source.host != "github.com":
        return "hosted on GitLab"
    return f"shares the fork network of {plan.network}"


def copy_description(plan: RepoPlan) -> str:
    return f"Copy of {plan.source.url} for the wafer.space GF180 benchmark ({_why(plan)})"


def _public_url(plan: RepoPlan, sources: dict[str, RestoreSource]) -> str:
    if plan.strategy == "restore":
        src = sources.get(plan.source.key)
        return src.public_url if src else plan.source.url
    if plan.source.host != "github.com":
        return plan.source.url
    return f"https://{plan.source.host}/{plan.current_name or plan.source.full_name}"


def local_clone_path(cfg: LocalConfig, target: str) -> Path:
    """Where the local bare clone of bench repo `target` (org/name) lives."""
    return cfg.work_dir / "mirrors" / f"{target.split('/', 1)[1]}.git"


def _clone_or_fetch(url: str, dest: Path) -> Git:
    if not dest.exists():
        return Git.clone_bare(url, dest)
    git = Git(dest)
    git.fetch(url, ["+refs/heads/*:refs/heads/*"], prune=True)
    git.fetch(url, ["refs/tags/*:refs/tags/*"])  # no prune: keeps our tapeout tags
    return git


def _tag_tapeouts(git: Git, plan: RepoPlan, tapeouts: list[Tapeout], why: str, log: Log) -> None:
    existing = set(git.tags())
    for t in tapeouts:
        if t.tag in existing:
            continue
        commit = git.rev_parse(t.commit) if t.commit else None
        if commit is None:
            log("missing-commit", target=plan.target, tag=t.tag, commit=t.commit)
            continue
        git.create_tag(t.tag, commit, tag_message(t, plan.source.url, why))


def run_copy(
    plan: RepoPlan,
    tapeouts: dict[str, list[Tapeout]],
    gh: GitHub,
    cfg: LocalConfig,
    org: str,
    remote_fmt: str = "git@github.com:{full}.git",
    *,
    log: Log,
    sources: dict[str, RestoreSource] | None = None,
    dry_run: bool = False,
    sleep: Callable[[float], None] = time.sleep,
) -> str:
    """Copy one copy/restore plan into the org. Logs one `copy` event; returns its status:
    "pushed", "exists (laid out)", "no source", "dry-run: <public|local mirror>" or "error: ..."."""
    if plan.strategy not in STRATEGIES:
        raise ValueError(f"run_copy handles copy/restore plans, not {plan.strategy!r}")
    sources = sources or {}
    public = _public_url(plan, sources)
    url = resolve_clone_url(public, cfg)
    kind = "public" if url == public else "local mirror"
    fields = {"source": plan.source.url, "target": plan.target, "from": kind}

    def done(status: str) -> str:
        log("copy", status=status, **fields)
        return status

    if dry_run:
        return done(f"dry-run: {kind}")
    why = _why(plan)
    src = sources.get(plan.source.key) if plan.strategy == "restore" else None
    if src:
        why += f"; copied from {src.description}"
    try:
        try:
            git = _clone_or_fetch(url, local_clone_path(cfg, plan.target))
        except GitError:
            if plan.strategy == "restore":
                return done("no source")
            raise
        _tag_tapeouts(git, plan, tapeouts.get(plan.source.key, []), why, log)
        try:
            laid_out = gh.get_repo(plan.target).default_branch == "about"
        except NotFound:
            name = plan.target.split("/", 1)[1]
            description = copy_description(plan)
            retry_rate_limited(
                lambda: gh.create_repo(org, name, description), sleep=sleep, log=log, target=plan.target
            )
            laid_out = False
        remote = remote_fmt.format(full=plan.target)
        if not laid_out:  # once laid out, the original branches live under upstream/*
            git.push(remote, ["refs/heads/*:refs/heads/*"])
        git.push(remote, ["refs/tags/*:refs/tags/*"])
        return done("exists (laid out)" if laid_out else "pushed")
    except (GitError, GitHubError) as e:
        return done(f"error: {e}")


def _copy(args) -> int:
    plans = [p for p in read_plans(args.plans) if p.strategy in STRATEGIES]
    if args.only:
        only = {o.lower() for o in args.only}
        plans = [p for p in plans if plan_matches(p, only)]
    tapeouts = tapeouts_by_repo(load_designs(args.catalog))
    cfg = load_config(args.config)
    sources = restore_sources(args.restore_sources)
    log = EventLog(args.log).write
    gh = GhCli()
    failed = 0
    for i, p in enumerate(plans, 1):
        status = run_copy(
            p, tapeouts, gh, cfg, args.org, args.remote_fmt, log=log, sources=sources, dry_run=args.dry_run
        )
        print(f"{i}/{len(plans)} {p.target}: {status}", flush=True)
        failed += status.startswith("error:")
    return 1 if failed else 0


def add_subcommand(subparsers) -> None:
    p = subparsers.add_parser("copy", help="copy/restore repos that cannot be forks into the bench org")
    p.add_argument(
        "--only", nargs="+", metavar="NAME", help="only these repos (bench, current or source name)"
    )
    p.add_argument("--dry-run", action="store_true", help="only log what would be done")
    p.add_argument(
        "--restore-sources",
        type=Path,
        default=Path("data/restore_sources.toml"),
        help="public sources of deleted repos",
    )
    p.set_defaults(handler=_copy)
