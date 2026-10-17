"""Fork upstream repos into the bench org and tag their taped-out commits."""

import sys
import time
from collections.abc import Callable
from typing import Any, Literal

from wafer_space_bench.catalog import Tapeout, load_designs, tapeouts_by_repo
from wafer_space_bench.events import EventLog
from wafer_space_bench.github import GhCli, GitHub, GitHubError, NotFound, RateLimited
from wafer_space_bench.naming import COMMIT_SOURCE
from wafer_space_bench.scan import RepoPlan, read_plans

Log = Callable[..., None]  # log(event: str, **fields)
TagStatus = Literal["created", "exists", "missing-commit"]


def tag_message(t: Tapeout, source_url: str, why: str | None = None) -> str:
    suffix = f" ({why})" if why else ""
    return f"Taped out as {t.tag}\n\nSource: {source_url}{suffix}\nCommit source: {COMMIT_SOURCE[t.org]}\n"


def retry_rate_limited(
    fn: Callable[[], Any],
    *,
    sleep: Callable[[float], None],
    log: Log,
    base_wait: float = 60,
    max_wait: float = 960,
    **context,
) -> Any:
    """Call `fn`; on RateLimited log `backoff`, sleep, double the wait (capped), retry."""
    wait = base_wait
    while True:
        try:
            return fn()
        except RateLimited:
            log("backoff", wait_s=wait, **context)
            sleep(wait)
            wait = min(wait * 2, max_wait)


def _exists(gh: GitHub, full: str) -> bool:
    try:
        gh.get_repo(full)
    except NotFound:
        return False
    return True


def ensure_fork(
    gh: GitHub,
    plan: RepoPlan,
    org: str,
    *,
    sleep: Callable[[float], None],
    log: Log,
    base_wait: float = 60,
    max_wait: float = 960,
    ready_polls: int = 30,
) -> Literal["created", "exists"]:
    target = plan.target
    if _exists(gh, target):
        return "exists"
    name = target.split("/", 1)[1]
    got = retry_rate_limited(
        lambda: gh.fork(plan.current_name, org, name),
        sleep=sleep,
        log=log,
        base_wait=base_wait,
        max_wait=max_wait,
        source=plan.current_name,
    )
    if got.lower() != target.lower():
        raise GitHubError(f"fork network already has {got}")
    for i in range(ready_polls):  # forks are created asynchronously
        if gh.branches_visible(target):
            return "created"
        if i < ready_polls - 1:
            sleep(2)
    log("fork-not-ready", target=target)
    return "created"  # a re-run finishes the tags


def ensure_tags(
    gh: GitHub,
    repo_full: str,
    tapeouts: list[Tapeout],
    source_url: str,
    log: Log,
    *,
    sleep: Callable[[float], None] = time.sleep,
    why: str | None = None,
) -> dict[str, TagStatus]:
    """Create each missing tapeout tag through the API; `source_url` and `why` go into the
    tag message (`tag_message`). Existing tags are never changed."""
    out: dict[str, TagStatus] = {}
    for t in tapeouts:
        status: TagStatus
        if gh.get_tag(repo_full, t.tag) is not None:
            status = "exists"
        else:
            full = gh.resolve_commit(repo_full, t.commit) if t.commit else None
            if full is None:
                status = "missing-commit"
            else:
                message = tag_message(t, source_url, why)
                retry_rate_limited(
                    lambda t=t, full=full, message=message: gh.create_tag(repo_full, t.tag, full, message),
                    sleep=sleep,
                    log=log,
                    target=repo_full,
                    tag=t.tag,
                )
                status = "created"
        out[t.tag] = status
        log("tag", target=repo_full, tag=t.tag, commit=t.commit, status=status)
    return out


def run_fork(
    plans: list[RepoPlan],
    tapeouts: dict[str, list[Tapeout]],
    gh: GitHub,
    org: str,
    *,
    delay: float = 20,
    sleep: Callable[[float], None] = time.sleep,
    log: Log,
) -> int:
    """Fork and tag every strategy=fork plan. Returns the number of failed repos."""
    todo = [p for p in plans if p.strategy == "fork"]
    failed = 0
    for i, p in enumerate(todo, 1):
        n = f"{i}/{len(todo)}"
        fields = {"n": n, "source": p.current_name, "target": p.target}
        not_ready = False

        def watch(event, _log=log, **kw):
            nonlocal not_ready
            not_ready = not_ready or event == "fork-not-ready"
            _log(event, **kw)

        status = None
        try:
            status = ensure_fork(gh, p, org, sleep=sleep, log=watch)
            if not_ready:
                failed += 1
                log("fork", status="created (not ready; re-run to tag)", **fields)
            else:
                url = f"https://github.com/{p.current_name}"
                ensure_tags(gh, p.target, tapeouts.get(p.source.key, []), url, log, sleep=sleep)
                log("fork", status=status, **fields)
        except GitHubError as e:
            failed += 1
            log("fork", status=f"error: {e}", **fields)
        if status == "created":
            sleep(delay)
    return failed


def plan_matches(p: RepoPlan, only: set[str]) -> bool:
    """True if `p` is named in `only` (lowercased bench, current or source full name; a
    `none` plan has only its source key `<shuttle>/<code>`)."""
    names = {p.target.partition("/")[2].lower(), p.current_name.lower(), p.source.key} - {""}
    return bool(names & only)


def select_only(
    plans: list[RepoPlan], only: list[str] | None, strategies: tuple[str, ...], command: str
) -> list[RepoPlan] | None:
    """The plans named by `only` (all of them if `only` is empty). None, after a warning on
    stderr, if `only` names none of the plans with `strategies` (the command's own): the
    caller exits 2 rather than silently doing nothing."""
    if not only:
        return plans
    names = {o.lower() for o in only}
    picked = [p for p in plans if plan_matches(p, names)]
    if not any(p.strategy in strategies for p in picked):
        print(
            f"wsbench {command}: warning: --only matched no {'/'.join(strategies)} plan: {' '.join(only)}",
            file=sys.stderr,
        )
        return None
    return picked


def _fork(args) -> int:
    plans = select_only(read_plans(args.plans), args.only, ("fork",), "fork")
    if plans is None:
        return 2
    tapeouts = tapeouts_by_repo(load_designs(args.catalog))
    log = EventLog(args.log).write
    failed = run_fork(plans, tapeouts, GhCli(), args.org, delay=args.delay, log=log)
    return 1 if failed else 0


def add_subcommand(subparsers) -> None:
    p = subparsers.add_parser("fork", help="fork strategy=fork repos into the bench org and tag tapeouts")
    p.add_argument(
        "--only", nargs="+", metavar="NAME", help="only these repos (bench, current or source name)"
    )
    p.add_argument("--delay", type=float, default=20, help="seconds to wait after each created fork")
    p.set_defaults(handler=_fork)
