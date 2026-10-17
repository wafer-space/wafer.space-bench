"""Daily sync of the bench repos with their upstreams (`wsbench sync`).

Per bench repo (`sync_repo`):
- not in the org yet: "pending"; not laid out yet: run the layout (and nothing else);
- a `restore` has no upstream and is skipped;
- upstream gone, or listing no branches: change nothing ("upstream-missing");
- mirror upstream's branches to `upstream/<b>` exactly: deletes first, then creates and
  force-updates, so a rename of `x` to `x/y` cannot collide. A fork uses the refs API (its
  commits are already in the fork network; no clone). A copy fetches upstream into its bare
  mirror `work_dir/mirrors/<bench name>.git` and pushes, since its objects must be uploaded;
- create the catalog's tapeout tags and `tapeout/*` branches that the repo lacks;
- regenerate `about`, committing only when its content (not just `generated`) changed.

Nothing else is written: an existing `tapeout/*` branch is never moved (a mismatch is
logged), tags are only ever created, and `dev/*` and other branches are never touched.
A second run with nothing new upstream makes no writes.
"""

import argparse
import time
from collections.abc import Callable
from dataclasses import dataclass

from wafer_space_bench.catalog import Tapeout, load_designs, tapeouts_by_repo
from wafer_space_bench.config import LocalConfig, load_config, resolve_clone_url
from wafer_space_bench.copy import copy_why, local_clone_path
from wafer_space_bench.events import EventLog
from wafer_space_bench.fork import Log, ensure_tags, plan_matches, retry_rate_limited
from wafer_space_bench.git import Git, GitError
from wafer_space_bench.github import GhCli, GitHub, GitHubError, NotFound
from wafer_space_bench.layout import about_repo, apply_layout, push_about
from wafer_space_bench.naming import classify_branch
from wafer_space_bench.scan import RepoPlan, read_plans

STRATEGIES = ("fork", "copy", "restore")
INDEX_REPO = "wafer-space-bench/wafer-space-bench.github.io"
UPSTREAM = "upstream/"
PROGRESS_EVERY = 25


@dataclass(frozen=True)
class MirrorPlan:
    set: dict[str, str]  # bench branch (upstream/<b>) -> sha to create or force-update
    delete: list[str]  # bench branches (upstream/<b>) upstream no longer has


def plan_mirror(upstream: dict[str, str], current: dict[str, str]) -> MirrorPlan:
    """What makes the bench repo's `upstream/*` equal `upstream` (branch -> sha). Only the
    `upstream/*` entries of `current` (the bench repo's branches) are considered, and only
    `upstream/*` names are emitted; unchanged shas are omitted."""
    want = {UPSTREAM + b: sha for b, sha in upstream.items()}
    have = {b: sha for b, sha in current.items() if b.startswith(UPSTREAM)}
    return MirrorPlan(
        set={b: sha for b, sha in sorted(want.items()) if have.get(b) != sha},
        delete=sorted(set(have) - set(want)),
    )


def _upstream_url(plan: RepoPlan) -> str:
    if plan.source.host == "github.com":
        return f"https://github.com/{plan.current_name or plan.source.full_name}"
    return plan.source.url


def _fetch_mirror(plan: RepoPlan, cfg: LocalConfig) -> Git:
    """The copy's bare mirror with its heads exactly upstream's (fetched with prune, no tags).
    A failed fetch raises: a stale mirror must never be pushed as the current upstream."""
    url = resolve_clone_url(_upstream_url(plan), cfg)
    dest = local_clone_path(cfg, plan.target)
    if not dest.exists():
        return Git.clone_bare(url, dest)
    git = Git(dest)
    git.fetch(url, ["+refs/heads/*:refs/heads/*"], prune=True, no_tags=True)
    return git


def _upstream_branches(
    plan: RepoPlan, gh: GitHub, cfg: LocalConfig, log: Log
) -> tuple[dict[str, str], Git | None] | None:
    """(upstream's branches, the copy's mirror or None for a fork); None if upstream is gone
    or lists no branches (never mirrored as "delete everything")."""
    target = plan.target
    if plan.source.host == "github.com":
        try:
            gh.get_repo(plan.current_name)
        except NotFound:
            log("upstream-missing", target=target, source=plan.current_name)
            return None
    if plan.strategy == "fork":
        branches, mirror = gh.list_branches(plan.current_name), None
    else:
        mirror = _fetch_mirror(plan, cfg)
        branches = {h: mirror.rev_parse(f"refs/heads/{h}") for h in mirror.heads()}
    if not branches:
        log("upstream-empty", target=target, source=plan.current_name or plan.source.full_name)
        return None
    return branches, mirror


def _sync(
    plan: RepoPlan,
    tapeouts: dict[str, list[Tapeout]],
    gh: GitHub,
    cfg: LocalConfig,
    org: str,
    *,
    remote_fmt: str,
    now: str,
    dry_run: bool,
    log: Log,
    sleep: Callable[[float], None],
) -> str:
    target = plan.target
    try:
        laid_out = gh.get_repo(target).default_branch == "about"
    except NotFound:
        return "pending"
    if not laid_out:
        if dry_run:
            return "dry-run: needs layout"
        status = apply_layout(
            plan, tapeouts, gh, cfg, org, remote_fmt=remote_fmt, now=now, log=log, sleep=sleep
        )
        return "laid-out" if status.startswith("ok:") else status
    if plan.strategy == "restore":
        return "skipped: restore"

    got = _upstream_branches(plan, gh, cfg, log)
    if got is None:
        return "upstream-missing"
    upstream, mirror = got
    current = gh.list_branches(target)
    mp = plan_mirror(upstream, current)
    added = sum(b not in current for b in mp.set)
    counts = f"+{added} ~{len(mp.set) - added} -{len(mp.delete)}"
    if dry_run:
        log("mirror-plan", target=target, add=added, update=len(mp.set) - added, delete=len(mp.delete))
        return f"dry-run: {counts}"

    wrote = False

    def write(fn: Callable[[], object], **context) -> None:
        nonlocal wrote
        retry_rate_limited(fn, sleep=sleep, log=log, target=target, **context)
        wrote = True

    remote = remote_fmt.format(full=target)
    if mirror is None:  # fork: the commits are in the network already
        for b in mp.delete:
            write(lambda b=b: gh.delete_ref(target, f"heads/{b}"), ref=b)
        for b, sha in mp.set.items():
            write(lambda b=b, sha=sha: gh.set_ref(target, f"heads/{b}", sha), ref=b)
    else:  # copy: push the objects; deletes in their own push, first
        if mp.delete:
            mirror.push(remote, [f":refs/heads/{b}" for b in mp.delete])
            wrote = True
        if mp.set:
            mirror.push(remote, [f"+{sha}:refs/heads/{b}" for b, sha in mp.set.items()])
            wrote = True
    if mp.set or mp.delete:
        log("mirror", target=target, set=sorted(mp.set), delete=mp.delete)
    branches = {b: sha for b, sha in current.items() if b not in mp.delete} | mp.set

    tos = tapeouts.get(plan.source.key, [])
    created = 0
    if tos:
        if plan.strategy == "fork":  # the same message as `wsbench fork` gives
            source, why = _upstream_url(plan), None
        else:  # the same message as `wsbench copy` gives (copy.py)
            source, why = plan.source.url, copy_why(plan)
        statuses = ensure_tags(gh, target, tos, source, log, sleep=sleep, why=why)
        created = sum(s == "created" for s in statuses.values())
        wrote = wrote or created > 0
        for t in tos:
            if statuses.get(t.tag) == "missing-commit":
                continue
            sha = gh.get_tag(target, t.tag)
            if sha is None:
                continue
            branch = t.branch
            if branch not in branches:
                write(lambda branch=branch, sha=sha: gh.set_ref(target, f"heads/{branch}", sha), ref=branch)
                branches[branch] = sha
            elif branches[branch] != sha:  # tapeout branches are frozen: never moved
                log(
                    "tapeout-mismatch", target=target, branch=branch, branch_sha=branches[branch], tag_sha=sha
                )

    # The same branch set layout writes: about plus the bench namespaces.
    names = {b for b in branches if classify_branch(b) in ("upstream", "tapeout", "dev")} | {"about"}
    git = about_repo(cfg, target, remote, "about" in current)
    about = push_about(git, remote, plan, tos, names, now)
    if not wrote and not about:
        return "unchanged"
    return f"ok: {counts}" + (", about" if about else "") + (f", {created} tags" if created else "")


def sync_repo(
    plan: RepoPlan,
    tapeouts: dict[str, list[Tapeout]],
    gh: GitHub,
    cfg: LocalConfig,
    org: str,
    *,
    remote_fmt: str,
    now: str,
    dry_run: bool = False,
    log: Log,
    sleep: Callable[[float], None] = time.sleep,
) -> str:
    """Sync bench repo `plan.target` (in `org`) with its upstream; `tapeouts` is keyed by
    RepoRef.key and `now` is the ISO time for bench.yaml. Returns "pending", "laid-out"
    (or the layout's own non-ok status), "upstream-missing", "skipped: restore",
    "unchanged", "ok: +A ~U -D[, about][, N tags]" (upstream/* branches added, updated,
    deleted), "dry-run: +A ~U -D", "dry-run: needs layout" or "error: ..."."""
    if plan.target.split("/", 1)[0].lower() != org.lower():
        raise ValueError(f"{plan.target} is not in {org}")
    if plan.strategy not in STRATEGIES:
        raise ValueError(f"sync handles fork/copy/restore plans, not {plan.strategy!r}")
    try:
        return _sync(
            plan,
            tapeouts,
            gh,
            cfg,
            org,
            remote_fmt=remote_fmt,
            now=now,
            dry_run=dry_run,
            log=log,
            sleep=sleep,
        )
    except (GitError, GitHubError) as e:
        return f"error: {e}"


def run_sync(
    plans: list[RepoPlan],
    tapeouts: dict[str, list[Tapeout]],
    gh: GitHub,
    cfg: LocalConfig,
    org: str,
    *,
    only: list[str] | None = None,
    dry_run: bool = False,
    remote_fmt: str,
    now: str,
    log: Log,
    index_repo: str = INDEX_REPO,
    delay: float = 2,
    sleep: Callable[[float], None] = time.sleep,
    shard: tuple[int, int] | None = None,
) -> int:
    """sync_repo every fork/copy/restore plan (only those `only` names, if given), logging one
    `sync` event each and sleeping `delay` after each repo that wrote. If any repo changed
    (and this is not a dry run), dispatch `bench-updated` to `index_repo` once at the end.
    `shard` (K, N) keeps only the plans at 0-based positions i with i % N == K - 1 in the
    sorted, `only`-filtered list, so shards 1..N partition the plans.
    Returns the number of repos that ended in an error or a layout conflict."""
    todo = sorted((p for p in plans if p.strategy in STRATEGIES), key=lambda p: (p.source.key, p.target))
    if only:
        names = {o.lower() for o in only}
        todo = [p for p in todo if plan_matches(p, names)]
    if shard is not None:
        k, n = _check_shard(*shard)
        todo = [p for i, p in enumerate(todo) if i % n == k - 1]
    outside = [p.target for p in todo if p.target.split("/", 1)[0].lower() != org.lower()]
    if outside:  # checked before any repo is touched
        raise ValueError(f"not in {org}: {', '.join(outside)}")
    failed, changed = 0, False
    for i, p in enumerate(todo, 1):
        try:
            status = sync_repo(
                p,
                tapeouts,
                gh,
                cfg,
                org,
                remote_fmt=remote_fmt,
                now=now,
                dry_run=dry_run,
                log=log,
                sleep=sleep,
            )
        except (GitError, GitHubError) as e:  # sync_repo returns these; belt and braces
            status = f"error: {e}"
        except Exception as e:  # noqa: BLE001 -- a bug hit by one repo must not stop the others
            status = f"error: {type(e).__name__}: {e}"
        log("sync", n=f"{i}/{len(todo)}", target=p.target, status=status)
        ok = status == "laid-out" or status.startswith("ok:")
        changed = changed or ok
        failed += status.startswith(("error:", "conflict:"))
        if i % PROGRESS_EVERY == 0 or i == len(todo):
            print(f"synced {i}/{len(todo)}", flush=True)
        if (ok or status.startswith("error:")) and i < len(todo):
            sleep(delay)
    if changed and not dry_run:
        try:
            retry_rate_limited(
                lambda: gh.dispatch(index_repo, "bench-updated"), sleep=sleep, log=log, target=index_repo
            )
            log("dispatch", target=index_repo, event_type="bench-updated", status="ok")
        except GitHubError as e:  # the index also rebuilds on its own schedule
            log("dispatch", target=index_repo, event_type="bench-updated", status=f"error: {e}")
            print(f"dispatch to {index_repo} failed: {e}", flush=True)
    return failed


def _check_shard(k: int, n: int) -> tuple[int, int]:
    if not 1 <= k <= n:
        raise ValueError(f"shard {k}/{n}: need 1 <= K <= N")
    return k, n


def parse_shard(text: str) -> tuple[int, int]:
    """'K/N' (1-based K) -> (K, N); argparse type for --shard."""
    k, sep, n = text.partition("/")
    try:
        if not sep:
            raise ValueError(text)
        return _check_shard(int(k), int(n))
    except ValueError as e:
        raise argparse.ArgumentTypeError(f"--shard wants K/N with 1 <= K <= N, not {text!r}") from e


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _sync_cmd(args) -> int:
    failed = run_sync(
        read_plans(args.plans),
        tapeouts_by_repo(load_designs(args.catalog)),
        GhCli(),
        load_config(args.config),
        args.org,
        only=args.only,
        dry_run=args.dry_run,
        remote_fmt=args.remote_fmt,
        now=_now(),
        log=EventLog(args.log).write,
        delay=args.delay,
        shard=args.shard,
    )
    return 1 if failed else 0


def add_subcommand(subparsers) -> None:
    p = subparsers.add_parser("sync", help="mirror upstream branches, add new tapeouts, refresh about")
    p.add_argument(
        "--only", nargs="+", metavar="NAME", help="only these repos (bench, current or source name)"
    )
    p.add_argument("--dry-run", action="store_true", help="only log what would change; no writes")
    p.add_argument("--delay", type=float, default=2, help="seconds to wait after each repo that wrote")
    p.add_argument(
        "--shard",
        type=parse_shard,
        metavar="K/N",
        help="only the K-th of N interleaved slices of the plans (1-based K)",
    )
    p.set_defaults(handler=_sync_cmd)
