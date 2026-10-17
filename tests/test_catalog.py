from pathlib import Path

import pytest

from wafer_space_bench.catalog import Design, load_designs, tapeouts_by_repo

SMALL = Path(__file__).parent / "data" / "designs-small.csv"
REAL = Path(__file__).parent.parent / "data" / "designs.csv"


def test_multi_use_repo_groups_tapeouts():
    t = tapeouts_by_repo(load_designs(SMALL))["rejunity/ws0-z80-open-silicon-gf180mcu"]
    assert sorted(x.tag for x in t) == ["wafer.space/ws-run1/HZ80", "wafer.space/ws-run2/QZ80"]
    assert t[0].branch.startswith("tapeout/wafer.space/")


def test_design_without_repo_is_skipped():
    groups = tapeouts_by_repo(load_designs(SMALL))
    assert all(k for k in groups)
    assert all(x.slot != "BRWN" for ts in groups.values() for x in ts)


def test_tapeouts_match_original_url():
    assert "joannec34/teenytpu" in tapeouts_by_repo(load_designs(SMALL))


def test_tapeout_fields():
    ts = tapeouts_by_repo(load_designs(SMALL))["urish/tt-simon-game"]
    assert len(ts) == 2
    x = next(t for t in ts if t.run == "ttgf26a")
    assert (x.org, x.slot, x.source_key) == ("tinytapeout", "tt_um_urish_simon", "urish/tt-simon-game")
    assert x.page == "https://app.tinytapeout.com/shuttles/ttgf26a"


def test_unknown_shuttle_raises():
    d = Design("p", "nope", "c", "proj", "https://github.com/a/b", "", "", "", "")
    with pytest.raises(KeyError, match="nope"):
        tapeouts_by_repo([d])


def test_real_catalog_has_unique_tags():
    tags = [t.tag for ts in tapeouts_by_repo(load_designs(REAL)).values() for t in ts]
    assert len(tags) == len(set(tags)) and len(tags) > 400
