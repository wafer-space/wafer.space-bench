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
from wafer_space_bench.naming import COMMIT_SOURCE
from wafer_space_bench.scan import RepoPlan, read_plans

STRATEGIES = ("copy", "restore")

# Tapeout tags (<org>/<run>/<slot>) are frozen: never forced, on fetch or on push (R12).
TAPEOUT_NAMESPACES = tuple(COMMIT_SOURCE)
# Upstream tags may move (forced); the negative refspecs keep tapeout namespaces out.
UPSTREAM_TAG_REFSPECS = ["+refs/tags/*:refs/tags/*", *(f"^refs/tags/{o}/*" for o in TAPEOUT_NAMESPACES)]


def _is_tapeout_tag(tag: str) -> bool:
    return tag.split("/", 1)[0] in TAPEOUT_NAMESPACES


def push_tags(git: Git, remote: str) -> None:
    """Push upstream tags forced, then tapeout tags never forced (a differing remote
    tapeout tag is rejected and raises GitError). Negative refspecs apply to a whole
    push, and a push matching no refs fails, hence separate pushes of non-empty sets."""
    tags = git.tags()
    if any(not _is_tapeout_tag(t) for t in tags):
        git.push(remote, UPSTREAM_TAG_REFSPECS)
    spaces = sorted({t.split("/", 1)[0] for t in tags if _is_tapeout_tag(t)})
    if spaces:
        git.push(remote, [f"refs/tags/{o}/*:refs/tags/{o}/*" for o in spaces])


@dataclass(frozen=True)
class RestoreSource:
    public_url: str
    description: str


def restore_sources(path: Path) -> dict[str, RestoreSource]:
    """Restore sources keyed by RepoRef.key (lowercased owner/name)."""
    with open(path, "rb") as f:
        data = tomllib.load(f)
    return {k.lower(): RestoreSource(v["public_url"], v["description"]) for k, v in data.items()}


def copy_why(plan: RepoPlan) -> str:
    """Why `plan` is a copy, for its description and its tapeout tag messages."""
    if plan.strategy == "restore":
        return "upstream repository was deleted"
    if plan.source.host != "github.com":
        return "hosted on GitLab"
    return f"shares the fork network of {plan.network}"


def copy_description(plan: RepoPlan) -> str:
    return f"Copy of {plan.source.url} for the wafer.space GF180 benchmark ({copy_why(plan)})"


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


def _clone_or_fetch(url: str, dest: Path, target: str, log: Log) -> Git:
    """Clone `url` into `dest`, or refresh an existing clone. A failed first clone raises
    GitError; a failed refresh logs `fetch-failed` and keeps the existing clone."""
    if not dest.exists():
        return Git.clone_bare(url, dest)
    git = Git(dest)
    try:
        git.fetch(url, ["+refs/heads/*:refs/heads/*"], prune=True, no_tags=True)
        # No prune (keeps our tapeout tags); upstream tags in tapeout namespaces are ignored.
        git.fetch(url, UPSTREAM_TAG_REFSPECS, no_tags=True)
    except GitError as e:
        log("fetch-failed", target=target, error=str(e))
    return git


def fetch_tapeout_tags(git: Git, remote: str) -> None:
    """Fetch the bench repo's tapeout tags into the local clone, never forced: tags made
    elsewhere (another clone, or sync through the API) are reused, not made again. A local
    tag that differs from the bench repo's is refused and raises GitError."""
    git.fetch(remote, [f"refs/tags/{o}/*:refs/tags/{o}/*" for o in TAPEOUT_NAMESPACES], no_tags=True)


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
    "created+pushed" (the repo was created), "pushed", "exists (laid out)", "no source", "dry-run: <public|local mirror>" or "error: ..."."""
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
    why = copy_why(plan)
    src = sources.get(plan.source.key) if plan.strategy == "restore" else None
    if src:
        why += f"; copied from {src.description}"
    try:
        try:
            git = _clone_or_fetch(url, local_clone_path(cfg, plan.target), plan.target, log)
        except GitError:
            if plan.strategy == "restore":
                return done("no source")
            raise
        remote = remote_fmt.format(full=plan.target)
        try:
            laid_out = gh.get_repo(plan.target).default_branch == "about"
            exists = True
        except NotFound:  # first run: nothing to fetch from yet
            laid_out = exists = False
        if exists:
            fetch_tapeout_tags(git, remote)
        _tag_tapeouts(git, plan, tapeouts.get(plan.source.key, []), why, log)
        created = False
        if not exists:
            name = plan.target.split("/", 1)[1]
            description = copy_description(plan)
            retry_rate_limited(
                lambda: gh.create_repo(org, name, description), sleep=sleep, log=log, target=plan.target
            )
            created = True
        if not laid_out:  # once laid out, the original branches live under upstream/*
            git.push(remote, ["refs/heads/*:refs/heads/*"], atomic=True)
        push_tags(git, remote)
        if laid_out:
            return done("exists (laid out)")
        return done("created+pushed" if created else "pushed")
    except (GitError, GitHubError) as e:
        return done(f"error: {e}")


def run_copies(
    plans: list[RepoPlan],
    tapeouts: dict[str, list[Tapeout]],
    gh: GitHub,
    cfg: LocalConfig,
    org: str,
    remote_fmt: str = "git@github.com:{full}.git",
    *,
    log: Log,
    sources: dict[str, RestoreSource] | None = None,
    dry_run: bool = False,
    delay: float = 20,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """run_copy every copy/restore plan, sleeping `delay` after each created repo.
    Returns the number of plans that ended in an error or with no source to copy from."""
    todo = [p for p in plans if p.strategy in STRATEGIES]
    failed = 0
    for i, p in enumerate(todo, 1):
        status = run_copy(
            p, tapeouts, gh, cfg, org, remote_fmt, log=log, sources=sources, dry_run=dry_run, sleep=sleep
        )
        print(f"{i}/{len(todo)} {p.target}: {status}", flush=True)
        failed += status.startswith("error:") or status == "no source"
        if status == "created+pushed":
            sleep(delay)
    return failed


def _copy(args) -> int:
    plans = read_plans(args.plans)
    if args.only:
        only = {o.lower() for o in args.only}
        plans = [p for p in plans if plan_matches(p, only)]
    failed = run_copies(
        plans,
        tapeouts_by_repo(load_designs(args.catalog)),
        GhCli(),
        load_config(args.config),
        args.org,
        args.remote_fmt,
        log=EventLog(args.log).write,
        sources=restore_sources(args.restore_sources),
        dry_run=args.dry_run,
        delay=args.delay,
    )
    return 1 if failed else 0


def add_subcommand(subparsers) -> None:
    p = subparsers.add_parser("copy", help="copy/restore repos that cannot be forks into the bench org")
    p.add_argument(
        "--only", nargs="+", metavar="NAME", help="only these repos (bench, current or source name)"
    )
    p.add_argument("--dry-run", action="store_true", help="only log what would be done")
    p.add_argument("--delay", type=float, default=20, help="seconds to wait after each created repo")
    p.add_argument(
        "--restore-sources",
        type=Path,
        default=Path("data/restore_sources.toml"),
        help="public sources of deleted repos",
    )
    p.set_defaults(handler=_copy)
