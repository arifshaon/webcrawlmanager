# Command line

Everything SWM does can be run from the command line. Run a command as
`python -m webarc.cli <command>` in a source installation, or as
`swm <command>` if SWM is installed as a package or with the Windows
installer. `python -m webarc.cli <command> --help` lists a command's
options.

## Contents

- [Commands at a glance](#commands-at-a-glance)
- [How command-line jobs relate to the dashboard](#how-command-line-jobs-relate-to-the-dashboard)
- [serve](#serve)
- [record](#record)
- [crawl and validate](#crawl-and-validate)
- [collection](#collection)
- [metadata](#metadata)
- [replay](#replay)
- [inspect and extract](#inspect-and-extract)
- [index](#index)
- [index-warc](#index-warc)
- [resources](#resources)
- [Environment variables](#environment-variables)

## Commands at a glance

| Command | What it does |
|---|---|
| `serve` | Start the web dashboard |
| `record URL` | Record a session: you browse, SWM archives what loads |
| `crawl CONFIG` | Run an automated crawl from a YAML configuration |
| `validate CONFIG` | Check a configuration and print it with all defaults filled in |
| `collection …` | Create, list, show, re-index and delete collections |
| `metadata export JOB` | Write a job's metadata as a sheet (`metadata.csv`) |
| `replay FOLDER` | Replay the WARC files in a folder in your browser |
| `inspect FOLDER` | List what the WARC files in a folder captured |
| `extract FOLDER OUT` | Copy WARC files into one smaller file for sharing or diagnosis |
| `index CAPTURE` | Make search documents from a social media capture |
| `index-warc JOB` | Run warc-indexer over a crawl's or recording's WARC files |
| `resources` | Show spare CPU, memory and disk, and what running jobs use |

## How command-line jobs relate to the dashboard

By default, a `crawl` or `record` started from the command line is a full
member of the dashboard: it is registered in the dashboard's state file,
placed in a collection, and listed with the collection's other jobs.

- `--db` is the dashboard's state file. The default,
  `./webarc-state/webarc.db`, is the dashboard's own when it runs from the
  same folder.
- `--warc-root` is the dashboard's storage root (default `./warcs`), under
  which the Default collection lives.
- `--collection NAME` puts the job in that collection. It accepts a name,
  an identifier or a number. A name that matches no collection is an
  error, so a typo never creates a collection by accident; add
  `--create-collection` to make it on the spot. Without `--collection`,
  the job goes into the Default collection.
- `--standalone` runs the job outside any collection, writing to the
  configuration's output folder and keeping no record in the state file.

Run command-line jobs from the folder the dashboard runs in, or give
`--db` and `--warc-root` the same paths the dashboard uses, so both see
the same jobs and collections.

## serve

Start the dashboard at <http://127.0.0.1:8080>.

```text
swm serve [--host HOST] [--port PORT] [--db DB] [--warc-root WARC_ROOT]
          [--simulate] [--allow-remote-recording]
```

| Option | Meaning |
|---|---|
| `--host` | Address to listen on. Default `127.0.0.1` (this computer only). |
| `--port` | Port. Default `8080`. |
| `--db` | State file. Default `./webarc-state/webarc.db`. |
| `--warc-root` | Storage root. Default `./warcs`. |
| `--simulate` | Run simulated jobs without a browser, to try the dashboard. |
| `--allow-remote-recording` | Allow recordings even when the dashboard listens on a network address. The browser then opens on the server's desktop. |

## record

Open a visible browser at `URL` and record everything that loads while
you browse. Close the browser or press `Ctrl+C` to finish.

```text
swm record URL [--name NAME] [--output OUTPUT] [--browser {headed,native}]
           [--operator OPERATOR] [-v] [--db DB] [--collection COLLECTION]
           [--standalone] [--create-collection]
```

| Option | Meaning |
|---|---|
| `--name` | Session name. Default: taken from the host. |
| `--output` | Folder for a standalone session, which is written to `<output>/<name>/`. |
| `--browser` | `headed` (SWM-managed Chrome, recommended) or `native` (your installed Chrome, with a dedicated profile). |
| `--operator` | Who is capturing, stored in the WARC. |
| `-v` | Show detailed logging. |
| `--db`, `--collection`, `--standalone`, `--create-collection` | See [How command-line jobs relate to the dashboard](#how-command-line-jobs-relate-to-the-dashboard). |

```bash
swm record https://example.org/ --name example-native --browser native --operator "Archive Team"
```

## crawl and validate

```text
swm validate CONFIG
swm crawl CONFIG [-v] [--db DB] [--yes] [--wait] [--no-resource-check]
          [--collection COLLECTION] [--warc-root WARC_ROOT] [--standalone]
          [--create-collection]
```

`validate` reads the configuration and prints it with every default
filled in, without crawling. `crawl` runs it. The configuration is
described in the [Capture guide](capture-guide.md#crawl-settings).

Before a crawl starts, SWM checks whether the machine is short of CPU,
memory or disk, using the warning levels in the dashboard's Settings. At a
terminal, it asks whether to start anyway, wait or cancel.

| Option | Meaning |
|---|---|
| `--yes`, `-y` | Start even if the machine is short of something, without asking. |
| `--wait` | If the machine is short of something, wait until it is free, then start. |
| `--no-resource-check` | Skip the check. |
| `-v` | Show detailed logging. |
| `--db`, `--warc-root`, `--collection`, `--standalone`, `--create-collection` | See [How command-line jobs relate to the dashboard](#how-command-line-jobs-relate-to-the-dashboard). |

A crawl run without a terminal starts anyway and prints the warning.

## collection

```text
swm collection create NAME [--description TEXT] [--storage-dir FOLDER]
                           [--metadata-json JSON] [--metadata-file FILE]
                           [--no-cross-job-dedup]
swm collection list [--json]
swm collection show COLLECTION [--json]
swm collection reindex COLLECTION
swm collection index-warc COLLECTION [--memory SIZE]
swm collection delete COLLECTION [--purge] [--yes]
```

Every `collection` command also takes `--db` and `--warc-root`.
`COLLECTION` is a name, identifier or number.

| Command | What it does |
|---|---|
| `create` | Make a collection and its folder. `--storage-dir` puts the folder somewhere other than the default storage root. `--no-cross-job-dedup` makes every job store everything in full. |
| `list` | List collections and how many jobs each holds. |
| `show` | Describe one collection and list its jobs. |
| `reindex` | Rebuild the collection's duplicate index from its WARC files. The WARC files are not changed. |
| `index-warc` | Run warc-indexer over every job's WARC files. `--memory` sets Java's memory (default: the Settings value, else `2g`). |
| `delete` | Say what deleting means, then ask. `--purge` deletes the files from disk too; `--yes` skips the question. |

Metadata for `create` is a JSON array of `{name, value}` fields, or a
file: JSON in that shape, or a metadata sheet as the dashboard exports it.

```bash
swm collection create "QNL 2026" --description "The library's own sites" \
    --metadata-json '[{"name": "Subject", "value": "Libraries"}]'
swm collection create "Elections" --metadata-file elections-metadata.csv
swm crawl config.yaml --collection qnl-2026
swm record https://example.org/ --collection "QNL 2026"
swm collection delete qnl-2026
```

## metadata

```text
swm metadata export JOB_FOLDER [--output FILE]
```

Writes the job's metadata as `metadata.csv` in its folder (or to
`--output`; `-` prints it), one row per seed with a `*` row for the whole
job.

A crawl configuration can carry metadata for the whole job and for each
seed:

```yaml
crawl_name: qatar-ballers
operator: Qatar National Library
metadata:
  Subject: [Football, Qatar]
  Rights: Captured for preservation; rights remain with the publisher
seeds:
  - url: https://example.org/
    metadata:
      Title: Example site
```

## replay

```text
swm replay FOLDER [--url URL] [--collection NAME] [--replay-root FOLDER]
           [--host HOST] [--port PORT] [--self-host]
```

Combines the WARC files in `FOLDER` into a replay archive, builds a small
ReplayWeb.page site, starts a local server and opens your browser.

| Option | Meaning |
|---|---|
| `--url` | The captured address to open first. |
| `--collection` | Name for the replay. Default: taken from the folder name. |
| `--replay-root` | Folder for the generated replay files. |
| `--host` | Address to listen on. Default `127.0.0.1`. |
| `--port` | Port. Default `8091`; if taken, the next free port (up to ten above) is used and shown. |
| `--self-host` | Use ReplayWeb.page files kept locally instead of the jsDelivr CDN, for offline machines. Put compatible `ui.js` and `sw.js` in `<replay-root>/vendor/`. |

```bash
swm replay warcs/example-session --port 8094 --url https://example.org/page
```

## inspect and extract

```text
swm inspect FOLDER [--grep TEXT] [--hosts]
swm extract FOLDER OUTPUT.warc.gz [--max-mb MB]
```

`inspect` lists every captured response; `--hosts` counts them per host
instead, and `--grep` shows only addresses containing the text. It also
reports pages whose dynamic content looked incomplete.

`extract` copies the WARC files into one file, leaving out bodies larger
than `--max-mb`. Use it for diagnosis or sharing, not as a replacement
for the original.

## index

Make search documents from a Facebook, Instagram, X or YouTube capture.

```text
swm index CAPTURE_FOLDER [--output FILE] [--collection NAME]
          [--platform {auto,facebook,instagram,x,youtube}]
          [--source-root ROOT] [--relocate] [--json]
```

| Option | Meaning |
|---|---|
| `--output`, `-o` | Where to write. Default `<capture>/index/<platform>-index.jsonl`, with a summary in `index-manifest.json`. |
| `--collection` | Collection name every document carries. Default: the capture's name. |
| `--platform` | Which platform the capture is. Default: detected from its manifest. |
| `--source-root` | The path or web address under which the WARC files will be kept. Each document's `source_file_path` becomes that prefix plus the file name. Default: where each file is now. |
| `--relocate` | Do not re-index: rewrite `source_file_path` in an existing index to `--source-root`. Needs neither the records nor the WARC files; takes the capture folder or the index file itself. |
| `--json` | Print the summary as JSON. |

WARC files usually move when a repository takes them in. The file name
plus `source_file_offset` stays the stable key, and `warc_key_id`
identifies the record wherever the file ends up.

```bash
swm index warcs/12 --collection "Heritage 2026"
swm index warcs/12 --source-root https://repo.example/warcstore/
swm index warcs/12/index/facebook-index.jsonl --relocate --source-root s3://archive/warcs
```

## index-warc

Run warc-indexer over a crawl's or recording's WARC files, writing
`<warc file>.jsonl` beside each one. The program must be built first; see
[Search indexing](user-guide.md#crawls-and-recordings).

```text
swm index-warc JOB_FOLDER [--warc FILE] [--collection NAME] [--memory SIZE]
               [--db DB] [--json]
```

| Option | Meaning |
|---|---|
| `--warc` | Index only this WARC file (by name). |
| `--collection` | Collection name every document carries. Default: the folder name. |
| `--memory` | Java's memory. Default: the Settings value, else `2g`. |
| `--db` | Use the Indexer settings (Java, program, configuration) from this state file, if it exists. |
| `--json` | Print the run's record as JSON. |

It shows progress as it goes and, on failure, the reason and the
indexer's last lines.

## resources

```text
swm resources [--db DB] [--warc-root WARC_ROOT] [--json]
```

Shows spare CPU, memory and disk, and what the running jobs in the state
file use. `--json` prints the raw reading. The machine's CPU and memory
are read from Windows or Linux directly; each job's own share needs
`psutil`, which `requirements.txt` installs.

## Environment variables

| Variable | Used for |
|---|---|
| `SWM_THEME_AI_PROVIDER`, `SWM_THEME_AI_MODEL`, `SWM_THEME_AI_ENDPOINT`, `ANTHROPIC_API_KEY` | The AI theme judge for command-line crawls (the dashboard uses Settings instead). Other theme judge settings follow the same pattern: `theme.ai.<name>` becomes `SWM_THEME_AI_<NAME>`. |
| `SWM_WARC_INDEXER_JAR`, `SWM_WARC_INDEXER_CONF`, `SWM_JAVA` | Where warc-indexer, its configuration and Java are, when there is no dashboard state file. |
| `SWM_TOOLS_DIR` | A folder holding ffmpeg and Deno for YouTube captures, if they are not on the PATH. |
