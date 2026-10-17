"""Bring every bench repo to the archival-mirror settings (`wsbench settings`).

The bench repos are archival mirrors, not places for collaboration (spec: Repository
settings). Per repo, `apply_settings` reads the current state, compares it with
`desired_settings` and writes only what differs, so a second run makes no writes:
- Issues, Projects, Wiki and Discussions off; pull requests off only where the API
  reports `has_pull_requests` (otherwise left alone, logged once per run);
- description and homepage (the repo's row in the index);
- topics: the org, the process, the strategy, the tapeout orgs and runs;
- Actions disabled, Dependabot alerts and security updates off;
- rulesets `bench-frozen-branches` and `bench-frozen-tags`: no update, deletion or
  force-push of `tapeout/**` branches and tapeout tags, for anyone (no bypass actors).
  Creating them stays allowed, so `fork`, `copy`, `layout` and `sync` keep working.
"""

import re
import time
from collections.abc import Callable
from dataclasses import dataclass

from wafer_space_bench.catalog import Tapeout, load_designs, tapeouts_by_repo
from wafer_space_bench.events import EventLog
from wafer_space_bench.fork import Log, plan_matches, retry_rate_limited, select_only
from wafer_space_bench.github import GhCli, GitHub, GitHubError, NotFound
from wafer_space_bench.scan import RepoPlan, read_plans
from wafer_space_bench.sync import _check_shard, parse_shard, shard_of

STRATEGIES = ("fork", "copy", "restore")
INDEX_URL = "https://wafer-space-bench.github.io"
MAX_DESCRIPTION = 350
MAX_TOPICS = 20  # GitHub's limit
BASE_TOPICS = ("wafer-space-bench", "gf180mcu", "open-silicon")
FROZEN_RULES = ("deletion", "non_fast_forward", "update")
FROZEN_TAG_PREFIXES = ("wafer.space", "google", "tinytapeout")
PROGRESS_EVERY = 25


@dataclass
class DesiredSettings:
    repo: dict  # REPO_SETTINGS fields (github.py) -> value
    topics: list[str]
    actions_enabled: bool
    vulnerability_alerts: bool
    ruleset: dict  # ruleset name -> ruleset (the REST body)


def topic_for(name: str) -> str:
    """A GitHub topic for `name`: lowercase, other than [a-z0-9-] becomes `-`, runs of `-`
    collapse, no `-` at either end, at most 50 chars (`wafer.space` -> `wafer-space`)."""
    t = re.sub(r"-+", "-", re.sub(r"[^a-z0-9-]", "-", name.lower())).strip("-")
    return t[:50].rstrip("-")


def _ruleset(name: str, target: str, include: list[str]) -> dict:
    return {
        "name": name,
        "target": target,
        "enforcement": "active",
        "conditions": {"ref_name": {"include": include, "exclude": []}},
        "rules": [{"type": t} for t in FROZEN_RULES],
        "bypass_actors": [],
    }


FROZEN_RULESETS = {
    "bench-frozen-branches": _ruleset("bench-frozen-branches", "branch", ["refs/heads/tapeout/**"]),
    "bench-frozen-tags": _ruleset(
        "bench-frozen-tags", "tag", [f"refs/tags/{p}/**" for p in FROZEN_TAG_PREFIXES]
    ),
}


def _upstream_url(plan: RepoPlan) -> str:
    """The upstream as it is now (a renamed GitHub repo by its current name)."""
    if plan.source.host == "github.com":
        return f"https://github.com/{plan.current_name or plan.source.full_name}"
    return plan.source.url


def _description(plan: RepoPlan, tos: list[Tapeout]) -> str:
    runs = sorted({f"{t.org}/{t.run}" for t in tos})
    head = f"Benchmark copy of {_upstream_url(plan)}"
    tail = " (upstream deleted)" if plan.strategy == "restore" else ""
    if not tos:
        return (head + tail)[:MAX_DESCRIPTION]
    n = f"{len(tos)} tapeout{'s' if len(tos) != 1 else ''}"
    text = f"{head}: {n} on {', '.join(runs)}{tail}"
    if len(text) > MAX_DESCRIPTION:
        text = f"{head}: {n} on {len(runs)} runs{tail}"
    return text[:MAX_DESCRIPTION]


def desired_settings(
    plan: RepoPlan, tapeouts: dict[str, list[Tapeout]], index_url: str = INDEX_URL
) -> DesiredSettings:
    """What bench repo `plan.target` should look like; pure (spec: Repository settings)."""
    tos = tapeouts.get(plan.source.key, [])
    name = plan.target.partition("/")[2]
    topics: list[str] = []
    for t in [
        *BASE_TOPICS,
        plan.strategy,
        *sorted({topic_for(t.org) for t in tos}),
        *sorted({topic_for(t.run) for t in tos}),
    ]:
        if t and t not in topics:
            topics.append(t)
    return DesiredSettings(
        repo={
            "description": _description(plan, tos),
            "homepage": f"{index_url}/#{name}",
            "has_issues": False,
            "has_projects": False,
            "has_wiki": False,
            "has_discussions": False,
            "has_pull_requests": False,
        },
        topics=topics[:MAX_TOPICS],
        actions_enabled=False,
        vulnerability_alerts=False,
        ruleset={k: dict(v) for k, v in FROZEN_RULESETS.items()},
    )


def read_settings(gh: GitHub, full: str) -> DesiredSettings:
    """The current settings of `full`, in the shape of DesiredSettings (only the bench
    rulesets, by name). Raises NotFound if the repo does not exist."""
    repo = gh.get_repo_settings(full)
    return DesiredSettings(
        repo=repo,
        topics=gh.get_topics(full),
        actions_enabled=gh.get_actions_enabled(full),
        vulnerability_alerts=gh.get_vulnerability_alerts(full),
        ruleset={r["name"]: r for r in gh.list_rulesets(full) if r.get("name") in FROZEN_RULESETS},
    )


def _ruleset_key(r: dict) -> tuple:
    """The parts of a ruleset that matter, as GitHub returns or as we send it."""
    ref = (r.get("conditions") or {}).get("ref_name") or {}
    return (
        r.get("target"),
        r.get("enforcement"),
        sorted(ref.get("include") or []),
        sorted(ref.get("exclude") or []),
        sorted(x.get("type") for x in r.get("rules") or []),
        r.get("bypass_actors") or [],
    )


def _same(have, want) -> bool:
    if isinstance(want, str):  # GitHub reports an empty description or homepage as null
        return (have or "") == want
    return have == want


def diff_settings(current: DesiredSettings, desired: DesiredSettings) -> dict[str, tuple]:
    """{field: (current, desired)} for each field that differs. A `has_pull_requests` the
    API does not report (None) is not a difference: it cannot be set."""
    out: dict[str, tuple] = {}
    for k, want in desired.repo.items():
        have = current.repo.get(k)
        if k == "has_pull_requests" and have is None:
            continue
        if not _same(have, want):
            out[k] = (have, want)
    if sorted(current.topics) != sorted(desired.topics):
        out["topics"] = (current.topics, desired.topics)
    if current.actions_enabled != desired.actions_enabled:
        out["actions_enabled"] = (current.actions_enabled, desired.actions_enabled)
    if current.vulnerability_alerts != desired.vulnerability_alerts:
        out["vulnerability_alerts"] = (current.vulnerability_alerts, desired.vulnerability_alerts)
    for name, want in desired.ruleset.items():
        have = current.ruleset.get(name)
        if have is None or _ruleset_key(have) != _ruleset_key(want):
            out[f"ruleset:{name}"] = (have, want)
    return out


def _apply(
    plan: RepoPlan,
    tapeouts: dict[str, list[Tapeout]],
    gh: GitHub,
    *,
    dry_run: bool,
    sleep: Callable[[float], None],
    log: Log,
) -> str:
    target = plan.target
    try:
        current = read_settings(gh, target)
    except NotFound:
        return "pending"
    if current.repo.get("has_pull_requests") is None:
        log("pull-requests-unsupported", target=target)
    want = desired_settings(plan, tapeouts)
    diff = diff_settings(current, want)
    if not diff:
        return "unchanged"
    fields = ", ".join(diff)
    if dry_run:
        log("settings-plan", target=target, fields=list(diff))
        return f"dry-run: {fields}"

    def write(fn: Callable[[], object], **context) -> None:
        retry_rate_limited(fn, sleep=sleep, log=log, target=target, **context)

    repo_fields = {k: want.repo[k] for k in diff if k in want.repo}
    if repo_fields:
        write(lambda: gh.update_repo(target, repo_fields), setting="repo")
    if "topics" in diff:
        write(lambda: gh.set_topics(target, want.topics), setting="topics")
    if "actions_enabled" in diff:
        write(lambda: gh.set_actions_enabled(target, want.actions_enabled), setting="actions")
    if "vulnerability_alerts" in diff:
        write(
            lambda: gh.set_vulnerability_alerts(target, want.vulnerability_alerts),
            setting="vulnerability_alerts",
        )
    for name, rs in want.ruleset.items():
        if f"ruleset:{name}" in diff:
            write(lambda rs=rs: gh.put_ruleset(target, rs), setting=name)
    return f"ok: {fields}"


def apply_settings(
    plan: RepoPlan,
    tapeouts: dict[str, list[Tapeout]],
    gh: GitHub,
    org: str,
    *,
    dry_run: bool = False,
    sleep: Callable[[float], None] = time.sleep,
    log: Log,
) -> str:
    """Bring bench repo `plan.target` (in `org`) to `desired_settings`, writing only what
    differs. Returns "pending" (not in the org yet), "unchanged", "dry-run: <fields>",
    "ok: <fields>" or "error: ..."."""
    if plan.target.split("/", 1)[0].lower() != org.lower():
        raise ValueError(f"{plan.target} is not in {org}")
    try:
        return _apply(plan, tapeouts, gh, dry_run=dry_run, sleep=sleep, log=log)
    except GitHubError as e:
        return f"error: {e}"


def run_settings(
    plans: list[RepoPlan],
    tapeouts: dict[str, list[Tapeout]],
    gh: GitHub,
    org: str,
    *,
    only: list[str] | None = None,
    shard: tuple[int, int] | None = None,
    dry_run: bool = False,
    delay: float = 1,
    sleep: Callable[[float], None] = time.sleep,
    log: Log,
) -> int:
    """apply_settings to every fork/copy/restore plan (`none` plans have no repo), or only
    those `only` names, or only shard (K, N) by sync's stable `shard_of`. Logs one
    `settings` event per repo and sleeps `delay` after each repo that changed or failed.
    Returns the number of repos that ended in an error."""
    todo = sorted((p for p in plans if p.strategy in STRATEGIES), key=lambda p: (p.source.key, p.target))
    if only:
        names = {o.lower() for o in only}
        todo = [p for p in todo if plan_matches(p, names)]
    if shard is not None:
        k, n = _check_shard(*shard)
        todo = [p for p in todo if shard_of(p.target, n) == k]
    outside = [p.target for p in todo if p.target.split("/", 1)[0].lower() != org.lower()]
    if outside:  # checked before any repo is touched
        raise ValueError(f"not in {org}: {', '.join(outside)}")

    once: set[str] = set()

    def run_log(event: str, **fields) -> None:
        if event == "pull-requests-unsupported":  # the API's answer is the same for every repo
            if event in once:
                return
            once.add(event)
        log(event, **fields)

    failed = 0
    for i, p in enumerate(todo, 1):
        try:
            status = apply_settings(p, tapeouts, gh, org, dry_run=dry_run, sleep=sleep, log=run_log)
        except Exception as e:  # noqa: BLE001 -- a bug hit by one repo must not stop the others
            status = f"error: {type(e).__name__}: {e}"
        log("settings", n=f"{i}/{len(todo)}", target=p.target, status=status)
        failed += status.startswith("error:")
        if i % PROGRESS_EVERY == 0 or i == len(todo):
            print(f"settings {i}/{len(todo)}", flush=True)
        if status.startswith(("ok:", "error:")) and i < len(todo):
            sleep(delay)
    return failed


def _settings_cmd(args) -> int:
    plans = read_plans(args.plans)
    if select_only(plans, args.only, STRATEGIES, "settings") is None:  # checked before sharding
        return 2
    failed = run_settings(
        plans,
        tapeouts_by_repo(load_designs(args.catalog)),
        GhCli(),
        args.org,
        only=args.only,
        shard=args.shard,
        dry_run=args.dry_run,
        delay=args.delay,
        log=EventLog(args.log).write,
    )
    return 1 if failed else 0


def add_subcommand(subparsers) -> None:
    p = subparsers.add_parser(
        "settings", help="set repo settings: features off, description, topics, frozen rulesets"
    )
    p.add_argument(
        "--only", nargs="+", metavar="NAME", help="only these repos (bench, current or source name)"
    )
    p.add_argument(
        "--shard",
        type=parse_shard,
        metavar="K/N",
        help="only the K-th of N slices of the plans (the same slices as sync)",
    )
    p.add_argument("--dry-run", action="store_true", help="only log what would change; no writes")
    p.add_argument("--delay", type=float, default=1, help="seconds to wait after each repo that changed")
    p.set_defaults(handler=_settings_cmd)
