# Publishing SWM on PyPI

*Plan, 2 October 2026. Nothing here is done yet: SWM is not published on
PyPI, and the repository has not been changed for it.*

How to make SWM installable with `pip install`, what already works, what
has to change first, and in what order.

## Contents

- [Goal](#goal)
- [What already works](#what-already-works)
- [Decisions made](#decisions-made)
- [What needs doing](#what-needs-doing)
- [Order of work](#order-of-work)
- [Open decisions](#open-decisions)

## Goal

Anyone with Python 3.10 or newer can run:

```bash
pip install simple-webcrawl-manager
playwright install chromium
swm serve
```

and get a working dashboard, without cloning the repository. The Windows
installer stays as it is.

## What already works

A trial build from a clean copy of the repository, using the current
`pyproject.toml` without changes:

- **Both files build:** the wheel
  (`simple_webcrawl_manager-1.1-py3-none-any.whl`, 663 KB) and the source
  archive (`simple_webcrawl_manager-1.1.tar.gz`, 805 KB).
- **`twine check` passes** for both, which is the check PyPI's upload
  applies.
- **Everything the dashboard needs is inside the wheel:**
  `dashboard.html`, `dashboard_hardening.js`, `help_text.yaml`, the fonts,
  and every theme's `theme.json` and icons.
- **It installs and runs:** in a fresh virtual environment,
  `pip install` of the wheel succeeds and `swm --help` works.
- **ReplayWeb.page** needs nothing from the repository: replay downloads
  its files from jsDelivr on first use, as it does today.

So the packaging itself is sound. The work is in making SWM usable
straight after `pip install`.

## Decisions made

### Package name

| Name | On PyPI |
|---|---|
| `simple-webcrawl-manager` | free |
| `webarc` | free |
| `webcrawlmanager` | free |
| `swm` | **taken** |

Publish as **`simple-webcrawl-manager`**. Keep the import name `webarc`
and the commands `swm` and `webarc`. A command name does not need to be
free on PyPI. Register the name early by making the first upload, to
TestPyPI and then PyPI.

### Where SWM keeps its data

No change. `pip install` installs the program only and creates no
folders. SWM creates its folders when a command first runs (`swm serve`,
`crawl`, `record` and so on), in **the folder it is run from**:

- `webarc-state/webarc.db`: the database (`--db`);
- `warcs/`: the captures (`--warc-root`, or `--output` for `record`);
- `replay/`: replay sites (`--replay-root`).

To keep data elsewhere, pass those options. This matches the Windows
installer, whose `Start SWM Server.cmd` starts SWM inside the
installation folder.

What must change is the help text. Today `swm serve --help` lists `--db`
and `--warc-root` with no explanation. Every command that has these
options should say, for example:

```text
--db PATH         SWM's database. Default: webarc-state/webarc.db in the
                  folder you run swm from.
--warc-root PATH  Where captures are saved. Default: warcs/ in the folder
                  you run swm from.
```

The pip install section of the README then says the same in one line:
SWM keeps its data in the folder you start it from, so start it from the
same folder each time, or pass `--db` and `--warc-root`.

A per-user data folder and an `SWM_HOME` setting were considered and set
aside.

## What needs doing

### 1. The dashboard must work after a plain install

Today `pip install simple-webcrawl-manager` leaves out FastAPI and
uvicorn (they are in the `dashboard` extra), so `swm serve` fails with
`No module named 'fastapi'`.

- Move `fastapi` and `uvicorn` into the main `dependencies`: the dashboard
  is the main way SWM is used. Keep `dashboard` as an empty extra so
  existing install commands still work.
- Add an **`all`** extra for every optional feature (Instagram listing,
  YouTube, the AI theme judge), matching what the Windows installer sets
  up: `pip install "simple-webcrawl-manager[all]"`.

### 2. What pip cannot install

These have to be documented, and where possible checked by SWM:

| What | Needed for | How |
|---|---|---|
| Chromium for Playwright | Everything | `playwright install chromium` after `pip install`. A small `swm setup` command could run it for the user. |
| Google Chrome | Visible browser windows (recordings, social media) | Installed separately, as today |
| ffmpeg, Deno or Node.js | Some YouTube captures | Installed separately; the YouTube tab and Settings page already report what is missing |
| Java and the warc-indexer jar | Search indexing only | See below |

**warc-indexer.** The jar is built from the repository's `warc-indexer/`
folder, which is not in the package. `webarc/warc_indexer.py` falls back
to looking in that folder through `_repo_root()`, which does not exist
after a pip install. The same goes for its configuration,
`config/swm-indexer.conf`. Pip users set both in **Settings → Indexer**,
or with `SWM_WARC_INDEXER_JAR` and `SWM_WARC_INDEXER_CONF`. Releases could
also attach a prebuilt jar with its configuration.
The docs need to say this; the error message when no jar is found should
say it too.

### 3. One version number

The version is **1.1.2**, the latest GitHub release. It is written in
two places, `version` in `pyproject.toml` and `__version__` in
`webarc/__init__.py` (which also goes into each new WARC's `warcinfo`
record as `webarc/1.1.2`), and the two must be changed together.

- Keep one source: either `__version__` in `webarc/__init__.py`, read by
  setuptools as a dynamic version, or git tags through `setuptools-scm`.
- Add `swm --version`.
- PyPI never accepts the same version twice, even after a deletion, so
  every upload needs a new number. A PyPI release and the GitHub
  release of the same number should be the same code, so if the first
  PyPI release includes changes made after `v1.1.2`, it is `1.1.3` (or
  `1.2.0`).

### 4. Package metadata

- **Licence:** replace the table form `license = {text = ...}` with the
  current form. setuptools warns that the table form stops being
  accepted after 18 February 2027:

  ```toml
  license = "LicenseRef-PolyForm-Noncommercial-1.0.0"
  license-files = ["LICENSE"]
  ```

- **PolyForm Noncommercial on PyPI** is allowed: PyPI accepts licences
  that are not OSI-approved. The PyPI page should say near the top that
  commercial use needs a separate written licence, as the README does.
- **Links:** `[project.urls]` for the homepage, documentation, issues and
  changelog.
- **Classifiers and keywords:** Python versions, operating systems,
  `Environment :: Web Environment`, `Topic :: Internet :: WWW/HTTP`,
  `Topic :: System :: Archiving`; keywords such as `warc`,
  `web-archiving`, `crawler`, `playwright`.
- **README links:** PyPI shows the README on its own, so relative links
  (`docs/user-guide.md`, `LICENSE`, `install/README.md`) break there.
  Either make them full GitHub links, or give PyPI a shorter description
  of its own with full links.
- The source archive includes `tests/`. That is harmless and can stay.

### 5. Release workflow

A GitHub Actions workflow, `.github/workflows/publish-pypi.yml`, run when
a version tag such as `v1.2.0` is pushed:

1. Run the test suite.
2. Build the wheel and source archive; run `twine check`.
3. Install the built wheel into a clean environment on Linux, Windows and
   macOS. Run `swm --help`, then `swm serve --simulate` and check that the
   dashboard answers.
4. Publish with **Trusted Publishing**: GitHub proves to PyPI which
   repository and workflow are uploading, so no API token is stored
   anywhere. This is set up once on pypi.org by the account holder.
5. First, run the whole workflow against **TestPyPI**, and install from
   there to check.

The workflow should run only on tags, so it does not interfere with the
installer workflows on `main`.

### 6. Documentation

- README: a **pip install** section beside the Windows installer and the
  install-from-source section: install, `playwright install chromium`,
  `swm serve`, and the note on where data is kept.
- User guide and command-line guide: the same, and `swm` in place of
  `python -m webarc.cli` where that reads better.
- Developer guide: how a release to PyPI is made.

### 7. The Windows installer (later, optional)

The installer could later install SWM from PyPI instead of downloading
the source from GitHub. Not needed for the first PyPI release.

## Order of work

1. **Packaging changes**, on one branch: dependencies and the `all`
   extra (step 1), one version number and `swm --version` (step 3),
   metadata (step 4), the `--db` and `--warc-root` help text, and
   optionally `swm setup`.
2. **Release workflow** (step 5): test it on TestPyPI, install from
   TestPyPI on each system, then make the first real release.
3. **Documentation** (step 6), in the same release.
4. Later: the Windows installer (step 7).

## Open decisions

- Whether FastAPI and uvicorn become main dependencies (recommended).
- Whether to add `swm setup` for the Playwright browser, or only
  document `playwright install chromium`.
- Where the version number lives: `__version__` or git tags.
- Who holds the PyPI account. Trusted Publishing and the TestPyPI
  account are set up on pypi.org and test.pypi.org by that person.
