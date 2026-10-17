"""Machine-local configuration (bench.local.toml, never committed): work dir and mirrors.

`mirrors` maps a public URL prefix to a local directory holding bare mirrors laid out
the same way, so clones come from local disk when a mirror exists. Local paths stay
on this machine: they never appear in descriptions, tags or logged events.
"""

import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class LocalConfig:
    work_dir: Path = Path("work")
    mirrors: dict[str, Path] = field(default_factory=dict)


def load_config(path: Path) -> LocalConfig:
    """Read `path`; a missing file gives the defaults."""
    path = Path(path)
    if not path.exists():
        return LocalConfig()
    with open(path, "rb") as f:
        data = tomllib.load(f)
    return LocalConfig(
        work_dir=Path(data.get("work_dir", "work")),
        mirrors={prefix: Path(p) for prefix, p in data.get("mirrors", {}).items()},
    )


def _child(parent: Path, name: str) -> Path | None:
    """`parent / name`, else the one entry of `parent` equal to `name` ignoring case."""
    exact = parent / name
    if exact.exists():
        return exact
    if not parent.is_dir():
        return None
    hits = [c for c in parent.iterdir() if c.name.casefold() == name.casefold()]
    return hits[0] if len(hits) == 1 else None


def _find(base: Path, rest: str) -> Path | None:
    """`base / rest` (or `rest.git`), matching each path part exactly or else ignoring case
    (GitHub names are case-insensitive; a mirror may be stored under another spelling)."""
    *dirs, last = rest.split("/")
    here: Path | None = base
    for d in dirs:
        here = _child(here, d)
        if here is None:
            return None
    for name in (last, f"{last}.git"):
        hit = _child(here, name)
        if hit is not None:
            return hit
    return None


def resolve_clone_url(public_url: str, cfg: LocalConfig) -> str:
    """The local mirror of `public_url` if the longest matching mirrors prefix has one
    (as `rest` or `rest.git`, case-insensitively if no exact match), else `public_url`."""
    url = public_url.rstrip("/").removesuffix(".git")
    matches = [p for p in cfg.mirrors if url.startswith(p)]
    if not matches:
        return public_url
    prefix = max(matches, key=len)
    rest = url[len(prefix) :].strip("/")
    hit = _find(Path(cfg.mirrors[prefix]), rest) if rest else None
    return str(hit) if hit is not None else public_url
