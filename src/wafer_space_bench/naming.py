"""Naming rules for bench repos, tapeout tags and branch namespaces."""

import re
from dataclasses import dataclass
from typing import Literal

_REPO_RE = re.compile(r"(?<![\w.-])(github\.com|gitlab\.com)[/:]([^/\s]+)/([^/\s#?()]+)")


@dataclass(frozen=True)
class RepoRef:
    host: str
    owner: str
    name: str

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.name}"

    @property
    def url(self) -> str:
        return f"https://{self.host}/{self.owner}/{self.name}"

    @property
    def key(self) -> str:
        return self.full_name.lower()


def parse_repo_url(url: str) -> RepoRef | None:
    """Parse a github.com or gitlab.com repo reference out of free text."""
    m = _REPO_RE.search(url)
    if not m:
        return None
    return RepoRef(m.group(1), m.group(2), m.group(3).removesuffix(".git"))


def bench_name(full_name: str) -> str:
    """Name of the copy in the bench org: 'owner/name' -> 'owner--name'."""
    return full_name.replace("/", "--")


@dataclass(frozen=True)
class Run:
    org: str
    run: str
    page: str


RUNS: dict[str, Run] = {
    "ws-run1": Run("wafer.space", "ws-run1", "https://wafer.space/ws-run1/"),
    "ws-run2": Run("wafer.space", "ws-run2", "https://wafer.space/ws-run2/"),
    "gfmpw-0": Run("google", "gf180mcu-mpw0", "https://wafer.space/google-gf180mcu-mpw0/"),
    "mpw-18h1": Run("google", "gf180mcu-mpw18h1", ""),
    **{
        f"tt-gf-{s}": Run("tinytapeout", f"ttgf{s}", f"https://app.tinytapeout.com/shuttles/ttgf{s}")
        for s in ("0p1", "0p2", "0p3", "26a", "26b", "26c", "26d")
    },
}

COMMIT_SOURCE: dict[str, str] = {
    "wafer.space": "design_ref head recorded by the ws-run1/2 audit",
    "google": "designer commit from Efabless tapeout/logs/git.info",
    "tinytapeout": "commit from the shuttle repo's projects/<name>/commit_id.json",
}


def tag_name(org: str, run: str, slot: str) -> str:
    return f"{org}/{run}/{slot}"


BranchKind = Literal["about", "upstream", "tapeout", "dev", "original"]


def classify_branch(name: str) -> BranchKind:
    if name == "about":
        return "about"
    for kind in ("upstream", "tapeout", "dev"):
        if name.startswith(kind + "/"):
            return kind
    return "original"
