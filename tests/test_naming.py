import pytest

from wafer_space_bench.naming import (
    COMMIT_SOURCE,
    RUNS,
    RepoRef,
    Run,
    bench_name,
    classify_branch,
    parse_repo_url,
    tag_name,
)


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://github.com/urish/tt-simon-game", ("github.com", "urish", "tt-simon-game")),
        (
            "https://github.com/AvalonSemiconductors/ws-submission-2025/",
            ("github.com", "AvalonSemiconductors", "ws-submission-2025"),
        ),
        (
            "https://github.com/gregdavill/gf180-mpw0-serv.git",
            ("github.com", "gregdavill", "gf180-mpw0-serv"),
        ),
        ("git@github.com:a/b.git", ("github.com", "a", "b")),
        ("https://gitlab.com/ChipFlow/gfmpw", ("gitlab.com", "ChipFlow", "gfmpw")),
        (
            "github.com/ashbir/sensor-harness-gf180 (README badge)",
            ("github.com", "ashbir", "sensor-harness-gf180"),
        ),
    ],
)
def test_parse_repo_url(url, expected):
    ref = parse_repo_url(url)
    assert ref == RepoRef(*expected)


@pytest.mark.parametrize("url", ["", "(slot repo)", "https://example.com/x/y"])
def test_parse_repo_url_rejects(url):
    assert parse_repo_url(url) is None


def test_repo_ref_properties():
    ref = RepoRef("github.com", "Foo", "Bar")
    assert ref.full_name == "Foo/Bar"
    assert ref.url == "https://github.com/Foo/Bar"
    assert ref.key == "foo/bar"


def test_bench_name():
    assert bench_name("1oketto/teenytpu") == "1oketto--teenytpu"


def test_tag_name():
    assert tag_name("wafer.space", "ws-run1", "HZ80") == "wafer.space/ws-run1/HZ80"


def test_runs_use_website_names():
    assert RUNS["gfmpw-0"] == Run("google", "gf180mcu-mpw0", "https://wafer.space/google-gf180mcu-mpw0/")
    assert RUNS["tt-gf-26a"].run == "ttgf26a"
    assert RUNS["tt-gf-0p1"].page == "https://app.tinytapeout.com/shuttles/ttgf0p1"
    assert RUNS["mpw-18h1"].page == ""
    assert RUNS["ws-run2"] == Run("wafer.space", "ws-run2", "https://wafer.space/ws-run2/")
    assert len(RUNS) == 11


def test_commit_source():
    assert set(COMMIT_SOURCE) == {"wafer.space", "google", "tinytapeout"}
    assert COMMIT_SOURCE["google"] == "designer commit from Efabless tapeout/logs/git.info"


@pytest.mark.parametrize(
    "b,kind",
    [
        ("about", "about"),
        ("upstream/main", "upstream"),
        ("tapeout/google/gf180mcu-mpw0/6079", "tapeout"),
        ("dev/repro/x", "dev"),
        ("main", "original"),
        ("aboutx", "original"),
        ("upstreamfoo", "original"),
        ("devel", "original"),
    ],
)
def test_classify_branch(b, kind):
    assert classify_branch(b) == kind
