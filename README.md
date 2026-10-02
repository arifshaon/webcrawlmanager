# Simple Webcrawl Manager (SWM)

SWM saves websites and social media accounts as **web archives**: files in
the standard **WARC** format that keep a page exactly as a browser received
it, with its images, stylesheets, scripts, fonts, data requests and media.
You can open an archive again later and browse it as it was, on your own
computer, without anything being uploaded.

SWM is made for people who need a faithful record of web content, not a
screenshot: archivists and librarians, researchers, journalists, and legal
and compliance teams.

> **Licence:** SWM is source-available under the
> [PolyForm Noncommercial License 1.0.0](LICENSE). Non-commercial use is
> free; commercial use needs a separate written licence. See
> [Licence and citation](#licence-and-citation).

## Contents

- [What SWM does](#what-swm-does)
- [Install](#install)
- [Get started](#get-started)
- [Documentation](#documentation)
- [Use it responsibly](#use-it-responsibly)
- [Licence and citation](#licence-and-citation)

## What SWM does

SWM always uses a **real browser**, so it captures what a person would see:
pages that only appear after scrolling or clicking, lazy-loaded images,
media players and pages behind a sign-in. A plain download tool misses most
of this.

You can capture in several ways, from fully manual to fully automatic:

| Way of capturing | Who drives the browser | Good for |
|---|---|---|
| **Record a session** | You browse; SWM records everything that loads | Pages that need a sign-in, clicks, forms, galleries or video players |
| **Automated crawl** | SWM follows links from starting pages, within rules you set | Whole sites or sections that can be explored predictably |
| **Theme-based crawl** | As above, but SWM keeps only pages about one topic | Collecting one subject from a large site |
| **Facebook, Instagram, X, YouTube** | SWM scrolls and reads in a browser you have signed in to | A Page, profile, account, channel, post or video |

Every capture keeps an honest record of itself:

- **What was and was not collected,** in a manifest written beside the
  archive, including gaps and failures.
- **Your sign-in stays out of the archive.** Passwords, cookies and session
  tokens are removed before anything is saved.
- **Descriptive metadata** (Dublin Core fields such as title, subject and
  rights) is stored with each capture and inside each WARC file.

Around the captures, SWM helps you organise and use them:

- **Collections** group related jobs in one folder and store files that
  repeat across jobs only once.
- **Local replay** opens an archive in your browser through Webrecorder
  ReplayWeb.page.
- **Search indexing** produces documents for SolrWayback and other search
  tools, each pointing back to its place in the archive.
- **Quality checks** list what was captured and flag pages whose content
  may be incomplete.

You can work from the **web dashboard** or the **command line**.

## Install

### On Windows: the one-file installer

`install/SWM-Setup-1.0.ps1` installs SWM with its own private copy of
Python and the browser it needs. It does not touch any Python already on
your computer.

1. Download `SWM-Setup-1.0.ps1` from the `install` folder of this
   repository.
2. Right-click it and choose **Run with PowerShell**, or run it from a
   PowerShell window:

   ```powershell
   powershell -ExecutionPolicy Bypass -File .\SWM-Setup-1.0.ps1
   ```

3. When it finishes, start SWM with **Start SWM Server.cmd** in the
   installation folder, which is
   `%LOCALAPPDATA%\Programs\Simple Webcrawl Manager` unless you chose
   another. Then open <http://127.0.0.1:8080> in your browser.

The installation folder also has `swm.cmd` for the command line. The
installer's options (`-InstallDir`, `-DashboardPort`, `-ReplayPort`,
`-Branch` and others) are listed at the top of the script; `-Branch` sets
which branch of this repository it installs. If a download fails, the
installer shows the address so you can download the file yourself and
select it. More detail: [install/README.md](install/README.md).

### On any system: install from source

You need **Python 3.10 or newer**. For visible browser windows (recordings
and social media captures) you also need **Google Chrome**.

```bash
python -m venv .venv
source .venv/bin/activate              # Windows: .venv\Scripts\Activate.ps1
pip install -r requirements.txt -r requirements-dashboard.txt
playwright install chromium
```

If PowerShell refuses to run `Activate.ps1`, allow scripts you created
yourself, once:

```powershell
Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
```

### Optional extras

Install these only for the features that need them.

| For | Install |
|---|---|
| YouTube capture | `pip install -e ".[youtube]"` (yt-dlp with its `default` and `curl-cffi` extras), plus **ffmpeg** and **Deno** (or Node.js) on the PATH. Windows: `winget install --id Gyan.FFmpeg -e` and `winget install --id DenoLand.Deno -e`. macOS: `brew install ffmpeg deno`. Linux: your distribution's packages. |
| Instagram listing through gallery-dl | `pip install gallery-dl` |
| The AI judge for themes, using Claude | `pip install -e ".[theme-ai]"` (a local model through Ollama or LM Studio needs nothing extra) |
| Full-text search of crawls and recordings | Java 11 or newer, then build the bundled warc-indexer once; see [Search indexing](docs/user-guide.md#search-indexing) |

Install Python packages into the same Python that runs SWM. The Windows
installer does not add these extras; for an installed copy, use its own
Python, for example
`"<install folder>\.runtime\python\python.exe" -m pip install "yt-dlp[default,curl-cffi]"`.
The dashboard's YouTube tab and **Settings** page report what they found
and what is missing.

## Get started

### With the dashboard

1. Start it:

   ```bash
   python -m webarc.cli serve
   ```

2. Open <http://127.0.0.1:8080>.
3. Go to **Collections** and choose **New collection**, for example
   "Library news 2026". (You can skip this; jobs without a collection go
   into one called *Default*.)
4. Choose **New job**, pick a kind of capture, fill in the form and choose
   the collection. The line **Will be saved in** shows exactly where the
   files will go.
5. Start the job and confirm. Follow it on the **Jobs** page.
6. When it finishes, choose **Replay** on the job to browse what was
   captured.

To try the dashboard without opening any browser or website, start it with
`python -m webarc.cli serve --simulate`.

### With the command line

```bash
# Record a session: a browser opens; browse, then close it to finish
python -m webarc.cli record https://example.org/ --name example-session

# Run an automated crawl from a configuration file
python -m webarc.cli validate config.yaml
python -m webarc.cli crawl config.yaml

# Check and replay what was captured
python -m webarc.cli inspect ./warcs/example-session --hosts
python -m webarc.cli replay ./warcs/example-session
```

`config.yaml` in this repository is a commented example of a crawl
configuration. If you installed with the Windows installer, use `swm`
in place of `python -m webarc.cli`.

## Documentation

| Guide | What it covers |
|---|---|
| [Dashboard and collections](docs/user-guide.md) | The dashboard page by page, collections and storage, metadata, replay, search indexing, machine resources, themes, troubleshooting |
| [Capture guide](docs/capture-guide.md) | Choosing a way to capture, browser modes, recordings, crawls and themes, and Facebook, Instagram, X and YouTube in detail, with what each one saves |
| [Command line](docs/command-line.md) | Every command and option |
| [Developer guide](docs/developer-guide.md) | How SWM is built, running the tests, the code layout, design rules, building the indexer and the Windows installer |
| [Research notes](docs/research/) | Design research behind the X and YouTube captures, and a parked idea for a theme judge without AI |

## Use it responsibly

- **Capture only what you are authorised to preserve.** A recording or a
  signed-in capture can include private pages and personal data. Store and
  share the archives according to their sensitivity.
- **SWM does not evade restrictions.** When a site blocks it, SWM slows
  down and then stops. For lasting blocks, ask the site owner to allow
  your archiving address; see [Blocked sites](docs/user-guide.md#blocked-sites).
- **Sign-ins are yours.** You sign in yourself in a visible browser; SWM
  never asks for or stores a password.

## Licence and citation

Copyright © 2026 Arif Shaon.

The software is licensed under the
[PolyForm Noncommercial License 1.0.0](LICENSE). It permits personal,
research, educational, charitable, government and other non-commercial
use as defined in its terms. You may share copies and permitted
modifications with the licence and notices intact.

Commercial use is **not** permitted without a separate written licence
from Arif Shaon. This includes offering a paid service with SWM, building
it into a commercial product, or using it for other commercial benefit.
Because of this restriction, SWM is source-available rather than
OSI-approved open-source software.

Documentation, screenshots or training materials may be licensed
separately under a Creative Commons licence where they are marked as
such; otherwise everything in the repository follows `LICENSE`.

If you use SWM in research, institutional work or published
documentation, please acknowledge **Arif Shaon** and cite this repository
using [`CITATION.cff`](CITATION.cff).
