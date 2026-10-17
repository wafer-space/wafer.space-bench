"""Build the data and files for the public index site (https://wafer-space-bench.github.io)."""

import collections
import json
import shutil
from collections.abc import Callable
from pathlib import Path

import yaml

from wafer_space_bench.catalog import Tapeout, load_designs, tapeouts_by_repo
from wafer_space_bench.events import EventLog
from wafer_space_bench.github import GhCli, GitHub
from wafer_space_bench.scan import RepoPlan, read_plans

Log = Callable[..., None]  # log(event: str, **fields)


def _read_bench_yaml(gh: GitHub, full: str, log: Log) -> dict | None:
    """The parsed bench.yaml of a laid-out repo; None (with a log line) if it is unusable."""
    try:
        text = gh.get_file(full, "bench.yaml", "about")
    except (ValueError, TypeError, KeyError) as e:  # not UTF-8 or not base64, or not a file
        log("index-bad-yaml", repo=full, error=f"{type(e).__name__}: {e}"[:200])
        return None
    if text is None:
        return None
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        log("index-bad-yaml", repo=full, error=str(e).splitlines()[0] if str(e) else "yaml error")
        return None
    if not isinstance(data, dict) or "source" not in data:
        log("index-bad-yaml", repo=full, error="not a mapping with a source key")
        return None
    return data


def _runs(tags: list[tuple[str, str]]) -> list[str]:
    return sorted({f"{org}/{run}" for org, run in tags})


def _row(plan: RepoPlan, state: str) -> dict:
    name = plan.target.split("/", 1)[1]
    src = plan.source
    return {
        "name": name,
        "url": f"https://github.com/{plan.target}",
        "upstream": f"https://{src.host}/{plan.current_name or src.full_name}",
        "original_upstream": src.url,
        "strategy": plan.strategy,
        "state": state,
        "tapeouts": [],
        "runs": [],
        "licence": "",
        "branches": 0,
        "generated": "",
    }


def _from_catalog(row: dict, tapeouts: list[Tapeout]) -> None:
    row["tapeouts"] = [t.tag for t in tapeouts]
    row["runs"] = _runs([(t.org, t.run) for t in tapeouts])
    row["licence"] = next((t.licence for t in tapeouts if t.licence), "")


def _https(value) -> str | None:
    """`value` if it is an https:// URL (safe as a link), else None."""
    return value if isinstance(value, str) and value.startswith("https://") else None


def _from_bench_yaml(row: dict, data: dict) -> None:
    """Fill `row` from bench.yaml. Links are taken only if https://; else the plan's stay."""
    source = data.get("source")
    source = source if isinstance(source, dict) else {}
    tapeouts = [t for t in data.get("tapeouts") or [] if isinstance(t, dict)]
    branches = data.get("branches") or []
    url = _https(source.get("url"))
    if url:
        row["upstream"] = url
        if source.get("original_url"):
            row["original_upstream"] = _https(source["original_url"]) or row["original_upstream"]
        else:
            row["original_upstream"] = url
    row["tapeouts"] = [str(t.get("tag", "")) for t in tapeouts]
    row["runs"] = _runs([(str(t.get("org", "")), str(t.get("run", ""))) for t in tapeouts])
    row["licence"] = str(source.get("licence") or "")
    row["branches"] = sum(1 for b in branches if str(b).startswith("upstream/"))
    row["generated"] = str(data.get("generated") or "")


def index_rows(
    plans: list[RepoPlan],
    tapeouts: dict[str, list[Tapeout]],
    gh: GitHub,
    org: str,
    *,
    log: Log = lambda event, **fields: None,
) -> list[dict]:
    """One row per planned bench repo, sorted by name. The org's repos are the truth."""
    in_org = {r.full_name.lower(): r for r in gh.list_repos(org)}
    rows = []
    for p in plans:
        if p.strategy == "none":
            row = _row(p, "no-source")
            _from_catalog(row, tapeouts.get(p.source.key, []))
        else:
            repo = in_org.get(p.target.lower())
            data = None
            if repo is not None and repo.default_branch == "about":
                data = _read_bench_yaml(gh, repo.full_name, log)
            if data is not None:
                row = _row(p, "laid-out")
                _from_bench_yaml(row, data)
            else:
                row = _row(p, "created" if repo is not None else "pending")
                _from_catalog(row, tapeouts.get(p.source.key, []))
        rows.append(row)
    return sorted(rows, key=lambda r: r["name"].lower())


def write_site(rows: list[dict], site_dir: Path, out_dir: Path) -> None:
    """Copy the `site/` tree to `out_dir` (overwriting) and write `_data/repos.json`."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    shutil.copytree(site_dir, out_dir, dirs_exist_ok=True)
    data = out_dir / "_data"
    data.mkdir(exist_ok=True)
    (data / "repos.json").write_text(json.dumps(rows, indent=1) + "\n")


def _index(args) -> int:
    log = EventLog(args.log).write
    plans = read_plans(args.plans)
    tapeouts = tapeouts_by_repo(load_designs(args.catalog))
    rows = index_rows(plans, tapeouts, GhCli(), args.org, log=log)
    write_site(rows, args.site, args.out)
    counts = dict(sorted(collections.Counter(r["state"] for r in rows).items()))
    log("index", repos=len(rows), counts=counts)
    print(f"index: {len(rows)} repos, " + ", ".join(f"{k} {n}" for k, n in counts.items()))
    return 0


def add_subcommand(subparsers) -> None:
    p = subparsers.add_parser("index", help="build the index site source from the org's repos")
    p.add_argument("--out", type=Path, required=True, help="directory to write the Jekyll source to")
    p.add_argument(
        "--site", type=Path, default=Path("site"), help="site template directory (default: ./site)"
    )
    p.set_defaults(handler=_index)
