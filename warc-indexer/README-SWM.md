# warc-indexer, the SWM fork

This folder is a copy of [netarchivesuite/warc-indexer](https://github.com/netarchivesuite/warc-indexer)
at release **3.5.1** (upstream commit `76e95db`, 14 September 2026), the
Royal Danish Library's continuation of the British Library's
webarchive-discovery indexer. It reads WARC and ARC files, runs Apache Tika
over every record, and writes one document per record to Solr, OpenSearch or
files, in the schema SolrWayback and the UK Web Archive tooling use.

It is kept here so that Simple Webcrawl Manager (SWM) can index the WARC
files it captures with a known, patched build. It is **not yet wired into
SWM**: nothing in the Python package runs it. That comes after it has been
tested on real captures.

Everything under this folder is licensed under the **GNU General Public
License version 2** (see `LICENSE.GPL2`), as upstream is. The copyright
notices and file headers of the original are unchanged; each modified file
carries a notice at the top saying it was changed and when, as the licence
asks. SWM itself, outside this folder, keeps its own licence: SWM runs the
indexer as a separate program and never links to it, which is the same
arrangement SWM has with gallery-dl.

## What was changed, and why

Two defects were found while indexing an SWM Facebook capture. Both are
small, both are meant to go upstream as a pull request, and this fork is
what SWM builds until they are merged.

### 1. The charset the server declared is now honoured

**Symptom.** A Facebook page served as `Content-Type: text/html;
charset="utf-8"` and without a `<meta charset>` tag was indexed as
ISO-8859-1. Every non-Latin character in the extracted text was mojibake:
Bengali names became `à¦¤à¦¾à¦œ`, Arabic would fare the same, and the
language was detected as Latin. Any page that relies on the HTTP header
alone, which includes the large social platforms, is affected.

**Cause.** `TikaPayloadAnalyser` handed Tika the raw bytes with no encoding
hint. Tika sniffed them and, on a page that is mostly ASCII script, guessed
ISO-8859-1. Passing the declared charset to Tika as a hint is not enough on
its own: Tika's default detector treats it as a bias, and a page really sent
as windows-1256 still came out as Cyrillic.

**Change** (`src/main/java/uk/bl/wa/analyser/payload/TikaPayloadAnalyser.java`).
The charset parameter of the served `Content-Type`, which the indexer
already records in `content_type_served`, is parsed and, when it names a
charset the JVM supports, placed in the Tika metadata before parsing. An
encoding detector that returns that charset first, and otherwise defers to
Tika's default detection, is put into the parse context, where Tika's HTML
and plain-text parsers look for one. The served type itself is never passed,
so Tika's type detection is unchanged. This is what a browser does: the HTTP
header's charset takes precedence over sniffing.

**Evidence.** `TikaPayloadAnalyserCharsetTest` covers the header shapes
servers send (quoted, unquoted, aliases, unknown names), a UTF-8 page with
Bengali and Arabic and no meta tag, and a page sent as windows-1256. On the
SWM sample WARC the post's text now indexes intact where the unmodified
3.5.1 build garbles it.

### 2. The JSON Lines output now names the WARC file's path

**Symptom.** With `-o DIR -F jsonl`, every document had `source_file_path:
null`, while the XML and Solr outputs carried the path. A consumer of the
JSON output could not locate the record without knowing which folder the
WARC was in.

**Cause.** `SolrRecord.toMemento()`, which the JSON writer uses, copied the
file name and offset but never the path; `Memento` had the JSON property
but no setter for it.

**Change** (`src/main/java/uk/bl/wa/solr/SolrRecord.java`,
`src/main/java/uk/bl/wa/Memento.java`). The path is copied like the other
two fields, through a new accessor pair. `SolrRecordMementoTest` checks the
JSON.

## Build

Java 11 or newer and Maven. From this folder:

```bash
mvn -q package                 # runs the test suite, then builds the jar
mvn -q -DskipTests package     # jar only
```

The result is `target/warc-indexer-3.5.1-jar-with-dependencies.jar`, about
130 MB, self-contained. `target/` is ignored by git; the jar is never
committed.

## Run

One WARC to JSON Lines, no Solr involved:

```bash
java -Xmx2g -jar target/warc-indexer-3.5.1-jar-with-dependencies.jar \
  -c src/main/resources/reference.conf \
  -o ./out -F jsonl --collection "My collection" \
  path/to/capture.warc.gz
```

Other destinations are `-s http://host:8983/solr/collection` for Solr and
`-e http://host:9200/index` for OpenSearch; exactly one of `-o`, `-s`, `-e`
is allowed. `java -jar ... --help` lists everything, and `--dump` prints the
effective configuration.

## Test

```bash
mvn -q test                                              # everything
mvn -q -Dtest='TikaPayloadAnalyserCharsetTest,SolrRecordMementoTest' test
```

Some upstream tests spin up an embedded Solr and take a few minutes.

## Sending the changes upstream

The diff against 3.5.1 is confined to the files named above plus the two
test classes. To offer it back: fork `netarchivesuite/warc-indexer` on
GitHub, apply these changes on a branch, and open a pull request. When it is
merged, SWM can return to upstream releases and this folder can go.

## Bringing in a newer upstream

Replace the tree with the new release, then re-apply the change from each
file whose header carries an SWM notice, and re-run the two test classes.
