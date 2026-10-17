# wafer.space-bench tooling Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the one-off `experiments/tools/bench_*.py` scripts into a tested, CI-checked Python package (`wsbench`). It is the first content of `wafer-space/wafer.space-bench`. It finds benchmark repos, forks or copies them into `wafer-space-bench`, applies the branch layout and generates the index site.

**Architecture:**
- A `src/` layout package. Pure logic (naming, catalog, planning, README rendering, index rows) is separated from two side-effect boundaries: a `GitHub` protocol (real: `gh api`; tests: an in-memory fake) and a `Git` wrapper (real git on temp repos in tests).
- Each CLI subcommand is a thin function over those modules.
- State lives in committed CSV data files plus a git-ignored JSONL event log.

**Tech Stack:** Python ≥3.11, uv, hatchling, pytest, pytest-socket, ruff, jinja2, pyyaml, `gh` CLI, git, GitHub Actions, Jekyll (GitHub Pages).

**Spec:** `docs/superpowers/specs/2026-10-17-bench-tooling-design.md`

## Global Constraints

- **Repos:**
  - Tooling repo `wafer-space/wafer.space-bench` (public).
  - Bench org `wafer-space-bench`.
  - Index repo `wafer-space-bench/wafer-space-bench.github.io`.
- **Bench repo name:** `<owner>--<repo>` of the *current* upstream full name.
- **Tag:** `<org>/<run>/<slot>`, with org ∈ {`wafer.space`, `google`, `tinytapeout`}. Runs are `ws-run1`, `ws-run2`, `gf180mcu-mpw0`, `gf180mcu-mpw18h1`, `ttgf0p1`…`ttgf26d`. Never use short shuttle codes.
- **Branches:** `about` (default, orphan), `upstream/<b>`, `tapeout/<org>/<run>/<slot>`, `dev/…`.
- **Rate limit:** on HTTP 403 "was submitted too quickly", back off 60 s, doubling, capped at 960 s, and retry the same repo. Default pacing between creations is 20 s.
- **Privacy:** no committed file or published string may contain `/backups`, `/home/`, `big-storage`, `~/github` or `/tmp/claude`. Local mirror paths come only from git-ignored `bench.local.toml`.
- **Tests:** no network (`pytest --disable-socket --allow-unix-socket`). Git runs for real in `tmp_path`.
- **Conventions:** match `ws-run-reports` (src layout, hatchling, `[dependency-groups] dev`, `cli.py:main`). Licence Apache-2.0.
- **Commit trailer:** `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Review Focus

1. **A rate limit mid-run, then the run is re-run.** No duplicate repos or tags; the run finishes where it stopped. Covered by `test_fork_backoff_then_success` and `test_run_fork_resumes` (Task 6).
2. **An upstream branch that collides with our namespaces**, such as `about`, `upstream/x`, or both `x` and `x/y`. Report a conflict and change nothing in that repo. Covered by `test_layout_conflicts` (Task 9).
3. **A taped-out commit unreachable from the bench repo.** That tag is reported as `missing-commit`; other tags still get created. Covered by `test_tags_missing_commit` (Task 6).
4. **A local path leaking into a tag message, description, README or index.** The privacy tests fail. Covered by `test_no_banned_strings_in_tracked_files` (Task 1), `test_tag_message_has_no_local_paths` (Task 6) and `test_copy_description_public` (Task 7).
5. **An upstream renamed between scan and fork.** The target uses the current name, and tags still match the catalog's original URL. Covered by `test_plan_renamed_repo` (Task 5) and `test_tapeouts_match_original_url` (Task 3).

---

### Task 1: Repository scaffold, CI and privacy test

**Files:**
- Create: `pyproject.toml`, `README.md`, `LICENSE` (Apache-2.0), `.gitignore`, `src/wafer_space_bench/__init__.py`, `src/wafer_space_bench/cli.py`, `.github/workflows/ci.yml`, `tests/test_privacy.py`, `tests/test_cli.py`
- Move in: `docs/superpowers/specs/2026-10-17-bench-tooling-design.md`, `docs/superpowers/plans/2026-10-17-bench-tooling.md` (from `experiments/`)

**Interfaces:**
- Produces: `wafer_space_bench.cli.main(argv: list[str] | None = None) -> int`; console script `wsbench`. Each later task adds a subcommand via `add_subcommand(subparsers)` in its module, registered in `cli.py`.

Working copy: `~/github/wafer-space/wafer.space-bench` (`git init`). The GitHub repo is created in Task 11.

- [ ] **Step 1: Write `pyproject.toml`**
  - `name = "wafer-space-bench"`, `requires-python = ">=3.11"`.
  - Dependencies: `jinja2`, `pyyaml>=6.0.3`.
  - `[project.scripts] wsbench = "wafer_space_bench.cli:main"`.
  - `[dependency-groups] dev = ["pytest", "pytest-socket", "ruff"]`.
  - `[tool.pytest.ini_options] testpaths = ["tests"]`, `addopts = "--disable-socket --allow-unix-socket"`.
  - `[tool.ruff] line-length = 110`.
  - Then run `uv lock`.
- [ ] **Step 2: `.gitignore`.** Ignore `bench.local.toml`, `*.jsonl`, `.venv/`, `__pycache__/`, `work/`.
- [ ] **Step 3: Write the failing tests**

```python
# tests/test_privacy.py
BANNED = ["/backups", "/home/", "big-storage", "~/github", "/tmp/claude"]

def test_no_banned_strings_in_tracked_files():
    files = subprocess.run(["git", "ls-files"], capture_output=True, text=True, check=True).stdout.split()
    hits = [(f, b) for f in files if f != "tests/test_privacy.py"
            for b in BANNED if b in Path(f).read_text(errors="ignore")]
    assert hits == []

# tests/test_cli.py
def test_help_lists_subcommands(capsys):
    with pytest.raises(SystemExit) as e:
        main(["--help"])
    assert e.value.code == 0
    assert "wsbench" in capsys.readouterr().out
```

- [ ] **Step 4: Run** `uv run pytest -q`. Expected: FAIL (no `cli` module).
- [ ] **Step 5: Implement `cli.main`.** Use argparse with `prog="wsbench"`, subparsers, and global options:
  - `--org` (default `wafer-space-bench`)
  - `--catalog` (default `data/designs.csv`)
  - `--plans` (default `data/bench_repos.csv`)
  - `--log` (default `work/events.jsonl`)
  - `--config` (default `bench.local.toml`)
- [ ] **Step 6: Write `.github/workflows/ci.yml`**
  - Trigger on `push` and `pull_request`; runs on `ubuntu-latest`.
  - Steps: `actions/checkout@v4` (`fetch-depth: 0`), `astral-sh/setup-uv@v6`, `uv sync --locked`, `uv run ruff check`, `uv run ruff format --check`, `uv run pytest -q`.
  - Matrix: Python `3.11` and `3.13` via `uv python install`.
- [ ] **Step 7: Run** `uv run pytest -q && uv run ruff check && uv run ruff format --check`. Expected: all pass.
- [ ] **Step 8: Commit** "Scaffold wafer.space-bench package, CI and privacy test".

### Task 2: Naming

**Files:** Create `src/wafer_space_bench/naming.py`, `tests/test_naming.py`

**Interfaces:**
- Produces:
  - `RepoRef(host: str, owner: str, name: str)`: a frozen dataclass with `.full_name -> "owner/name"`, `.url -> "https://host/owner/name"` and `.key -> full_name.lower()`.
  - `parse_repo_url(url: str) -> RepoRef | None`.
  - `bench_name(full_name: str) -> str`.
  - `Run(org: str, run: str, page: str)`.
  - `RUNS: dict[str, Run]`, keyed by catalog shuttle: `ws-run1`, `ws-run2`, `gfmpw-0`, `mpw-18h1`, `tt-gf-0p1`…`tt-gf-26d`.
  - `tag_name(org: str, run: str, slot: str) -> str`.
  - `COMMIT_SOURCE: dict[str, str]`, with the spec's three strings.
  - `classify_branch(name: str) -> Literal["about", "upstream", "tapeout", "dev", "original"]`.

- [ ] **Step 1: Failing tests**

```python
@pytest.mark.parametrize("url,expected", [
    ("https://github.com/urish/tt-simon-game", ("github.com", "urish", "tt-simon-game")),
    ("https://github.com/AvalonSemiconductors/ws-submission-2025/", ("github.com", "AvalonSemiconductors", "ws-submission-2025")),
    ("https://github.com/gregdavill/gf180-mpw0-serv.git", ("github.com", "gregdavill", "gf180-mpw0-serv")),
    ("git@github.com:a/b.git", ("github.com", "a", "b")),
    ("https://gitlab.com/ChipFlow/gfmpw", ("gitlab.com", "ChipFlow", "gfmpw")),
    ("github.com/ashbir/sensor-harness-gf180 (README badge)", ("github.com", "ashbir", "sensor-harness-gf180")),
])
def test_parse_repo_url(url, expected): ...

@pytest.mark.parametrize("url", ["", "(slot repo)", "https://example.com/x/y"])
def test_parse_repo_url_rejects(url): assert parse_repo_url(url) is None

def test_bench_name(): assert bench_name("1oketto/teenytpu") == "1oketto--teenytpu"
def test_tag_name(): assert tag_name("wafer.space", "ws-run1", "HZ80") == "wafer.space/ws-run1/HZ80"
def test_runs_use_website_names():
    assert RUNS["gfmpw-0"] == Run("google", "gf180mcu-mpw0", "https://wafer.space/google-gf180mcu-mpw0/")
    assert RUNS["tt-gf-26a"].run == "ttgf26a"

@pytest.mark.parametrize("b,kind", [("about","about"), ("upstream/main","upstream"),
    ("tapeout/google/gf180mcu-mpw0/6079","tapeout"), ("dev/repro/x","dev"), ("main","original"), ("aboutx","original")])
def test_classify_branch(b, kind): assert classify_branch(b) == kind
```

- [ ] **Step 2: Run** `uv run pytest tests/test_naming.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement.** Port from `experiments/tools/bench_repos.py` (`parse_repo`) and `bench_migrate.py` (`RUNS`, including the page URLs). For TT runs the page is `https://app.tinytapeout.com/shuttles/ttgf<s>`; for `mpw-18h1` it is `""`.
- [ ] **Step 4: Run** the tests. Expected: PASS.
- [ ] **Step 5: Commit** "Add naming rules for bench repos, tags and branches".

### Task 3: Catalog

**Files:**
- Create: `src/wafer_space_bench/catalog.py`, `data/designs.csv` (copied from `experiments/catalog/designs.csv`), `tests/test_catalog.py`, `tests/data/designs-small.csv`

**Interfaces:**
- Consumes: `RepoRef`, `parse_repo_url`, `RUNS`, `tag_name` (Task 2).
- Produces:
  - `Design`: dataclass with fields `pool, shuttle, code, project, repo, commit, licence, kind, size` (all `str`).
  - `load_designs(path: Path) -> list[Design]`.
  - `Tapeout`: dataclass with fields `org, run, slot, project, size, kind, commit, page, source_key` and properties `.tag` and `.branch` (the latter is `"tapeout/" + tag`).
  - `tapeouts_by_repo(designs: list[Design]) -> dict[str, list[Tapeout]]`, keyed by `RepoRef.key` of the catalog URL.

- [ ] **Step 1: Make the test fixture.** `tests/data/designs-small.csv` holds 6 real rows:
  - `ws-run1/HZ80` and `ws-run2/QZ80` (rejunity Z80, multi-use);
  - `tt-gf-0p2` and `tt-gf-26a` `tt_um_urish_simon`;
  - `ws-run1/BRWN` (no repo);
  - `tt-gf-26a/tt_um_joannec34_teenytpu`, whose catalog URL is `joannec34/teenytpu` (since renamed to `1oketto/teenytpu`).
- [ ] **Step 2: Failing tests**

```python
def test_multi_use_repo_groups_tapeouts():
    t = tapeouts_by_repo(load_designs(SMALL))["rejunity/ws0-z80-open-silicon-gf180mcu"]
    assert sorted(x.tag for x in t) == ["wafer.space/ws-run1/HZ80", "wafer.space/ws-run2/QZ80"]
    assert t[0].branch.startswith("tapeout/wafer.space/")

def test_design_without_repo_is_skipped():
    assert all(k for k in tapeouts_by_repo(load_designs(SMALL)))

def test_tapeouts_match_original_url():
    assert "joannec34/teenytpu" in tapeouts_by_repo(load_designs(SMALL))

def test_real_catalog_has_unique_tags():
    tags = [t.tag for ts in tapeouts_by_repo(load_designs(Path("data/designs.csv"))).values() for t in ts]
    assert len(tags) == len(set(tags)) and len(tags) > 400
```

- [ ] **Step 3: Run.** Expected: FAIL.
- [ ] **Step 4: Implement.** Copy `data/designs.csv`, then run the Task 1 privacy test. If it flags the `notes` column, drop that column from the committed copy. The `notes` column is not used by this package.
- [ ] **Step 5: Run** `uv run pytest -q`. Expected: PASS, including the privacy test.
- [ ] **Step 6: Commit** "Add design catalog and tapeout grouping".

### Task 4: GitHub boundary (protocol, `gh` implementation, fake)

**Files:**
- Create: `src/wafer_space_bench/github.py`, `tests/fakes.py`, `tests/test_github.py`

**Interfaces:**
- Produces:
  - Exceptions: `GitHubError(Exception)`, plus subclasses `NotFound(GitHubError)` and `RateLimited(GitHubError)`.
  - `RepoInfo`: dataclass with `full_name: str, fork: bool, network: str, default_branch: str, archived: bool, size_kb: int`.
  - `GitHub` Protocol with these methods:
    - `get_repo(full_name) -> RepoInfo` (raises `NotFound`);
    - `resolve_commit(full_name, sha) -> str | None`;
    - `fork(source_full_name, org, name) -> None`;
    - `branches_visible(full_name) -> bool`;
    - `get_tag(full_name, tag) -> str | None`;
    - `create_tag(full_name, tag, sha, message) -> None`;
    - `create_repo(org, name, description) -> None`;
    - `set_default_branch(full_name, branch) -> None`;
    - `list_repos(org) -> list[RepoInfo]`.
  - `GhCli(runner=subprocess.run)` implements it with `gh api`.
  - `tests/fakes.py::FakeGitHub` implements it in memory:
    - `add_upstream(full, network=None, commits=(), deleted=False, renamed_to=None, archived=False)`;
    - `rate_limit_next(n)`, which makes the next `n` creations raise `RateLimited`;
    - `ready_after(n)`, which makes `branches_visible` false for the first `n` polls;
    - inspection dicts `.repos`, `.tags` and `.default_branch`.
  - The fake enforces the fork-network rule: one repo per network per org. A second fork raises `GitHubError("already forked in network")`.

- [ ] **Step 1: Failing tests**
  - **GhCli:** `runner` is replaced by a stub returning `CompletedProcess`.
    - `test_ghcli_fork_args`: asserts `["gh","api","-X","POST","repos/a/b/forks","-f","organization=o","-f","name=a--b","-F","default_branch_only=false"]`.
    - `test_ghcli_rate_limited`: stderr `"gh: was submitted too quickly (HTTP 403)"` raises `RateLimited`.
    - `test_ghcli_not_found`: stderr containing `HTTP 404` raises `NotFound`.
    - `test_ghcli_get_repo_follows_source`: `source.full_name` becomes `network`.
  - **Contract tests**, parametrised over `FakeGitHub` only:
    - `test_fake_one_fork_per_network`;
    - `test_fake_rate_limit_then_ok`;
    - `test_fake_renamed_repo_reports_current_name`.
- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Implement** `GhCli`. Port the `gh()` helper from `experiments/tools/bench_fork.py`, classifying errors by stderr. Then implement `FakeGitHub`.
- [ ] **Step 4: Run.** Expected: PASS.
- [ ] **Step 5: Commit** "Add GitHub boundary with gh implementation and in-memory fake".

### Task 5: Event log and scan

**Files:**
- Create: `src/wafer_space_bench/events.py`, `src/wafer_space_bench/scan.py`, `tests/test_scan.py`, `data/bench_repos.csv` (generated in Step 5)

**Interfaces:**
- Consumes: Tasks 2–4.
- Produces:
  - `EventLog(path: Path, clock=time.gmtime)` with `.write(event: str, **fields) -> None`, which appends one JSON line with an ISO time.
  - `RepoPlan`: dataclass with `source: RepoRef, current_name: str, status: Literal["live","archived","missing","gitlab"], network: str, default_branch: str, size_kb: int, strategy: Literal["fork","copy","restore","none"], target: str, missing_commits: list[str]`.
  - `plan_repos(designs: list[Design], gh: GitHub) -> list[RepoPlan]`, sorted by `source.key`.
  - `write_plans(path, plans)` and `read_plans(path) -> list[RepoPlan]`, which round-trip.
  - CLI: `wsbench scan` (`add_subcommand`).

- [ ] **Step 1: Failing tests**, using `FakeGitHub` and in-test designs:
  - `test_plan_first_in_network_forks_rest_copy`: two repos in network `wafer-space/gf180mcu-project-template`. The alphabetically first is `fork`, the other `copy`.
  - `test_plan_gitlab_is_copy`.
  - `test_plan_deleted_is_restore`.
  - `test_plan_renamed_repo`: catalog `joannec34/teenytpu`, current `1oketto/teenytpu`. Expect `target == "wafer-space-bench/1oketto--teenytpu"` and `source.full_name == "joannec34/teenytpu"`.
  - `test_plan_missing_commit_listed`.
  - `test_plans_round_trip(tmp_path)`.
- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Implement.** Port `scan()` and the strategy loop from `experiments/tools/bench_repos.py`, without the thread pool. Keep it sequential; `--jobs` is a later concern (YAGNI).
- [ ] **Step 4: Run.** Expected: PASS.
- [ ] **Step 5: Generate the real plans.** Run `uv run wsbench scan`, which only GETs from live GitHub. Commit `data/bench_repos.csv` after diffing it against `experiments/catalog/bench_repos.csv`. The only expected differences are column naming and repos that changed state since the earlier scan.
- [ ] **Step 6: Commit** "Add scan: plan fork/copy/restore per upstream repo".

### Task 6: Fork and tag

**Files:** Create `src/wafer_space_bench/fork.py`, `tests/test_fork.py`

**Interfaces:**
- Consumes: Tasks 2–5.
- Produces:
  - `tag_message(t: Tapeout, source_url: str, why: str | None = None) -> str`.
  - `ensure_fork(gh, plan, org, *, sleep, log, base_wait=60, max_wait=960, ready_polls=30) -> Literal["created","exists"]`.
  - `ensure_tags(gh, repo_full, tapeouts, source_url, log) -> dict[str, Literal["created","exists","missing-commit"]]`.
  - `run_fork(plans, tapeouts, gh, org, *, delay=20, sleep=time.sleep, log)`.
  - CLI: `wsbench fork [--only NAME ...] [--delay S]`.

- [ ] **Step 1: Failing tests**
  - `test_fork_backoff_then_success`: `fake.rate_limit_next(2)` gives result `"created"`, and the recorded sleeps start with `[60, 120]`.
  - `test_fork_backoff_caps_at_960`: `rate_limit_next(6)`; the waits are `[60,120,240,480,960,960]`.
  - `test_fork_other_error_not_retried`.
  - `test_fork_exists_is_noop`.
  - `test_fork_waits_until_ready`: `ready_after(3)`.
  - `test_tags_created_then_exists`: run twice; the second run gives all `"exists"`.
  - `test_tags_missing_commit`: one unreachable SHA gives `"missing-commit"`, and the other tags are still created.
  - `test_tag_message_has_no_local_paths`: no banned string; the message contains the source URL and `COMMIT_SOURCE[t.org]`.
  - `test_run_fork_resumes`: a rate limit hits on the 2nd of 3 repos; re-running yields 3 forks, no duplicates, and `delay` sleeps only after `"created"`.
- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Implement.** Port from `experiments/tools/bench_fork.py`. Tags are annotated, created via `gh.create_tag`.
- [ ] **Step 4: Run.** Expected: PASS.
- [ ] **Step 5: Commit** "Add fork and tapeout tagging with rate-limit backoff".

### Task 7: Git wrapper, local config and copy/restore

**Files:**
- Create: `src/wafer_space_bench/git.py`, `src/wafer_space_bench/config.py`, `src/wafer_space_bench/copy.py`, `data/restore_sources.toml`, `bench.local.toml.example`, `tests/conftest.py`, `tests/test_git.py`, `tests/test_copy.py`

**Interfaces:**
- Consumes: Tasks 2–6.
- Produces:
  - `Git(path: Path)` with:
    - `rev_parse(ref) -> str | None`;
    - `update_ref(ref, target)`, `delete_ref(ref)`;
    - `heads() -> list[str]`, `tags() -> list[str]`;
    - `hash_blob(text) -> str`;
    - `mktree(entries: dict[str, str]) -> str`;
    - `commit_tree(tree, parents: list[str], message) -> str`;
    - `create_tag(name, target, message)`;
    - `fetch(remote, refspecs)`, `push(remote, refspecs)`.
  - `Git.clone_bare(url, dest) -> Git`. The committer identity is fixed as `wafer-space-bench <bench@wafer.space>`.
  - `LocalConfig(work_dir: Path, mirrors: dict[str, Path])`, with `load_config(path) -> LocalConfig`. A missing file gives `work_dir=Path("work")` and `mirrors={}`.
  - `RestoreSource(public_url: str, description: str)`.
  - `restore_sources(path) -> dict[str, RestoreSource]`, keyed by `RepoRef.key`. Entries are the 4 deleted Google repos, each pointing at its public `https://foss-eda-tools.googlesource.com/third_party/shuttle/gf180mcu/<run>/slot-NNN` URL.
  - `resolve_clone_url(public_url, cfg) -> str`: the local mirror if a `mirrors` prefix matches and the path exists, else the public URL.
  - `copy_description(plan) -> str`.
  - `run_copy(plan, tapeouts, gh, cfg, org, remote_fmt="git@github.com:{full}.git", log) -> str`.
  - CLI: `wsbench copy [--only ...] [--dry-run]`.
- `tests/conftest.py` fixture `upstream(tmp_path)` creates a non-bare repo with branches `main`, `feature/x` and `ttgf0p2`, 3 commits, and returns `(path, {name: sha})`. Fixture `remote(tmp_path)` creates an empty bare repo and returns a `file://` URL.

- [ ] **Step 1: Failing tests**
  - `test_mktree_commit_tree_orphan`: an orphan commit has no parents and `ls-tree` lists `README.md` and `bench.yaml`.
  - `test_copy_pushes_branches_and_tags`:
    - Setup: `FakeGitHub`, an `upstream` fixture as source, and `remote_fmt` pointing at the `remote` fixture.
    - Assert: the remote has `main` and `feature/x`, plus annotated tag `tinytapeout/ttgf0p2/tt_um_x` on the right SHA, and `fake.repos` contains the new repo.
  - `test_copy_idempotent`.
  - `test_resolve_clone_url_prefers_local_mirror` and `test_resolve_clone_url_falls_back_to_public`.
  - `test_copy_description_public`: no banned string, and the text names the strategy reason.
  - `test_copy_none_reports_no_source`.
- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Implement.** Port from `experiments/tools/bench_copy.py`, replacing hard-coded `/backups` paths with `LocalConfig.mirrors` and `data/restore_sources.toml`. Create repos via `gh.create_repo`, with the same `RateLimited` backoff as Task 6: extract `retry_rate_limited(fn, sleep, log, base_wait, max_wait)` into `fork.py` and reuse it.
- [ ] **Step 4: Run.** Expected: PASS.
- [ ] **Step 5: Commit** "Add copy/restore of repos that cannot be forks".

### Task 8: `about` branch content

**Files:**
- Create: `src/wafer_space_bench/about.py`, `src/wafer_space_bench/templates/about_README.md.j2`, `tests/test_about.py`, `tests/golden/about_README.md`

**Interfaces:**
- Consumes: Tasks 2, 3, 5, 7.
- Produces:
  - `about_data(plan, tapeouts, branches: list[str], now: str) -> dict` with keys `source{url, original_url, kind, why, licence, upstream_default_branch}`, `tapeouts[]`, `branches`, `generated`, `tooling`, `index`.
  - `render_readme(data: dict, repo_full: str) -> str`.
  - `write_about_commit(git: Git, data, readme) -> str | None`, which returns `None` when the tree is unchanged.

- [ ] **Step 1: Failing tests**
  - `test_readme_matches_golden`: the data comes from the `urish/tt-simon-game` plan with 4 tapeouts and `now="2026-10-17T00:00:00Z"`. Compare to `tests/golden/about_README.md`, derived from the pilot README in `experiments`, with absolute `https://github.com/<repo>/tree/<branch>` links.
  - `test_readme_links_absolute`: no `../../`.
  - `test_about_yaml_round_trips`.
  - `test_about_commit_unchanged_returns_none`.
  - `test_about_commit_is_orphan_then_linear`: the first commit has 0 parents; a changed second commit has the first as parent.
- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Implement.** Port from `experiments/tools/bench_migrate.py` (`bench_data`, `readme`, `about_commit`) and move the README text into the Jinja2 template.
- [ ] **Step 4: Run.** Expected: PASS.
- [ ] **Step 5: Commit** "Add about branch generation".

### Task 9: Branch layout

**Files:** Create `src/wafer_space_bench/layout.py`, `tests/test_layout.py`

**Interfaces:**
- Consumes: Tasks 2–8.
- Produces:
  - `LayoutPlan(upstream: dict[str, str], tapeout: list[str], delete: list[str], conflicts: list[str])`.
  - `plan_layout(heads: list[str], tags: list[str]) -> LayoutPlan`.
  - `apply_layout(plan, tapeouts, gh, cfg, org, *, remote_fmt, now, log) -> str`. It returns `"pending"` if the repo is not in the org yet, `"conflict: …"`, or `"ok: N upstream, M tapeout"`.
  - CLI: `wsbench layout [--only ...] [--dry-run]`.

- [ ] **Step 1: Failing tests**
  - `test_plan_layout_maps_original_branches`: `main` maps to `upstream/main`, and `feature/x` to `upstream/feature/x`.
  - `test_plan_layout_tapeout_from_tags`: only `<org>/<run>/<slot>` tags whose org is in `COMMIT_SOURCE`.
  - `test_layout_conflicts`: an upstream with branch `about`, or with both `x` and `x/y`, gives a non-empty `conflicts` and no changes to the remote.
  - `test_apply_layout_end_to_end`: the `upstream` fixture is pushed to a `remote` with a tag. Afterwards the remote heads are exactly `{about, upstream/*, tapeout/*}`, and `fake.default_branch[...] == "about"`.
  - `test_apply_layout_sets_default_before_delete`: `FakeGitHub` records call order.
  - `test_apply_layout_idempotent`.
  - `test_apply_layout_pending`: the repo is not in the fake.
- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Implement.** Port `migrate()` from `experiments/tools/bench_migrate.py`, adding the conflict check and the pending check.
- [ ] **Step 4: Run.** Expected: PASS.
- [ ] **Step 5: Commit** "Add branch layout: about, upstream/*, tapeout/*".

### Task 10: Index site

**Files:**
- Create in the tooling repo: `src/wafer_space_bench/index.py`, `tests/test_index.py`, `site/_config.yml`, `site/index.md`, `site/assets/table.js`
- Generated into the index repo checkout: `_data/repos.json`

**Interfaces:**
- Consumes: Tasks 3, 4, 5.
- Produces:
  - `index_rows(plans, tapeouts, org_repos: list[RepoInfo]) -> list[dict]`. Each row has keys:
    - `name, url, upstream, original_upstream, strategy`;
    - `state`, one of `pending`, `forked`, `laid-out`, `no-source`;
    - `tapeouts` (a list of tags), `runs` (sorted unique `org/run`), `licence`.
  - `write_site(rows, site_dir: Path, out_dir: Path)` copies `site/` and writes `_data/repos.json`.
  - CLI: `wsbench index --out PATH`.
- Site:
  - `index.md` explains why the forks exist: it reuses the purpose paragraph and the namespace table from the spec.
  - It renders a table from `site.data.repos` with columns repo, upstream, strategy, state, runs, tapeouts and licence.
  - `table.js` adds a client-side text filter. It is plain JS, with no external scripts.
  - `_config.yml` sets `title: wafer.space benchmark repositories` and `theme: minima`.

- [ ] **Step 1: Failing tests**
  - `test_index_states`: `FakeGitHub` repos with default branch `about` give `laid-out`; others give `forked`; a planned repo absent from the org gives `pending`; `strategy=none` gives `no-source`.
  - `test_index_rows_sorted_and_json_serialisable`.
  - `test_write_site_outputs`: `_data/repos.json`, `index.md` and `_config.yml` exist under `out_dir`.
- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run.** Expected: PASS. Then build locally to check it renders: `docker run --rm -v "$PWD/out:/srv/jekyll" jekyll/jekyll:4.2.2 jekyll build`. Nix/Docker only; never install Ruby on the host.
- [ ] **Step 5: Commit** "Add index site generator".

### Task 11: Cutover

**Files:**
- Modify: `experiments/tools/` (delete `bench_repos.py`, `bench_fork.py`, `bench_copy.py`, `bench_migrate.py`), `experiments/notes/DATA-INDEX.md`
- Modify: systemd user unit `bench-fork-resume`

- [ ] **Step 1: Create the GitHub repos.** Create `wafer-space/wafer.space-bench` (public) and `wafer-space-bench/wafer-space-bench.github.io` (public) using the Task 6 `retry_rate_limited`. Push the tooling repo.
- [ ] **Step 2: Verify CI.** `gh run watch` on the first push. Expected: the workflow passes on Python 3.11 and 3.13.
- [ ] **Step 3: Re-point the resume timer.** If it hasn't fired yet, stop it (`systemctl --user stop bench-fork-resume.timer`) and re-create it to run `uv run wsbench fork --delay 20` from the tooling checkout at the same time, `2026-10-18 12:45`. If it has already fired, let it finish and continue with the new CLI.
- [ ] **Step 4: Run the real operations,** in order, after the forks complete:
  - `uv run wsbench copy`;
  - `uv run wsbench layout`;
  - `uv run wsbench index --out ../wafer-space-bench.github.io`, then push that repo and enable Pages (`gh api -X POST repos/wafer-space-bench/wafer-space-bench.github.io/pages -f build_type=legacy -f 'source[branch]=main'`).

  Each run's progress is logged to `work/events.jsonl` and reported every 5 minutes with an ETA.
- [ ] **Step 5: Verify.**
  - `curl -sf https://wafer-space-bench.github.io/` returns 200.
  - `wsbench index` shows no `forked` rows left (everything `laid-out`, `pending` only for known gaps).
  - Spot-check 3 repos with `git ls-remote`.
- [ ] **Step 6: Clean up `experiments`.** Delete `experiments/tools/bench_*.py`, point `DATA-INDEX.md` at the new repo, and commit "Move bench tooling to wafer-space/wafer.space-bench".
