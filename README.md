# Simple Webcrawl Manager (SWM) — configurable per-seed browser-based web archiving

A Python crawler that archives websites to standards-compliant **WARC/1.1** files,
driving a real browser per seed with per-seed configuration:

- **Browser mode per seed**: `headless` (bundled Chromium), `headed` (visible Chrome),
  or `native` (attach to your system's default Chrome via CDP remote debugging).
- **Standard crawl semantics**: seeds, scope rules (same-host / same-domain /
  path-prefix / regex include-exclude), max depth, max pages, robots.txt.
- **Human-like navigation**: randomised delays, incremental scrolling, mouse jitter,
  `networkidle` waits — pages render fully (JS, lazy-loaded images) before capture.
- **WARC output**: `warcinfo`, `request`, `response`, and digest-based `revisit`
  records written with `warcio`, gzip-compressed, one WARC per seed (rotated by size).

## Architecture

```
config.yaml ──► crawler.py (per-seed orchestrator)
                   │
                   ├── scope.py     URL canonicalisation + scope decisions
                   ├── frontier.py  BFS queue, dedup, depth tracking
                   ├── browser.py   Playwright driver (headless/headed/native CDP)
                   │                 human-like behaviours, link extraction
                   └── capture.py   Response/request event capture → warcio writer
                                     (SHA-1 payload digests, revisit dedup)
```

Capture is done at the browser network layer (Playwright request/response events),
the same general approach as Browsertrix Crawler: you archive exactly what the
browser saw, including XHR/fetch traffic, fonts, media, and JS-rendered content.

## Two ways to run — pick either

webarc works entirely from the command line. The dashboard is an **optional**
layer on top of the same crawl engine; you never need it to run a crawl.

### A. Command line (no dashboard, minimal install)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt        # core only — no FastAPI/uvicorn
playwright install chromium            # only for headless mode

python -m webarc.cli crawl config.yaml # run a crawl
python -m webarc.cli validate config.yaml
```

This path has no server, no database, no background processes — it just crawls
each seed in the config and writes WARCs to `output_dir`.

### Interactive recording (command line only for now)

The interactive recording workflow is currently available from the **command
line only**; it is not yet exposed through the SWM dashboard. Use it when a
website requires a person to navigate menus, open modals, play media, log in, or
perform other interactions that an automated crawl may not reproduce reliably.

Start a recording session with a visible browser:

```powershell
python -m webarc.cli record `
  https://example.org/ `
  --name example-session
```

By default, the WARC files are written to `warcs/<name>/`. Use `--output` to
choose another WARC root:

```powershell
python -m webarc.cli record `
  https://example.org/ `
  --name example-session `
  --output D:\web-archives
```

During the session:

1. Browse normally in the opened browser window.
2. Open the pages, menus, pop-ups, embedded viewers and media that need to be
   preserved.
3. Use the **SWM Recording** control at the bottom-right of the page to pause,
   resume, or capture the current page.
4. Close the browser window, or press `Ctrl+C` in the terminal, to finish and
   finalise the WARC.

Everything loaded by the browser while recording may be written to the WARC,
including cookies, authenticated pages, form submissions and private content.
Treat the resulting files according to the sensitivity of the recorded session.

For embedded or streaming video, open the player, allow it to load, and play or
seek through the portions that need to be preserved. Streaming media is captured
as the browser requests it, so unplayed segments may not be present.

Inspect the completed recording:

```powershell
python -m webarc.cli inspect warcs/example-session
python -m webarc.cli inspect warcs/example-session --hosts
python -m webarc.cli inspect warcs/example-session --grep youtube
```

Replay it locally:

```powershell
python -m webarc.cli replay warcs/example-session
```

The `headed` browser mode is recommended and is the default. The advanced
`native` mode can attach to system Chrome through CDP:

```powershell
python -m webarc.cli record `
  https://example.org/ `
  --name example-native-session `
  --browser native
```

### B. Web dashboard (optional)

Adds add/monitor/pause/resume/stop and storage tracking in a browser. Install
the extra packages, then start the server:

```bash
pip install -r requirements.txt -r requirements-dashboard.txt
python -m webarc.cli serve             # → http://127.0.0.1:8080
```

The dashboard uses port **8080** by default. To use a different port, pass the
`--port` option, for example:

```bash
python -m webarc.cli serve --port 8085 # → http://127.0.0.1:8085
```

Both paths use the identical crawl engine, so scope rules, human-like
navigation, block detection, and WARC output behave the same either way. Running
`serve` without the dashboard packages installed prints how to add them rather
than failing obscurely.

## Install and run on Windows

webarc runs natively on Windows — no WSL required. The control plane is
cross-platform by design (process kill uses `TerminateProcess`, Chrome
autodetection knows the standard `C:\Program Files` install paths).

### 1. Prerequisites

- **Python 3.11 or newer** from [python.org](https://www.python.org/downloads/windows/)
  or the Microsoft Store. During the python.org install, tick
  **"Add python.exe to PATH"**.
- **Google Chrome** — only needed for `headed` or `native` browser modes;
  `headless` mode uses Playwright's bundled Chromium instead.

Verify in a terminal (PowerShell or Command Prompt):

```powershell
python --version
```

### 2. Create a virtual environment and install

From the project folder:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1          # PowerShell
# .venv\Scripts\activate.bat        # Command Prompt alternative

pip install -r requirements.txt     # core crawler
playwright install chromium         # bundled browser for headless mode
```

If PowerShell refuses to run `Activate.ps1` with an error about execution
policies, allow local scripts for your user once:

```powershell
Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
```

then re-run `.venv\Scripts\Activate.ps1`.

### 3. Run a crawl

```powershell
python -m webarc.cli validate config.yaml   # check the config parses
python -m webarc.cli crawl config.yaml      # run it
```

WARCs are written to the `output_dir` from the config (default `.\warcs`).

### 4. Optional: the web dashboard

```powershell
pip install -r requirements-dashboard.txt
python -m webarc.cli serve                  # → http://127.0.0.1:8080
```

To run the dashboard on another port:

```powershell
python -m webarc.cli serve --port 8085      # → http://127.0.0.1:8085
```

Or try it without any browser install first:

```powershell
python -m webarc.cli serve --simulate
```

### 5. Optional: replay captured WARCs

```powershell
python -m webarc.cli replay .\warcs --url https://example.org/
```

This opens ReplayWeb.page in your default browser; press Ctrl+C in the
terminal to stop the replay server.

### Windows notes

- **Browser modes**: `headless` needs only `playwright install chromium`.
  `headed` and `native` use your installed Chrome — webarc looks in
  `C:\Program Files\Google\Chrome` and `C:\Program Files (x86)\Google\Chrome`
  automatically; set `browser.chrome_path` in the config if Chrome lives
  elsewhere.
- **Native mode**: close other Chrome windows first, or set a dedicated
  `user_data_dir` in the config (e.g. `.\chrome-profile-webarc`) so the
  CDP-debugging instance doesn't clash with your everyday browser session.
- **Paths in configs**: forward slashes work fine in YAML on Windows
  (`output_dir: ./warcs`); if you use backslashes, quote the value.
- **Firewall prompt**: the first `serve` or `replay` run may trigger a
  Windows Defender Firewall prompt — both servers bind to `127.0.0.1` only,
  so allowing (or even cancelling) the prompt doesn't expose anything to the
  network.
- **Stopping crawls from the dashboard**: Stop is graceful (finishes the
  current page, closes the WARC); the Kill fallback uses `TerminateProcess`
  on Windows, which ends the worker immediately — the current WARC may be
  left without its final records, though records already flushed remain
  readable.

## Run

```bash
python -m webarc.cli crawl config.yaml
```

### Native browser mode

For `mode: native`, webarc launches your installed Chrome with
`--remote-debugging-port` and attaches over CDP, so the crawl runs in a real,
visible Chrome window. Chrome (111+) only enables CDP remote debugging on a
non-default profile, so webarc always runs it with a dedicated `user_data_dir`
— yours from the config, or `./chrome-profile-webarc` if unset. If a Chrome
instance is already running on that profile, close it first (or point
`user_data_dir` elsewhere).

## Dashboard (v2)

A web control panel to add crawls, watch live progress, pause/resume/stop, and
track storage — instead of watching log lines.

```bash
pip install -r requirements.txt        # now includes fastapi + uvicorn
python -m webarc.cli serve              # → http://127.0.0.1:8080
```

Open the URL, paste a crawl config into the **New crawl** box, press **Start
crawl**. Each crawl launches as an isolated worker subprocess and writes into
its own folder under `warcs/<crawl_id>/`, so storage accounting is exact.

Controls per crawl:
- **Pause** — the worker finishes the current page, then blocks. Progress freezes.
- **Resume** — continues from where it paused (same process, in-memory frontier).
- **Stop** — finishes the current page, closes the WARC cleanly, exits (graceful).
- **Delete** — removes the record; optionally purges the WARC files from disk.

The storage strip shows captured bytes, active-crawl count, and disk free/used.

### Try the dashboard without a browser install

```bash
python -m webarc.cli serve --simulate
```

`--simulate` runs browserless fake crawls (timed page visits writing tiny WARC
records) so you can exercise the whole control plane — add, pause, resume, stop,
storage, delete — before installing Playwright's Chromium. Real crawls use the
same control plane; only the capture engine differs.

### How control works (architecture)

State lives in SQLite (`webarc-state/webarc.db`, WAL mode) shared between the
server and workers. The API writes a control command (`pause`/`resume`/`stop`)
to the crawl row; the worker polls it between pages via a `StoreController` and
acts on it. This gives clean, race-free pause/stop without threads or signals in
the hot path. A hard-kill endpoint (`/kill`) is the fallback if a worker wedges;
it's cross-platform (TerminateProcess on Windows, SIGTERM group on POSIX).

Note: pause keeps the worker process alive and idling — it does not persist the
frontier to disk, so a server restart ends running crawls. Cross-restart
resumption would need frontier serialisation (a reasonable v3 item, and the
place to add crawl scheduling/recurring snapshots).

## Replay / QA

### WAF / bot-block handling

Government and enterprise sites often sit behind a WAF (F5 BIG-IP ASM, Cloudflare,
Imperva Incapsula, Akamai, PerimeterX). When one decides a request is a bot it
serves a block/challenge page — F5's is the "The requested URL was rejected …
Your support ID is: N" page, frequently returned as **HTTP 200**, so status codes
alone won't catch it.

webarc watches every visited page for these block signatures (content first,
status codes 403/429/503 as backup). When it sees a block, per seed:

1. **First block → back off.** Slows the crawl (`block_backoff_factor`× the
   inter-page delay) and cools down (`block_cooldown` seconds) before the next
   page. Block pages are not harvested for links.
2. **Block persists → stop the seed.** After `block_max_consecutive` blocks in a
   row it stops crawling that seed and marks it `blocked`, on the reasoning that
   the rest of the site is gated the same way — continuing only antagonises the
   WAF and risks a harder IP ban.
3. **Block clears → resume normal pace.** A single successful page resets the
   back-off and the counter.

Tunable per seed under `behavior:`:

```yaml
    behavior:
      detect_blocks: true
      block_backoff_factor: 3.0    # delay multiplier applied on each block
      block_cooldown: 30           # base cooldown seconds (scales with streak)
      block_max_consecutive: 3     # stop the seed after this many blocks in a row
```

This is deliberately a *back off and stop* policy, not an evasion one. The durable
fix for a persistently blocking government site is to have QNL's crawl IP and an
honest identifying User-Agent allowlisted by the site's operator — set the UA via
`browser.user_agent`.

## Replay (ReplayWeb.page)

webarc replays with **Webrecorder ReplayWeb.page** (wabac.js). Replay runs
entirely in your browser via a service worker — there is **no replay server**,
so it works on any Python version (3.13 / 3.14 included) with **no pywb and no
extra dependencies**. Nothing is uploaded anywhere; everything stays on
`127.0.0.1`.

### From the command line (any WARC folder)

```bash
python -m webarc.cli replay ./warcs/diwan --url https://www.diwan.gov.qa/?sc_lang=en
```

webarc combines the folder's WARCs into one archive, writes a small
ReplayWeb.page site, starts a plain static server, and opens your browser. The
`--url` seeds which captured page to show first (optional). Ctrl+C stops it.

Runs in the **same environment as the crawler** — no separate Python 3.11/3.12
venv, because replay no longer uses pywb.

### From the dashboard

Each crawl with captured data has a **Replay** button — it builds that crawl's
ReplayWeb.page site and opens it in a new tab.

### Offline / air-gapped machines

By default the ReplayWeb.page UI (`ui.js`) and service worker (`sw.js`) load
from the jsDelivr CDN (pinned version). For a machine with no internet, download
those two files once from `https://cdn.jsdelivr.net/npm/replaywebpage/` into
`./replay/vendor/` and pass `--self-host`; webarc will reference the local copies
instead of the CDN.

## Known limitations (honest notes)

- Request/response records are **reconstructed from browser network events**, not
  raw TCP capture. Status lines and header ordering are faithful, but this is the
  same trade-off Browsertrix/ArchiveWeb.page make. If you need proxy-level
  byte-exact capture, run the browser through `warcprox` instead (the browser
  driver already accepts a `proxy` option — point it at warcprox and disable
  the internal writer).
- Large streaming media (HLS/DASH segments) is captured segment-by-segment as
  requested by the page; full stream capture needs behaviour scripts.
- `native` mode attaches to whatever pages Chrome opens; keep the profile clean.

## Licence and citation

This project is available under the [MIT License](LICENSE). You may use, modify,
and redistribute the software provided that the copyright and licence notice are
retained.

If you use **webarc** in research, institutional work, another software project,
or published documentation, please acknowledge **Arif Shaon** and cite this
repository using the metadata in [`CITATION.cff`](CITATION.cff).
