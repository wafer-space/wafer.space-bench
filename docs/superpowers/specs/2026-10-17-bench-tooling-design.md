# wafer.space-bench tooling: design

Status: agreed decisions from 2026-10-17. This is the spec the plan
`docs/superpowers/plans/2026-10-17-bench-tooling.md` implements.

## Purpose

Maintain copies of every benchmark design repository in the GitHub org
`wafer-space-bench`, with provenance, tapeout tags and a consistent branch
layout, and publish an index of them. This replaces the one-off scripts in
`experiments/tools/bench_*.py` with a tested, CI-checked Python package. The
package is the first content of the public repo `wafer-space/wafer.space-bench`.

## Repositories

| Repo | Visibility | Holds |
|---|---|---|
| `wafer-space/wafer.space-bench` | public | this tooling, the design catalog, public notes and results |
| `wafer-space/wafer.space-bench-private` | private | NDA and other private material (not this plan) |
| `wafer-space-bench/wafer-space-bench.github.io` | public | the Pages workflow that builds the index; its source lives in the tooling repo's `index-repo/` |
| `wafer-space-bench/<owner>--<repo>` | public | one per upstream benchmark repo |

## Naming

- Bench repo name: `<owner>--<repo>` of the **current** upstream name (after renames).
- Tapeout tag: annotated tag `<org>/<run>/<slot>`.
  - `org` is one of `wafer.space`, `google`, `tinytapeout`.
  - `run` uses the names on the wafer.space website: `ws-run1`, `ws-run2`, `gf180mcu-mpw0`, `gf180mcu-mpw18h1`, `ttgf0p1`, `ttgf0p2`, `ttgf0p3`, `ttgf26a`, `ttgf26b`, `ttgf26c`, `ttgf26d`.
  - `slot` is the run manifest CODE, or the `tt_um_*` name for Tiny Tapeout.
- The tag message states the source repo URL and where the commit came from:
  - `wafer.space`: "design_ref head recorded by the ws-run1/2 audit"
  - `google`: "designer commit from Efabless tapeout/logs/git.info"
  - `tinytapeout`: "commit from the shuttle repo's projects/<name>/commit_id.json"
- Never use short shuttle codes.

## Branch namespaces in every bench repo

| Ref | Meaning |
|---|---|
| `about` | Default branch. Orphan history. Holds `bench.yaml` (source, kind, why, licence, tapeouts, branches, generated time) and `README.md` generated from it. |
| `upstream/<branch>` | Exact mirror of the upstream branch. Only the sync job writes it. |
| `tapeout/<org>/<run>/<slot>` | Frozen. Same commit as the tag of the same name. |
| `dev/repro/<org>/<run>/<slot>` | Reproduce the original result with the original pinned tools. |
| `dev/upgrade/<org>/<run>/<slot>` | Latest development LibreLane + gf180mcu PDK. |
| `dev/process/<config>/<org>/<run>/<slot>` | Alternative process config (`m035n3`, `i135x5`, …). |
| `dev/<topic>` | Other development. |

The README links to the index (`https://wafer-space-bench.github.io`) and the
tooling repo (`https://github.com/wafer-space/wafer.space-bench`).

## Copy strategies

| Strategy | When |
|---|---|
| `fork` | First repo of each GitHub fork network. |
| `copy` | GitHub repo whose fork network already has a bench repo (GitHub allows one per network per org), or any GitLab repo. Plain repo, pushed from a local bare clone. |
| `restore` | Upstream deleted. Plain repo pushed from a local archived mirror. |
| `none` | No source known. Reported, not created. |

## GitHub behaviour to respect

- **Repo creation (forks and new repos) has an undocumented secondary limit.**
  About 150 in 30 minutes, then HTTP 403 "was submitted too quickly" for over
  20 minutes. When refused, back off 60 s, doubling to a 960 s cap, and retry
  the same repo. Pace creations 20 s apart by default.
- **Forks are created asynchronously.** Wait until the fork's branches are
  visible before tagging.
- **A tag in a fork may point at any commit in the fork network.**
- **The default branch cannot be deleted.** Set `about` as the default before
  deleting the original branch names.

## Operations

All are idempotent. Re-running after a partial failure finishes the job. Each
action appends one JSON line to an event log.

1. `scan`: read-only. Query each upstream and plan a strategy per repo.
2. `fork`: create forks and tapeout tags.
3. `copy`: create copy/restore repos, push branches and tags.
4. `layout`: apply the branch namespaces and generate the `about` branch.
   Branches are written with git, not the REST refs API (one API write per ref
   would eat the secondary write limit): from the repo's local bare clone
   (seeded from a local mirror when there is one, which is only read), one
   atomic push creates every `upstream/*` and `tapeout/*` branch, the default
   branch is set to `about` through the API, then one atomic push with
   `--force-with-lease` deletes the original branches that have not moved.
5. `sync`: keep existing bench repos current (see Automation).
6. `index`: generate the index site data by reading the bench repos.

`scan`, `fork` and `copy` are run by hand (they create repos, hit the
creation rate limit, and `restore` needs local mirrors). `sync` and `index`
run unattended on a schedule.

## Automation (GitHub Actions)

| Workflow | Lives in | Schedule | Auth | Does |
|---|---|---|---|---|
| `sync.yml` | `wafer-space/wafer.space-bench` | daily, plus manual dispatch (`only`, `dry_run` inputs) | GitHub App installation token | `wsbench sync`, then triggers the index rebuild |
| `ci.yml` | `wafer-space/wafer.space-bench` | push, pull request | none | lint, format, tests, actionlint |
| `pages.yml` | `wafer-space-bench/wafer-space-bench.github.io` | every 6 hours, manual, `repository_dispatch: bench-updated` | the workflow's own read-only `GITHUB_TOKEN` | check out the tooling repo, `wsbench index`, Jekyll build, deploy to Pages |

**`sync` per bench repo:**
- Not laid out yet: run `layout`.
- Mirror upstream branches to `upstream/<b>`: create, force-update and delete
  so `upstream/*` exactly matches upstream. A `fork` mirrors through the REST
  refs API (the commits are already in the fork network, so no clone). A
  `copy` of a live upstream fetches it and pushes. A `restore` has no
  upstream and is skipped.
- Upstream gone (404): change nothing, log `upstream-missing`.
- Deletes are applied before creates, so an upstream branch renamed from `x`
  to `x/y` does not collide with the old `upstream/x`.
- One repo failing (API or git error) is logged and the run continues; the
  command exits non-zero at the end if any repo failed.
- Create any tapeout tags and `tapeout/*` branches the catalog has that the repo lacks.
- Regenerate `about` when the branch list or tapeouts changed; no commit otherwise.
- Never touch `tapeout/*`, tags or `dev/*`.

**Index pulls from the org:** it lists the org's repos and reads each repo's
`about:bench.yaml` through the contents API. Repos planned in
`data/bench_repos.csv` but absent from the org appear as `pending`. The
generated data is never committed; Pages is built by the workflow
(`build_type=workflow`).

**Delegated auth: one GitHub App** (`wafer-space-bench-bot`) installed on all
repos of `wafer-space-bench`, not deploy keys (one key per repo would mean
hundreds of keys). Repository permissions: Administration read/write (default
branch), Contents read/write, Workflows read/write (mirrored branches may
change `.github/workflows`), Metadata read. The tooling repo holds variable
`BENCH_APP_ID` and secret `BENCH_APP_PRIVATE_KEY`. The workflow mints a token
with `actions/create-github-app-token` (`owner: wafer-space-bench`) and runs
`gh auth setup-git` so git pushes use it.

**Actions are disabled in the bench repos** (org policy: enabled only for the
index repo). Otherwise every mirrored branch push would run the upstream
designs' CI in our org.

## Repository settings (`wsbench settings`)

The bench repos are archival mirrors, not places for collaboration. One idempotent
command brings every bench repo to the settings below. It reads the current
state, changes only what differs, supports `--dry-run`, and also runs daily
as part of `sync.yml` so newly created repos are fixed automatically.

| Setting | Value | Why |
|---|---|---|
| Issues, Projects, Wiki, Discussions | off | Reports belong upstream or in the tooling repo |
| Pull requests | off where the API allows it; otherwise left alone and reported | Same; changes go through `dev/*` by the bench team |
| Description | `Benchmark copy of <upstream URL>: <N> tapeout(s) on <runs>` (≤ 350 chars), or `… (upstream deleted)` for restores | Says what the repo is at a glance |
| Homepage | `https://wafer-space-bench.github.io/#<repo name>` (the index row) | Links back to the index |
| Topics | `wafer-space-bench`, `gf180mcu`, `open-silicon`, the strategy (`fork`, `copy` or `restore`), the tapeout org (`wafer-space`, `google` or `tinytapeout`) and each run (`ws-run1`, `gf180mcu-mpw0`, `ttgf0p2`, …) | Makes the org searchable and filterable |
| Actions | disabled for the repo | Belt and braces with the org policy; mirrored upstream CI never runs |
| Dependabot alerts and security updates | off | Mirrors of frozen tapeout code; avoids alert noise and bot PRs |
| Rulesets `bench-frozen-branches` and `bench-frozen-tags` (a ruleset has one target) | block update, deletion and force-push of `refs/heads/tapeout/**` (branches) and of tags `refs/tags/wafer.space/**`, `refs/tags/google/**`, `refs/tags/tinytapeout/**` (tags); creation stays allowed | Enforces "frozen" on GitHub itself; the only bypass actor is the bench App, so the tooling can correct a wrong tapeout ref |
| Forking | left allowed | Others may build on the bench repos |

The index gives each row an anchor `id` equal to the repo name so the homepage link lands on it.
Every published string obeys the privacy rule.

## Privacy rule

**Nothing committed to the public repos, and nothing published (tag messages,
repo descriptions, `about` content, index), may contain local paths.** Banned:
`/backups`, `/home/`, `big-storage`, `~/github`, `/tmp/claude`. Local mirror
locations come only from a git-ignored local config file.

## Testing

- **No network in tests.** GitHub is replaced by an in-memory fake that
  enforces the fork-network rule, async fork readiness and injected rate
  limits.
- **Git operations run for real** against temporary repositories on disk, with
  `file://` remotes.
- **Sleeps are injected,** so backoff is tested without waiting.
- **CI on GitHub Actions** runs lint, format check, tests and `actionlint` on
  every push and pull request.
