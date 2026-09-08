# YouTube capture

*Design note and spike record, 7 September 2026. Technical scope only.*

How SWM captures a YouTube channel's videos, Shorts, live streams and
community posts, a single video, or a playlist, as an evidence package in
the same convention as the Facebook, Instagram and X modes.

## Contents

- [Verdict](#verdict)
- [Two engines](#two-engines)
- [What the spike showed](#what-the-spike-showed)
- [Evidence, not just data](#evidence-not-just-data)
- [Selection and storage](#selection-and-storage)
- [Sign-in and the lent session](#sign-in-and-the-lent-session)
- [Comments](#comments)
- [Disk, pause and stop](#disk-pause-and-stop)
- [Package layout](#package-layout)
- [What the first real capture must confirm](#what-the-first-real-capture-must-confirm)

## Verdict

Use yt-dlp as a library for everything it covers (channel tabs,
playlists, video records, video comments, downloads) and a browser on a
dedicated profile only for what it does not (the Posts tab and post
comments). Do not build a YouTube client of our own: YouTube's player
challenges, signature ciphers, `n`-parameter transforms and proof-of-origin
tokens change on their own schedule, yt-dlp tracks them within days, and
nothing SWM could write would keep up. Do not compare yt-dlp with
gallery-dl; gallery-dl was tried for Instagram listing and did not work
well enough, and the comparison is not fair to either.

## Two engines

**yt-dlp** is imported, not run as a program. SWM hands it an options dict
per call: `extract_flat` with `lazy_playlist` for tab listings, full
extraction with `getcomments` for one video, and a download call with the
format selector the curator's resolution choice produces
(`bestvideo*[height<=1080]+bestaudio/best[height<=1080]/…`), `continuedl`
for resuming a partial file, subtitle languages, thumbnail and live-chat
writing, and a progress hook. The hook is where SWM's stop and disk
controls reach into a download: it raises yt-dlp's `DownloadCancelled`,
which leaves the `.part` file for the next attempt. yt-dlp solves the
player's JavaScript challenges in a runtime it is pointed at
(`js_runtimes`, deno or node) and fetches its solver scripts from its own
releases (`remote_components: ejs:github`). A proof-of-origin token
provider (`bgutil-ytdlp-pot-provider`, script mode) can be named and is
reported in the manifest when present.

**The browser** is Chromium on a persistent profile, launched without
the automation signal (`navigator.webdriver` is false), as the X mode
learned to do. It opens `/@handle/posts`, scrolls, opens each post page and
presses "View replies". It reads `ytInitialData` from the documents and
the JSON of the `youtubei/v1/browse` and `youtubei/v1/next` responses the
page itself makes, in both the older renderer shape
(`backstagePostRenderer`, `commentRenderer`) and the entity shape served
since 2024 (`commentViewModel` ordering plus `commentEntityPayload`
mutations). It never builds a `youtubei` request: continuation tokens are
taken from the page's own responses and used only by the page. There is
no direct-request fallback in v1.

Posts are read before any video, so the browser is finished with YouTube
before its session is lent to the downloader.

## What the spike showed

Run from this development environment (a data-centre address, no signed-in
session), with yt-dlp 2026.08.19:

| Step | Result |
| --- | --- |
| Channel tab listing (`/videos`, `/shorts`, `/streams`), flat | Works anonymously. Videos and Live entries carry `timestamp`, `availability`, `duration`, `live_status`; Shorts entries carry only id, title, thumbnails and view count. |
| One video's full record | "Sign in to confirm you're not a bot" on every player client tried, and still with the proof-of-origin provider. |
| Download | Not reachable past the bot check from here. |
| Chromium to youtube.com | Connection reset, with and without the proxy. |

Consequences in the design: the listing walk uses the flat entries' dates
when present and reads a video in full only when its entry has none;
the bot check is a *hold*, not a failure, and is answered by the
curator's sign-in and the lent session; the Posts collector was built
from YouTube's documented shapes and a fixture site in those shapes, and
the first real capture calibrates it.

## Evidence, not just data

The package keeps the two engines' outputs apart because they are
different kinds of evidence:

- `evidence/yt-dlp/<id>.info.json` is yt-dlp's reading of a video, kept
  whole, comments included. The manifest labels it *tool-derived
  metadata*, `verbatim_platform_response: false`. Every video record and
  video comment names the file it was derived from.
- `raw/responses/` holds YouTube's responses to the browser, verbatim,
  session material removed, `verbatim_platform_response: true`. At the
  end only responses a kept post or post comment was read from remain.
  Every post and post comment names its response.

## Selection and storage

Two controls, kept separate on purpose. **What to select** is one of the
modes the other social captures use: latest N per target (counted across
the tabs the curator ticked), a date range, until stopped, end of listing,
or since the last capture (the previous newest item id and date, per
target, from the store). **What to store** is the maximum resolution
(best, 2160 down to 360, or none), thumbnails, captions, automatic
captions, live-chat replays, post images, and comments with a cap and a
sort.

A video seen on two tabs is one record. A private or deleted entry in a
listing is an absence, with the reason, never a video. A post by another
channel that YouTube lists on the Posts tab is skipped and counted.

## Sign-in and the lent session

SWM never holds a password. On `LoginRequired` (yt-dlp's bot check, an
age gate, the browser landing on the sign-in page) or `CheckpointRequired`
(consent, a "sorry" page, "unusual traffic") the run holds, opens the
relevant page in the window, and waits for "I have resolved it". It then
reads the browser's cookies through Playwright and writes them to a
temporary Netscape cookie file (mode 0600, outside the package) that
yt-dlp reads as `cookiefile`; the file is deleted when the run ends. The
cookies never enter the package or the WARC; the WARC session drops
`cookie` and `authorization` on the request side and redacts session
identifiers in bodies. The manifest records `viewer: signed_in` and, when
signed out, what that bounds.

## Comments

One model for video and post comments: `target_type`, `target_id`,
`parent_id`, `thread_root_id`, `reply_depth`, with YouTube's own
`<parent>.<reply>` ids. yt-dlp's `parent: root` becomes no parent. Each
item's comment capture is graded with the vocabulary the Instagram and X
modes use: `reported_count_reached`, `partial`, `capped`,
`no_comments_reported`, `exhausted_unverified`, `disabled`, `blocked`,
`stopped_by_curator`. The default cap is 1,000 per item, sorted newest
first, replies included.

## Disk, pause and stop

The worker hands the engine a disk check built from the warning level in
Settings and a critical level a third of it. Below the warning level the
run holds before the next file and continues by itself once space is
freed; below the critical level a download in progress is cancelled
through the progress hook, the partial file is kept, and the download
resumes after the hold. Pause holds between items; Stop and save ends
the run after the current step, keeping a partial download and saying so
in the events. Progress reports carry the current download's bytes,
percentage, speed and ETA.

## Package layout

```
youtube-manifest.json        youtube-checkpoint.json    youtube-events.jsonl
youtube-channels.json        youtube-videos.jsonl/.csv  youtube-posts.jsonl/.csv
youtube-comments.jsonl/.csv  youtube-playlists.jsonl    youtube-playlist-items.jsonl
youtube-media.json           checksums.sha256
media/videos/<id>/           the video, thumbnail, captions, live chat
media/posts/<id>/            post images at the largest size offered
media/channels/<id>/         the avatar
evidence/yt-dlp/<id>.info.json
raw/responses/               the browser's observed responses
pages/                       reader pages built from the records
*.warc.gz                    optional; the Posts tab and each video's watch page, never video streams
```

The manifest's `replay` section says what a WARC of this capture can
show (page structure, post text and images, thumbnails) and cannot (video
playback, complete interactive comments, session-dependent behaviour),
and that a green replay load is not evidence of completeness.

## What the first real capture must confirm

- That the Posts tab and post pages still carry `ytInitialData` and
  continue through `youtubei/v1/browse` and `youtubei/v1/next`, in the
  shapes the extractor reads. A run that lists nothing records the
  requests the page made under `client_anomaly` in its events.
- Whether per-video reads pass without a sign-in from the institution's
  network, or whether the lent session is needed from the start.
- Whether the proof-of-origin provider is needed for downloads, and at
  which resolutions YouTube serves separate streams that need ffmpeg.
- The comment counts YouTube reports against what yt-dlp reads under the
  cap, and the grading that results.
