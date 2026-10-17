"""Content of a bench repo's `about` branch: bench.yaml plus a README generated from it.

The `about` branch is an orphan history. Its tip is rewritten only when something other than
the `generated` timestamp changes, so the daily sync does not add a commit every day.
"""

import copy

import jinja2
import yaml

from wafer_space_bench.catalog import Tapeout
from wafer_space_bench.git import Git
from wafer_space_bench.naming import COMMIT_SOURCE
from wafer_space_bench.scan import RepoPlan

TOOLING = "https://github.com/wafer-space/wafer.space-bench"
INDEX = "https://wafer-space-bench.github.io"
ABOUT_REF = "refs/heads/about"
MESSAGE = "Update bench.yaml and README"

_env = jinja2.Environment(
    loader=jinja2.PackageLoader("wafer_space_bench", "templates"),
    keep_trailing_newline=True,
    trim_blocks=True,
    lstrip_blocks=True,
    autoescape=False,
    undefined=jinja2.StrictUndefined,
)


def _why(plan: RepoPlan) -> str:
    if plan.strategy == "fork":
        return "GitHub fork of the upstream repository"
    if plan.strategy == "copy":
        if plan.status == "gitlab":
            return "copy: hosted on GitLab"
        return f"copy: shares the fork network of {plan.network}"
    if plan.strategy == "restore":
        return "restored: the upstream repository was deleted"
    raise ValueError(f"no about data for strategy {plan.strategy!r}")


def about_data(plan: RepoPlan, tapeouts: list[Tapeout], branches: list[str], now: str) -> dict:
    src = plan.source
    if src.host == "github.com":
        url = f"https://github.com/{plan.current_name or src.full_name}"
    else:
        url = src.url
    return {
        "source": {
            "url": url,
            "original_url": src.url if url.lower() != src.url.lower() else None,
            "kind": plan.strategy,
            "why": _why(plan),
            "licence": next((t.licence for t in tapeouts if t.licence), None),
            "upstream_default_branch": plan.default_branch or None,
        },
        "tapeouts": [
            {
                "org": t.org,
                "run": t.run,
                "slot": t.slot,
                "project": t.project,
                "size": t.size,
                "kind": t.kind,
                "commit": t.commit,
                "commit_source": COMMIT_SOURCE[t.org],
                "tag": t.tag,
                "branch": t.branch,
                "page": t.page,
            }
            for t in tapeouts
        ],
        "generated": now,
        "tooling": TOOLING,
        "index": INDEX,
        "branches": sorted(branches),
    }


def render_readme(data: dict, repo_full: str) -> str:
    """The README text. It never contains the `generated` timestamp (see write_about_commit)."""
    why = data["source"]["why"]
    return _env.get_template("about_README.md.j2").render(
        name=repo_full.split("/", 1)[-1],
        repo_full=repo_full,
        source=data["source"],
        why=why[:1].upper() + why[1:],
        tapeouts=data["tapeouts"],
        branches=data["branches"],
        tooling=data["tooling"],
        index=data["index"],
    )


def _dump(data: dict) -> str:
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True)


def _without_generated(data: dict) -> dict:
    d = copy.deepcopy(data)
    d.pop("generated", None)
    return d


def write_about_commit(git: Git, data: dict, readme: str) -> str | None:
    """Commit bench.yaml and README.md to refs/heads/about; None if only `generated` would change."""
    tip = git.rev_parse(ABOUT_REF)
    if tip is not None:
        old_yaml = git.show(ABOUT_REF, "bench.yaml")
        old_readme = git.show(ABOUT_REF, "README.md")
        if old_yaml is not None and old_readme == readme:
            try:
                old = yaml.safe_load(old_yaml)
            except yaml.YAMLError:
                old = None
            if isinstance(old, dict) and _without_generated(old) == _without_generated(data):
                return None
    tree = git.mktree({"bench.yaml": git.hash_blob(_dump(data)), "README.md": git.hash_blob(readme)})
    commit = git.commit_tree(tree, [tip] if tip else [], MESSAGE)
    git.update_ref(ABOUT_REF, commit)
    return commit
