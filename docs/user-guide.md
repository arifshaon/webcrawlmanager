# Dashboard and collections

This guide explains how to use SWM's web dashboard: where your files go,
how collections work, and how to replay, describe and search what you
capture. For the different kinds of capture themselves, see the
[Capture guide](capture-guide.md).

## Contents

- [Key ideas](#key-ideas)
- [Starting the dashboard](#starting-the-dashboard)
- [The dashboard, page by page](#the-dashboard-page-by-page)
- [Where your files are kept](#where-your-files-are-kept)
- [Collections](#collections)
- [Confirmations](#confirmations)
- [Describing a capture (metadata)](#describing-a-capture-metadata)
- [Replay](#replay)
- [Search indexing](#search-indexing)
- [Machine resources](#machine-resources)
- [Appearance and themes](#appearance-and-themes)
- [Changing the help text](#changing-the-help-text)
- [Blocked sites](#blocked-sites)
- [Known limitations](#known-limitations)

## Key ideas

- **WARC** is the standard file format for web archives. One WARC file
  holds many *records*: each request the browser made and the response it
  got. SWM writes compressed WARC 1.1 files (`.warc.gz`) and never changes
  a WARC once it is written.
- A **job** is one capture: a recording, a crawl, or a social media
  capture. Each job has its own folder.
- A **collection** groups jobs that belong together, such as "the 2026
  election sites", in one folder of its own.
- A **seed** is a starting address for a crawl. A **target** is the same
  for a social media capture: an account, post, channel or video.
- A **revisit** record is how a WARC says "this content is the same as
  that earlier record" instead of storing it again.

## Starting the dashboard

```bash
python -m webarc.cli serve
```

Then open <http://127.0.0.1:8080>. To use another port, add
`--port 8085`. To try the dashboard without opening any browser or
website, add `--simulate`; jobs then run as harmless simulations.

The dashboard keeps its state (jobs, collections and settings) in
`webarc-state/webarc.db` in the folder it was started from. Captures go
under `warcs/` unless you choose another storage location.

**Recordings and social media captures open a browser window on the
computer running the dashboard.** If the dashboard is reached from another
computer, those windows would open on the server, so recording is turned
off in that case. `--allow-remote-recording` turns it back on deliberately.

## The dashboard, page by page

The menu on the left has four pages. **New job** is also in the header.

### Jobs

The **Jobs** page shows an overview of your work and the list of jobs.

- **Overview tiles** show total and active jobs, collections, and pages
  and posts captured. A chart shows how many jobs are in each state, and
  another shows the jobs started each day; **Show as table** turns that
  chart into a table. Drag a tile or panel by the handle on its top edge
  to reorder it, or hide it with its **×**. **Customise** brings hidden
  ones back, reorders them by keyboard and resets the layout. The layout
  is kept in your browser.
- **All jobs** lists every job. Above it, you can search by name or `#id`
  and filter by type (crawl, recording, Facebook, Instagram, X, YouTube),
  status (active, waiting to start, completed, stopped, failed),
  collection and the date the job was created. The filter is remembered
  until you clear it. You can sort the list, and show it as a table or as
  cards.

Each job shows its progress: pages or posts captured, size on disk, and
for social media captures what was collected against what the platform
reported (for example, comments 126 / 128). Its controls are:

- **Pause**, **Resume** and **Stop** while it runs. A job waiting for the
  machine to have room has **Start now** and **Cancel**. **Force stop**
  ends a job that does not respond to Stop.
- **Replay** to browse what was captured. A social media capture has
  **Open pages** (readable pages built from its records) and **Replay
  WARC**.
- **Continue** for a stopped Facebook capture, which starts a linked
  follow-on capture.
- The **⋮** menu: **Metadata** (describe the capture), **Index** or
  **Index WARC** (search indexing), **Selection** (for a theme-based
  crawl, see the [Capture guide](capture-guide.md#the-selection-report))
  and **Page changes** (what changed since the collection's last capture).
- **Delete**, which explains what depends on the job before anything is
  removed.
- **Stored in** shows the job's folder, with **Open** or **Copy** (see
  [Where your files are kept](#where-your-files-are-kept)).

The machine's disk, CPU and memory are shown at the foot of the menu.

### Collections

The **Collections** page lists your collections as cards. **New
collection** is beside the page title. Above the list you can search
(name, identifier or description), filter (with running jobs, needs
attention, not indexed for search, empty), sort (last activity, name,
size, number of jobs) and switch between a list and a grid. These choices
are kept in your browser. See [Collections](#collections) for what each
card shows.

### New job

**New job** has a tab for each kind of capture: **Automated crawl**,
**Record session**, **Facebook**, **Instagram**, **X** and **YouTube**.
Each form is divided into panels:

- Choose a panel's header (or the chevron button) to fold or unfold it.
  A folded panel shows what has been filled in, such as
  "example-crawl · Example Library".
- Every field has a **?** that explains it in plain words.
- The crawl form has a **Guided form** and a **Raw YAML** tab. Less
  common settings are under **Advanced crawl settings**.
- Under the **Collection** picker, **Will be saved in** shows the exact
  folder the job will use. **New collection…** beside the picker makes a
  collection without leaving the form.
- Every form ends with **Describe this capture**, for its metadata.

### Settings

- **Storage**: the default storage location for new collections.
- **Resource warnings**: when to warn that the machine is short of CPU,
  memory or disk before a job starts.
- **Indexer**: where Java, the warc-indexer program and its configuration
  are, for full-text search of crawls and recordings.
- **Theme judge (AI, optional)**: the AI model that judges pages for
  theme-based crawls.
- **Dashboard themes**: install, download and remove themes.

Every **Save** asks you to confirm and says what the change affects.

## Where your files are kept

Every job and collection shows where its files are:

- **Stored in** on each job and collection shows its folder. Next to it,
  **Open** shows the folder in your computer's file manager (Explorer on
  Windows, Finder on macOS, the desktop's file manager on Linux), and
  **Copy** copies the path (Linux uses `xdg-open`). **Open** appears only when you use the
  dashboard on the computer it runs on (at `127.0.0.1` or `localhost`) and
  that computer has a desktop; otherwise the folder would open on the
  server, so **Copy** is shown instead. SWM only ever opens a job's or
  collection's own folder.
- **Will be saved in**, under the Collection picker on every job form,
  shows the folder a new job will use. By default a job goes into its
  collection's folder, as `<collection>/jobs/<job number>`. If you give a
  job its own **Storage location**, it is saved there and still belongs to
  its collection.
- The **New collection** form shows the folder the collection will get,
  `<default location>/collections/<identifier>`, and warns at once if
  that identifier is taken.
- **Settings › Storage** shows the default storage location as a full
  path. It decides where *new collections* go, and where the Default
  collection is made the first time it is needed. **Changing it moves
  nothing:** existing jobs and collections stay where they are, and new
  jobs in an existing collection still go into that collection's folder.

**Sizes on disk** never slow the dashboard down. A folder of up to 2,000
files and folders is measured on every refresh. A larger one is measured
in the background: its card shows **Calculating…** until the first figure
arrives, then refreshes it every 30 seconds ("recalculating" meanwhile).
Before you delete something, its size is measured exactly, because that
figure is part of what you confirm.

## Collections

A collection groups the jobs that belong together and keeps them in one
folder. It is also the unit within which repeated files are stored only
once.

Every job belongs to a collection: the one you choose, or the **Default**
collection if you choose none. SWM makes the Default collection under the
default storage location the first time a job needs it. Jobs made before
collections existed stay where they were and are listed as "Older jobs,
not in a collection".

### What a collection has

- A **name**, which you can change, and an **identifier** made from the
  name when the collection is created (for example `qnl-2026`). The
  identifier never changes, because it is written into folder paths and
  archive records. Two collections cannot have the same name or
  identifier; the form says so at once.
- A **folder**, `collections/<identifier>/` under the storage location (or
  a location you choose when creating it). It holds `collection.json`, the
  shared index `index.sqlite`, and `jobs/`, which holds every job.
- **Descriptive metadata**, in the same fields a job has. Each job
  inherits the collection's values for any field it does not set itself.
  Each job also carries a `Relation` naming the collection and a
  `Collection` field with its identifier, so a WARC file still says which
  collection it came from after it leaves the folder.

The folder is fixed when the collection is made. To keep a collection
somewhere else, create a new collection with that storage location, then
delete the old one if you no longer need it.

### A collection's card

Each card on the **Collections** page shows:

- its jobs and their states, size on disk, metadata fields and last
  activity;
- its storage location, with **Copy** and, where possible, **Open**;
- **payloads reused**: files stored once and shared across its jobs ("Off:
  each job keeps its own copy" when that is turned off, "None yet" before
  any repeat);
- **search documents**: how many documents it has in the search index, or
  "Not indexed";
- **View jobs** (the job list filtered to this collection) and **Replay**;
- a **⋮** menu with **Edit** (name, description and the store-once
  setting), **Metadata**, **Index for search**, **Rebuild duplicate index**
  and **Delete**;
- a warning with **Re-crawl them** when pages are missing their original
  (see below).

**New collection** and **Edit** open the same form in a pop-up.

### Stored once across the collection

Within a job, SWM already stores a repeated file only once: the second time
it is written as a *revisit* record pointing at the first copy. A
collection extends this across all its jobs. `index.sqlite` in the
collection's folder records every capture (address, date, content
fingerprint, and the WARC file and record that hold it). When any job
meets content the collection already holds (an image, a stylesheet, a
script, a media file), it writes a revisit pointing at the existing copy.

Nothing is lost: revisit records are standard WARC 1.1 records (the
`identical-payload-digest` profile) that replay tools understand. The same address with different content is stored in
full, so the index is also a history of each page. Even a collection's
first job benefits, because the stylesheet, scripts and images its pages
share are stored once. `dedup-summary.json` in a job's folder has the
numbers, and each job's row shows how many files it reused (hover for how
many of those are held by other jobs).

This is on by default and can be turned off per collection, on the form
or with `--no-cross-job-dedup` on the command line. Every job then stores
everything in full.

**Two consequences:**

- A job's WARC files are no longer complete on their own. Replaying a job
  brings in the WARC files of the jobs it refers to; replaying the
  collection loads them all.
- **Deleting** a job that later jobs refer to leaves their pages without
  that content. Before you delete, the dialog names the jobs and the
  number of records that refer into it. Afterwards, the collection's card
  lists the pages missing their original, and **Re-crawl them** sets them
  up as the starting pages of a new crawl in the same collection. Once
  stored again, they leave the list.

### What changed since last time

Two captures of the same page are rarely byte-for-byte identical (a
timestamp, a session value, the menu item marked as current). So the index
also keeps a fingerprint of each page's *words and links*, ignoring
scripts, styles and markup. Compared with the collection's last capture of
each page, a job's pages are reported as **new**, **changed** or
**unchanged**. A page that now answers "not found" (404 or 410) is
**gone**, and pages the collection held that the job did not reach are
**not visited** (which says nothing about whether they still exist).

The counts are on the job's row; **Page changes** in its **⋮** menu,
`changes.json` in its folder, or `/api/crawls/<id>/changes`, lists every
page. Unchanged pages are still
stored, as the WARC standard requires; the saving is in the files around
them.

### Deleting

Deleting a collection removes it and its jobs from the dashboard. The
dialog tells you how many jobs it holds, its size and where its files are.
Tick **Also delete its files from disk** to remove the files as well;
unticked (the default), the files stay where they are. The collection's
index is not kept either way, so a new collection with the same name
starts fresh. A collection is not deleted while one of its jobs is
running unless you force it.

Jobs in one collection can run at the same time, from the dashboard or
the command line.

### Upkeep

- **Rebuild duplicate index** reads every job's WARC files back into the
  collection's index. Use it for jobs made before the index existed, or if
  the index was lost. The WARC files are not changed.
- **Index for search** runs warc-indexer over every crawl's and
  recording's WARC files, each document carrying the collection's name.
  See [Search indexing](#search-indexing).

Both are also available on the command line: `swm collection reindex` and
`swm collection index-warc`. See [Command line](command-line.md#collection).

## Confirmations

Nothing starts, is created, changes or is deleted without your
confirmation. Starting any job, **Start now**, **Continue**, **Stop**,
**Force stop**, creating a collection, saving changes to a collection or
a description, each Save in Settings, and deleting a job or collection
all open a dialog that says what will happen and the facts behind it:
what is captured, the collection and the exact folder; or, for a
deletion, what depends on it and where its files are.

- The button names the action ("Start crawl", "Delete collection").
- **Cancel**, **Escape** or a click outside the dialog changes nothing.
- A deletion's dialog opens with **Cancel** selected, so pressing Enter
  alone never deletes.
- **Pause** and **Resume** are not confirmed, because both can be undone
  at once.

## Describing a capture (metadata)

Every job form ends with **Describe this capture**: who or what is
captured and why, in the terms a catalogue uses. The model is
Archive-It's: the fifteen Dublin Core 1.1 elements (Title, Creator,
Subject, Description, Publisher, Contributor, Date, Type, Format,
Identifier, Source, Language, Relation, Coverage, Rights) plus
**Collector**. Every element can repeat, and you can add custom fields.

Values can be set at two levels. The **Whole job** tab applies to every
seed or target; a seed's own tab holds values that replace the job's for
that element. Title, Identifier, Date, Type and Collector are filled in
from the job where you leave them empty; nothing else is guessed.

SWM writes the metadata to:

- `metadata.json` in the job's folder: the job's fields, each seed's own
  fields, and each seed's effective fields (its own over the job's, with
  the defaults filled in). This is the current, authoritative record.
- a `metadata` record beside the information record at the start of each
  WARC file, in `application/warc-fields` format with `dc.` names
  (`dc.title`, `dc.subject`, plus `collector` and `custom.<name>`), so a
  file that leaves the folder still says what it is;
- the manifest of a Facebook or Instagram capture, and a *Description*
  table at the top of its readable pages.

To change metadata later, choose **Metadata** in a job's **⋮** menu (or on
a collection's card). `metadata.json` and the manifest are rewritten; a
WARC file already written keeps the values of its moment. The same dialog
exports a sheet (`metadata.csv`: one row per seed, a column per value, and
a `*` row for the whole job). Every form can **import** such a sheet or
**copy** the metadata of an earlier job, which suits a monthly recapture
of the same account.

For the command line and YAML, see [Command line](command-line.md#metadata).

## Replay

SWM replays archives locally with **Webrecorder ReplayWeb.page**. Nothing
is uploaded: the archive is served from your own computer at
`127.0.0.1`.

- **Replay** on a job opens its archive at the page it started from.
- **Replay** on a collection opens a page listing the collection's
  distinct starting addresses, each with the captures behind it. Replay
  uses the collection's combined archive, so content one job refers to
  another for is there. A social media capture gets **Open pages** (the
  readable pages of its latest capture, with older ones a link away) and
  **Replay WARC** when it has a WARC.
- If a job's starting page is not in its archive (for example, a theme
  that did not keep it), replay opens on a list of the pages that are,
  and says why.

The replay server uses port 8091. If that port is taken, it uses the next
free one (up to ten above) and links to the port it actually uses.

## Search indexing

SWM can turn captures into search documents in the schema of
**warc-indexer** (webarchive-discovery), the format SolrWayback and the
UK Web Archive tools use. Each document points back to its WARC record, so
a search result can be replayed.

### Social media captures

A Facebook, Instagram, X or YouTube capture has **Index** in its **⋮**
menu. It writes one document per post, comment, profile or video to
`index/<platform>-index.jsonl` in the job's folder, with a summary in
`index/index-manifest.json`, and offers the file for download. Afterwards
the menu shows the document count and indexes again on request.

A generic WARC indexer would index the hundreds of scripts and images
behind each page, whose text is mostly navigation. SWM indexes the
records themselves instead: the text, author, time and media of each
item. What each document holds is described in the
[Developer guide](developer-guide.md#search-document-fields).

### Crawls and recordings

Crawls and recordings hold ordinary web pages, which are indexed from the
WARC files by **warc-indexer** itself. SWM includes a patched copy in
[`warc-indexer/`](../warc-indexer/README-SWM.md). It needs **Java 11 or
newer**. Build it once in that folder:

```bash
./mvnw -q -DskipTests package          # Windows: .\mvnw.cmd -q -DskipTests package
```

Then choose **Index WARC** in a crawl's or recording's **⋮** menu. It runs
in the background, and the job shows which file it is on, how many
documents so far and how long it has run. It writes
`<warc file name>.jsonl` beside each WARC file, a log in `warc-index.log` and a record
of the run in `warc-index-manifest.json`. If it fails, the job shows the
indexer's own error and a link to the full log.

**Settings › Indexer** shows where Java, the warc-indexer program and its
configuration are, and how much memory it may use. Each may be left
empty: SWM then looks for Java through `JAVA_HOME` and the PATH, for the
program in `warc-indexer/target`, and for the configuration beside it. If
something is missing, **Index WARC** opens this section with the reason
and the exact build command for your machine.

To index a whole collection, use **Index for search** on its card.

## Machine resources

The menu shows the machine's spare CPU, memory and disk, and each running
job shows what its worker, browser and helpers use.

Before a job starts, SWM checks the machine against the levels in
**Settings › Resource warnings**: by default, less than 15% of CPU or
memory free, or less than 10% of the disk the job will write to. If a
level is crossed, you can **start anyway**, **wait** or **cancel**. A job
told to wait is shown as *waiting* and starts by itself when every
resource is back above its level (one waiting job at a time, oldest
first), or straight away with **Start now**. Running jobs are never paused
by this check.

The command line reports the same with `swm resources`; see
[Command line](command-line.md#resources).

## Appearance and themes

**Appearance**, at the top right, sets the dashboard theme, the colours
(follow the system, light or dark), a high-contrast option and the text
size (default, large or larger). Your choices are kept in this browser.

Every colour pair meets WCAG AA contrast, no text is smaller than 12 px,
the pages have proper headings and a skip link, and the dashboard passes
axe-core's WCAG 2.2 AA checks.

### The themes

A theme sets the dashboard's colours, the icon for each kind of job and
for collections, its layout and its typeface. SWM includes four:

- **Nebula** (the default): indigo and violet, light and dark palettes,
  full-colour icons, the Inter typeface and the **board** layout.
- **Aurora**: white cards on a soft blue-grey, with a vivid blue accent,
  also in the board layout.
- **Midnight**: dark navy and cyan, made for dark mode.
- **SWM standard**: the classic layout.

The board layout has the overview tiles, the status and activity charts,
the busiest collections and the job table described under
[Jobs](#jobs). The status chart's colours are fixed rather than themed,
chosen so that every pair stays distinct for colour-blind readers, and
its counts are always written beside it.

**Settings › Dashboard themes** lists the themes with their icons,
installs a new one from a `.zip`, downloads any theme as a starting point,
and removes installed ones. Installed themes are kept in `ui-themes/`
beside the dashboard's database; a theme folder copied there by hand is
picked up too.

### Making your own theme

A theme is one folder:

```text
harbour/
  theme.json
  icons/crawl.svg  recording.svg  facebook.svg  instagram.svg
        x.svg  youtube.svg  collection.svg
```

```json
{"schema": "swm-ui-theme-v1", "name": "Harbour", "version": "1.0",
 "author": "Reading Room", "description": "Teal and slate.",
 "icon_style": "mono", "icon_size": "normal", "layout": "classic", "font": "system",
 "colors": {"light": {"accent": "#0E7490", "accent-hover": "#155E75"},
            "dark":  {"accent": "#67E8F9"}},
 "icons": {"instagram": "icons/camera.svg"}}
```

- Only `name` is required. The folder name is the theme's id; a theme
  installed from a zip takes its id from `id` in `theme.json`, or else
  from its name.
- **Colours** replace the dashboard's own colours one by one, separately
  for light and dark. The standard theme's `theme.json` lists every colour
  and is the easiest starting point; download it from Settings.
- **Icons** the theme leaves out come from the standard theme. With
  `"icon_style": "mono"` an icon is drawn in its job type's colour, so it
  follows light, dark and high contrast; with `"color"` it is shown as
  drawn. `"icon_size": "large"` shows each job's icon as a tile beside its
  name.
- `"layout"` is `"classic"` or `"board"`, and `"font"` is `"system"` or
  `"inter"` (Inter is included under the SIL Open Font License). A theme
  chooses a layout and typeface; it cannot add new ones. High contrast
  always uses the dashboard's own colours.

**A theme cannot run anything.** Colours must be plain colour values
(`#hex`, `rgb()`, `hsl()`). Icons must be plain SVG drawings, with no
scripts, event handlers, embedded content or outside references, and are
only ever shown as images. A zip that fails any check is refused, with the
reasons. A folder copied in by hand keeps what passes, and Settings says
what was left out. Settings also flags any colour pair below WCAG AA
contrast (4.5:1).

## Changing the help text

The text behind each **?** comes from `webarc/help_text.yaml`, one entry
per field. To change it for your installation without editing SWM, copy
that file to `help_text.yaml` next to the dashboard's database
(`webarc-state/help_text.yaml` by default) and keep only the entries you
want to change. Each one replaces the built-in text; an empty entry hides
that **?**. The file is read again every time the page loads.

## Blocked sites

Government and company sites often sit behind protection services (F5,
Cloudflare, Imperva, Akamai and others) that answer with a block or
challenge page, sometimes with a normal "200 OK" status. SWM detects these
pages and, for automated crawls:

1. on the first block, slows down and pauses for a while;
2. after repeated blocks, stops that seed;
3. after a successful page, returns to normal.

Some services show a short challenge page that solves a puzzle and then
loads the real page (for example AWS WAF's "202" challenge). SWM waits up
to `challenge_grace` seconds for the real page before capturing it.

Pages that load their records separately (search portals, endless
scrolling lists) depend on those data requests being archived intact. SWM
warns when a page's content looks incomplete (blocked or empty data
requests, content lost when the page moved on), and
`swm inspect <job folder>` lists the affected addresses afterwards. A page
can replay with its layout intact but its records missing, so check these
warnings before relying on a capture.

This is **not** a way around restrictions. For lasting blocks, ask the
site owner to allow your archiving address and an honest, identifying
browser name. The settings involved are listed in the
[Capture guide](capture-guide.md#crawl-settings).

## Known limitations

- Records are rebuilt from the browser's network events, not from raw
  network traffic.
- Streaming media is kept only as far as the browser requested it: the
  parts that were played or loaded.
- Signed, tokenised and time-limited addresses may behave differently in
  replay.
- An interactive recording holds only the pages and actions you visited.
- `native` mode can see every page opened in its Chrome profile; use a
  clean, dedicated profile.
- Large recordings take longer to open and replay.
- Recordings started from the dashboard open the browser on the computer
  running the dashboard.
- Facebook can change its pages and data without notice. SWM records what
  it could not read and never claims a Page capture is complete. Only
  Facebook **Pages** are supported, and comment limits are approximate
  because one Facebook response can carry several comments.
- YouTube video files are the version YouTube served within the chosen
  resolution, joined by yt-dlp; the original upload is never available.
  Community post dates are estimates from the relative time YouTube shows.
  Poll results, members-only and age-restricted content, and anything
  YouTube withholds from the session are not captured.
- Pausing a job keeps its worker running; a paused job does not survive a
  restart of the dashboard.
