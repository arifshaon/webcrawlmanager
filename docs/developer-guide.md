# Developer guide

This guide is for people working on SWM's code: how it is built, how to
run the tests, where things live, and the rules the code keeps. For using
SWM, start with the [README](../README.md).

## Contents

- [Setting up](#setting-up)
- [Running the tests](#running-the-tests)
- [How SWM is built](#how-swm-is-built)
- [Code layout](#code-layout)
- [Files on disk](#files-on-disk)
- [Design rules](#design-rules)
- [The dashboard's API](#the-dashboards-api)
- [The dashboard's front end](#the-dashboards-front-end)
- [Search document fields](#search-document-fields)
- [warc-indexer](#warc-indexer)
- [The Windows installer and releases](#the-windows-installer-and-releases)
- [Research notes](#research-notes)

## Setting up

```bash
git clone https://github.com/arifshaon/webcrawlmanager.git
cd webcrawlmanager
python -m venv .venv
source .venv/bin/activate                  # Windows: .venv\Scripts\Activate.ps1
pip install -r requirements.txt
pip install -e ".[dashboard]" httpx        # the dashboard, and httpx for its tests
playwright install chromium
```

Python 3.10 or newer is needed. The optional extras (`youtube`,
`instagram-listing`, `theme-ai`) are defined in `pyproject.toml`; install
them only to work on those features. `pip install -e .` also installs the
`swm` command.

## Running the tests

```bash
python -m unittest discover tests
```

The suite (about a thousand tests) includes browser tests that start
Chromium and a local test site, and some that open visible windows. On a
machine without a display, run them under a virtual display:

```bash
xvfb-run -a python -m unittest discover tests
```

`tests/chrome_for_tests.py` finds a Chrome to drive; browser tests skip
themselves if there is none. Run one module with
`python -m unittest tests.test_collections`.

GitHub Actions runs the whole suite on every push to `main` and
`feature/record-session` and on every pull request
(`.github/workflows/tests.yml`).

Browser tests that start their own dashboard server wait for it to stop in
`tearDownClass`, because the server module keeps some state at module
level and the next test would otherwise share it.

## How SWM is built

```text
Dashboard
browser ──► dashboard.html ──► server.py (FastAPI) ──► store.py (SQLite)
                                    │
                                    └─► worker.py, one process per job
                                            │
         ┌──────────────┬──────────────┬────┴──────────┬──────────────┐
         ▼              ▼              ▼               ▼              ▼
    crawler.py     recorder.py    facebook.py    instagram.py, x.py, youtube.py
         │              │              │               │
         └──── browser.py (Playwright) ┴───────────────┘
                        │
                        ▼
                  capture.py (WarcSession) ──► WARC files
                        │
                        └─► dedup_index.py (the collection's index.sqlite)

Command line
cli.py ──► the same modules, in the same process

Afterwards
WARC folder ──► inspect / extract
            ──► replay.py ──► ReplayWeb.page in the browser
            ──► indexer.py (social captures) or warc_indexer.py (crawls, recordings)
```

- **Capture happens at the browser's network events.** SWM records what
  the browser asked for and received, including requests made by
  JavaScript and by the person browsing, and turns them into WARC request
  and response records with `warcio`.
- **Each job runs in its own worker process.** The dashboard and workers
  share state through SQLite (`store.py`); `control.py` is the seam
  between a crawl loop and the outside world (pause, resume, stop,
  progress reports).
- **Social media captures** are built from a browser part that observes
  the platform's own traffic (`*_browser.py`), a reader for what that
  traffic contains (`*_extract.py`), the capture logic (`facebook.py`,
  `instagram.py`, `x.py`, `youtube.py`) and readable pages built from the
  records (`*_render.py`).

## Code layout

All code is in `webarc/`.

| Area | Modules |
|---|---|
| Command line and dashboard | `cli.py` (commands), `server.py` (FastAPI server and API), `worker.py` (runs one job), `store.py` (SQLite state), `control.py` (pause, resume, stop), `procs.py` (is a process alive), `resources.py` (CPU, memory, disk), `sizes.py` (folder sizes measured in the background), `desktop.py` (opening a folder in the file manager), `help.py` (the "?" texts) |
| Crawling and recording | `config.py` (configuration with per-seed overrides), `crawler.py` (crawl loop), `frontier.py` (queue, depth, robots.txt), `scope.py` (address tidying and scope), `browser.py` (launching and driving the browser), `consent.py` (cookie pop-ups), `detect.py` (block pages and back-off), `recorder.py` and `recording_runtime.py` (interactive recording) |
| Writing archives | `capture.py` (WARC writing and revisits), `redaction.py` (keeping the session out), `metadata.py` (descriptive metadata), `collections.py` (collections), `dedup_index.py` (the collection's payload index), `changes.py` (what changed since last time), `theme.py` (theme-based selection and the selection report) |
| Social media | `facebook.py`, `facebook_render.py`; `instagram.py`, `instagram_browser.py`, `instagram_gallery.py`, `instagram_render.py`; `x.py`, `x_browser.py`, `x_extract.py`, `x_render.py`; `youtube.py`, `youtube_browser.py`, `youtube_extract.py`, `youtube_ytdlp.py`, `youtube_render.py` |
| Afterwards | `replay.py` (local replay), `indexer.py` (search documents for social captures), `warc_indexer.py` (running warc-indexer) |
| Dashboard look | `appearance.py` (themes), `dashboard.html`, `dashboard_hardening.js`, `help_text.yaml`, `ui_themes/`, `fonts/` |

Tests are in `tests/`, one module per area (`test_collections.py`,
`test_theme.py`, `test_x_browser.py` and so on), with test sites and
sample data in `tests/fixtures/`.

## Files on disk

**The state folder** (`webarc-state/` by default, beside the state file):

| Path | What it holds |
|---|---|
| `webarc.db` | Jobs, collections, progress and settings, including the AI judge's API key |
| `browser-profiles/<platform>/` | The dedicated Chrome profile for Facebook, Instagram, X and YouTube; sensitive |
| `ui-themes/` | Installed dashboard themes |
| `help_text.yaml` | Optional local changes to the "?" texts |

**The storage root** (`warcs/` by default, or the location set in
Settings):

```text
warcs/
  collections/<identifier>/
    collection.json         the collection's description and policy
    index.sqlite            every payload the collection holds, and page fingerprints
    jobs/<job number>/      one folder per job
```

**A job's folder** can hold, depending on the kind of job:

| File | What it is |
|---|---|
| `*.warc.gz` | The WARC files |
| `metadata.json` | Descriptive metadata, the authoritative copy |
| `dedup-summary.json` | Payloads stored and reused |
| `changes.json` | What changed against the collection's last capture |
| `selection.jsonl`, `theme-summary.json`, `pages/selection.html` | A theme-based crawl's decisions and report |
| `<platform>-manifest.json`, `-checkpoint.json`, `-events.jsonl` | A social capture's record of itself |
| `media/`, `raw/`, `evidence/`, `pages/`, `checksums.sha256` | A social capture's media, source responses, tool output and readable pages |
| `index/` | Search documents for a social capture |
| `<warc>.jsonl`, `warc-index.log`, `warc-index-manifest.json` | warc-indexer's output for a crawl or recording |

Replay builds its sites in a separate replay folder (`replay/` by
default) and never writes into a job's folder.

## Design rules

These rules run through the code. Keep them when changing it.

- **A WARC is never changed once written.** Corrections and new
  information go into new files beside it (`metadata.json`, the
  manifest, the index). Replay compatibility fixes are applied only to the
  replay copy (`replay.py`), never to the WARC.
- **Never claim completeness that was not observed.** A timeline that
  stopped yielding is "the end of what was available" or "stalled", not
  "all posts"; comment collections are graded; gaps are recorded with
  their reason.
- **Provenance is explicit.** Each record names the response, tool or
  file it came from. Tool output (gallery-dl, yt-dlp) is kept apart from
  the platform's own responses and labelled as such.
- **The capturing session stays out.** `redaction.py` removes cookies,
  authorisation headers, sign-in fields and the session values in page
  data before writing. It never rewrites JavaScript, and where a block of
  page data states its own length, that length is updated, because the
  page rejects a block whose length does not match.
- **Social captures observe.** They read what the platform sends its own
  page and do not post, like, vote or comment. Requests SWM makes itself
  are recorded as such.
- **No silent changes for the user.** Starting, creating, editing and
  deleting are confirmed; the dashboard shows where files are and what an
  action will affect.
- **The dashboard is accessible.** WCAG 2.2 AA (checked with axe-core),
  a heading structure, keyboard use throughout, and colour contrast in
  every theme. Anything only shown in colour is also written out.
- **Separate programs keep their own licences.** gallery-dl (GPL-2.0) and
  the warc-indexer jar run as separate programs; yt-dlp is imported as a
  library under its own licence.

## The dashboard's API

The dashboard's page talks to `server.py` over a JSON API under `/api/`.
It is not a stable public interface yet, but it is usable for scripting:
for example `GET /api/crawls` lists jobs, `GET /api/crawls/<id>/changes`
returns a job's page changes, `GET /api/crawls/<id>/selection` a theme's
selection report, and `POST /api/crawls/<id>/index` indexes a social
capture, taking `source_root` and `relocate` in its body as the `index`
command takes `--source-root` and `--relocate`. The routes are defined in
`create_app()` in `server.py`.

## The dashboard's front end

The dashboard is one page, `webarc/dashboard.html`, with its styles and
script inline, plus `dashboard_hardening.js` for the job list. There is no
build step: edit the file and reload. The page refreshes its data every
two seconds; renderers redraw only what changed, so open menus and
selections survive a refresh.

- **Help texts** come from `help_text.yaml` through `/api/help`; a test
  checks that every **?** has a short, plain-text entry.
- **Themes** are folders under `webarc/ui_themes/` (built in) and
  `ui-themes/` in the state folder (installed). `appearance.py` loads and
  checks them; colours are CSS variables on `:root`, separately for light
  and dark.
- **Headings** follow two levels on every page: `.page-title` and
  `.section-heading`.

## Search document fields

`indexer.py` writes one document per item in the
[warc-indexer](https://github.com/ukwa/webarchive-discovery) Solr schema.
Only field names the schema defines are written; a document that would
not load is left out and counted as invalid in the summary.

- **Identity and place:** `id` (`facebook:post:<id>`, `x:user:<id>`, …),
  `type` (`Facebook Post`, `Instagram Comment`, `YouTube Video`, …), `url`,
  `url_norm`, `host`, `domain`.
- **Content:** `content` (the post, caption, comment or description),
  `title`, `author`, `keywords` (hashtags, tags), `content_language` where
  the platform gives it, `links` and `links_images`.
- **Time:** `publication_date` (when the item was written), and
  `crawl_date` and `wayback_date` (when it was captured, from the WARC
  record).
- **Evidence:** `source_file`, `source_file_path`, `source_file_offset`,
  `warc_key_id` (the WARC record's own id), `status_code` and the
  payload's `hash`. Comments point at the post page they were read from.
- **Context:** `collection`, `institution` (the operator), `access_terms`
  and `wct_subjects` from the capture's Rights and Subject metadata, and
  every platform detail without a schema field (reaction counts, parent
  ids, handles) as `key=value` entries in `content_metadata_ss`.

Items whose page is not in a WARC (a YouTube video read through yt-dlp, a
capture run without WARC writing) are still indexed, without the evidence
fields.

## warc-indexer

`warc-indexer/` holds a patched copy of warc-indexer 3.5.1 with fixes
that SWM's captures needed: the character set the server declared is
honoured, and the JSON output carries the WARC path and record type. See
[warc-indexer/README-SWM.md](../warc-indexer/README-SWM.md). Build it with
the Maven wrapper (Java 11 or newer; the wrapper fetches Maven):

```bash
cd warc-indexer
./mvnw -q -DskipTests package              # Windows: .\mvnw.cmd -q -DskipTests package
```

`warc_indexer.py` launches the jar as a separate program and reads what it
writes. It finds Java, the jar and the configuration through Settings,
then `SWM_JAVA`, `SWM_WARC_INDEXER_JAR` and `SWM_WARC_INDEXER_CONF`, then
`JAVA_HOME`, the PATH and `warc-indexer/target`.

## The Windows installer and releases

`install/` holds the Windows installer and the scripts that build it;
[install/README.md](../install/README.md) has more on the layout and build.

- **The installer** is an Inno Setup wizard, `install/SWM-Windows-Setup.iss`,
  around a PowerShell engine, `install/install-windows.ps1`, which is
  published unchanged as `install/SWM-Setup-<version>.ps1` (the release
  workflow checks that the two are identical). The engine installs a
  private Python (CPython 3.13, with pinned SHA-256 checks) and uv, SWM
  with every optional extra declared in `pyproject.toml`, and Playwright's
  Chromium under `<install folder>\.runtime`; copies Playwright's FFmpeg to
  `.runtime\tools` for yt-dlp; picks free dashboard and replay ports; and
  writes `Start SWM Server.cmd`, `swm.cmd`, `server-port.txt`,
  `START-HERE.txt` and `.swm-install.json` (what was installed, from
  where, and when).
- **Its options:** `-SourceMode LatestRelease` (the latest published
  GitHub release, the default) or `-SourceMode Branch -Branch <name>`;
  `-InstallMode Fresh` (replaces the private runtime) or `Update` (keeps
  the configuration, state and runtime where possible; `config.yaml` is
  backed up before the source is refreshed); `-InstallDir`,
  `-DashboardPort`, `-ReplayPort`, `-SourceArchivePath` (install from a
  downloaded source zip) and `-NonInteractive`. The wizard offers
  the same choices, loading the branch list from GitHub.
- **Releases** are built by `.github/workflows/release-windows-<version>.yml`
  (currently 1.1.1) on GitHub's Windows runners: it compiles the wizard,
  signs it, attaches the `.exe`, the `.ps1` and their SHA-256 checksums to
  a GitHub release, and records build provenance that
  `gh attestation verify` checks. The signing certificate is self-signed,
  so Windows does not trust it by default; production releases should use
  a trusted code-signing certificate and timestamp.
  `.github/workflows/build-unsigned-windows-installer.yml` builds an
  unsigned installer for testing.
- The version is `version` in `pyproject.toml`.
- Older installers (`SWM-Setup-0.*.zip`, `SWM-Setup-1.0.*`) are kept in
  `install/` for reference.

## Research notes

Design research is kept in `docs/research/`:

- [X capture](research/x-capture.md): reading X's signed-in web client:
  the requests to observe, the timeline's structure, attribution and media
  rules, and what breaks. Each finding is marked verified or unverified.
- [YouTube capture](research/youtube-capture.md): why yt-dlp reads and
  downloads the videos while a browser reads the Posts tab, what the trial
  run showed, and what the first real capture had to confirm.
- [A theme judge without AI](research/theme-nlp-fallback.md) (parked):
  how established language-processing libraries could judge theme pages
  when no AI judge is configured, and the comparison to run before
  choosing one.
