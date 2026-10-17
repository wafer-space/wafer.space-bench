import json
from pathlib import Path

import yaml
from fakes import FakeGitHub

from wafer_space_bench.catalog import Tapeout
from wafer_space_bench.cli import main
from wafer_space_bench.index import index_rows, write_site
from wafer_space_bench.naming import RepoRef
from wafer_space_bench.scan import RepoPlan, write_plans

ORG = "wafer-space-bench"
SITE = Path(__file__).resolve().parent.parent / "site"


def plan(name="a/up", strategy="fork", current=None, target=None, host="github.com"):
    owner, repo = name.split("/")
    cur = name if current is None else current
    return RepoPlan(
        RepoRef(host, owner, repo),
        cur,
        "live",
        cur,
        "main",
        1,
        strategy,
        target or f"{ORG}/{owner}--{repo}",
    )


def tapeout(source_key="a/up", run="ttgf0p2", slot="tt_um_x", licence="MIT"):
    return Tapeout("tinytapeout", run, slot, "P", "1x1", "digital", licence, "abc", "", source_key)


def bench_yaml(**over):
    data = {
        "source": {"url": "https://github.com/a/up", "original_url": None, "licence": "Apache-2.0"},
        "tapeouts": [{"org": "tinytapeout", "run": "ttgf0p2", "slot": "tt_um_x", "tag": "t"}],
        "branches": ["upstream/main", "upstream/dev", "about"],
        "generated": "2026-10-17T00:00:00Z",
    }
    data.update(over)
    return yaml.safe_dump(data)


def org_repo(gh, full, default="main"):
    gh.add_upstream(full, branches={default: "c1"})


class Events(list):
    def __call__(self, event, **fields):
        self.append((event, fields))


def test_index_states():
    gh = FakeGitHub()
    org_repo(gh, f"{ORG}/a--laid", "about")
    gh.files[(f"{ORG}/a--laid", "about", "bench.yaml")] = bench_yaml()
    org_repo(gh, f"{ORG}/b--created", "main")
    plans = [
        plan("a/laid"),
        plan("b/created"),
        plan("c/pending", strategy="restore"),
        plan("d/none", strategy="none"),
    ]
    rows = {r["name"]: r for r in index_rows(plans, {}, gh, ORG)}
    assert {n: r["state"] for n, r in rows.items()} == {
        "a--laid": "laid-out",
        "b--created": "created",
        "c--pending": "pending",
        "d--none": "no-source",
    }
    assert rows["a--laid"]["url"] == f"https://github.com/{ORG}/a--laid"
    assert rows["a--laid"]["branches"] == 2


def test_index_matches_case_insensitively_and_ignores_other_repos():
    gh = FakeGitHub()
    org_repo(gh, f"{ORG}/A--Up")
    org_repo(gh, f"{ORG}/wafer-space-bench.github.io")
    rows = index_rows([plan("A/Up", target=f"{ORG}/a--up")], {}, gh, ORG)
    assert [(r["name"], r["state"]) for r in rows] == [("a--up", "created")]


def test_index_reads_bench_yaml():
    gh = FakeGitHub()
    org_repo(gh, f"{ORG}/a--up", "about")
    extra = {"org": "google", "run": "gf180mcu-mpw0", "slot": "s1", "tag": "g"}
    gh.files[(f"{ORG}/a--up", "about", "bench.yaml")] = bench_yaml(
        tapeouts=[
            {"org": "tinytapeout", "run": "ttgf0p2", "slot": "tt_um_x", "tag": "tinytapeout/ttgf0p2/tt_um_x"},
            extra | {"tag": "google/gf180mcu-mpw0/s1"},
        ],
        source={
            "url": "https://github.com/z/new",
            "original_url": "https://github.com/a/up",
            "licence": "ISC",
        },
    )
    (row,) = index_rows([plan()], {"a/up": [tapeout()]}, gh, ORG)
    assert row["tapeouts"] == ["tinytapeout/ttgf0p2/tt_um_x", "google/gf180mcu-mpw0/s1"]
    assert row["runs"] == ["google/gf180mcu-mpw0", "tinytapeout/ttgf0p2"]
    assert row["licence"] == "ISC"
    assert row["upstream"] == "https://github.com/z/new"
    assert row["original_upstream"] == "https://github.com/a/up"
    assert row["generated"] == "2026-10-17T00:00:00Z"


def test_index_non_laid_out_uses_plans_and_catalog():
    gh = FakeGitHub()
    p = plan("a/up", current="a/renamed")
    tos = {"a/up": [tapeout(slot="s2"), tapeout(slot="s1", run="ttgf0p1")]}
    (row,) = index_rows([p], tos, gh, ORG)
    assert row["state"] == "pending"
    assert row["tapeouts"] == ["tinytapeout/ttgf0p2/s2", "tinytapeout/ttgf0p1/s1"]
    assert row["runs"] == ["tinytapeout/ttgf0p1", "tinytapeout/ttgf0p2"]
    assert row["licence"] == "MIT"
    assert row["upstream"] == "https://github.com/a/renamed"
    assert row["original_upstream"] == "https://github.com/a/up"
    assert row["strategy"] == "fork"
    assert row["branches"] == 0
    assert row["generated"] == ""


def test_index_bad_bench_yaml_is_created_not_crash():
    gh = FakeGitHub()
    for n in ("a--bad", "b--notmap", "c--nosource"):
        org_repo(gh, f"{ORG}/{n}", "about")
    gh.files[(f"{ORG}/a--bad", "about", "bench.yaml")] = "source: [unclosed\n  : :"
    gh.files[(f"{ORG}/b--notmap", "about", "bench.yaml")] = "- just\n- a list\n"
    gh.files[(f"{ORG}/c--nosource", "about", "bench.yaml")] = "tapeouts: []\n"
    org_repo(gh, f"{ORG}/d--ok", "about")
    gh.files[(f"{ORG}/d--ok", "about", "bench.yaml")] = bench_yaml()
    log = Events()
    rows = index_rows(
        [plan("a/bad"), plan("b/notmap"), plan("c/nosource"), plan("d/ok")], {}, gh, ORG, log=log
    )
    assert {r["name"]: r["state"] for r in rows} == {
        "a--bad": "created",
        "b--notmap": "created",
        "c--nosource": "created",
        "d--ok": "laid-out",
    }
    assert [e for e, _ in log].count("index-bad-yaml") == 3


def test_index_rows_sorted_and_json_serialisable():
    gh = FakeGitHub()
    org_repo(gh, f"{ORG}/b--x", "about")
    gh.files[(f"{ORG}/b--x", "about", "bench.yaml")] = bench_yaml()
    plans = [plan("z/Last"), plan("b/x"), plan("A/first", strategy="none")]
    rows = index_rows(plans, {"z/last": [tapeout("z/last")]}, gh, ORG)
    assert [r["name"] for r in rows] == ["A--first", "b--x", "z--Last"]
    keys = {
        "name", "url", "upstream", "original_upstream", "strategy", "state",
        "tapeouts", "runs", "licence", "branches", "generated",
    }  # fmt: skip
    assert all(set(r) == keys for r in rows)
    assert json.loads(json.dumps(rows)) == rows


def test_write_site_outputs(tmp_path):
    out = tmp_path / "out" / "nested"
    (out).mkdir(parents=True)
    (out / "index.md").write_text("stale")
    rows = [{"name": "a--b", "state": "created"}]
    write_site(rows, SITE, out)
    assert json.loads((out / "_data" / "repos.json").read_text()) == rows
    assert (out / "index.md").read_text() != "stale"
    assert (out / "_config.yml").exists()
    assert (out / "assets" / "table.js").exists()
    write_site(rows, SITE, out)  # idempotent


def test_write_site_creates_out_dir(tmp_path):
    write_site([], SITE, tmp_path / "x" / "y")
    assert (tmp_path / "x" / "y" / "_data" / "repos.json").read_text().strip() == "[]"


def test_site_escapes_inserted_fields():
    md = (SITE / "index.md").read_text()
    for field in ("name", "url", "upstream", "original_upstream", "strategy", "state", "licence"):
        assert f"repo.{field} | escape" in md, field
    assert "repo.tapeouts | join" in md and "escape" in md.split("repo.tapeouts | join")[1].split("}}")[0]
    assert 'src="http' not in md and "https://cdn" not in (SITE / "assets" / "table.js").read_text()


def test_cli_index_writes_site(tmp_path, monkeypatch):
    gh = FakeGitHub()
    org_repo(gh, f"{ORG}/a--up")
    monkeypatch.setattr("wafer_space_bench.index.GhCli", lambda: gh)
    plans = tmp_path / "data" / "bench_repos.csv"
    write_plans(plans, [plan()])
    (tmp_path / "data" / "designs.csv").write_text(
        "pool,shuttle,code,project,repo,commit,licence,licence_ok,kind,analog,flow,flow_family,"
        "toolchain,rebuild,size,std_libs,sram,core_voltage,mim,mim_src,hres,hres_src,dnwell,efuse,"
        "m4_pct,m5_pct,contains,notes,baseline_wave\n"
    )
    import shutil

    shutil.copytree(SITE, tmp_path / "site")
    monkeypatch.chdir(tmp_path)
    out = tmp_path / "out"
    log = tmp_path / "events.jsonl"
    rc = main(
        ["--org", ORG, "--catalog", str(tmp_path / "data" / "designs.csv"), "--plans", str(plans),
         "--log", str(log), "index", "--out", str(out)]
    )  # fmt: skip
    assert rc == 0
    rows = json.loads((out / "_data" / "repos.json").read_text())
    assert rows[0]["state"] == "created"
    ev = [json.loads(line) for line in log.read_text().splitlines()]
    assert ev[-1]["event"] == "index" and ev[-1]["counts"] == {"created": 1}


# --- final review: robustness and site location ---------------------------------


class BadFiles(FakeGitHub):
    """get_file fails like GhCli does on non-UTF-8 content or a directory listing."""

    def get_file(self, full_name, path, ref):
        if full_name.endswith("--binary"):
            raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")
        if full_name.endswith("--dir"):
            raise TypeError("list indices must be integers or slices, not str")
        return super().get_file(full_name, path, ref)


def test_index_undecodable_bench_yaml_is_logged_not_crash():
    gh = BadFiles()
    for n in ("a--binary", "b--dir", "c--ok"):
        org_repo(gh, f"{ORG}/{n}", "about")
    gh.files[(f"{ORG}/c--ok", "about", "bench.yaml")] = bench_yaml()
    log = Events()
    rows = index_rows([plan("a/binary"), plan("b/dir"), plan("c/ok")], {}, gh, ORG, log=log)
    assert {r["name"]: r["state"] for r in rows} == {
        "a--binary": "created",
        "b--dir": "created",
        "c--ok": "laid-out",
    }
    assert [f["repo"] for e, f in log if e == "index-bad-yaml"] == [f"{ORG}/a--binary", f"{ORG}/b--dir"]


def test_index_hrefs_from_bench_yaml_must_be_https():
    gh = FakeGitHub()
    org_repo(gh, f"{ORG}/a--up", "about")
    gh.files[(f"{ORG}/a--up", "about", "bench.yaml")] = bench_yaml(
        source={"url": "javascript:alert(1)", "original_url": "http://github.com/a/up", "licence": "MIT"}
    )
    (row,) = index_rows([plan("a/up", current="a/renamed")], {}, gh, ORG)
    assert row["state"] == "laid-out"
    assert row["upstream"] == "https://github.com/a/renamed"  # the plan's value
    assert row["original_upstream"] == "https://github.com/a/up"
    gh.files[(f"{ORG}/a--up", "about", "bench.yaml")] = bench_yaml(
        source={"url": "https://github.com/z/new", "original_url": "ftp://x/y", "licence": "MIT"}
    )
    (row,) = index_rows([plan("a/up", current="a/renamed")], {}, gh, ORG)
    assert row["upstream"] == "https://github.com/z/new"
    assert row["original_upstream"] == "https://github.com/a/up"


def _index_cli_setup(tmp_path, monkeypatch):
    gh = FakeGitHub()
    org_repo(gh, f"{ORG}/a--up")
    monkeypatch.setattr("wafer_space_bench.index.GhCli", lambda: gh)
    plans = tmp_path / "elsewhere" / "deep" / "plans.csv"
    write_plans(plans, [plan()])
    cat = tmp_path / "designs.csv"
    cat.write_text("pool,shuttle,code,project,repo,commit,licence,kind,size\n")
    base = ["--org", ORG, "--catalog", str(cat), "--plans", str(plans), "--log", str(tmp_path / "e.jsonl")]
    return base


def test_cli_index_site_dir_is_cwd_site_not_next_to_plans(tmp_path, monkeypatch):
    import shutil

    base = _index_cli_setup(tmp_path, monkeypatch)
    root = tmp_path / "root"
    shutil.copytree(SITE, root / "site")
    monkeypatch.chdir(root)
    assert main([*base, "index", "--out", str(tmp_path / "out")]) == 0
    assert (tmp_path / "out" / "index.md").exists()


def test_cli_index_site_option(tmp_path, monkeypatch):
    base = _index_cli_setup(tmp_path, monkeypatch)
    monkeypatch.chdir(tmp_path)  # no site/ here
    assert main([*base, "index", "--site", str(SITE), "--out", str(tmp_path / "out")]) == 0
    assert (tmp_path / "out" / "_config.yml").exists()
