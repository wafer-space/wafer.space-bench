import subprocess

import yaml

from wafer_space_bench.about import about_data, render_readme, write_about_commit
from wafer_space_bench.catalog import Tapeout
from wafer_space_bench.git import Git
from wafer_space_bench.naming import RepoRef
from wafer_space_bench.scan import RepoPlan

REPO_FULL = "wafer-space-bench/urish--tt-simon-game"
NOW = "2026-10-17T00:00:00Z"
COMMITS = {
    "ttgf0p2": "44063952977f4a69178d8ffe0fb78dbe5b5cb0ff",
    "ttgf0p3": "3a0a46fc4a3ffa4bc1d69b58dd80e01f830c8277",
    "ttgf26a": "1ad919a9c6007aa477518746419dd642cd1b07b6",
    "ttgf26b": "3a0a46fc4a3ffa4bc1d69b58dd80e01f830c8277",
}
BRANCHES = [
    "about",
    "tapeout/tinytapeout/ttgf0p2/tt_um_urish_simon",
    "tapeout/tinytapeout/ttgf0p3/tt_um_urish_simon",
    "tapeout/tinytapeout/ttgf26a/tt_um_urish_simon",
    "tapeout/tinytapeout/ttgf26b/tt_um_urish_simon",
    "upstream/main",
    "upstream/ttgf0p2",
    "upstream/ttgf26a",
    "upstream/ttihp0p4",
    "upstream/ttihp26a",
    "upstream/ttihp26b",
]


def plan(**kw):
    base = {
        "source": RepoRef("github.com", "urish", "tt-simon-game"),
        "current_name": "urish/tt-simon-game",
        "status": "live",
        "network": "",
        "default_branch": "main",
        "size_kb": 1,
        "strategy": "fork",
        "target": "wafer-space-bench/urish--tt-simon-game",
    }
    return RepoPlan(**{**base, **kw})


def tapeouts(page=True):
    return [
        Tapeout(
            "tinytapeout",
            run,
            "tt_um_urish_simon",
            "Simon Says memory game",
            "1x1",
            "digital (Verilog)",
            "",
            sha,
            f"https://app.tinytapeout.com/shuttles/{run}" if page else "",
            "urish/tt-simon-game",
        )
        for run, sha in COMMITS.items()
    ]


def data(now=NOW, **kw):
    return about_data(plan(**kw), tapeouts(), list(reversed(BRANCHES)), now)


def test_readme_matches_golden():
    from pathlib import Path

    golden = (Path(__file__).parent / "golden" / "about_README.md").read_text()
    assert render_readme(data(), REPO_FULL) == golden


def test_data_shape():
    d = data()
    assert list(d) == ["source", "tapeouts", "generated", "tooling", "index", "branches"]
    assert list(d["source"]) == ["url", "original_url", "kind", "why", "licence", "upstream_default_branch"]
    assert d["source"]["original_url"] is None and d["source"]["licence"] is None
    assert d["branches"] == BRANCHES
    assert d["tapeouts"][0]["commit_source"].startswith("commit from the shuttle repo")
    assert d["tapeouts"][0]["branch"] == "tapeout/tinytapeout/ttgf0p2/tt_um_urish_simon"


def test_why_and_original_url_variants():
    d = data(strategy="copy", network="foo/bar", current_name="Urish/TT-Simon-Game")
    assert d["source"]["why"] == "copy: shares the fork network of foo/bar"
    assert d["source"]["original_url"] is None  # differs only by case
    d = data(strategy="copy", status="gitlab", current_name="other/name")
    assert d["source"]["why"] == "copy: hosted on GitLab"
    assert d["source"]["original_url"] == "https://github.com/urish/tt-simon-game"
    d = data(strategy="restore")
    assert d["source"]["why"] == "restored: the upstream repository was deleted"
    gl = about_data(
        plan(source=RepoRef("gitlab.com", "a", "b"), strategy="copy", status="gitlab", current_name=""),
        tapeouts(),
        [],
        NOW,
    )
    assert gl["source"]["url"] == "https://gitlab.com/a/b"
    assert gl["source"]["original_url"] is None


def test_readme_variants():
    d = about_data(plan(current_name="other/name", strategy="restore"), tapeouts(page=False), [], NOW)
    d["source"]["licence"] = "MIT"
    text = render_readme(d, REPO_FULL)
    assert "(originally <https://github.com/urish/tt-simon-game>)" in text
    assert "- This repository: Restored: the upstream repository was deleted." in text
    assert "- Licence: MIT" in text
    assert "| tinytapeout | ttgf0p2 | tt_um_urish_simon" in text


def test_readme_links_absolute():
    text = render_readme(data(), REPO_FULL)
    assert "../../" not in text
    assert f"https://github.com/{REPO_FULL}/tree/tapeout/tinytapeout/ttgf0p2/tt_um_urish_simon" in text


def test_about_yaml_round_trips(tmp_path):
    g = Git.init_bare(tmp_path / "r.git")
    d = data()
    write_about_commit(g, d, render_readme(d, REPO_FULL))
    assert yaml.safe_load(g.show("refs/heads/about", "bench.yaml")) == d


def test_about_commit_unchanged_returns_none(tmp_path):
    g = Git.init_bare(tmp_path / "r.git")
    d = data()
    first = write_about_commit(g, d, render_readme(d, REPO_FULL))
    d2 = data(now="2027-01-01T00:00:00Z")
    assert write_about_commit(g, d2, render_readme(d2, REPO_FULL)) is None
    assert g.rev_parse("refs/heads/about") == first


def test_about_commit_is_orphan_then_linear(tmp_path):
    g = Git.init_bare(tmp_path / "r.git")
    d = data()
    first = write_about_commit(g, d, render_readme(d, REPO_FULL))
    d2 = data(strategy="restore")
    second = write_about_commit(g, d2, render_readme(d2, REPO_FULL))
    assert second and second != first and g.rev_parse("refs/heads/about") == second

    def parents(c):
        out = subprocess.run(
            ["git", "rev-list", "--parents", "-n1", c], cwd=g.path, capture_output=True, text=True, check=True
        )
        return out.stdout.split()[1:]

    assert parents(first) == []
    assert parents(second) == [first]
    msg = subprocess.run(
        ["git", "log", "-1", "--format=%s", second], cwd=g.path, capture_output=True, text=True, check=True
    )
    assert msg.stdout.strip() == "Update bench.yaml and README"
