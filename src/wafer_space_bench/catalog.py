"""Design catalog: load designs.csv and group tapeouts by source repo."""

import csv
from dataclasses import dataclass
from pathlib import Path

from wafer_space_bench.naming import RUNS, parse_repo_url, tag_name


@dataclass(frozen=True)
class Design:
    pool: str
    shuttle: str
    code: str
    project: str
    repo: str
    commit: str
    licence: str
    kind: str
    size: str


@dataclass(frozen=True)
class Tapeout:
    org: str
    run: str
    slot: str
    project: str
    size: str
    kind: str
    licence: str
    commit: str
    page: str
    source_key: str

    @property
    def tag(self) -> str:
        return tag_name(self.org, self.run, self.slot)

    @property
    def branch(self) -> str:
        return "tapeout/" + self.tag


def load_designs(path: Path) -> list[Design]:
    with open(path, newline="") as f:
        return [Design(**{k: row[k] for k in Design.__dataclass_fields__}) for row in csv.DictReader(f)]


def tapeouts_by_repo(designs: list[Design]) -> dict[str, list[Tapeout]]:
    """Group tapeouts by the RepoRef.key of the catalog URL; unparseable repos are skipped."""
    out: dict[str, list[Tapeout]] = {}
    for d in designs:
        ref = parse_repo_url(d.repo)
        if ref is None:
            continue
        try:
            run = RUNS[d.shuttle]
        except KeyError:
            raise KeyError(f"unknown shuttle {d.shuttle!r} for design {d.code!r}") from None
        out.setdefault(ref.key, []).append(
            Tapeout(
                org=run.org,
                run=run.run,
                slot=d.code,
                project=d.project,
                size=d.size,
                kind=d.kind,
                licence=d.licence,
                commit=d.commit,
                page=run.page,
                source_key=ref.key,
            )
        )
    return out
