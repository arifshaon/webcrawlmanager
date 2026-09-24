# Simple Webcrawl Manager (SWM)

**Simple Webcrawl Manager (SWM)** captures websites into standards-compliant
**WARC/1.1** archives using a real browser, so what is preserved is what a
browser actually saw: the pages themselves and the images, stylesheets,
scripts, fonts and XHR/fetch and media traffic behind them. It is built for
people who need a faithful, self-describing record of web content rather than a
screenshot or a one-off scrape: archivists, researchers, journalists, and legal
and compliance teams.

Two ideas run through the tool.

- **A real browser reaches real content.** Driven either by a person or by
  human-like automation, the browser loads lazy images, infinite-scroll feeds,
  media players and pages that only render after interaction or sign-in, which a
  plain HTTP crawler never sees.
- **Provenance is kept honest.** Every capture is written with its own manifest,
  records what was and was not collected, redacts the operator's own credentials
  and session tokens before anything is saved, and can be replayed locally
  without uploading the archive anywhere.

SWM offers a spectrum of capture workflows, from fully manual to fully
automatic:

- **Interactive recording** — a person drives a visible browser and SWM records
  the traffic their session generates. Best for authenticated pages, complex
  interactions and one-off captures.
- **Automated crawling** — SWM follows configured seeds and scope rules with
  human-like scrolling, randomised delays, mouse movement and network-idle
  waits, and can optionally keep only pages that match a theme, judged by rules
  or an AI model you plug in.
- **Social platform captures** — purpose-built, strictly observational
  collectors for **Facebook** Pages and posts, **Instagram**, **X** and
  **YouTube**. Each produces normalised records, media and browsable reader
  pages alongside the WARC.

Every workflow can run against SWM's bundled browser or an installed system
Chrome (through the Chrome DevTools Protocol, with a dedicated profile that
keeps archiving separate from everyday browsing), and the whole tool is usable
from the **command line** or an optional web **dashboard** that starts,
monitors, pauses, resumes, continues and replays jobs.

Key capabilities:

- **Browser-level WARC capture** — pages, images, stylesheets, JavaScript,
  fonts, XHR/fetch traffic, requested media and other responses the browser
  sees, written to standards-compliant WARC/1.1.
- **Faithful provenance** — per-capture manifests, credential and session-token
  redaction, and honest records of coverage and gaps.
- **Local replay** — replay WARCs through Webrecorder ReplayWeb.page with no
  upload.
- **Inspection and QA** — list captured URLs, summarise hosts, search recordings
  and cut smaller diagnostic WARCs.
- **Search indexing of social captures** — one document per post, comment,
  profile or video in the warc-indexer (SolrWayback) schema, each pointing at
  its WARC record, from the command line or a dashboard button.
- **Flexible browser modes** — `headless`, `headed` and `native` (system Chrome
  via CDP); automated crawls support all three, interactive recording supports
  `headed` and `native`.

## Workflows at a glance

| Workflow | Who controls the browser? | Browser modes | Available through |
|---|---|---|---|
| Interactive recording | The operator | `headed`, `native` | Command line and dashboard |
| Facebook Page capture | SWM scrolls; the curator handles login, verification and manual overrides | `headed`, `native` | Dashboard |
| Instagram capture | SWM drives the signed-in browser in a window; the curator handles sign-in and verification | `headed`, `native` | Dashboard |
| YouTube capture | yt-dlp lists, reads and downloads; SWM drives a browser for the Posts tab; the curator handles sign-in | `headed`, `native` | Dashboard |
| Automated crawling | SWM, using human-like browser behaviour | `headless`, `headed`, `native` | Command line and dashboard |
| Replay and QA | The operator | Default browser | Command line; replay also available from the dashboard |

> **Licence:** SWM is source-available under the
> [PolyForm Noncommercial License 1.0.0](LICENSE). Non-commercial use is permitted;
> commercial use requires a separate written licence from the author.

## Contents

- [Quick start](#quick-start)
- [Install](#install)
- [Choose a capture workflow](#choose-a-capture-workflow)
- [Interactive recording](#interactive-recording)
  - [Start a recording](#start-a-recording)
  - [Operate the recording session](#operate-the-recording-session)
  - [Record embedded and streaming media](#record-embedded-and-streaming-media)
  - [Recording options](#recording-options)
  - [Privacy and sensitive content](#privacy-and-sensitive-content)
- [Facebook Page capture](#facebook-page-capture)
- [Instagram capture](#instagram-capture)
- [X capture](#x-capture)
- [YouTube capture](#youtube-capture)
- [Automated crawling](#automated-crawling)
- [Theme-based capture](#theme-based-capture)
- [Collections](#collections)
- [Browser modes](#browser-modes)
- [Inspection and QA](#inspection-and-qa)
- [Indexing social captures](#indexing-social-captures)
- [Replay](#replay-replaywebpage)
- [Dashboard](#dashboard)
- [Architecture](#architecture)
- [WAF and bot-block handling](#waf-and-bot-block-handling)
- [Known limitations](#known-limitations)
- [Research notes](#research-notes)
- [Licence and citation](#licence-and-citation)

## Quick start

### Record a manual browsing session

PowerShell:

```powershell
python -m webarc.cli record `
  https://example.org/ `
  --name example-session
```

Bash:

```bash
python -m webarc.cli record \
  https://example.org/ \
  --name example-session
```

The visible browser opens immediately. Browse the content that needs to be
preserved, then close the browser or press `Ctrl+C` to finalise the WARC.

### Run an automated crawl

```bash
python -m webarc.cli validate config.yaml
python -m webarc.cli crawl config.yaml
```

### Inspect and replay captured WARCs

```bash
python -m webarc.cli inspect ./warcs/example-session --hosts
python -m webarc.cli replay ./warcs/example-session
```

## Install

### Requirements

- Python 3.10 or newer.
- Playwright, `warcio` and PyYAML, installed from `requirements.txt`.
- Google Chrome for `headed` or `native` browser modes. Playwright's bundled
  Chromium is used for headless crawling.
- For YouTube capture: `yt-dlp` with its `default` and `curl-cffi` extras
  (`pip install -e ".[youtube]"` brings all of it, into the
  same Python that runs SWM), `ffmpeg` to join the video and audio streams
  YouTube serves separately above 720p, and a JavaScript runtime (`deno`
  or `node`) for yt-dlp's player challenges. The Windows installer
  installs all three (ffmpeg and deno through winget; `-SkipYouTubeHelpers`
  leaves them out). Elsewhere: `winget install --id Gyan.FFmpeg -e` and
  `winget install --id DenoLand.Deno -e` on Windows, `brew install ffmpeg
  deno` on macOS, the distribution's packages on Linux. ffmpeg and deno are
  found on the PATH or under the directory `SWM_TOOLS_DIR` names. The
  dashboard's YouTube tab reports which of these it found, refuses to start
  a video run without yt-dlp, and without ffmpeg asks YouTube for
  single-file renditions instead of leaving separate streams.

### Windows

From PowerShell in the project folder:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
playwright install chromium
```

When PowerShell blocks the activation script, allow locally created scripts for
the current user once:

```powershell
Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
```

Then activate the environment again:

```powershell
.venv\Scripts\Activate.ps1
```

### Linux or macOS

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

## Choose a capture workflow

Use **interactive recording** when the content depends on decisions or actions by
an operator, for example:

- authenticated or restricted sessions that you are authorised to preserve;
- search forms, filters or navigation paths that are difficult to automate;
- modal windows, galleries, expandable menus and embedded viewers;
- audio and video players that must be opened or played;
- content generated only after scrolling, clicking or other interaction.

Use **automated crawling** when a website can be explored predictably from one or
more seed URLs using repeatable scope, depth and page-limit rules.

Use **Facebook Page capture** for authorised collection of a public or
authenticated Facebook Page timeline. The curator signs in directly in a
visible, persistent Chrome profile; SWM never receives the password. SWM can
scroll automatically, while the curator can pause scrolling, resolve a
checkpoint or navigate manually without pausing WARC capture.

The two approaches can be used together. An automated crawl can capture the main
site structure, while an interactive recording preserves difficult pages,
applications, media players or authenticated areas.

## Interactive recording

The `record` command opens a visible browser and records the requests and responses
created while the operator browses. It does not automatically follow every link;
the operator decides what is visited and activated.

Recording sessions can be started from the command line (below) or from the
dashboard's **Record session** tab (see [Dashboard](#dashboard)).

The recording browser, like the Facebook, Instagram and X capture
browsers, is launched without Chrome's automation signal
(`navigator.webdriver` is false and no "controlled by automated test
software" bar is shown): a person is driving it, and sites that read that
signal at sign-in, X among them ("We are limiting your login"), otherwise
refuse a password typed by hand. Automated crawls keep the default. A
headed recording still opens a fresh browser context each time, so a site
sees a new device at every sign-in; for a site that throttles new devices,
the `native` browser mode with a dedicated profile keeps the session
between recordings.

### Start a recording

```powershell
python -m webarc.cli record `
  https://example.org/ `
  --name example-session
```

SWM will:

1. create the output folder `warcs/example-session/`;
2. open the starting URL in a visible browser;
3. record browser request and response traffic;
4. write the session to one or more compressed WARC files;
5. finalise the WARC when the session is stopped.

Choose another output root with `--output`:

```powershell
python -m webarc.cli record `
  https://example.org/ `
  --name example-session `
  --output D:\web-archives
```

This writes the session under:

```text
D:\web-archives\example-session\
```

Use a distinct name for each recording so sessions remain separate and easy to
inspect and replay.

### Operate the recording session

During recording:

1. Browse normally in the opened browser window.
2. Visit every page that needs to be preserved.
3. Open menus, tabs, accordions, pop-ups, galleries and embedded viewers.
4. Scroll through pages and allow dynamically loaded content to finish loading.
5. Use the **SWM Recording** control at the bottom-right of the page to pause,
   resume or explicitly capture the current page.
6. Close a modal or player before opening the next one when the site expects only
   one active viewer at a time.
7. Close the browser window, or press `Ctrl+C` in the terminal, to stop recording
   and finalise the WARC.

Stopping the session cleanly is preferable to terminating Python through Task
Manager or killing the terminal, because SWM then has an opportunity to close the
current WARC correctly.

### Record embedded and streaming media

Opening a page containing a video is not always enough to preserve the media.
Streaming systems usually request video or audio in segments as playback
progresses.

For each player that needs to be preserved:

1. Open the player or modal.
2. Wait for the player interface and poster image to load.
3. Start playback.
4. Let the required portion play for several seconds.
5. Seek through other required portions when necessary.
6. Close the player before opening another one.

Only media data actually requested by the browser can be captured. Unplayed or
unrequested parts of a stream may not be available during replay.

SWM includes replay compatibility handling for dynamically created media iframes,
including YouTube players opened through Fancybox. The WARC itself remains
unchanged; the compatibility handling is applied only during local replay.

### Recording options

```text
record URL
  --name NAME          Session name; otherwise derived from the host
  --output PATH        WARC root; session is written to PATH/NAME/
  --browser headed     Visible Playwright-managed Chrome; recommended default
  --browser native     Attach to system Chrome through CDP; advanced
  --operator NAME      Operator name stored in WARC metadata
  -v, --verbose        Show detailed logging
```

Example using native Chrome:

```powershell
python -m webarc.cli record `
  https://example.org/ `
  --name example-native-session `
  --browser native `
  --operator "Archive Team"
```

For `native` mode, use a dedicated Chrome profile and close any Chrome process
already using that profile. Native mode attaches to pages opened by that Chrome
instance, so keep the recording profile clean.

### Privacy and sensitive content

Everything loaded during an interactive session may be written to the WARC,
including authenticated pages, cookies, form submissions, private URLs and media.
Record only content you are authorised to preserve, and manage the resulting WARC
according to its sensitivity and applicable access restrictions.

## Facebook Page capture

The dashboard's **Facebook** tab captures a Facebook **Page** in a visible
Chrome window. Personal profiles, groups and other Facebook surfaces are out of
scope for v1. Use it only for material that you are authorised to preserve.

The browser initially opens with automatic scrolling paused. Sign in if needed,
confirm that the requested Page is open, then use the in-browser control or the
dashboard to select **Start / resume scrolling**. **Pause scrolling** stops only
SWM's scrolling loop: network exchanges, manual navigation and records loaded by
curator actions continue to be written to WARC and noted in the manifest.
Chrome retains its own session cookies in the dedicated local profile. Before
WARC writing, SWM removes Cookie/Authorization request headers, Set-Cookie
response headers and recognised login/session fields from request bodies.

Capture modes are:

- **Date range** — normalise posts between `From` and optional `To`; stop only
  after five consecutive non-pinned timeline posts older than `From`. Pinned
  posts are ignored by the stopping calculation.
- **Latest N posts** — count N non-pinned timeline posts; pinned posts are
  retained without consuming the limit.
- **Until I stop it** — keep scrolling under curator control.
- **End of available timeline** — stop after repeated scroll attempts expose no
  new posts. This is recorded as `end_of_available_timeline`, never “all posts.”
- **Since last capture** — use the durable newest post ID/date from the Page's
  previous run as the lower boundary.

Every run writes WARC plus:

- `facebook-posts.jsonl` and `facebook-posts.csv`;
- comment JSONL/CSV when comments are requested;
- `facebook-album-context.jsonl` and `.csv` when the curator stepped through
  Facebook's photo viewer during the capture (see below);
- `facebook-manifest.json`, recording selection, exclusions, stopping rule,
  failures, detected gaps and continuation provenance;
- `facebook-checkpoint.json` and `facebook-events.jsonl`.

A Facebook photo belongs to an album as well as to the post it was published
in, and the photo viewer's arrows walk the album, not the post. Opening a
post's photo and stepping onwards therefore shows photos of other posts. SWM
keeps each photo seen in the viewer as *album context*: its own post id and
permalink, album, date and caption, with the full-size image fetched into
`media/`. The manifest's `album_context` section says which post the album
was walked from and how many of the photos were that post's own; the reader
page for the post shows them under "Photos opened in the viewer", each marked
as this post's or another's. No post record is made from a viewer response.

When a single post's comments are read, the thread is read automatically in
two stages before the curator is asked for anything. First the thread is
scrolled with wheel events aimed at the comments' own scroll container (in
the single-post dialog, the dialog's scrollable region), as a reader scrolls
it, and expanded through Facebook's own "View more comments" controls;
Facebook is
given thirty seconds without a new comment, not counting time a comment page
is still on its way. Every comment page Facebook serves says whether another
follows, and the capture reads that: a thread Facebook has ended is finished,
whatever count the post states. If Facebook still reports a further page but
scrolling no longer makes the page ask for it, the page is asked to repeat
its own last comment request with the cursor Facebook returned, one page
every one and a half to two and a half seconds, until the thread ends or the
requested number is reached. Nothing is invented and nothing leaves the page:
the request is the client's own with only the cursor changed, and the
response is written to the WARC and read like any other. Only when that too
fails (the page never made such a request, or Facebook refuses it) does the
capture hold and say why, and the curator can load a page by hand and resume,
or stop and save. The manifest records how many pages loaded after a click,
after a scroll, and by continuation, and what Facebook last said about the
thread; `facebook-events.jsonl` records every round.

Posts newer than an optional `To` date are necessarily traversed to reach older
posts. Their exchanges remain in the raw WARC but they are excluded from the
normalised exports. Comment capture is deliberately opt-in because Facebook
paginates comments separately per post; maximum-comments and include-replies
controls bound this best-effort expansion but can still add substantial runtime
and blocking risk.

Facebook verification is a recoverable state. Resolve the checkpoint in the
visible browser and select **Verification resolved — resume**. A stopped or
failed run can be **Continued** as a new, provenance-linked capture; post IDs
already present in the durable Page index are traversed but not re-exported.

The managed headed browser uses an SWM-owned persistent Facebook profile below
the state directory. This lets a curator remain signed in between authorised
captures without SWM asking for or storing the password itself. Protect that
profile as sensitive local data.

Only one Chrome can hold that profile at a time. If a window using it is
still open when a capture starts (typically left from an earlier capture
whose worker ended without closing it), Chrome hands the Page to that window
and the new capture cannot open a browser: the job fails with a message naming
the window to close, and the SWM panel in the old window, once a question to
the capture has gone unanswered for forty-five seconds, shows **Not
connected** with no buttons rather than a Start button that does nothing.
Close every window using the profile, then start the capture again.

The session material the page's bootstrap carries (the capturing account's
ids, the request-signing tokens, the nonces) is removed from the WARC. The
account's ids are replaced by a fixed synthetic number rather than a text
marker: Facebook's client reads `USER_ID` as a number when it starts, and a
capture whose bootstrap said otherwise showed the post for a moment and
then only the logo. Facebook and Instagram captures made between 2 and 16
September 2026 carry the text marker and replay that way; capture them
again.

## Instagram capture

The dashboard's **Instagram** tab captures a profile's posts and reels, or a
single post or reel, through a Chrome the curator has signed in to. SWM
keeps client-compatibility failures to a minimum by letting Instagram's own
web application build the requests in a real browser, and collects from what
Instagram serves it. Instagram can still rate-limit or challenge the account,
session or network; the capture treats those as conditions to wait out or
hand to the curator, never as completeness. Explore, hashtag and
location results are refused as targets: they are selected by Instagram's
ranking and cannot be presented as an archive of anything.

A Chrome window opens for the run and stays until it ends; sign in there,
or clear a verification, when Instagram asks. (A run without a window was
offered once and did not hold up, so it is no longer an option.) A profile is
identified by its numeric id once read, and a post is the profile's only when
the profile's own listing request returned it, under its timeline connection,
and it names no other owner. A signed-in page carries the viewer's feed,
suggestions and other profiles' listings; none of that is the profile's.

**Profile listing** is a choice of two:

- **Browser** — scroll the profile and read what Instagram serves its own
  client, page by page.
- **gallery-dl** — attempt to enumerate the profile through Instagram's
  direct per-user API, in Instagram's order with pinned flags and dates, on
  the browser's signed-in session. It can be faster than scrolling, but
  Instagram may rate-limit or restrict it. SWM runs it with its user
  configuration ignored and every setting stated, reads its output as it
  streams so the capture's stopping rules apply post by post, stops it once
  the capture has what it asked for, and resumes from the cursor it reported
  after a rate limit rather than starting again. The browser is the authority
  on the session: if Instagram refuses the borrowed session while the browser
  is signed out, the curator is asked to sign in once; if it refuses it while
  the browser is signed in, the capture lists through the browser instead and
  the manifest records the fallback and why. Post discovery and listing
  metadata come from gallery-dl and are kept under `evidence/listings/` as
  its output, not as Instagram's responses. Selected media is fetched through
  the browser where possible; comments are collected from the browser. Needs
  `pip install gallery-dl` (GPL-2.0, run as a separate program).

Capture modes match Facebook's: date range, latest N (pinned posts recognised
and kept without consuming the count), until stopped, and since last capture.

Every run writes:

- `media/` — media at the highest resolution available to the capture (the
  CDN derivative Instagram served, which is not shown to be the uploader's
  master), requested by the page itself, content-addressed by SHA-256, every
  carousel component in order;
- `raw/responses/` — every response a kept record was read from, verbatim
  but with the browser session's own bootstrap material removed, and
  `raw/posts/`, `raw/profiles/`, `raw/comments/` — the extracted nodes; every
  record's provenance names its response, decoder, document and path;
- `instagram-posts.jsonl`/`.csv`, `instagram-comments.jsonl`/`.csv`,
  `instagram-profiles.json`, `instagram-media.json`, `checksums.sha256`;
- `instagram-manifest.json`, `instagram-checkpoint.json`,
  `instagram-events.jsonl`;
- `pages/` — reader pages built from the package's own records, plainly
  marked as not Instagram;
- optionally a WARC of every exchange the browser made, credentials and
  session material redacted, which is the record of how Instagram presented
  what was collected. With the gallery-dl listing, the post pages opened for
  comments and the media fetched are in it; the profile listing is not,
  since the browser did not do that part.

The redaction removes the capturing session's tokens and ids from page
bootstrap data and from query strings and form bodies; it never rewrites
JavaScript, and a bootstrap block that states its own byte length is made
to state the new one, since the page's client discards a block whose
number no longer matches. (Captures made with versions between the
introduction of body redaction and 5 September 2026 lack both safeguards,
and their WARCs replay as an empty page with the logo; such a capture
needs to be run again.)

Comments are opt-in. Each post carries a grade of what its comment
collection can support as evidence: complete against Instagram's reported
count, partial, limited by the capture's cap, none reported, or exhausted but
unverified where nothing independent confirms the count. A thread that
stopped yielding is never taken as proof it was complete.

## X capture

The dashboard's **X** tab captures an account's posts, a single post with
its conversation, or what X shows for a hashtag or search, through a
Chrome the curator has signed in to. It is built the way the Instagram
mode is, and X makes it simpler: every request X's web client makes names
its operation in the URL (`/i/api/graphql/<id>/UserOriginalsTimeline`), so SWM
recognises the operations it wants from the page's own traffic and reads
the answers. The rotating operation ids, drifting feature flags and
obfuscated transaction header that every home-made X client chases never
have to be produced. X's rate limits and account checks remain: a 429 is
waited out until the reset time X sends, and a sign-in or verification is
handed to the curator in the window.

Targets, one per line: `@handle` or a profile address (with or without
`/with_replies` or `/media`), a post address, `#hashtag`, or
`search: words`. twitter.com addresses are read as x.com ones. Home,
notifications, messages and the rest of the signed-in client's own pages
are refused.

Attribution follows the rule that fixed Instagram's contamination: the
account is resolved to its numeric id first, and a post is the account's
only when one of the account's own listing operations (keyed by that id)
returned it and its author is that id. X renames these operations from
time to time: the first capture, in September 2026, showed
`UserOriginalsTimeline`, `UserRepliesTimeline` and `UserVideoTimeline`
where the open-source readers knew `UserTweets`, `UserTweetsAndReplies`
and `UserMedia`; SWM accepts both sets, and a capture that lists nothing
records every operation the page made under `client_anomaly` in its
events so the next rename is visible. The home feed, notifications and recommendations
the signed-in client fetches beside the target are written to the WARC
(a WARC without them replays with errors the page never showed), counted
in the manifest under `operations_observed`, and never produce records.

Records are X's own, not Instagram's:

- **Replies are posts**, with their own id, author and media, not comments.
  Every retained post carries `relationship` (original, reply, repost,
  quote) and `capture_role` (target, or conversation_context for other
  people's posts pulled in to explain a conversation, which never count
  toward the account's total).
- **Reposts are kept** as what the account chose to publish on its
  timeline, with the original post and its author under `original_post`;
  the account is never shown as the original's author, in the records or
  on the reader pages. They can be left out with an option.
- **Quotes** are the account's post with the quoted post under
  `quoted_post`.
- **Media** is fetched as a request SWM makes, and the index says so:
  images at the `orig` rendition X serves for a `name=orig` request (with
  `4096x4096`, `large` and the bare URL as fallbacks), videos and GIFs as
  the highest-bitrate progressive MP4 X advertises, never the HLS playlist.
  Each entry records the rendition the page loaded, the one requested,
  what was actually fetched, who asked, and how.
- **Conversations** (opt-in, and always for a post target) open each
  post's page and keep what it replies to and the replies under it as
  context, graded per post like Instagram's comments: complete against the
  reported count, partial, capped, none reported, or exhausted but
  unverified. Deleted or withheld posts in a thread are recorded as
  absences with X's reason.
- **Searches and hashtags** keep every result with its author; the
  manifest describes the capture as what X served this account for this
  query, in this tab (Latest or Top), at this time.

Modes match Instagram's: latest N (the pinned post recognised from X's own
pin instruction and kept without consuming the count), date range, until
stopped, end of observed timeline, and since last capture. X does not
signal the end of a timeline, so a walk that finds no unseen post across
the stall rounds is reported as `timeline_stalled`, never as the end of
the account's posts. Post ids are time-ordered, so since-last compares
ids, and keeps going a little past the previous capture's newest post
before stopping.

Every run writes `x-posts.jsonl`/`.csv`, `x-users.json`, `x-media.json`,
`x-manifest.json`, `x-checkpoint.json`, `x-events.jsonl`,
`checksums.sha256`, `media/`, `raw/responses/` (only responses a kept
record or media file was read from, session material removed) with
`raw/posts/` and `raw/users/`, `pages/` built from the records, and
optionally a WARC with `x-csrf-token`, `x-client-transaction-id`, the
bearer and the cookies redacted on the request side, and the signed-in
account's id removed from the page's session object. JavaScript is never
rewritten: X's bundle builds a URL as `?access_token=${…}` inside a
template string, and a redaction that landed there once left a bundle
that no longer parsed, so replay showed "Something went wrong. Try
reloading." Captures made before that fix need to be run again.

Replaying an X archive has one more requirement that Instagram's does
not. X's client decides whether it is signed in from cookies it can read
(`twid`, `ct0`), and a signed-out client asks a different host and the
sign-in flow for everything, none of which an archive made signed in
holds; the cookies themselves are never archived, since they were set
before the capture began. The replay page therefore sets placeholder
values for those two cookies on the replay origin before the archived
document runs, for archives whose start page is on X, and the client then
asks for the URLs the archive holds. The placeholders are not the
capturing account's values. Replies X served none of are not in the
archive either: a conversation page that carried only the post and a
cursor at capture is what replays. The design and its
reasoning are in `docs/research/x-capture.md`.

## YouTube capture

The dashboard's **YouTube** tab captures a channel's videos, Shorts, live
streams and community posts, a single video, or a playlist. Two engines do
the work, each on the part it is good at:

- **yt-dlp**, imported as a library, lists the channel tabs and playlists,
  reads each video's record and comments, and downloads the files. It is
  the most maintained reader of YouTube's player and its challenges, and
  SWM tracks it rather than competing with it.
- **A browser** on a dedicated Chrome profile reads the Posts tab and post
  comments, which yt-dlp does not cover. It opens the pages a person would
  open, scrolls, and presses "View replies"; it observes what YouTube's own
  client asks for (`ytInitialData` in the page, `youtubei/v1/browse` and
  `youtubei/v1/next` continuations) and never builds those requests itself.

The two are kept apart in the package, because they are different kinds
of evidence. `evidence/yt-dlp/<id>.info.json` is yt-dlp's reading of a
video, whole, and every video record and video comment says which file it
was derived from; the manifest labels it *tool-derived metadata*, not a
platform response. `raw/responses/` holds YouTube's responses to the
browser verbatim, session material removed, pruned at the end to the
responses a kept post or comment was read from.

Targets, one per line: `@handle` or a channel address (with or without a
tab), a `/channel/UC…` address, a video address in any of its forms
(`watch?v=`, `youtu.be`, `/shorts/`, `/live/`), a playlist address, or a
bare video or channel id. The signed-in client's own pages (subscriptions,
history, results) are refused.

What is captured is chosen with two controls that are deliberately
separate: **what to select** (the same modes as the other social captures:
latest N, date range, until stopped, end of listing, since last capture)
and **what to store** (the maximum resolution, from best down to none;
thumbnails, captions, automatic captions, live-chat replays, post images,
comments). A channel's tabs are listed in flat mode, and only a video that
passes the selection is read in full; the date-bounded modes read a video
first when its listing entry carries no date. Posts are read before any
video, so the browser is finished with YouTube before its session is lent
to the downloader.

Records:

- **Videos** carry YouTube's availability vocabulary verbatim (`public`,
  `unlisted`, `private`, `premium_only`, `subscriber_only`,
  `needs_auth`) plus `deleted`, `unavailable` and `unknown`; a private or
  deleted entry in a listing is recorded as an absence with a reason, never
  as a video. Each file under `media/videos/<id>/` is described: the
  rendition YouTube served within the allowed resolution (muxed by yt-dlp;
  never the upload), the thumbnail, each caption track with its language,
  the live-chat replay.
- **Posts** carry their kind (text, image, images, poll, quiz, video,
  shared), the images at the largest size the page offered under
  `media/posts/<id>/`, a poll's options without results (results require a
  vote, which the capture never casts), and both the relative time YouTube
  shows ("3 weeks ago") and an estimate from it, marked as such. A post by
  someone else that YouTube lists on the tab is not the channel's and is
  skipped.
- **Comments** are one model for videos and posts alike: `target_type`,
  `target_id`, `parent_id`, `thread_root_id`, `reply_depth`, with YouTube's
  own `<parent>.<reply>` ids. Each item's comment capture is graded:
  complete against the reported count, partial, capped, none reported,
  exhausted but unverified, disabled, blocked, or stopped by the curator.
  The default cap is 1,000 per item, newest first.
- **Playlists** keep the list and its items in order, including the
  entries YouTube reports as private or deleted.

Sign-in is the curator's, never SWM's. YouTube may answer a request with
"Sign in to confirm you're not a bot", most often from a data-centre
address and for the per-video reads; the run then holds, opens the
sign-in page in the window, and after the curator resolves it lends the
browser's session to yt-dlp as a temporary cookie file readable by the
current user only, outside the package, deleted when the run ends. A
dedicated institutional Google account is the right one to use. The
manifest records whether the capture was signed in and what that means.

Disk space is watched. Below the warning level from Settings the run holds
before the next file and continues by itself once space is freed; below a
critical level a download in progress is stopped mid-file, the partial
file is kept, and the same download resumes after the hold. Stop and save
during a download keeps the partial file too and says so in the events.

Every run writes `youtube-videos.jsonl`/`.csv`, `youtube-posts.jsonl`/`.csv`,
`youtube-comments.jsonl`/`.csv`, `youtube-channels.json`,
`youtube-playlists.jsonl`, `youtube-playlist-items.jsonl`,
`youtube-media.json`, `youtube-manifest.json`, `youtube-checkpoint.json`,
`youtube-events.jsonl`, `checksums.sha256`, `media/`, `evidence/yt-dlp/`,
`raw/responses/`, `pages/` built from the records, and optionally a WARC
of the browser's exchanges on the Posts tab and on each captured video's
watch page, loaded as a person would see it. Video streams are never in
the WARC, so YouTube's own player has nothing to play on replay; during
local replay SWM swaps that player for one playing the downloaded file,
with a note saying so, and the WARC stays as captured. A WARC opened in
another viewer shows the page without playback. Video streams are never
in the WARC: the downloaded files are the objects, and the manifest's replay
statement says what the WARC can and cannot show. Captures of the Posts
tab were built against YouTube's documented shapes and a fixture; the
first real capture calibrates them, and a run that lists nothing records
the requests the page made under `client_anomaly` in its events. The
design and the spike behind it are in `docs/research/youtube-capture.md`.

## Automated crawling

Automated crawling uses YAML configuration to define seeds, browser behaviour,
scope and output settings:

```bash
python -m webarc.cli validate config.yaml
python -m webarc.cli crawl config.yaml
```

SWM drives a real browser rather than relying only on direct HTTP requests. Its
human-like behaviours can include randomised delays, incremental scrolling, mouse
movement and network-idle waits so that JavaScript and lazy-loaded resources have
time to appear before capture.

Core crawl capabilities include:

- same-host, same-domain, path-prefix and regular-expression scope rules;
- maximum depth and maximum page limits;
- URL canonicalisation and frontier deduplication;
- optional robots.txt handling;
- randomised delays, scrolling, mouse movement and network-idle waits;
- block-page detection and controlled back-off;
- compressed WARC/1.1 output with request, response, warcinfo and revisit records.

## Theme-based capture

An automated crawl can carry a **theme**: only pages about one topic are
kept, and every page looked at is written to a selection list with the
reason. It applies to automated crawls alone: in a recorded session the
curator reads each page and decides, and the social media modes select by
account and date.

**What a theme is.** A name, a brief in plain words ("news about the
restoration of heritage sites in Doha; not general tourism"), terms and
phrases in any language (Arabic spelling variants, clitics and plurals
are matched: مكتبة, المكتبات and مكتبةٍ are one term), terms that rule a
page out, address patterns that count for the theme or are never
fetched, hub patterns for listing pages, a date window, and a minimum
score. It lives in the crawl form, the record form, or a `theme:` block
in the YAML configuration, and is written whole into the job's
`theme-summary.json`.

**How a crawl uses it.** Three tiers, cheapest first. Address rules
decide before anything is requested. Links are then triaged from what
the parent page says about them, the link text and the words around it,
and a link the theme is confident about is never fetched. What remains
is fetched, read, and judged from the page's *main* content, with the
menus, headers and footers set aside so a site-wide "Culture" link does
not make every page cultural. Only then is the page's traffic committed
to the WARC. A rejected page cost a request and leaves no record in the
archive; a page the judge could not place goes to a separate review WARC
under `review/`, outside the collection until a curator accepts it. Hub
pages, including the starting page, are always followed and kept as the
way in unless the theme says otherwise. A rejected page's links are not
followed.

**The rules judge** is always on and explainable: a term in the headline
scores 3, in the section, tags or description 2, each mention in the
text 1, an address rule 3; the page is kept at the minimum score, unsure
below it, rejected at zero or on a hard rule (an excluded term in the
headline, a date outside the window, an excluded address).

**The AI judge** is optional and answers the actual question, "is this
page about this news?", from what SWM already holds. It never fetches a
page itself: a fetch by the model would be a different fetch, by a
different client, of a page that may not be the one in the archive. What
it is sent is a choice per theme. The default sends the address, the
headline, the section, the date and a short excerpt of the text and asks
for one word, yes, no or unsure: a few hundred tokens per page, so a
whole crawl fits inside a modest tokens-per-minute allowance. The address
and title alone is smaller still, and judges from less. The full text
asks for reasons and quoted evidence, which SWM checks against the page,
and costs the most. Links are triaged in one call per page from their
text and surroundings, and the model names only the links it is
confident are not the theme's; everything else is fetched and read.

Configure the judge under Settings: Anthropic's Claude through its API
(`pip install -e ".[theme-ai]"`), Azure OpenAI (the resource address
such as `https://my-resource.openai.azure.com`, the deployment name in
the model field, the key and an API version), or any model behind an
OpenAI-compatible endpoint, which for a local Ollama or LM Studio means
nothing leaves the machine. A tokens-per-minute figure makes SWM pace its
questions to stay under the provider's allowance and wait when the minute
is full; an answer of 429 is waited out for the time the provider asks.
The size of each question follows the allowance too: a most-tokens-per-
question setting, or a twentieth of the minute's allowance when it is
left empty, trims the page's excerpt to fit and splits long link lists
into several smaller questions. A ceiling on calls per job guards the
bill; past it the rules decide. A
theme chooses how the two judges combine: the AI decides with the rules
as pre-filter and explanation (the default), the AI breaks ties only, or
both must agree.

**Provenance.** `selection.jsonl` holds one line per page judged and per
link triaged: the decision, which judge made it, the rules' score and
the matched passages, the AI's answer (and, with the full text, its
confidence, reasons and quoted evidence), what it was sent, the model and
a hash of the prompt. `theme-summary.json` holds the theme, the judge,
the counts, the estimated tokens spent and the waits. The job list shows
kept, left out and held for review as the run goes, and a **Selection**
button opens a page built from the log. The API key is kept in the
dashboard's database and never written into a capture.

## Collections

A collection groups the jobs that belong together and gives them a directory
of their own. It is the unit a curator thinks in ("the 2026 election sites",
"the library's own channels") and, in a later version, the unit within which
a page already captured is not stored again unless it has changed.

Every collection has:

- a **name**, which can change, and an **identifier** derived from the name
  when the collection is created, which never changes because it is written
  into directory paths and archive records;
- a **directory** of its own, `collections/<identifier>/` under the storage
  root (or under a location of your choosing), holding `collection.json` and
  a `jobs/` folder that every job run against the collection is placed in;
- **descriptive metadata** in the same fields a job has (the Dublin Core
  elements plus Collector, repeatable, custom fields allowed). Each job in
  the collection inherits these values for every element it does not set
  itself, the way a seed inherits its job's, and carries a `Relation` naming
  the collection and a `Collection` field carrying its identifier, so a WARC
  that leaves the folder still says which collection it came from.

From the dashboard, the **Collections** page lists every collection with its
job count by status, size on disk and last activity, and offers *Jobs* (the
job list filtered to that collection), *Describe* (its metadata), *Replay*
(every WARC in the collection as one archive) and *Delete*. A new collection
is made on that page, or from any job form with **New collection…** beside
the collection picker. The job list can be filtered by collection, and each
job's row names the collection it belongs to.

From the command line:

```bash
swm collection create "QNL 2026" --description "The library's own sites" \
    --metadata-json '[{"name": "Subject", "value": "Libraries"}]'
swm collection create "Elections" --metadata-file elections-metadata.csv
swm collection list
swm collection show qnl-2026
swm crawl config.yaml --collection qnl-2026        # the job goes under the collection
swm record https://example.org/ --collection "QNL 2026"
swm collection delete qnl-2026                     # states what it means, then asks
```

`--collection` accepts a name, an identifier or an id. A name that matches no
collection is an error rather than a new collection, so a typo never files a
job in a collection of its own; add `--create-collection` to make it on the
spot. A job run from the command line against a collection is registered in
the dashboard's state file and listed with the collection's other jobs.
Metadata for `create` is a JSON array of `{name, value}` fields, or a file:
JSON in that shape, or a metadata sheet as the dashboard exports one.

**Stored once across the collection.** Within one job SWM already stores a
repeated payload as a WARC *revisit* record pointing at the first copy. A
collection makes that table durable and shared: `index.sqlite` in the
collection's directory records every capture (URL, date, payload digest, and
the WARC file and record that hold it), and a payload any job of the
collection already holds (an image, a stylesheet, a script, a media file) is
written as a revisit pointing at that copy, whichever job meets it again.
Bytes are saved; nothing is lost, because a revisit is a standard WARC 1.1
record (`identical-payload-digest` profile) that replay tools resolve. The
same URL with different content is stored in full, so the index is also a
history of each page. Each job's row on the dashboard says how many payloads
it reused and how many of those other jobs hold; `dedup-summary.json` in the
job's folder has the numbers. This is on by default for a new collection and
can be turned off per collection (the checkbox on the form, or
`--no-cross-job-dedup` on the command line), in which case every job stores
everything in full.

Two consequences follow. A job's WARC is no longer self-contained on its own:
replaying a job brings in the WARCs of the jobs it refers to, and replaying
the collection loads them all. And **deleting** a job that later jobs refer
into leaves their pages without that content. A job or a collection can
always be deleted, but what that means is stated first and nothing changes
until you confirm: the delete warning names the jobs and the number of
records that refer into the job. After such a deletion the collection lists
the pages that are missing their original, and **Re-crawl** on the
Collections page sets them up as the seeds of a new crawl in the same
collection; once stored again they leave the list. Deleting a collection
removes its jobs from the dashboard; with `--purge` (or the dashboard's
second prompt) their files are deleted from disk as well, otherwise the files
stay where they are (the index does not: it describes jobs that no longer
exist, and a collection made later under the same name starts its own). A
collection is not deleted while one of its jobs is running unless the
deletion is forced. Jobs of one collection can run at the same time, from
the dashboard or the command line; the index is written one capture at a
time, so nobody waits on anybody. A collection's directory is recorded as an
absolute path, so a command-line job finds it from any working directory.

**Upkeep.** A collection's row has **Edit** (its name, description and the
store-once policy; the identifier and the directory are fixed at creation),
**Rebuild index** (`swm collection reindex NAME`), which reads every job's
WARC files back into the payload index (for jobs made before the index
existed, or an index that was lost; the WARCs are not changed), and
**Index WARCs** (`swm collection index-warc NAME`), which runs warc-indexer
over every crawl's and recording's WARC files in turn, each document
carrying the collection's name; see [Indexing crawls and
recordings](#indexing-crawls-and-recordings). A job in a collection indexed
on its own carries the collection's name too.

## Browser modes

Browser mode is a major part of SWM's capture strategy.

### `headless`

- Uses Playwright's bundled Chromium without a visible window.
- Intended for automated crawls.
- Suitable for repeatable background capture where a visible browser is not
  required.

### `headed`

- Opens a visible Playwright-managed Chrome or Chromium browser.
- Recommended default for interactive recording.
- Also available for automated crawls when visual observation is useful.

### `native`

- Launches or attaches to installed system Chrome through the Chrome DevTools
  Protocol.
- Available for both interactive recording and automated crawling.
- Useful when a site behaves differently in system Chrome, when visible operator
  interaction is important, or when an authorised authenticated workflow needs a
  persistent dedicated profile.
- Chrome remote debugging requires a non-default `user_data_dir`; SWM uses a
  dedicated profile so the archive session does not interfere with normal Chrome.
- Close any Chrome process already using the same profile before starting.

Native mode is not a way to evade website restrictions. It provides a more
realistic browser environment while capture scope, authorisation and responsible
crawl behaviour remain the operator's responsibility.

## Inspection and QA

List captured responses:

```powershell
python -m webarc.cli inspect warcs/example-session
```

Summarise captured hosts:

```powershell
python -m webarc.cli inspect warcs/example-session --hosts
```

Find URLs containing a word or hostname:

```powershell
python -m webarc.cli inspect warcs/example-session --grep youtube
python -m webarc.cli inspect warcs/example-session --grep googlevideo.com
```

For diagnosis or sharing, create a smaller WARC that excludes bodies above a
chosen size:

```powershell
python -m webarc.cli extract `
  warcs/example-session `
  example-session-small.warc.gz `
  --max-mb 1
```

The extracted file is intended for inspection and troubleshooting. It is not a
complete replacement for the original recording when large media or documents
have been removed.

## Indexing social captures

A Facebook, Instagram, X or YouTube capture leaves normalised records beside
its WARC files: the posts, comments, profiles, videos and channels it
collected, each with its text, author, time and media. A generic WARC indexer
never sees them; it indexes the hundreds of scripts and images behind one
page whose extracted text is mostly navigation. SWM indexes the records
instead.

`index` turns a social capture into one search document per item, using the
field names of the **warc-indexer** (webarchive-discovery) Solr schema that
SolrWayback and the UK Web Archive tooling use. The documents load into the
same index as warc-indexer's own output, or into any search engine, and each
one points at the WARC record of the page it came from (`source_file_path`
and `source_file_offset`), so a search hit is an archive citation that a
Wayback-style engine can replay.

```powershell
python -m webarc.cli index warcs/12
python -m webarc.cli index warcs/12 --collection "Heritage 2026" --json
```

The platform is detected from the capture's manifest. Output goes to
`<capture>/index/<platform>-index.jsonl`, one JSON object per line, with a
summary in `<capture>/index/index-manifest.json`: documents by type, how
many found their WARC record, and any warnings. `--output` writes elsewhere.

From the dashboard, every finished social job has an **Index** button. It
writes the same files, reports the counts, and offers the index file for
download; the button then shows the document count and re-indexes on demand.

What a document carries:

- **Identity and place**: `id` (`facebook:post:<id>`, `x:user:<id>`, ...),
  `type` (`Facebook Post`, `Instagram Comment`, `YouTube Video`, ...), `url`,
  `url_norm`, `host`, `domain`.
- **Content**: `content` (the post, caption, comment or description),
  `title`, `author`, `keywords` (hashtags, tags), `content_language` where
  the platform gives it, `links` and `links_images`.
- **Time**: `publication_date` (when the item was written) and `crawl_date`
  and `wayback_date` (when it was captured, taken from the WARC record).
- **Evidence**: `source_file`, `source_file_path`, `source_file_offset`,
  `warc_key_id` (the WARC record's own id), `status_code` and the record's
  payload `hash`. Comments point at the post page they were read from.
- **Context**: `collection` (the capture name unless `--collection` says
  otherwise), `institution` (the operator), `access_terms` and
  `wct_subjects` from the capture's Rights and Subject metadata, and every
  platform detail that has no schema field (reaction counts, parent ids,
  handles) as `key=value` entries in `content_metadata_ss`.

By default `source_file_path` is the full path of where each WARC is when
the index is made. WARC files move when they are ingested, so two things
keep the pointer usable afterwards. `--source-root` sets the path or URL
prefix under which the files will be kept, used as given, and every
`source_file_path` becomes that prefix plus the file name. And
`--relocate` rewrites the pointers in an existing index to a new root
without re-reading the records or the WARCs, which need not exist any more;
it takes the capture folder or the index file itself. The file name plus
`source_file_offset` is the stable key throughout, the convention
warc-indexer's consumers and SolrWayback's file resolvers already follow,
and `warc_key_id` identifies the record wherever the file ends up.

```powershell
python -m webarc.cli index warcs/12 --source-root https://repo.example/warcstore/
python -m webarc.cli index warcs/12 --relocate --source-root /mnt/repository/warcs
python -m webarc.cli index warcs/12/index/facebook-index.jsonl --relocate --source-root s3://archive/warcs
```

In the API the same options are `source_root` and `relocate` in the body of
the index request. Without a WARC beside the records (a capture run with
WARC writing off) the documents are still complete and searchable, only
without the evidence fields.

Only field names the schema defines are emitted; a document that would not
load is left out and counted as invalid in the summary. Items whose page is
not in a WARC (a YouTube video read through yt-dlp, a capture run without
WARC writing) are still indexed, without the evidence fields.

### Indexing crawls and recordings

An automated crawl or an interactive recording holds ordinary web pages,
which are indexed from the WARC itself by **warc-indexer**. A patched copy
of warc-indexer 3.5.1 lives in [`warc-indexer/`](warc-indexer/README-SWM.md)
with the fixes SWM's captures exposed: the charset the server declared is
honoured, and the JSON output carries the WARC path and the record type.
Build it once (`mvnw -DskipTests package` in that folder; only Java is
needed) and SWM finds the jar there. Java 11 or newer must be installed.

From the dashboard, every crawl and recording job with WARC files has an
**Index WARC** button. It runs the jar over the job's WARC files and writes
`<warc file name>.jsonl` beside each one, a document per record in
warc-indexer's schema. The run happens in the background and the job card
follows it: which file it is on, how many documents so far and how long it
has run, then the document count when it finishes. A failure is shown on
the card in the indexer's own words, with the last lines of its output and
a link to the whole log, which is `warc-index.log` in the job's folder
beside `warc-index-manifest.json`, the record of the run.

**Settings › Indexer** says where Java, the jar and the configuration are
and how much memory the jar may use. Each may be left empty, in which case
SWM looks for Java through `JAVA_HOME` and the PATH, for the jar in the
repository's `warc-indexer/target`, and for the configuration beside the
jar; the section reports what it found, or what is missing. When Java or
the jar cannot be found, pressing Index WARC opens that section with the
reason, so the path can be given there and the button tried again. The
Java entry accepts the `JAVA_HOME` folder or the executable itself, and
every path is checked before it is kept.

The same from the command line, all WARCs in a folder or one by name:

```powershell
python -m webarc.cli index-warc warcs/90
python -m webarc.cli index-warc warcs/90 --warc rec-x.com-seed001-20260907141427-00001.warc.gz --collection "X profile"
```

The command shows progress as it goes and, on a failure, the reason and
the indexer's last lines. With `--db` pointing at the dashboard's state
file (the default location is assumed) the Settings › Indexer paths apply
on the command line too. SWM launches the jar as a separate program and
reads what it writes, the same arrangement as with gallery-dl, so the
jar's licence stays its own. `SWM_WARC_INDEXER_JAR`, `SWM_WARC_INDEXER_CONF`
and `SWM_JAVA` point SWM at a jar, a configuration or a Java elsewhere when
there is no dashboard.

## Replay (ReplayWeb.page)

SWM uses **Webrecorder ReplayWeb.page** and wabac.js for local replay. Replay runs
in the browser through a service worker; pywb is not required. The archive stays
on the local machine and is served from `127.0.0.1`.

```bash
python -m webarc.cli replay ./warcs/example-session
```

SWM combines the selected folder's WARCs into a replay archive, creates a small
ReplayWeb.page site, starts a local static server and opens the browser.

Useful options:

```text
--url URL              Initial captured URL
--collection NAME      Local replay collection name
--replay-root PATH     Directory used for generated replay files
--host HOST            Local bind address; default 127.0.0.1
--port PORT            Local port; default 8091
--self-host            Use locally vendored ReplayWeb.page files
```

Example using a different port and initial URL:

```powershell
python -m webarc.cli replay `
  warcs/example-session `
  --port 8094 `
  --url https://example.org/page
```

By default, ReplayWeb.page assets are loaded from a pinned jsDelivr package. For
an offline machine, place compatible `ui.js` and `sw.js` files in
`./replay/vendor/` and use `--self-host`.

When the port is already taken, by another SWM or any other program, the
replay server steps up to the next free port (up to ten above) and says so;
the dashboard and the command line always link to the port actually served.

## Dashboard

The optional dashboard manages **automated crawls**, **interactive recording
sessions** and **Facebook Page captures**. The *New job* area has three tabs:
*Automated crawl* (guided form or raw YAML), *Record session* and *Facebook*.
Visible-browser controls work from the dashboard and the relevant in-page SWM
widget, and both control surfaces stay in sync.

Recording from the dashboard requires an interactive desktop on the server
machine, and is disabled (with an explanation) when the server binds to a
non-loopback address — pass `--allow-remote-recording` to override that check
deliberately. Recordings appear in the job list with a **REC** marker and
Facebook captures with an **FB** marker. Only one Facebook job can use the
persistent Facebook profile at a time.

Install and start the dashboard:

```bash
pip install -r requirements.txt -r requirements-dashboard.txt
python -m webarc.cli serve
```

The default address is:

```text
http://127.0.0.1:8080
```

Choose another port when needed:

```bash
python -m webarc.cli serve --port 8085
```

Dashboard controls include:

- **Start** an automated crawl from a configuration;
- **Pause** after the current page;
- **Resume** the same running process;
- **Stop** gracefully and close the WARC;
- **Delete** the crawl record and optionally its files;
- **Replay** captured crawl data;
- **Pause scrolling**, resume after login/verification, stop and continue a
  Facebook Page capture;
- monitor progress, storage use and available disk space;
- watch what the machine has left and what each running job is using.

### Describing a capture (metadata)

Every job form ends with **Describe this capture**: who or what is being
captured, and why, in the terms a catalogue uses. The model is Archive-It's
seed-level metadata: the fifteen Dublin Core 1.1 elements (Title, Creator,
Subject, Description, Publisher, Contributor, Date, Type, Format,
Identifier, Source, Language, Relation, Coverage, Rights) plus
**Collector**, every element repeatable, custom fields allowed, at two
levels. Values on the *Whole job* tab apply to every seed; a seed's or
target's own tab holds values that replace the job's for that element.
Title, Identifier, Date, Type and Collector are filled from the job where
left empty, and nothing else is guessed.

What is written:

- `metadata.json` in the job's folder: the job's fields, each seed's own
  fields, and each seed's *effective* fields (its own over the job's, with
  the defaults filled in). This is the record of truth and is current.
- a `metadata` WARC record beside the warcinfo in each WARC file, in
  `application/warc-fields` with `dc.` names (`dc.title`, `dc.subject`,
  …, plus `collector` and `custom.<name>`), so a file that leaves the folder
  still says what it is;
- a `metadata` section in a Facebook or Instagram manifest, and a
  *Description* table at the top of the reader pages.

Metadata can be changed after the fact from the job list (**Metadata** on
each row). `metadata.json` and the manifest are rewritten; a WARC already
written keeps the values of its moment. The same dialog exports a sheet
(`metadata.csv`, one row per seed with a column per value and repeated
columns for repeated elements, plus a `*` row for the job level), and every
form can **import** such a sheet or **copy** the metadata of an earlier
job, which suits a monthly recapture of the same account.

On the command line, a crawl YAML may carry a `metadata:` block at the top
(job level) and one inside any seed:

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

`swm metadata export <job folder>` writes the sheet for a finished job.

### Finding a job in the list

The bar above the job list narrows it as you type or choose: by name or
`#id`, by job type (automated crawl, recorded session, Facebook,
Instagram), by status (active, waiting, completed, stopped, failed) and by
the date the job was created, from and to. The figures keep refreshing
underneath, the count shows how many of the jobs match, and the filter is
remembered in the browser until you clear it.

### Help text beside each field

Every field on the job forms and the Settings page has a "?" that explains,
in plain words, what the field is for. The wording lives in
`webarc/help_text.yaml`, one entry per field, and is served to the page
when it loads. To change it for your installation without editing the
package, copy that file to `help_text.yaml` next to the dashboard's
database (`webarc-state/help_text.yaml` by default) and keep only the
entries you want to change: each one overrides the packaged text, an entry
left blank hides that "?", and the file is read again on every page load.

### Machine resources

The *Jobs* page shows the machine's spare CPU, memory and disk space, and
the CPU and memory used by all running jobs together; each running job's row
shows its own share (its worker, its browser and any helper it started).
The command line reports the same:

```bash
python -m webarc.cli resources            # add --json for the raw reading
```

Before a job starts, the dashboard checks the machine against warning levels
set under *Settings → Resource warnings*: by default, less than 15 % of CPU
or memory free, or less than 10 % of the disk the job will write to. When
a level is crossed the dashboard asks whether to **start anyway**, **wait**
or **cancel**. A job told to wait is created in full and shown as *waiting*;
the server starts it by itself once every resource is back above its level
(one waiting job per check, oldest first), or at once from its **Start now**
button. Running jobs are never paused by this check.

`swm crawl` makes the same check before it starts and, at a terminal, asks
the same question. `--yes` starts without asking, `--wait` waits for room,
`--no-resource-check` skips the check, and `--db` names the dashboard state
file whose warning levels apply. A crawl run without a terminal starts
anyway and prints the warning. The machine's CPU and memory are read
directly from Windows or Linux; each job's own share needs `psutil`, which
`requirements.txt` installs (re-run `pip install -r requirements.txt` in an
existing environment, then restart the server).

Try the dashboard without starting a browser crawl:

```bash
python -m webarc.cli serve --simulate
```

The dashboard stores control state in SQLite at
`webarc-state/webarc.db` by default. Pause keeps the worker process alive and does
not serialise the frontier for restart after a server reboot.

## Architecture

```text
Automated crawl
config.yaml ──► crawler.py ──► browser.py ──► capture.py ──► WARC
                    │
                    ├── scope.py
                    └── frontier.py

Interactive recording
record command ──► recorder.py ──► visible browser ──► capture.py ──► WARC

Quality assurance
WARC folder ──► inspect / extract ──► replay.py ──► ReplayWeb.page

Search
social capture records + WARC ──► indexer.py ──► index/<platform>-index.jsonl
                                                  (warc-indexer schema)
```

Capture occurs at the browser network-event layer. SWM records what the browser
requested and received, including JavaScript-generated resources and user-driven
network activity.

## WAF and bot-block handling

Government and enterprise sites may use F5, Cloudflare, Imperva, Akamai or similar
systems that return a block or challenge page. Some block pages return HTTP 200,
so status codes alone are insufficient.

For automated crawls, SWM follows a back-off-and-stop policy:

1. the first detected block increases the delay and applies a cooldown;
2. repeated blocks stop the seed after the configured threshold;
3. a successful page resets the block counter and delay behaviour.

JS-challenge WAFs (for example AWS WAF's HTTP 202 "challenge" action, used by
Figshare-based repository portals) serve a small interstitial that solves a
puzzle and then reloads the real page. The crawler waits up to
`behavior.challenge_grace` seconds for that to happen before capturing, so the
archive holds the real page rather than the interstitial. Challenge verdicts on
API subresources are archived faithfully but flagged loudly (see below), because
the page will replay without its dynamically loaded records.

Example configuration:

```yaml
behavior:
  detect_blocks: true
  block_backoff_factor: 3.0
  block_cooldown: 30
  block_max_consecutive: 3
  challenge_grace: 20       # seconds to let a WAF JS challenge clear
  scroll_max_screens: 40    # cap per-page infinite-scroll capture
```

Sites that load their records dynamically (search portals, infinite-scroll
repositories) depend entirely on their XHR/fetch responses being archived
intact. The crawler warns whenever a page's dynamic content looks incomplete
(WAF-challenged API calls, failed or empty API responses, bodies lost to
navigation), and `python -m webarc.cli inspect <warc-dir>` reports the affected
URLs after the fact — a page can replay with intact HTML but missing records,
so these warnings are the fastest way to spot a broken capture before relying
on it.

This is not an evasion mechanism. For persistent blocking, the appropriate
solution is to request allowlisting of the archive operator's IP address and an
honest identifying User-Agent from the website owner.

## Known limitations

- Request and response records are reconstructed from browser network events,
  rather than raw TCP capture.
- Streaming media is preserved only as the browser requests its manifests,
  segments and byte ranges.
- Signed, tokenised and time-limited URLs may behave differently during replay.
- Unvisited pages and interactions are not part of an interactive recording.
- `native` mode can attach to any page opened in its Chrome profile; use a clean,
  dedicated profile.
- Large recordings take longer to import and replay.
- Dashboard-started recordings open the browser on the machine running the
  server, so the server must run where the operator is sitting.
- Facebook GraphQL schemas and rendered controls can change without notice;
  SWM records extraction failures and does not claim that a Page capture is
  complete.
- Facebook Page capture is limited to Pages in v1. Comment limits are
  best-effort because one Facebook response can return several comments.
- YouTube video files are the rendition YouTube served within the allowed
  resolution, muxed by yt-dlp; the upload itself is never available. Post
  dates are estimates from the relative text YouTube shows. Poll results,
  members-only and age-restricted content, and anything YouTube withholds
  from a signed-out or bot-checked session are not captured.

## Research notes

Design research that shaped, or will shape, a capture mode lives under
`docs/research/`:

- [X capture, the Instagram way](docs/research/x-capture.md): how an X
  (formerly Twitter) mode would read the signed-in web client, with the
  endpoints to observe, the timeline anatomy, attribution and media rules,
  and what breaks. Each finding is marked verified or unverified.
- [YouTube capture](docs/research/youtube-capture.md): why yt-dlp reads
  and downloads the videos while a browser reads the Posts tab, what the
  spike from this environment showed, and what the first real capture
  must confirm.

## Licence and citation

Copyright © 2026 Arif Shaon.

The **software code** is licensed under the
[PolyForm Noncommercial License 1.0.0](LICENSE). The licence permits personal,
research, educational, charitable, government and other non-commercial uses as
defined in its terms. Copies and permitted modifications may be distributed with
the licence and required notices intact.

The licence does **not** permit commercial use. Using SWM to provide a paid
service, incorporate it into a commercial product, support revenue-generating
operations, or otherwise obtain commercial benefit requires a separate written
commercial licence from Arif Shaon.

Because commercial use is restricted, this project is **source-available rather
than OSI-approved open-source software**.

Creative Commons licences are intended primarily for creative works and
content rather than software code. Documentation, screenshots or training
materials may be licensed separately under a Creative Commons licence when they
are explicitly marked as such; unless otherwise stated, the repository materials
follow the software licence in `LICENSE`.

When using SWM in research, institutional work or published documentation, please
acknowledge **Arif Shaon** and cite this repository using the metadata in
[`CITATION.cff`](CITATION.cff).
