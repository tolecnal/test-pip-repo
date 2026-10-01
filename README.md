# test-pip-repo — a smoke-test suite for an internal devpi repository

A self-contained harness that answers one question, repeatably:

> **Does our internal pip repository still work?**

It does that the only way that really counts: it builds a real package, uploads it to
your devpi index, installs it back out of a fresh virtualenv, and checks that what came
back is byte-for-byte what went in — plus twenty other things around that path.

Written for `devpi-server` + `devpi-web`, and meant to be run by hand from a clone:

- **"Is the repo healthy right now?"** — `./pipcheck cycle`, or `./pipcheck doctor` for
  a read-only look.
- **"Did the upgrade break anything?"** — run it before, upgrade the server, run it
  again, then `./pipcheck compare` to see exactly what changed.

---

## What's in here

| Path | What it is |
|---|---|
| `pkg/` | `devpi-smoke`, a throwaway hello-world package. The thing that gets published. |
| `harness/` | The test harness. **Standard library only** — it never depends on the repo it tests. |
| `pipcheck` | The entry point. `./pipcheck <command>`. |
| `pipcheck.toml.example` | Committed template. Copy to `pipcheck.toml` (gitignored) for your server. |
| `Makefile` | Shortcuts (`make test`, `make doctor`, `make compare`, `make lint`, …). Optional. |
| `ruff.toml`, `.isort.cfg`, `pyrightconfig.json` | Lint, import-order and type-check settings. `make lint` runs all three. |
| `.venvs/` | Created on demand: one tooling venv, plus a throwaway venv per install test. |
| `reports/` | One JSON report per run. `./pipcheck compare` diffs them. |

Nothing is installed into your system Python, and nothing touches `~/.devpi`,
`~/.pypirc` or `~/.config/pip`.

---

## Quick start

Requires Python **3.11+** for the harness itself (it reads TOML with `tomllib`). The
Python used for the install tests is configured separately, so you can still test
against an older interpreter.

```bash
# 1. one-off: build/twine/devpi-client into .venvs/tooling
#    Tries PyPI, and falls back to your internal index if PyPI is unreachable.
#    Force the internal index with: ./pipcheck bootstrap --via-index
./pipcheck bootstrap

# 2. tell it where the repo is -- config file, or environment, or flags
cp pipcheck.toml.example pipcheck.toml   # optional; gitignored
export DEVPI_URL=https://devpi.internal:3141
export DEVPI_INDEX=testing/dev
export DEVPI_USER=testing
export DEVPI_PASSWORD=...          # or put non-secrets in pipcheck.toml

# 3. is the server sane? (read-only, 1 second)
./pipcheck doctor

# 4. the real thing: bump the version, publish, prove it came back correctly
./pipcheck cycle

# 5. after upgrading devpi: run it again, then diff the two runs
./pipcheck compare
```

`cycle` is the command you will actually use:

```
patch bump -> 0.3.1
built 0.3.1 build c9888e5025d5: devpi_smoke-0.3.1-py3-none-any.whl, devpi_smoke-0.3.1.tar.gz
uploaded 0.3.1 to https://devpi.internal:3141/testing/dev

[server]
  server_status           ok   devpi-server 6.20.3, devpi-web 5.1.1, role master, serial 56
  ...
[install]
  install_wheel           ok   wheel 0.3.1 build c9888e5025d5, six 1.17.0, script ok
  ...
  21 passed, 0 failed, 0 skipped in 38.1s
  repository behaves as expected -- https://devpi.internal:3141/testing/dev
```

Exit code is `0` when everything passed, `1` when a check failed, `2` on a setup
problem (no credentials, unusable interpreter, tooling venv missing).

---

## Controlling the version

The version lives in exactly one place: `version = "..."` in `pkg/pyproject.toml`.

```bash
./pipcheck show                      # current version, config, build history
./pipcheck cycle                     # patch bump (0.1.0 -> 0.1.1), then publish + test
./pipcheck cycle --bump minor        # 0.1.1 -> 0.2.0
./pipcheck cycle --bump major        # 0.2.0 -> 1.0.0
./pipcheck cycle --bump dev          # 1.0.0 -> 1.0.0.dev1   (pre-release handling)
./pipcheck cycle --set-version 2.5.0 # exact version
./pipcheck cycle --no-bump           # rebuild and re-publish the current version
./pipcheck bump patch                # bump only, publish nothing
```

Every build also gets a fresh random **build id** stamped into the package source.
That is what makes a stale cache visible: the version number alone cannot tell
"the release I just uploaded" apart from "a cached release that shares its number",
but the build id can. Every install check compares it.

---

## What gets checked

`./pipcheck list` prints this too. 21 checks in six phases; the suite stops early if
the server itself is unreachable.

**`server` — is the service there and configured sanely**

| Check | What a failure means |
|---|---|
| `server_status` | Server down, or (on a replica) never synced / reporting replication errors. |
| `index_api` | The index doesn't exist, or doesn't advertise its upload/simple URLs. |
| `index_config` | The index has no `bases`, so it can't serve public packages at all. |
| `web_ui` | `devpi-web` isn't installed or enabled — no browsable index, no search. |

**`publish` — did the upload land correctly**

| Check | What a failure means |
|---|---|
| `release_listed` | The release, or its wheel or its sdist, is missing from the simple index. |
| `name_normalisation` | `devpi_smoke` / `DevPI.Smoke` don't resolve — PEP 503 handling is broken, and pip will fail for some spellings. |
| `artifact_integrity` | Served bytes don't match the advertised sha256, or don't match what was built. pip will reject these files. |
| `release_metadata` | Dependency metadata was lost in transit (`requires_dist` no longer mentions `six`). |

**`install` — can a client actually consume it** (each in its own fresh venv, `--no-cache-dir`, index-url pointed only at your repo)

| Check | What a failure means |
|---|---|
| `install_wheel` | The pinned wheel won't install, is the wrong build, or lost its console script. |
| `install_sdist` | The sdist won't install — this also exercises resolving the *build backend* (setuptools) through your index. |
| `install_latest` | An unpinned `pip install` doesn't resolve to the newest version the index lists: version ordering is wrong. |
| `install_older_pin` | Older releases are no longer installable; history is broken. |
| `install_extras` | Extras don't resolve (`devpi-smoke[extra]` doesn't pull `idna`). |
| `pip_download` | `pip download` fails even though install works. |
| `package_tests` | The package's own pytest suite fails against the served copy — and installing `pytest` proves the mirror can serve a package with a real dependency graph. |

**`mirror` — is the PyPI proxy working**

| Check | What a failure means |
|---|---|
| `mirror_public_package` | A public package can't be installed through your index: `root/pypi` mirroring or outbound network is broken. |
| `mirror_cache` | A second fetch fails; the mirror isn't serving from its own cache. |

**`security` — are the boundaries where you think they are**

| Check | What a failure means |
|---|---|
| `anonymous_read` | Read access doesn't match `expect_anonymous_read` — either pip clients will get 401s, or the index is more open than intended. |
| `overwrite_protection` | A published release could be replaced with *different content* under the same filename (or a refused upload still changed it). Honours the index's `volatile` flag by default. |
| `upload_requires_auth` | The index accepted an upload from bogus credentials. Anonymous upload is open. |

**`web` — devpi-web**

| Check | What a failure means |
|---|---|
| `web_search` | The new release never appears in search: the whoosh indexer is stale or stopped. |

### Picking a subset

```bash
./pipcheck verify                                   # re-run the suite, no rebuild
./pipcheck verify --phase server,publish            # by phase
./pipcheck verify --only install_wheel,install_sdist
./pipcheck cycle  --skip package_tests,mirror_cache # faster loop
./pipcheck verify --json today.json                 # machine-readable, lands in reports/
```

---

## Configuration

`pipcheck.toml.example` is the committed template; copy it to `pipcheck.toml`, which is
**gitignored** because it is per-machine and may name internal hosts. Precedence,
lowest to highest:

built-in defaults → `pipcheck.toml` → `pipcheck.local.toml` → environment
(`DEVPI_URL`, `DEVPI_INDEX`, `DEVPI_USER`, `DEVPI_PASSWORD`) → flags (`--url`,
`--index`, `--user`, `--insecure`, `--config`).

No config file is required: the defaults plus environment variables are enough, which
enough to run entirely from the environment. `./pipcheck show` prints which files were
actually read.

**Never put a password in `pipcheck.toml.example`.** Use the environment, or
`pipcheck.local.toml`. If you later decide to commit a shared `pipcheck.toml` (drop it
from `.gitignore`), keep credentials in `pipcheck.local.toml` — that is what the two
layers are for.

Expectations worth setting deliberately:

```toml
expect_anonymous_read     = true    # can unauthenticated pip clients read this index?
expect_upload_auth        = true    # must uploads be authenticated? (keep true)
expect_overwrite_rejected = "auto"  # "auto" = follow the index's volatile flag
run_pytest                = true    # run pkg/tests inside the install venv
mirror_probe              = "wcwidth"  # public package used to test root/pypi proxying
# python                  = "/usr/bin/python3.9"  # interpreter for the install tests
```

For a self-signed internal certificate, use `--insecure` or `verify_tls = false`.

---

## Living in git

Tracked: the harness, the package source, `pipcheck.toml.example`, `README.md`,
`Makefile`, `.gitattributes`. Everything else is generated or local — see
`.gitignore`:

| Not tracked | Why |
|---|---|
| `pipcheck.toml`, `pipcheck.local.toml` | Per-machine; may hold credentials. |
| `pkg/src/devpi_smoke/_build_info.py` | Generated at build time; a fresh random build id every run. The package imports fine without it. |
| `.venvs/`, `dist/`, `reports/`, `.pipcheck-state.json` | Local working state. |

Two things to decide for yourselves:

- **The version line churns.** `pipcheck cycle` rewrites `version = "..."` in
  `pkg/pyproject.toml`, so every run leaves the tree dirty — and if several people run
  cycles against the same index, that line conflicts on every merge. Either commit the
  bumps (the history then shows what has been published), or stop tracking the churn
  locally:

  ```bash
  git update-index --skip-worktree pkg/pyproject.toml   # undo: --no-skip-worktree
  ```

  `./pipcheck show` always tells you the real current version either way.
- **Reports are gitignored, and they are your baselines.** Every run writes
  `reports/<timestamp>-<index>.json`, which is what `compare` reads. They are local to
  your clone: keep them, and do not run `clean --all` (which deletes them) between the
  before and after runs of an upgrade. Each report records the harness version and the
  devpi-server/devpi-web versions it ran against, so an old one stays interpretable.

Tag the repo when you change the suite, and bump `__version__` in
`harness/__init__.py`; "which suite version produced this report" is the first question
you will ask when a check starts failing after an upgrade.

---

## Server-side prerequisites

Run the harness from a **client** machine, not from the devpi host itself: that way
every check travels the same path your developers' pip does — DNS, reverse proxy, TLS,
firewall — and not just localhost. Running it on the host is still useful for isolating
"is it devpi or is it the network?" when something fails.

The harness tests an index; it doesn't create one. On the devpi server:

```bash
devpi use http://devpi.internal:3141
devpi login root --password <root-password>
devpi user -c testing password=<pw> email=<you>
devpi login testing --password <pw>
devpi index -c dev  bases=root/pypi volatile=True    # throwaway, replaceable releases
devpi index -c prod bases=root/pypi volatile=False   # immutable releases
```

Point the harness at both in turn. The `volatile` flag is exactly what
`overwrite_protection` keys off: on `prod` a re-release of the same version with
different content must come back `409 Conflict`; on `dev` it is allowed, and the
harness then insists the replacement was applied *completely*.

Two things to get right before pointing `cycle` at an index:

- **Use an index you are willing to pollute.** `cycle` publishes real `devpi-smoke`
  releases. On a **non-volatile** index they cannot be cleaned up afterwards — devpi
  answers `403 Forbidden: cannot delete version on non-volatile index` — so every run
  leaves a permanent release behind. Keep a dedicated throwaway index for routine runs.

  For an immutable index, publish **once** deliberately and then re-verify that same
  release as often as you like without adding another:

  ```bash
  ./pipcheck cycle --index testing/prod     # one permanent release; proves 409 on overwrite
  ./pipcheck verify --index testing/prod    # re-checks it; publishes nothing new
  ```

  `verify` works off `.pipcheck-state.json`, which is local to your checkout — on a
  different machine the publish-dependent checks will skip until that machine has
  published once itself. The phases that never need a release of our own are
  `server` and `mirror`:

  ```bash
  ./pipcheck verify --phase server,mirror   # safe against any index, publishes nothing
  ```
- **Give the harness its own account**, not `root`, scoped to the index it tests:

  ```bash
  devpi user -c pipcheck password=<pw>
  devpi index dev acl_upload=testing,pipcheck
  ```

  Then a leaked CI credential cannot touch your release indexes.

---

## Testing an upgrade

This is what the suite is for. Every run saves a report into `reports/`, so the
"before" baseline is there even when you forget to ask for one.

```bash
./pipcheck cycle            # before: against the version you are about to replace

#   ... upgrade devpi-server / devpi-web on the server, restart ...

./pipcheck cycle            # after
./pipcheck compare          # diff the two most recent runs
```

`compare` tells you what actually changed, and which devpi versions each run was
talking to:

```
comparing runs
  before  2026-10-01T17:59:07+00:00  https://devpi.internal:3141/testing/dev
          devpi-server 6.20.3, devpi-web 5.1.1
          21 pass
  after   2026-10-01T18:40:11+00:00  https://devpi.internal:3141/testing/dev
          devpi-server 6.21.0, devpi-web 5.2.0
          20 pass, 1 fail

regressions (1)
  web_search               pass -> fail  search did not list devpi-smoke within 20s (last HTTP 200)…

  20 of 21 shared checks unchanged
  verdict: 1 regression after the change
```

It exits `1` if anything regressed, `0` otherwise. A check that was already failing
before the upgrade is reported separately, so a pre-existing problem is never
mistaken for one the upgrade caused.

```bash
./pipcheck compare --list                 # every saved run, oldest first
./pipcheck compare <before> <after>       # specific runs, by filename or bare name
./pipcheck compare <before>               # that run against the most recent
```

With no arguments it takes the newest run and the most recent earlier run that covered
**the same checks** — so a quick `doctor` or a `--phase install` run in between does not
quietly become your baseline.

Two more things worth doing deliberately after an upgrade:

- `./pipcheck verify --phase install` — the releases published by the *previous* server
  version must still install. That is the regression that actually bites, and it needs
  no new upload.
- `./pipcheck bootstrap --via-index --rebuild` — install the tooling itself
  (`build`, `twine`, `devpi-client`) *through* the internal index. That exercises the
  mirror against a real dependency graph rather than one small package.

If you expect to run many cycles in a sitting, `--bump dev` keeps them out of your
release numbering (`0.1.4.dev1`, `.dev2`, …) while still producing a distinct release
and build id each time.

For a replica, point `--url` at the replica: `server_status` then checks that it has
synced with its primary and reports no replication errors, and the install phase
confirms the replica serves releases the primary accepted.

---

## Housekeeping

```bash
./pipcheck clean         # dist/, build state, test venvs, caches; keeps tooling and reports
./pipcheck clean --all   # also the tooling venv and reports/ -- deletes your baselines
```

`clean` leaves the tree as git sees it: every file it removes is generated or ignored.

## Development

The code is kept clean under **Ruff**, **isort** and **pyright**:

```bash
make lint      # ruff check, isort --check-only --diff, then pyright
make format    # ruff check --fix, then isort
```

`make lint` creates `.venvs/lint` from `requirements-dev.txt` on first use, and rebuilds
it whenever that file changes. None of it is needed to *run* pipcheck — the harness
itself is standard-library only; the lint venv also installs `six` and `idna` purely so
the type checker can resolve the test package's imports.

The settings live in the repo, so an editor's language server and the command line agree
rather than arguing about line length:

- **Ruff** (`ruff.toml`) — `select = ["ALL"]` with `preview = true`, at 100 columns,
  clean. The `ignore` list is short and each entry says why: printing is this tool's
  interface (`T201`); its long, specific error messages are the product, not a smell
  (`TRY003`, `EM101`, `EM102`); docstrings are required on modules and classes but not on
  every one-line helper (`D102`, `D103`, …); and `Fail`/`Skip` read as check outcomes
  rather than as `FailError` (`N818`). Tests additionally allow `assert` and local
  imports.

  Suppressions use Ruff's own directive with rule *names*, which is what preview asks for
  and reads better than a code: `# ruff: ignore[blind-except] -- a harness bug must not
  abort the suite`. There are eight, each with a reason. That syntax needs ruff ≥ 0.16.10,
  which is what `requirements-dev.txt` pins.
- **isort** (`.isort.cfg`) — `profile = black` at 100 columns, matching Ruff's import
  rules so the two never disagree.
- **pyright** (`pyrightconfig.json`) — `typeCheckingMode: "standard"`, clean. Strict mode
  reports ~70 further findings, essentially all `Any` propagating out of `resp.json()`:
  devpi's JSON payloads are deliberately treated as loose data and validated at the point
  of use, so pinning them down with TypedDicts would add weight without catching anything
  real. If you want strict, that is the work it implies.

Note that `ruff.toml` targets **py311** because that is what the harness needs. The test
package in `pkg/` declares `requires-python >=3.9`, so keep its code free of 3.10+
runtime idioms even where Ruff would permit them.

### Linting in CI

Both pipelines are in the repo and run exactly the `make lint` above, so CI cannot
disagree with your editor:

| File | Platform |
|---|---|
| `.github/workflows/lint.yml` | GitHub Actions |
| `.gitlab-ci.yml` | GitLab CI |

Each one installs `requirements-dev.txt`, runs Ruff, isort and pyright, and then
smoke-tests the CLI (`./pipcheck --version`, `list`, `show`) on the clean checkout — that
last step matters because a fresh clone has no generated `_build_info.py`, so it proves
the package still imports without its build stamp.

Neither pipeline runs the devpi suite. That is on purpose: `cycle` needs a reachable
devpi server and it publishes real releases to an index, so it stays a thing you run by
hand from a clone.

### Docstrings

pydoclint is enabled (`DOC201`, `DOC501`, `DOC502`), so a docstring on a function that
returns something documents what comes back, and one on a function that raises documents
what it raises — and *only* what it actually raises:

```python
def upload(cfg: Config, st: state.State, build_record: state.Build | None = None) -> Proc:
    """Upload the recorded build's artifacts with twine.

    Returns:
        The finished twine process. On success the build is marked uploaded in state,
        against the index it went to.

    Raises:
        RuntimeError: if nothing has been built, if no credentials are configured, or
            if the recorded artifacts are no longer on disk.
    """
```

Google-style sections, prose above them for the *why*. `Args:` is not required and is
mostly omitted — the signatures are typed and the names say enough. A check function
needs no docstring at all (its `description=` is its summary); if you give it one,
document the `Fail`/`Skip` it raises, since that is the contract a reader cares about.

Two more conventions worth keeping if you extend the suite:

- A new check is one function with `@check(name, phase, description, ...)`, returning a
  one-line summary on success and raising `Fail` (the repository misbehaved) or `Skip`
  (does not apply here). Declare `needs_upload=True` if it needs a published release;
  the runner then guarantees `ctx.build` exists.
- Checks should work when run alone (`--only <name>`). Fetch what you need through the
  `_listed(ctx)` / `_volatile(ctx)` helpers, which cache in `ctx.share`, rather than
  assuming an earlier check populated it.

---

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `pipcheck needs Python 3.11 or newer` | Run it with a newer interpreter: `PIPCHECK_PYTHON=python3.11 ./pipcheck …`. |
| `error: could not create venv` | `python3-venv` isn't installed (Debian/Ubuntu). |
| `error: upload needs credentials` | `DEVPI_USER`/`DEVPI_PASSWORD` unset. |
| `index 'x/y' does not exist` | Wrong `DEVPI_INDEX`, or the index was never created. |
| Everything skipped after `server_status` | Server unreachable; the suite short-circuits on purpose. |
| `install_*` fails but `release_listed` passes | Usually the mirror: pip can't reach `six`/`setuptools` through `root/pypi`. |
| Want to see every command | `-v`, and `--keep-venvs` to leave the test venvs on disk. |
