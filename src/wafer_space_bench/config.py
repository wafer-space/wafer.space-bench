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


def resolve_clone_url(public_url: str, cfg: LocalConfig) -> str:
    """The local mirror of `public_url` if the longest matching mirrors prefix has one
    (as `rest` or `rest.git`), else `public_url` itself."""
    url = public_url.rstrip("/").removesuffix(".git")
    matches = [p for p in cfg.mirrors if url.startswith(p)]
    if not matches:
        return public_url
    prefix = max(matches, key=len)
    rest = url[len(prefix) :].strip("/")
    for candidate in (Path(cfg.mirrors[prefix]) / rest, Path(cfg.mirrors[prefix]) / f"{rest}.git"):
        if rest and candidate.exists():
            return str(candidate)
    return public_url
