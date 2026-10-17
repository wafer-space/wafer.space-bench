"""Lay out a bench repo's branches: `about` (default), `upstream/<b>`, `tapeout/<tag>`.

A bench repo starts as a fork or pushed copy with upstream's own branch names. Laying it
out is done without cloning it (ruling R10): `git ls-remote` lists its refs, the refs API
creates `upstream/*` and `tapeout/*` branches from commits GitHub already has, and only
the small orphan `about` commit is built in a local work repo and pushed.

Order matters for crash safety: `about` and every new branch exist before the default
branch is switched, and the original branches are deleted only after the switch. A
re-run of a half-done repo picks up where it stopped. Once the default branch is `about`
the repo counts as laid out, and conflict checks no longer apply.
"""

import time
from collections.abc import Callable
from dataclasses import dataclass

from wafer_space_bench.about import ABOUT_REF, about_data, render_readme, write_about_commit
from wafer_space_bench.catalog import Tapeout, load_designs, tapeouts_by_repo
from wafer_space_bench.config import LocalConfig, load_config
from wafer_space_bench.events import EventLog
from wafer_space_bench.fork import Log, retry_rate_limited, select_only
from wafer_space_bench.git import Git, GitError
from wafer_space_bench.github import GhCli, GitHub, GitHubError, NotFound
from wafer_space_bench.naming import COMMIT_SOURCE, classify_branch
from wafer_space_bench.scan import RepoPlan, read_plans

STRATEGIES = ("fork", "copy", "restore")


@dataclass(frozen=True)
class LayoutPlan:
    upstream: dict[str, str]  # original branch -> upstream/<branch>
    tapeout: list[str]  # tapeout tags (<org>/<run>/<slot>); each gets branch tapeout/<tag>
    delete: list[str]  # original branches to delete once the default branch is `about`
    conflicts: list[str]  # names that block the layout (sha-blind; see apply_layout)


def is_tapeout_tag(tag: str) -> bool:
    parts = tag.split("/")
    return len(parts) == 3 and all(parts) and parts[0] in COMMIT_SOURCE


def plan_layout(heads: list[str], tags: list[str], *, upstream_dev: bool = False) -> LayoutPlan:
    """The layout for a repo with these branch and tag names.

    `upstream_dev`: the repo has never been laid out, so a `dev/*` branch is upstream's own
    (layout never creates one) and is copied to `upstream/dev/*` like any other. Once laid
    out, `dev/*` is ours and never renamed or deleted.

    Conflicts: an existing `about` (or, unless `upstream_dev`, `dev/*`) branch, and any name
    `x` alongside `x/...` among the existing and the new branch names (git cannot hold
    both). A clash is reported by the existing heads in it, or else by the branches the new
    names come from."""
    upstream_kinds = ("original", "dev") if upstream_dev else ("original",)
    ours = ("about",) if upstream_dev else ("about", "dev")
    originals = sorted(h for h in heads if classify_branch(h) in upstream_kinds)
    upstream = {b: f"upstream/{b}" for b in originals}
    tapeout = sorted(t for t in tags if is_tapeout_tag(t))
    conflicts = {h for h in heads if classify_branch(h) in ours}
    source = {new: b for b, new in upstream.items()} | {f"tapeout/{t}": f"tapeout/{t}" for t in tapeout}
    names = set(heads) | set(source) | {"about"}
    for x in names:
        for y in names:
            if y.startswith(x + "/"):
                existing = {x, y} & set(heads)
                conflicts |= existing or {source[n] for n in (x, y) if n in source}
    return LayoutPlan(upstream, tapeout, originals, sorted(conflicts))


def _clashes(name: str, names: set[str]) -> bool:
    """True if `name` and some other name in `names` are `x` and `x/...` (git cannot hold both)."""
    return any(n != name and (n.startswith(name + "/") or name.startswith(n + "/")) for n in names)


def _heads(refs: dict[str, str]) -> dict[str, str]:
    return {r.removeprefix("refs/heads/"): s for r, s in refs.items() if r.startswith("refs/heads/")}


def _work_repo(cfg: LocalConfig, target: str) -> Git:
    path = cfg.work_dir / "about" / f"{target.split('/', 1)[1]}.git"
    return Git(path) if path.exists() else Git.init_bare(path)


def _mirror_about(git: Git, remote: str, remote_has_about: bool) -> None:
    """Make the work repo's `about` match the remote's (absent if the remote has none)."""
    if remote_has_about:
        git.fetch(remote, [f"+{ABOUT_REF}:{ABOUT_REF}"], no_tags=True, depth=1)
    elif git.rev_parse(ABOUT_REF) is not None:
        git.delete_ref(ABOUT_REF)


def about_repo(cfg: LocalConfig, target: str, remote: str, remote_has_about: bool) -> Git:
    """The local work repo for bench repo `target`'s `about` branch, with `about` matching
    the remote's (absent if the remote has none)."""
    git = _work_repo(cfg, target)
    _mirror_about(git, remote, remote_has_about)
    return git


def push_about(
    git: Git, remote: str, plan: RepoPlan, tapeouts: list[Tapeout], branches: list[str] | set[str], now: str
) -> bool:
    """Regenerate bench.yaml and README.md for `plan.target` with these branch names, on top
    of the `about` that `about_repo` mirrored. Commits and pushes only when something other
    than `generated` changed (write_about_commit); True if a commit was pushed."""
    data = about_data(plan, tapeouts, sorted(branches), now)
    sha = write_about_commit(git, data, render_readme(data, plan.target))
    if sha is None:
        return False
    git.push(remote, [f"{sha}:{ABOUT_REF}"])
    return True


def _sha_conflicts(
    lp: LayoutPlan, heads: dict[str, str], tags: dict[str, str], ours_about: bool
) -> list[str]:
    """plan_layout's conflicts, refined with shas: `about` holding bench.yaml, and
    `upstream/X` or `tapeout/T` matching X or tag T, are left by an earlier partial run."""
    out = set(lp.conflicts)
    if ours_about:
        out.discard("about")
    for h, sha in heads.items():
        kind = classify_branch(h)
        source = {"upstream": heads, "tapeout": tags}.get(kind)
        if source is not None and source.get(h.split("/", 1)[1]) != sha:
            out.add(h)
    return sorted(out)


def _layout(
    plan: RepoPlan,
    tapeouts: list[Tapeout],
    gh: GitHub,
    cfg: LocalConfig,
    *,
    remote_fmt: str,
    now: str,
    log: Log,
    sleep: Callable[[float], None],
    dry_run: bool,
) -> tuple[str, bool]:
    """(status, whether anything on GitHub was written)."""
    target = plan.target
    try:
        laid_out = gh.get_repo(target).default_branch == "about"
    except NotFound:
        return "pending", False
    remote = remote_fmt.format(full=target)
    refs = Git.ls_remote(remote)
    heads = _heads(refs)
    tags = {r.removeprefix("refs/tags/"): s for r, s in refs.items() if r.startswith("refs/tags/")}
    lp = plan_layout(list(heads), list(tags), upstream_dev=not laid_out)

    git = about_repo(cfg, target, remote, "about" in heads)
    if not laid_out:
        conflicts = _sha_conflicts(lp, heads, tags, git.show(ABOUT_REF, "bench.yaml") is not None)
        if conflicts:
            return "conflict: " + ", ".join(conflicts), False
        if not any(classify_branch(h) in ("original", "upstream") for h in heads):
            return "empty", False  # nothing pushed yet: laying out would hide it from copy

    mismatches = 0
    upstream = dict(lp.upstream)
    if laid_out:  # conflicts were not checked: skip new originals that cannot be copied
        names = set(heads) | set(upstream.values()) | {f"tapeout/{t}" for t in lp.tapeout}
        for b, new in lp.upstream.items():
            if _clashes(new, names) or _clashes(b, names):
                log("branch-conflict", target=target, branch=b, new=new)
                del upstream[b]
                mismatches += 1
    branches = {h for h in heads if classify_branch(h) in ("upstream", "tapeout", "dev")}
    branches |= {"about", *upstream.values(), *(f"tapeout/{t}" for t in lp.tapeout)}
    counts = (
        f"{sum(classify_branch(b) == 'upstream' for b in branches)} upstream, "
        f"{sum(classify_branch(b) == 'tapeout' for b in branches)} tapeout"
    )
    if dry_run:
        return f"dry-run: {counts}", False

    changed = False

    def write(fn: Callable[[], object], **context) -> None:
        nonlocal changed
        retry_rate_limited(fn, sleep=sleep, log=log, target=target, **context)
        changed = True

    if push_about(git, remote, plan, tapeouts, branches, now):
        changed = True
    moves = []
    for b, new in upstream.items():
        if new not in heads:
            write(lambda new=new, b=b: gh.set_ref(target, f"heads/{new}", heads[b]), ref=new)
        elif heads[new] != heads[b]:  # only possible once laid out (else a conflict): leave both
            log("upstream-mismatch", target=target, branch=b, branch_sha=heads[b], upstream_sha=heads[new])
            mismatches += 1
            continue
        moves.append(b)
    for t in lp.tapeout:
        branch, commit = f"tapeout/{t}", tags[t]
        if branch not in heads:
            write(
                lambda branch=branch, commit=commit: gh.set_ref(target, f"heads/{branch}", commit), ref=branch
            )
        elif heads[branch] != commit:  # tapeout branches are frozen: never moved
            log("tapeout-mismatch", target=target, branch=branch, branch_sha=heads[branch], tag_sha=commit)
            mismatches += 1
    if not laid_out:
        write(lambda: gh.set_default_branch(target, "about"), ref="about")
    # Only after the default branch is `about`, and only originals that have not moved since
    # the listing and whose copy is verified on the remote; a re-run handles the rest.
    now_heads = _heads(Git.ls_remote(remote)) if moves else {}
    for b in moves:
        if now_heads.get(b) == heads[b] == now_heads.get(upstream[b]):
            write(lambda b=b: gh.delete_ref(target, f"heads/{b}"), ref=b)
        else:
            log("delete-skipped", target=target, branch=b)
    return f"ok: {counts}" + (f", {mismatches} mismatch" if mismatches else ""), changed


def _apply(
    plan: RepoPlan,
    tapeouts: dict[str, list[Tapeout]],
    gh: GitHub,
    cfg: LocalConfig,
    *,
    remote_fmt: str,
    now: str,
    log: Log,
    sleep: Callable[[float], None],
    dry_run: bool,
) -> tuple[str, bool]:
    try:
        status, changed = _layout(
            plan,
            tapeouts.get(plan.source.key, []),
            gh,
            cfg,
            remote_fmt=remote_fmt,
            now=now,
            log=log,
            sleep=sleep,
            dry_run=dry_run,
        )
    except (GitError, GitHubError) as e:
        status, changed = f"error: {e}", True
    log("layout", target=plan.target, status=status)
    return status, changed


def apply_layout(
    plan: RepoPlan,
    tapeouts: dict[str, list[Tapeout]],
    gh: GitHub,
    cfg: LocalConfig,
    org: str,
    *,
    remote_fmt: str,
    now: str,
    log: Log,
    sleep: Callable[[float], None] = time.sleep,
    dry_run: bool = False,
) -> str:
    """Lay out bench repo `plan.target` (in `org`); `tapeouts` is keyed by RepoRef.key and
    `now` is the ISO time for bench.yaml. Logs one `layout` event and returns its status:
    "pending" (not in the org yet), "empty" (no branches to lay out yet), "conflict: <names>",
    "ok: N upstream, M tapeout[, K mismatch]" (the counts of those branches in the final
    layout; K counts branches logged and left for a human), "dry-run: ..." or "error: ...".
    Idempotent: a laid-out repo with nothing new gets no writes."""
    if plan.target.split("/", 1)[0].lower() != org.lower():
        raise ValueError(f"{plan.target} is not in {org}")
    return _apply(
        plan, tapeouts, gh, cfg, remote_fmt=remote_fmt, now=now, log=log, sleep=sleep, dry_run=dry_run
    )[0]


def run_layouts(
    plans: list[RepoPlan],
    tapeouts: dict[str, list[Tapeout]],
    gh: GitHub,
    cfg: LocalConfig,
    org: str,
    *,
    remote_fmt: str,
    log: Log,
    now: Callable[[], str],
    dry_run: bool = False,
    delay: float = 5,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Lay out every fork/copy/restore plan, sleeping `delay` after each repo that changed.
    Returns the number of repos that ended in an error, a conflict or "empty"."""
    todo = [p for p in plans if p.strategy in STRATEGIES]
    outside = [p.target for p in todo if p.target.split("/", 1)[0].lower() != org.lower()]
    if outside:  # checked before any repo is touched
        raise ValueError(f"not in {org}: {', '.join(outside)}")
    failed = 0
    for i, p in enumerate(todo, 1):
        status, changed = _apply(
            p, tapeouts, gh, cfg, remote_fmt=remote_fmt, now=now(), log=log, sleep=sleep, dry_run=dry_run
        )
        print(f"{i}/{len(todo)} {p.target}: {status}", flush=True)
        failed += status == "empty" or status.startswith(("error:", "conflict:"))
        if changed and i < len(todo):
            sleep(delay)
    return failed


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _layout_cmd(args) -> int:
    plans = select_only(read_plans(args.plans), args.only, STRATEGIES, "layout")
    if plans is None:
        return 2
    failed = run_layouts(
        plans,
        tapeouts_by_repo(load_designs(args.catalog)),
        GhCli(),
        load_config(args.config),
        args.org,
        remote_fmt=args.remote_fmt,
        log=EventLog(args.log).write,
        now=_now,
        dry_run=args.dry_run,
        delay=args.delay,
    )
    return 1 if failed else 0


def add_subcommand(subparsers) -> None:
    p = subparsers.add_parser("layout", help="lay out bench repos: about, upstream/*, tapeout/* branches")
    p.add_argument(
        "--only", nargs="+", metavar="NAME", help="only these repos (bench, current or source name)"
    )
    p.add_argument("--dry-run", action="store_true", help="only check and log the plan; no writes")
    p.add_argument("--delay", type=float, default=5, help="seconds to wait after each repo that changed")
    p.set_defaults(handler=_layout_cmd)
