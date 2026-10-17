"""Workflow hardening: pinned third-party actions, least-privilege permissions, dependabot."""

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = sorted(
    [
        *(ROOT / ".github" / "workflows").glob("*.yml"),
        *(ROOT / "index-repo" / ".github" / "workflows").glob("*.yml"),
    ]
)
PINNED = ("astral-sh/setup-uv", "actions/create-github-app-token")


def load(path):
    return yaml.safe_load(path.read_text())


def test_pinned_actions_use_full_shas_with_version_comment():
    seen = set()
    for wf in WORKFLOWS:
        for line in wf.read_text().splitlines():
            m = re.search(r"uses:\s*([\w.-]+/[\w.-]+)@(\S+)(.*)", line)
            if not m or m.group(1) not in PINNED:
                continue
            seen.add(m.group(1))
            assert re.fullmatch(r"[0-9a-f]{40}", m.group(2)), (wf.name, line)
            assert re.fullmatch(r"\s+# v\d+\.\d+\.\d+", m.group(3)), (wf.name, line)
    assert seen == set(PINNED)


def test_same_action_pinned_to_one_sha_everywhere():
    shas: dict[str, set[str]] = {}
    for wf in WORKFLOWS:
        for action, sha in re.findall(r"uses:\s*([\w.-]+/[\w.-]+)@([0-9a-f]{40})", wf.read_text()):
            shas.setdefault(action, set()).add(sha)
    assert all(len(s) == 1 for s in shas.values()), shas


def test_ci_permissions_read_only():
    assert load(ROOT / ".github" / "workflows" / "ci.yml")["permissions"] == {"contents": "read"}


def test_pages_write_permissions_only_on_deploy_job():
    wf = load(ROOT / "index-repo" / ".github" / "workflows" / "pages.yml")
    assert wf["permissions"] == {"contents": "read"}
    assert "permissions" not in wf["jobs"]["build"]
    assert wf["jobs"]["deploy"]["permissions"] == {"pages": "write", "id-token": "write"}


def test_dependabot_watches_github_actions():
    cfg = load(ROOT / ".github" / "dependabot.yml")
    assert cfg["version"] == 2
    (upd,) = [u for u in cfg["updates"] if u["package-ecosystem"] == "github-actions"]
    assert set(upd["directories"]) == {"/", "/index-repo"}
    assert upd["schedule"]["interval"] in ("daily", "weekly", "monthly")
