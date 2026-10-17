# wafer.space-bench

Tooling (`wsbench`) that keeps a copy of every benchmark design repository in the GitHub
org [`wafer-space-bench`](https://github.com/wafer-space-bench), with provenance, tapeout
tags and one branch layout, and publishes an index of them at
<https://wafer-space-bench.github.io>.

The design is in [`docs/superpowers/specs/2026-10-17-bench-tooling-design.md`](docs/superpowers/specs/2026-10-17-bench-tooling-design.md).

## Requirements

- Python 3.11 or newer and [uv](https://docs.astral.sh/uv/).
- git 2.29 or newer (tapeout tags are protected with negative refspecs).
- The [GitHub CLI](https://cli.github.com/) `gh`, logged in (`gh auth login`) as a member
  of `wafer-space-bench` who may create repositories there. Every GitHub API call goes
  through `gh api`.
  - Classic token scopes: `repo` (create repos and forks in the org, push, refs, tags,
    and the default branch, which needs admin rights on the repo; you get them on repos
    you create), `workflow` (mirrored branches contain `.github/workflows` files) and
    `read:org`.
  - A fine-grained token instead needs, on all repositories of the org: Administration
    read/write (default branch), Contents read/write, Workflows read/write, Metadata read.
- git push access to the org. By default remotes are SSH (`git@github.com:{full}.git`),
  run with `ssh -o BatchMode=yes`, so an SSH key with write access to the org must be
  loaded; nothing prompts. To use HTTPS with the `gh` login instead:

      gh auth setup-git
      uv run wsbench --remote-fmt 'https://github.com/{full}.git' <command> ...

Install and look around:

    uv sync
    uv run wsbench --help
    uv run wsbench <command> --help

## Global options go before the command

    uv run wsbench [--org ORG] [--catalog CSV] [--plans CSV] [--log JSONL] [--config TOML] [--remote-fmt FMT] <command> [command options]

| Option | Default | Meaning |
|---|---|---|
| `--org` | `wafer-space-bench` | org holding the bench repos |
| `--catalog` | `data/designs.csv` | the design catalog |
| `--plans` | `data/bench_repos.csv` | one plan per source repo, written by `scan` |
| `--log` | `work/events.jsonl` | the event log |
| `--config` | `bench.local.toml` | machine-local settings (see below) |
| `--remote-fmt` | `git@github.com:{full}.git` | git URL of a bench repo; `{full}` is `org/name` |

`uv run wsbench fork --org x` is an error: write `uv run wsbench --org x fork`.

## Running it

Run from the repository root (paths default to `data/`, `work/` and `site/` there). The
first four commands are run by hand, in this order. Each is idempotent, so re-running
after a failure finishes the job.

1. `wsbench scan`: read-only. Looks up every catalog repo on GitHub and in the org and
   writes `data/bench_repos.csv`, one plan per source repo with a strategy:
   - `fork`: the first repo of a fork network (GitHub allows one fork per network per
     org). An existing fork in the org stays its network's fork.
   - `copy`: a later repo of an already forked network, or a GitLab repo; pushed as a
     plain repo.
   - `restore`: the upstream was deleted; pushed from a public shuttle slot repository
     (`data/restore_sources.toml`) or a local mirror.
   - `none`: the catalog has no repository for the design. Shown in the index as
     `no-source`, never created.

   A bench repo that already exists keeps its name: when an upstream is renamed after it
   was copied, `scan` logs `rename` and keeps the old target. Review the CSV diff before
   going on.
2. `wsbench fork`: creates the forks and their tapeout tags (through the API).
3. `wsbench copy`: creates the copy and restore repos from a local bare clone under
   `work/mirrors/`, tags the taped-out commits and pushes branches and tags. A restore
   needs a source: a deleted repo with neither a restore source nor a local mirror ends
   as `no source`, which counts as a failure.
4. `wsbench layout`: gives each bench repo the branch layout: an orphan `about` branch
   (the default; `bench.yaml` and a README generated from it), `upstream/<branch>` for
   every original branch, and `tapeout/<org>/<run>/<slot>` for every tapeout tag.
   Original branches are deleted only after every new branch is verified and `about` is
   the default branch.

The other three run unattended in GitHub Actions (see Automation), and can be run by hand:

5. `wsbench sync`: per bench repo, mirrors upstream's branches onto `upstream/*`
   (deletes first), adds new tapeout tags and `tapeout/*` branches, and regenerates
   `about` when it changed. Tapeout tags and branches are never moved, and nothing
   outside `upstream/*` is deleted. `--shard K/N` runs one of N slices; `--no-dispatch`
   and `--changed-file` are for the workflow.
6. `wsbench index --out DIR`: writes the Jekyll source of the index site: the `site/`
   template (or `--site DIR`) plus `_data/repos.json`, read from the org.
7. `wsbench settings`: brings every bench repo to the archival-mirror settings. It reads
   each repo's current state and writes only what differs, so a second run writes
   nothing; a repo not created yet is `pending`.
   - Issues, Projects, Wiki and Discussions off. Pull requests off where the API reports
     `has_pull_requests`; otherwise they are left alone and `pull-requests-unsupported`
     is logged once.
   - Description `Benchmark copy of <upstream URL>: <N> tapeout(s) on <runs>`, with
     ` (upstream deleted)` for a restore; homepage
     `https://wafer-space-bench.github.io/#<repo name>`, the repo's row in the index.
   - Topics `wafer-space-bench`, `gf180mcu`, `open-silicon`, the strategy, the tapeout
     orgs and runs (`wafer-space`, `ws-run1`, `ttgf0p2`, ...).
   - Actions disabled; Dependabot alerts and security updates off.
   - Rulesets `bench-frozen-branches` (`refs/heads/tapeout/**`) and `bench-frozen-tags`
     (`refs/tags/wafer.space/**`, `google/**`, `tinytapeout/**`): no update, deletion or
     force-push, with no bypass for anyone, the App included. Creating a tapeout branch or
     tag stays allowed, so the other commands keep working.

   `--shard K/N` takes the same slices as `sync`; `--delay` (default 1 s) paces the repos
   that changed. Forking stays allowed.

GitHub rate-limits repo creation (about 150 in 30 minutes). `fork` and `copy` pace
creations 20 s apart (`--delay`) and back off from 60 s up to 960 s when refused.

Options shared by `fork`, `copy`, `layout`, `sync` and `settings`:

- `--only NAME...`: only these repos. A name may be the bench name (`owner--repo`), the
  current upstream name or the catalog's source name (`owner/repo`), in any case; a
  `none` design is named `<shuttle>/<code>`. If no name matches a plan the command
  handles, it prints a warning and exits 2.
- `--dry-run` (`copy`, `layout`, `sync`, `settings`): log what would be done, write nothing.

Exit codes: 0 for success; 1 if any repo failed (an error, a layout conflict, an `empty`
repo for `layout`, `no source` for `copy`); 2 for usage errors and an `--only` that
matches nothing.

Do not run `copy` (or `layout`) while the daily sync runs, around 03:17 UTC: they write
the same repos.

## The event log

Every action appends one JSON line (`time`, `event`, then fields) to `work/events.jsonl`,
or to the file given by `--log`. Read it with `jq`, for example
`jq -c 'select(.event == "copy" and (.status | startswith("error")))' work/events.jsonl`.
The log never holds local paths; git errors are redacted.

## Machine-local configuration

Copy [`bench.local.toml.example`](bench.local.toml.example) to `bench.local.toml`
(git-ignored) and adjust it:

- `work_dir`: where local clones live (`work_dir/mirrors/<bench name>.git` for copies,
  `work_dir/about/` for the small repos that build `about` commits).
- `[mirrors]`: a public URL prefix mapped to a local directory of bare mirrors with the
  same layout. A clone uses `<dir>/<rest of URL>[.git]` when it exists (matching the case
  exactly or, failing that, ignoring case), else the public URL.

Without a mirror, a deleted repo can be restored only if `data/restore_sources.toml`
names a public source for it; otherwise `copy` reports `no source`. Local paths never
leave this machine: they are not allowed in anything committed or published.

## Automation (GitHub Actions)

| Workflow | Lives in | Runs | Does |
|---|---|---|---|
| `.github/workflows/ci.yml` | this repo | push, pull request | ruff, tests, actionlint |
| `.github/workflows/sync.yml` | this repo | daily 03:17 UTC; manual, with `only` and `dry_run` inputs | `wsbench sync` then `wsbench settings` in 8 shards (at most 2 at once), then one index dispatch |
| `index-repo/.github/workflows/pages.yml` | `wafer-space-bench/wafer-space-bench.github.io` | every 6 hours, manual, `repository_dispatch: bench-updated` | `wsbench index`, Jekyll build, deploy to Pages |

Each sync shard mints its own GitHub App token (a token lasts 60 minutes) and keeps its
`work/mirrors` in the Actions cache. After its sync it runs `wsbench settings` on the same
shard, even when the sync failed, so a repo created since the last run gets its settings
and frozen rulesets the same day. Shards run with `--no-dispatch` and upload a
`changed` marker with their event log. The final `notify` job dispatches `bench-updated`
to the index repo once, if any shard changed something.

One-time setup:

1. Create a GitHub App (`wafer-space-bench-bot`) owned by the `wafer-space-bench` org,
   with repository permissions Administration read/write (default branch, and every
   `wsbench settings` write: features, topics, Actions, Dependabot, rulesets), Contents
   read/write, Workflows read/write (mirrored branches may change `.github/workflows`)
   and Metadata read. It needs no webhook.
2. Install it on all repositories of `wafer-space-bench`.
3. In this repo (`wafer-space/wafer.space-bench`), add the Actions variable
   `BENCH_APP_ID` (the App's ID) and the secret `BENCH_APP_PRIVATE_KEY` (a private key of
   the App).
4. In the org's Actions policy, allow Actions only for
   `wafer-space-bench/wafer-space-bench.github.io`. They must stay disabled in the bench
   repos, or every mirrored push would run the upstream designs' CI in the org.
5. Create `wafer-space-bench/wafer-space-bench.github.io` with the contents of
   [`index-repo/`](index-repo/), and set its Pages source to GitHub Actions
   (`build_type=workflow`). It uses its own read-only `GITHUB_TOKEN`; the bench repos are
   public.

Third-party actions are pinned to commit SHAs, each with a `# vX.Y.Z` comment.
[`.github/dependabot.yml`](.github/dependabot.yml) proposes updates for this repo and for
`index-repo/`.

## Data

- `data/designs.csv`: the design catalog, one row per taped-out design.
- `data/bench_repos.csv`: the plans written by `scan`.
- `data/restore_sources.toml`: public sources for deleted upstream repos.

## Development

    uv run pytest -q
    uv run ruff check && uv run ruff format --check
    docker run --rm -v "$PWD:/repo" -w /repo rhysd/actionlint:1.7.7 -color .github/workflows/*.yml index-repo/.github/workflows/*.yml

Tests never use the network: GitHub is an in-memory fake (`tests/fakes.py`), and git runs
for real against `file://` repositories in a temporary directory.
