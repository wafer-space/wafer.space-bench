# wafer.space-bench tooling Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the one-off `experiments/tools/bench_*.py` scripts into a tested, CI-checked Python package (`wsbench`). It is the first content of `wafer-space/wafer.space-bench`. It finds benchmark repos, forks or copies them into `wafer-space-bench`, applies the branch layout, keeps them in sync with upstream from a scheduled Action, and generates the index site from a scheduled Pages workflow.

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
- **Automation:** `sync.yml` (daily, tooling repo, GitHub App token: `vars.BENCH_APP_ID`, `secrets.BENCH_APP_PRIVATE_KEY`, `owner: wafer-space-bench`) and `pages.yml` (every 6 h plus `repository_dispatch: bench-updated`, index repo, read-only `GITHUB_TOKEN`). No deploy keys. Generated index data is never committed. All workflow files pass `actionlint` in CI.
- **Sync never touches** `tapeout/*`, tags or `dev/*`; it only creates, force-updates and deletes `upstream/*` (deletes first) and regenerates `about`.
- **Conventions:** match `ws-run-reports` (src layout, hatchling, `[dependency-groups] dev`, `cli.py:main`). Licence Apache-2.0.
- **Commit trailer:** `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Review Focus

1. **A rate limit mid-run, then the run is re-run.** No duplicate repos or tags; the run finishes where it stopped. Covered by `test_fork_backoff_then_success` and `test_run_fork_resumes` (Task 6).
2. **An upstream branch that collides with our namespaces**, such as `about`, `upstream/x`, or both `x` and `x/y`. Report a conflict and change nothing in that repo. Covered by `test_layout_conflicts` (Task 9).
3. **A taped-out commit unreachable from the bench repo.** That tag is reported as `missing-commit`; other tags still get created. Covered by `test_tags_missing_commit` (Task 6).
4. **A local path leaking into a tag message, description, README or index.** The privacy tests fail. Covered by `test_no_banned_strings_in_tracked_files` (Task 1), `test_tag_message_has_no_local_paths` (Task 6) and `test_copy_description_public` (Task 7).
5. **An upstream renamed between scan and fork.** The target uses the current name, and tags still match the catalog's original URL. Covered by `test_plan_renamed_repo` (Task 5) and `test_tapeouts_match_original_url` (Task 3).
6. **Upstream force-pushes, deletes or renames a branch between syncs.** `upstream/*` follows exactly; `tapeout/*` and tags keep the taped-out commits. Covered by `test_sync_fork_uses_refs_api` and `test_sync_never_touches_tapeout_or_dev` (Task 11).

---

### Task 1: Repository scaffold, CI and privacy test

**Files:**
- Create: `pyproject.toml`, `README.md`, `LICENSE` (Apache-2.0), `.gitignore`, `src/wafer_space_bench/__init__.py`, `src/wafer_space_bench/cli.py`, `.github/workflows/ci.yml`, `tests/test_privacy.py`, `tests/test_cli.py`
- Already present: the spec and this plan (imported in the first commit)

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
- [ ] **Step 2: `.gitignore`.** Ignore `bench.local.toml`, `*.jsonl`, `.venv/`, `__pycache__/`, `work/`, `.superpowers/`, `out/`, `_site_src/`.
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
  - A separate `actionlint` job: `docker run --rm -v "$PWD:/repo" -w /repo rhysd/actionlint:1.7.7 -color .github/workflows/*.yml` (later tasks add `index-repo/.github/workflows/*.yml` to the list). Run it locally the same way, in Docker.
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
    - `list_repos(org) -> list[RepoInfo]` (paginated);
    - `list_branches(full_name) -> dict[str, str]` (branch name → SHA, paginated);
    - `set_ref(full_name, ref, sha) -> None`: `ref` like `heads/upstream/main`; creates it, or force-updates it if it exists;
    - `delete_ref(full_name, ref) -> None`;
    - `get_file(full_name, path, ref) -> str | None`: decoded text via the contents API, `None` on 404;
    - `dispatch(full_name, event_type) -> None`: `repository_dispatch`.
  - `GhCli(runner=subprocess.run)` implements it with `gh api`.
  - `tests/fakes.py::FakeGitHub` implements it in memory:
    - `add_upstream(full, network=None, commits=(), branches=None, deleted=False, renamed_to=None, archived=False)`; `branches` is `{name: sha}`, and its SHAs are added to `commits`;
    - `rate_limit_next(n)`, which makes the next `n` creations raise `RateLimited`;
    - `ready_after(n)`, which makes `branches_visible` false for the first `n` polls;
    - inspection dicts `.repos`, `.tags`, `.branches` (`{full: {name: sha}}`), `.files` (`{(full, ref, path): text}`), `.default_branch`, `.dispatches` (list of `(full, event_type)`), and `.calls`, an ordered list of `(method, args)` for every mutating call.
  - The fake enforces these GitHub rules:
    - One repo per network per org. A second fork raises `GitHubError("already forked in network")`.
    - A fork starts with a copy of the upstream's branches and can reference every commit in the network.
    - `set_ref` or `create_tag` with a SHA not in the repo's network raises `GitHubError`.
    - `delete_ref` of the default branch raises `GitHubError`.

- [ ] **Step 1: Failing tests**
  - **GhCli:** `runner` is replaced by a stub returning `CompletedProcess`.
    - `test_ghcli_fork_args`: asserts `["gh","api","-X","POST","repos/a/b/forks","-f","organization=o","-f","name=a--b","-F","default_branch_only=false"]`.
    - `test_ghcli_rate_limited`: stderr `"gh: was submitted too quickly (HTTP 403)"` raises `RateLimited`.
    - `test_ghcli_not_found`: stderr containing `HTTP 404` raises `NotFound`.
    - `test_ghcli_get_repo_follows_source`: `source.full_name` becomes `network`.
  - **Contract tests**, parametrised over `FakeGitHub` only:
    - `test_fake_one_fork_per_network`;
    - `test_fake_rate_limit_then_ok`;
    - `test_fake_renamed_repo_reports_current_name`;
    - `test_fake_set_ref_rejects_foreign_sha`;
    - `test_fake_cannot_delete_default_branch`.
  - `test_ghcli_list_branches_paginates`: the stub returns two pages (`--paginate` output concatenated as `gh` prints it); the result has every branch.
  - `test_ghcli_set_ref_updates_then_creates`: the PATCH (`force=true`) gets a 422 "Reference does not exist", so a POST to `git/refs` follows.
  - `test_ghcli_get_file_decodes_base64`.
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

### Task 10: Index site and its Pages workflow

**Files:**
- Create in the tooling repo:
  - `src/wafer_space_bench/index.py`, `tests/test_index.py`;
  - `site/_config.yml`, `site/index.md`, `site/assets/table.js`;
  - `index-repo/README.md` and `index-repo/.github/workflows/pages.yml`: the whole content of the index repo, pushed there in Task 12.
- Modify: `.github/workflows/ci.yml` to add `index-repo/.github/workflows/*.yml` to actionlint.
- Generated at build time only, never committed: `<out>/_data/repos.json`.

**Interfaces:**
- Consumes: Tasks 3, 4, 5, 8 (`bench.yaml` keys).
- Produces:
  - `index_rows(plans, tapeouts, gh, org) -> list[dict]`. It reads the org via `gh.list_repos(org)` and, per repo, `gh.get_file(full, "bench.yaml", "about")`. Each row has keys:
    - `name, url, upstream, original_upstream, strategy`;
    - `state`, one of `pending`, `created`, `laid-out`, `no-source`;
    - `tapeouts` (a list of tags), `runs` (sorted unique `org/run`), `licence`, `branches` (count of `upstream/*` from `bench.yaml`), `generated`.
  - For a `laid-out` repo, tapeouts, licence and source come from its `bench.yaml` (the repo is the truth). For other states they come from the plans and catalog.
  - `write_site(rows, site_dir: Path, out_dir: Path)` copies `site/` and writes `_data/repos.json`.
  - CLI: `wsbench index --out PATH`.
- `index-repo/.github/workflows/pages.yml`:
  - Triggers: `schedule` (`cron: "23 */6 * * *"`), `workflow_dispatch`, `repository_dispatch` (`types: [bench-updated]`).
  - Permissions: `contents: read`, `pages: write`, `id-token: write`. Concurrency group `pages`.
  - Steps:
    1. `actions/checkout@v4` of `wafer-space/wafer.space-bench`.
    2. `astral-sh/setup-uv@v6`, then `uv sync --locked`.
    3. `uv run wsbench index --out _site_src`, with `GH_TOKEN: ${{ github.token }}`.
    4. `actions/jekyll-build-pages@v1` (`source: _site_src`).
    5. `actions/upload-pages-artifact@v3`.
    6. `actions/deploy-pages@v4`, in the `github-pages` environment.
- `index-repo/README.md`: one paragraph. The site is generated; edit it in `wafer-space/wafer.space-bench` (`site/`, `index-repo/`), not here.
- Site:
  - `index.md` explains why the forks exist: it reuses the purpose paragraph and the namespace table from the spec.
  - It renders a table from `site.data.repos` with columns repo, upstream, strategy, state, runs, tapeouts and licence.
  - `table.js` adds a client-side text filter. It is plain JS, with no external scripts.
  - `_config.yml` sets `title: wafer.space benchmark repositories` and `theme: minima`.

- [ ] **Step 1: Failing tests**
  - `test_index_states`: a `FakeGitHub` repo with default branch `about` and a `bench.yaml` gives `laid-out`; one without gives `created`; a planned repo absent from the org gives `pending`; `strategy=none` gives `no-source`.
  - `test_index_reads_bench_yaml`: a laid-out repo whose `bench.yaml` lists a tapeout the catalog lacks shows that tapeout. The repo is the truth.
  - `test_index_bad_bench_yaml_is_created_not_crash`: unparseable YAML gives `created`, and the run continues.
  - `test_index_rows_sorted_and_json_serialisable`.
  - `test_write_site_outputs`: `_data/repos.json`, `index.md` and `_config.yml` exist under `out_dir`.
- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Implement** the module, `site/` and `index-repo/`.
- [ ] **Step 4: Run.** Expected: PASS.
  - Build locally to check the site renders: `docker run --rm -v "$PWD/out:/srv/jekyll" jekyll/jekyll:4.2.2 jekyll build`. Use Docker only; never install Ruby on the host.
  - actionlint passes on the new workflow.
- [ ] **Step 5: Commit** "Add index site generator and Pages workflow".

### Task 11: Sync and its scheduled workflow

**Files:** Create `src/wafer_space_bench/sync.py`, `tests/test_sync.py`, `.github/workflows/sync.yml`. Modify `cli.py` to add the global option `--remote-fmt` (default `git@github.com:{full}.git`).

**Interfaces:**
- Consumes: Tasks 2–9 (`apply_layout`, `about_data`, `render_readme`, `write_about_commit`, `ensure_tags`, `retry_rate_limited`).
- Produces:
  - `MirrorPlan(set: dict[str, str], delete: list[str])`: bench-repo branch name → SHA.
  - `plan_mirror(upstream: dict[str, str], current: dict[str, str]) -> MirrorPlan`. `current` is the bench repo's branches; only its `upstream/*` entries are considered. Unchanged SHAs are omitted.
  - `sync_repo(plan, tapeouts, gh, cfg, org, *, remote_fmt, now, dry_run, log) -> str`. It returns one of:
    - `"pending"` (not in the org);
    - `"laid-out"` (it just ran `apply_layout`);
    - `"upstream-missing"`;
    - `"skipped: restore"`;
    - `"unchanged"`;
    - `"ok: +A ~U -D[, about][, N tags]"`;
    - `"error: …"`.
  - `run_sync(plans, tapeouts, gh, cfg, org, *, only, dry_run, remote_fmt, now, log, index_repo="wafer-space-bench/wafer-space-bench.github.io") -> int`. It returns the number of failed repos. If anything changed and it is not a dry run, it calls `gh.dispatch(index_repo, "bench-updated")` once at the end.
  - CLI: `wsbench sync [--only NAME ...] [--dry-run]`. The exit code is 1 if any repo failed.
- How mirroring works:
  - `fork`: `gh.list_branches(current_name)` against `gh.list_branches(bench repo)`. Apply `delete_ref` for every delete first, then `set_ref`. No clone.
  - `copy`: `git fetch --prune` from the upstream URL into the bare mirror `cfg.work_dir/mirrors/<bench name>.git`, then push `+refs/heads/<b>:refs/heads/upstream/<b>` plus a delete refspec per removed branch. GitLab copies work the same way.
  - `about`: fetch only `refs/heads/about` from the bench repo into that bare repo, then call `write_about_commit`. Push only when it returns a new SHA.
- `.github/workflows/sync.yml`:
  - Triggers: `schedule` (`cron: "17 3 * * *"`); `workflow_dispatch` with inputs `only` (string, default empty) and `dry_run` (boolean, default false).
  - Concurrency: group `sync`, `cancel-in-progress: false`. `timeout-minutes: 300`. Permissions: `contents: read`.
  - Steps:
    1. `actions/create-github-app-token@v2` with `app-id: ${{ vars.BENCH_APP_ID }}`, `private-key: ${{ secrets.BENCH_APP_PRIVATE_KEY }}`, `owner: wafer-space-bench`.
    2. Checkout, `astral-sh/setup-uv@v6`, `uv sync --locked`.
    3. `gh auth setup-git`, with `GH_TOKEN` set to the app token.
    4. `actions/cache@v4`: path `work/mirrors`, key `mirrors-${{ github.run_id }}`, restore-keys `mirrors-`.
    5. `uv run wsbench --remote-fmt 'https://github.com/{full}.git' sync` with the inputs, logging to `work/events.jsonl`.
    6. `actions/upload-artifact@v4` of `work/events.jsonl` with `if: always()`.
  - Workflow inputs reach the shell only through `env:` (no `${{ }}` inside `run:`), so a crafted `only` input cannot inject commands.

- [ ] **Step 1: Failing tests** (FakeGitHub; real git for copy)
  - `test_plan_mirror_create_update_delete`: upstream `{main: B, new: C}`, current `{upstream/main: A, upstream/old: X, about: Y, tapeout/…: Z}` gives `set={upstream/main: B, upstream/new: C}` and `delete=[upstream/old]`.
  - `test_sync_fork_uses_refs_api`: upstream gains a branch, force-pushes `main`, and deletes a branch. Afterwards `fake.branches[bench]` has `upstream/*` matching upstream exactly, and in `fake.calls` every `delete_ref` comes before the first `set_ref`.
  - `test_sync_never_touches_tapeout_or_dev`: `tapeout/*`, `dev/*` and tags are identical before and after, even when upstream deleted the branch the tapeout came from.
  - `test_sync_copy_fetches_and_pushes`: the `upstream` fixture gains a commit on `main`; after sync, the remote's `upstream/main` is the new SHA.
  - `test_sync_upstream_missing_changes_nothing`: there are no mutating calls.
  - `test_sync_restore_skipped`.
  - `test_sync_not_laid_out_runs_layout`: the result is `"laid-out"`, and the default branch is `about`.
  - `test_sync_about_only_on_change`: run twice with the same `now`. The second run is `"unchanged"`, with no push and no new `about` commit.
  - `test_sync_adds_new_tapeout_tags`: a catalog tapeout missing from the repo gets its tag and its `tapeout/*` branch.
  - `test_sync_dry_run_changes_nothing`.
  - `test_run_sync_continues_after_error_and_dispatches_once`: the 1st of 3 repos raises `GitHubError` on `set_ref`. The others are synced, the return value is 1, and `fake.dispatches == [(index_repo, "bench-updated")]`.
  - `test_run_sync_no_change_no_dispatch`.
  - `test_sync_workflow_inputs_via_env`: parse `sync.yml` with PyYAML; no `run:` string contains `${{`.
- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Implement** `sync.py`, the CLI and `sync.yml`.
- [ ] **Step 4: Run** `uv run pytest -q`, then actionlint in Docker. Expected: PASS.
- [ ] **Step 5: Commit** "Add sync of upstream branches and scheduled sync workflow".

### Task 13: Repository settings (runs before the Task 12 cutover)

**Files:** Create `src/wafer_space_bench/settings.py`, `tests/test_settings.py`. Modify `github.py` and `tests/fakes.py` (new Protocol methods), `index.py` and `site/index.md` (row anchors), `.github/workflows/sync.yml` (a settings step per shard), and README.

**Interfaces:**
- Consumes: Tasks 2–11 (RepoPlan, tapeouts_by_repo, GitHub, retry_rate_limited, plan_matches, the shard selection from sync).
- Produces:
  - `DesiredSettings(repo: dict, topics: list[str], actions_enabled: bool, vulnerability_alerts: bool, ruleset: dict)`.
  - `desired_settings(plan, tapeouts, index_url="https://wafer-space-bench.github.io") -> DesiredSettings`. This is pure; the values come from the spec's Repository settings table.
  - `topic_for(name: str) -> str`: lowercase; anything outside `[a-z0-9-]` becomes `-`; runs of `-` collapse; trimmed to 50 chars. For example, `wafer.space` becomes `wafer-space`.
  - `diff_settings(current, desired) -> dict[str, tuple]`, mapping each field that differs to `(current, desired)`.
  - `apply_settings(plan, tapeouts, gh, org, *, dry_run, sleep, log) -> str`. It returns `"pending"`, `"unchanged"`, `"dry-run: <fields>"`, `"ok: <fields>"` or `"error: …"`.
  - `run_settings(plans, tapeouts, gh, org, *, only, shard, dry_run, delay, sleep, log) -> int`, which returns the number of failures.
  - CLI: `wsbench settings [--only …] [--shard K/N] [--dry-run] [--delay S]`.
  - GitHub Protocol additions, each in both GhCli and FakeGitHub:
    - `get_repo_settings(full) -> dict`, with keys `description, homepage, has_issues, has_projects, has_wiki, has_discussions, has_pull_requests` (`has_pull_requests` is None when the API doesn't report it);
    - `update_repo(full, fields: dict)`, a PATCH;
    - `get_topics(full) -> list[str]` and `set_topics(full, names)`;
    - `get_actions_enabled(full) -> bool` and `set_actions_enabled(full, enabled)`;
    - `get_vulnerability_alerts(full) -> bool` (204 means on, 404 means off) and `set_vulnerability_alerts(full, enabled)` (PUT or DELETE; also DELETE `automated-security-fixes`);
    - `list_rulesets(full) -> list[dict]` and `put_ruleset(full, ruleset: dict)`, which creates or updates the ruleset by name.
- Pull requests: send `has_pull_requests: false` only if `get_repo_settings` reports the key. Otherwise log `pull-requests-unsupported` once per run and leave them alone. Whether the live API supports it is verified at cutover.
- Ruleset: target `branch` with include `refs/heads/tapeout/**`, plus a second ruleset of target `tag` with the three tapeout namespaces. Rules: `deletion`, `non_fast_forward`, `update`. Enforcement `active`, no bypass actors. Names: `bench-frozen-branches` and `bench-frozen-tags`.

- [ ] **Step 1: Failing tests**
  - `test_desired_settings_fork_copy_restore`: exact description, homepage and topics for one plan of each strategy. The description has no banned strings and is at most 350 chars.
  - `test_topic_for_normalises`: `wafer.space` becomes `wafer-space`; also covers case, punctuation and the length cap.
  - `test_apply_settings_only_changes_diffs`: a second run returns `"unchanged"`, and the fake records no writes.
  - `test_apply_settings_dry_run_no_writes`.
  - `test_apply_settings_pending_when_absent`.
  - `test_pull_requests_left_alone_when_unsupported`.
  - `test_frozen_rulesets_shape`: the rules, the targets, and no bypass. Creation of `tapeout/*` is still allowed: a FakeGitHub that enforces rulesets lets `set_ref` create a new `heads/tapeout/x` but refuses to move or delete it. Add ruleset enforcement to the fake for this.
  - `test_layout_and_sync_work_under_rulesets`: rerun the end-to-end fork → copy → layout → sync scenario with the rulesets in force. No refused writes.
  - `test_run_settings_continues_after_error`.
  - `test_index_rows_have_anchor_ids`.
  - `test_sync_workflow_runs_settings`: each shard job runs `wsbench settings --shard` after sync, with inputs passed via env.
  - GhCli argument tests for each new method, mirroring the existing ones.
- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Implement.** Wrap every write in `retry_rate_limited`. Pace with `delay` (default 1 s) after repos that changed.
- [ ] **Step 4: Run** `uv run pytest -q`, ruff, and actionlint in Docker. Expected: PASS.
- [ ] **Step 5: Commit** "Add repository settings command".

### Task 12: Cutover (needs Tim: creates repos, an App, secrets and public pushes)

- [ ] **Step 0 (Tim):**
  - Create the GitHub App `wafer-space-bench-bot` with the permissions in the spec's Automation section.
  - Install it on all repos of `wafer-space-bench`.
  - Add `BENCH_APP_ID` (variable) and `BENCH_APP_PRIVATE_KEY` (secret) to `wafer-space/wafer.space-bench`.
  - Set the `wafer-space-bench` org Actions policy to "selected repositories": only `wafer-space-bench.github.io`.

**Files:**
- Modify: `experiments/tools/` (delete `bench_repos.py`, `bench_fork.py`, `bench_copy.py`, `bench_migrate.py`), `experiments/notes/DATA-INDEX.md`
- Modify: systemd user unit `bench-fork-resume`

- [ ] **Step 1: Create the GitHub repos.** Create `wafer-space/wafer.space-bench` (public) and `wafer-space-bench/wafer-space-bench.github.io` (public) using the Task 6 `retry_rate_limited`. Push the tooling repo.
- [ ] **Step 2: Verify CI.** `gh run watch` on the first push. Expected: the workflow passes on Python 3.11 and 3.13.
- [ ] **Step 3: Re-point the resume timer.** If it hasn't fired yet, stop it (`systemctl --user stop bench-fork-resume.timer`) and re-create it to run `uv run wsbench fork --delay 20` from the tooling checkout at the same time, `2026-10-18 12:45`. If it has already fired, let it finish and continue with the new CLI.
- [ ] **Step 4: Run the real operations,** in order, after the forks complete:
  - `uv run wsbench copy`;
  - `uv run wsbench layout`;
  - `uv run wsbench settings --dry-run --only <3 repos>`, check the diffs, then `uv run wsbench settings`. Confirm on one repo whether the API accepted `has_pull_requests` (log `pull-requests-unsupported` otherwise).
  - Push `index-repo/` as the content of `wafer-space-bench/wafer-space-bench.github.io`.
  - Enable Pages with `gh api -X POST repos/wafer-space-bench/wafer-space-bench.github.io/pages -f build_type=workflow`.
  - Run `gh workflow run pages.yml` there, then `gh workflow run sync.yml -f dry_run=true` in the tooling repo. Check both runs succeed before the first scheduled real sync.

  Each run's progress is logged to `work/events.jsonl` and reported every 5 minutes with an ETA.
- [ ] **Step 5: Verify.**
  - `curl -sf https://wafer-space-bench.github.io/` returns 200.
  - `wsbench index` shows no `created` rows left (everything `laid-out`, `pending` only for known gaps).
  - Spot-check 3 repos with `git ls-remote`.
- [ ] **Step 6: Clean up `experiments`.** Delete `experiments/tools/bench_*.py`, point `DATA-INDEX.md` at the new repo, and commit "Move bench tooling to wafer-space/wafer.space-bench".
