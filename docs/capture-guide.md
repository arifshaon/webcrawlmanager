# Capture guide

This guide explains each way SWM can capture, when to use it, and what it
saves. For the dashboard itself, collections and replay, see
[Dashboard and collections](user-guide.md). For every command-line option,
see [Command line](command-line.md).

## Contents

- [Choosing a way to capture](#choosing-a-way-to-capture)
- [Browser modes](#browser-modes)
- [Recording a session](#recording-a-session)
- [Automated crawls](#automated-crawls)
- [Theme-based crawls](#theme-based-crawls)
- [Social media captures: what they share](#social-media-captures-what-they-share)
- [Facebook](#facebook)
- [Instagram](#instagram)
- [X](#x)
- [YouTube](#youtube)
- [Checking a capture](#checking-a-capture)

## Choosing a way to capture

| Use | When |
|---|---|
| **Record a session** | The content depends on what a person does: signing in (to something you are authorised to preserve), search forms and filters, menus, pop-ups, galleries, embedded viewers, audio and video players, or content that appears only after scrolling or clicking. |
| **Automated crawl** | A site can be explored predictably from one or more starting addresses, within rules for which links to follow, how deep and how many pages. |
| **Theme-based crawl** | As an automated crawl, but you want only the pages about one topic. |
| **Facebook, Instagram, X or YouTube** | You want an account's or channel's posts, or a single post or video, as records with media and readable pages, not only as web pages. |

The ways can be combined. For example, a crawl captures a site's main
structure while a recording preserves its media players and signed-in
areas.

## Browser modes

SWM always uses a real browser, in one of three modes:

| Mode | What it is | Used by |
|---|---|---|
| `headless` | Playwright's bundled Chromium, with no window. | Automated crawls (the default). |
| `headed` | A visible Chrome or Chromium window managed by SWM. | Recordings (recommended), and crawls when you want to watch. |
| `native` | Your installed Google Chrome, controlled through the Chrome DevTools Protocol, with a dedicated profile. | Recordings and crawls, when a site behaves differently in ordinary Chrome or a signed-in session needs to persist. |

For `native` mode:

- SWM uses a **dedicated Chrome profile** (`user_data_dir` in the
  configuration; by default `./chrome-profile-webarc`), never your everyday one,
  because Chrome's remote control needs a separate profile and keeps your
  own browsing out of the archive.
- Close any Chrome window already using that profile before starting.
- SWM can see every page opened in that profile, so keep it clean.

`native` mode gives a more realistic browser. It is not a way around a
site's restrictions: what you capture, your authorisation and responsible
crawling remain your responsibility.

The recording browser and the social media browsers are started without
Chrome's automation signal (`navigator.webdriver` is false and there is no
"controlled by automated test software" bar), because a person is driving
them. Some sites, X among them, refuse a password typed in a
browser that shows that signal. Automated crawls keep it.

## Recording a session

In a recording, **you** browse in a visible browser and SWM saves the
traffic your browsing creates. SWM does not follow links on its own.

Start one from **New job › Record session**, or from the command line:

```bash
python -m webarc.cli record https://example.org/ --name example-session
```

SWM opens the address in a visible browser, records every request and
response, and writes them to compressed WARC files. From the command line
the files go to `warcs/example-session/` (`--output` chooses another
folder). From the dashboard they go into the chosen collection's folder.
Give each recording its own name so sessions stay separate.

### While recording

1. Browse normally and visit every page you need.
2. Open menus, tabs, accordions, pop-ups, galleries and embedded viewers.
3. Scroll through pages and let content finish loading.
4. Use the **SWM Recording** control at the bottom right of the page to
   pause, resume, or capture the current page explicitly.
5. Close one pop-up or player before opening the next, if the site
   expects only one at a time.
6. To finish, close the browser window, press `Ctrl+C` in the terminal,
   or choose **Stop** in the dashboard.

Stop cleanly rather than ending the program from Task Manager, so SWM can
close the WARC file properly.

### Video and audio

Opening a page with a video is often not enough, because players fetch
media in pieces as it plays. For each player you need:

1. open the player and wait for it to load;
2. start playback and let it play for several seconds;
3. move to any other parts you need;
4. close the player before opening another.

Only what the browser actually fetched can be replayed. SWM adds
compatibility handling during local replay for players created on the
fly, such as YouTube players opened in a Fancybox pop-up; the WARC itself
is not changed.

### Signing in and privacy

Everything that loads while you record can end up in the archive,
including signed-in pages, form submissions, private addresses and media.
Record only what you are authorised to preserve, and handle the files
according to their sensitivity.

In `headed` mode every recording starts with a fresh browser, so a site
sees a new device each time you sign in. For sites that limit new
devices, use `native` mode with its dedicated profile, which keeps you
signed in between recordings.

## Automated crawls

In an automated crawl, SWM starts from one or more **seeds** and follows
links within the rules you set, in a real browser. It behaves like a
person to give pages time to finish: random pauses, gradual scrolling,
mouse movement, and waiting for the page to fall quiet.

Start one from **New job › Automated crawl** (a guided form, or **Raw
YAML** for the full configuration), or from the command line:

```bash
python -m webarc.cli validate config.yaml      # check the configuration
python -m webarc.cli crawl config.yaml         # run it
```

What a crawl does:

- follows links within a **scope**: the same host, the same domain, a
  path prefix, or any address, narrowed further by include and exclude
  patterns;
- stops at a maximum **link depth** and a maximum **number of pages**;
- tidies addresses so the same page is not visited twice, and can obey
  `robots.txt`;
- **turns the browser cache off**, so every page's stylesheets, scripts
  and images are fetched from the site and archived, never answered from
  the cache or stored as empty "not modified" replies;
- counts and follows a page that loads but never falls quiet (analytics,
  polling, chat widgets) once the wait runs out, and logs it as
  unsettled;
- detects block pages and slows down or stops (see
  [Blocked sites](user-guide.md#blocked-sites));
- dismisses cookie consent pop-ups where it can, choosing "decline" by
  default;
- writes compressed WARC 1.1 files with request, response, information
  and revisit records, starting a new file at a set size.

### Crawl settings

`config.yaml` in the repository is a commented example. Settings under
`defaults:` apply to every seed, and any of them can be overridden for one
seed.

```yaml
crawl_name: example-crawl
operator: "Example Library — Digital Collections"
defaults:
  browser:
    mode: headless            # headless | headed | native
  scope:
    strategy: same-host       # same-host | same-domain | path-prefix | any
    exclude: ["/logout"]      # address patterns never followed
    max_depth: 2
    max_pages: 200
  behavior:
    obey_robots: true
    delay_range: [1.5, 4.0]   # seconds between pages
    wait_until: networkidle   # load | domcontentloaded | networkidle
    scroll: true
    scroll_max_screens: 40    # stop endless scrolling after this many screens
    challenge_grace: 20       # seconds to let a protection challenge clear
    detect_blocks: true
    block_backoff_factor: 3.0
    block_cooldown: 30
    block_max_consecutive: 3
  warc:
    max_size_mb: 900          # start a new WARC file after this size
    dedup: true               # store repeated content once within the crawl
seeds:
  - url: https://example.org/
```

A crawl can also carry `metadata:` (see
[Command line](command-line.md#metadata)) and a `theme:` (below).

## Theme-based crawls

A **theme** keeps only the pages of a crawl that are about one topic. Every
page SWM looks at is listed, with the reason it was or was not kept.
Themes apply to automated crawls only: in a recording you decide what to
visit, and social media captures select by account and date.

### What a theme is

- a **name** and a **brief** in plain words, for example "news about the
  restoration of heritage sites in Doha; not general tourism";
- **terms** and phrases in any language. Arabic spelling variants,
  prefixes and plurals are matched, so مكتبة, المكتبات and مكتبةٍ count as
  one term;
- terms that **rule a page out**;
- **address patterns** that count towards the theme, and patterns never
  fetched (log-in pages, comment feeds);
- **hub patterns** for listing pages (sections, tags, search results,
  page 2 of a list);
- an optional **date window**;
- a **minimum score** a page needs.

You set it in the crawl form's **Theme** panel, or in a `theme:` block in
the YAML. The theme is saved whole in the job's `theme-summary.json`.

### How a crawl uses it

SWM decides in three steps, cheapest first:

1. **Addresses.** Address rules decide before anything is fetched.
2. **Links.** Links are judged from their text and the words around them;
   a link the theme is sure about is never fetched.
3. **Pages.** The rest are fetched and judged from their *main* content,
   with menus, headers and footers set aside, so a site-wide "Culture"
   link does not make every page about culture.

Only then is a page's traffic written to the WARC. A page is either
**accepted** into the archive or **not accepted**; a page that is not
accepted leaves nothing in the archive, and its links are not followed.

**Hub pages**, including the starting page, are always followed, because
they lead to the theme's pages. With **Keep hub pages** ticked (the
default) they are also saved, so replay can start from them. Unticked,
they are followed but not saved; replay then opens on a list of the pages
that were.

### The rules judge

The rules judge is always on, and every point it gives is explained:

| Where a term is found | Points |
|---|---|
| In the headline | 3 |
| In the section, tags or description | 2 |
| Each mention in the text | 1 (up to 5) |
| The address matches an include pattern | 3 |

A page is accepted at the **minimum score**, 3 by default ("Minimum score
to keep a page" on the form, `min_score` in the YAML). At 3, one headline
match is enough; at 2, a section tag or two mentions in the text. A page
that scores 0, or breaks a hard rule (an excluded term in the headline, a
date outside the window, an excluded address), is ruled out.

A page scoring between 1 and the minimum "cannot be placed". **When a
page cannot be placed** decides what happens to it: **leave it out and
list it in the report** (the default) or **keep it**.

### The AI judge (optional)

An AI model can answer the real question, "is this page about this
topic?", from what SWM already has. It never fetches a page itself.

What it is sent is chosen per theme:

- **compact** (default): the address, headline, section, date and a short
  excerpt, with a one-word answer (yes, no or unsure). A few hundred
  tokens per page.
- **url**: the address and title only. Smaller, but judges from less.
- **full**: the full text, asking for reasons and quoted evidence, which
  SWM checks against the page. The most expensive.

Links are judged in one question per page, and the model may skip only
the links it is sure are off-topic.

A theme chooses how the two judges combine: the AI **decides** with the
rules as a first filter and explanation (the default), the AI only
**breaks ties**, or both must **agree**.

Set up the model under **Settings › Theme judge**:

- **Anthropic Claude** through its API (`pip install -e ".[theme-ai]"`);
- **Azure OpenAI**: the resource address (such as
  `https://my-resource.openai.azure.com`), the deployment name in the
  model field, the key and an API version;
- **any OpenAI-compatible endpoint**, such as a local Ollama or LM Studio,
  so nothing leaves the machine.

Limits keep the cost and rate under control:

- **Tokens per minute** makes SWM pace its questions to stay within the
  provider's allowance, and wait when the minute is full. A "too many
  requests" answer is waited out as the provider asks.
- **Most tokens per question** (or, if empty, a twentieth of the minute's
  allowance) shortens the excerpt and splits long link lists.
- **A maximum number of calls per job** caps the bill; after it, the
  rules decide.

The API key is kept in the dashboard's database and never written into a
capture. From the command line, the same settings come from environment
variables: `SWM_THEME_AI_PROVIDER`, `SWM_THEME_AI_MODEL`,
`SWM_THEME_AI_ENDPOINT` and `ANTHROPIC_API_KEY`.

### The selection report

**Selection** in the job's **⋮** menu opens a report of every page the
theme read, in three tabs: **Not accepted**, **Accepted** and **Links not
followed**. Each row has:

- the page's title, linked to its **live address** (opens in a new tab);
- its score against the score needed, such as **1 / 3**, or "rule" when a
  hard rule decided;
- the reason in plain words, for example "Not enough evidence: score 1
  (3 needed): 1 mention in the text", "Published 2019-05-01, before
  2025-01-01" or "No theme term or rule matched".

A page that was not accepted is not in the archive. Open it at its live
address to check it, tick the ones you want, and choose **Recrawl
selected**. The crawl form opens with those pages as seeds, link depth 0
(only those pages), the theme off and the same collection, ready for you
to check and start.

The same report is saved in the job's folder as `pages/selection.html`.
`selection.jsonl` holds one line per page judged and link considered:
the decision, which judge made it, the score and matched passages, the
AI's answer and what it was sent, the model and a fingerprint of the
prompt. `theme-summary.json` holds the theme, the counts, the estimated
tokens used and the waits.

Jobs made before this report existed may have a `review/` folder of pages
that could not be placed. The report lists those pages as not accepted;
delete the folder once you have recrawled what you need.

## Social media captures: what they share

The Facebook, Instagram, X and YouTube captures are started from their
tabs under **New job**. They have a lot in common.

- **They observe.** SWM opens the pages a person would open, scrolls, and
  reads what the platform sends to its own web page. It never posts,
  likes, votes or comments. Where SWM does make a request itself (X's
  media files, YouTube downloads through yt-dlp, the next page of a
  Facebook comment thread), the records say so.
- **You sign in, in a visible window.** Each platform has its own
  dedicated Chrome profile, kept under
  `webarc-state/browser-profiles/<platform>`, so you can stay signed in
  between captures. SWM never asks for or stores a password. Treat these
  profiles as sensitive. Only one window can use a profile at a time;
  close any leftover window before starting another capture.
- **Your session stays out of the archive.** Before anything is written,
  SWM removes cookies, authorisation headers, sign-in fields, and the
  account ids, tokens and other session values that the page carries.
  It never rewrites the platform's JavaScript.
- **Choosing what to capture.** All four offer similar modes:
  - **Latest N**: the newest N posts or videos. Pinned posts are
    recognised and kept without counting towards N.
  - **Date range**: posts between two dates.
  - **Until I stop it**: keep going until you stop.
  - **End of the timeline**: stop when nothing new appears. This is
    reported as "the end of what was available", never as "all posts".
  - **Since last capture**: continue from the newest post of the previous
    capture of the same account.
- **Waiting, not failing.** Rate limits, sign-in requests and
  verification checks pause the capture and ask you, in the window, to
  deal with them; it then carries on.
- **Comments are optional and graded.** You choose whether to collect
  comments (or replies and conversations), and how many. Each post's
  comment collection is labelled:
  *complete* against the count the platform reported, *partial*,
  *limited* by your cap, *none reported*, or *exhausted but unverified*
  (nothing more appeared, but nothing confirms the count). A thread that
  stopped loading is never taken as proof that it was complete.
- **What each saves:** records as JSON Lines and CSV, the media, the
  platform's own responses each record was read from, a manifest
  (`<platform>-manifest.json`), a checkpoint and an event log,
  `checksums.sha256`, readable pages in `pages/` (clearly marked as not the
  platform itself), and optionally a WARC of what the browser loaded,
  which is the record of how the platform presented the content.
- **Indexing:** **Index** in the job's **⋮** menu makes search documents;
  see [Search indexing](user-guide.md#search-indexing).

## Facebook

**What it captures:** a Facebook **Page**'s timeline, or a single post.
Personal profiles, groups and other parts of Facebook are not supported.

**How it works:** the browser opens with scrolling paused. Sign in if
needed, check that the right Page is open, then choose **Start / resume
scrolling** in the browser's SWM panel or in the dashboard. **Pause
scrolling** pauses only SWM's scrolling: anything you open or load by
hand is still written to the WARC and noted in the manifest.

**Choosing what to capture:**

- **Date range**: posts between *From* and an optional *To*. It stops
  after five posts in a row (not counting pinned ones) older than *From*.
  Posts newer than *To* are scrolled past to reach older ones; they stay
  in the WARC but not in the exported records.
- **Latest N posts**: N posts, not counting pinned ones.
- **Until I stop it**, **End of available timeline**
  (`end_of_available_timeline`, never "all posts") and **Since last
  capture**.

**Comments** are optional, because Facebook loads comments separately for
each post, which adds time and the risk of being blocked. Maximum
comments and include-replies settings limit it. When a post's comments
are read, SWM:

1. scrolls the comment area as a reader would and presses Facebook's own
   "View more comments", allowing thirty seconds without a new comment
   (not counting a page of comments still arriving);
2. reads Facebook's own statement of whether more comments follow; a
   thread Facebook says has ended is finished, whatever count the post
   shows;
3. if Facebook says more follow but scrolling no longer loads them, asks
   the page to repeat its own last comment request with the next-page
   marker Facebook gave, one page every 1.5 to 2.5 seconds. Only that
   marker changes, and the answer is archived like any other;
4. only if that fails too, pauses and tells you why, so you can load more
   by hand and resume, or stop and save.

The manifest records how many comment pages came from a click, a scroll
or a repeated request, and what Facebook last said about the thread.

**Photos opened in the viewer:** Facebook's photo viewer moves through an
*album*, not a post, so stepping onwards can show other posts' photos.
SWM keeps each photo seen there as *album context*: its own post id,
address, album, date and caption, with the full-size image in `media/`.
The manifest's `album_context` section says which post the album was
opened from and how many photos were that post's own; the post's readable
page lists them under "Photos opened in the viewer". No post record is
made from the viewer.

**Verification:** if Facebook asks you to verify, do it in the window and
choose **Verification resolved — resume**. A stopped or failed capture can
be **Continued** as a new, linked capture; posts already in the Page's
index are passed over rather than exported again.

**What it saves:** the WARC, `facebook-posts.jsonl`/`.csv`, comments as
JSONL/CSV when requested, `facebook-album-context.jsonl`/`.csv` when
photos were opened in the viewer, `facebook-manifest.json` (what was
selected and excluded, the stopping rule, failures, gaps and links to
earlier captures), `facebook-checkpoint.json` and `facebook-events.jsonl`.

**Notes:**

- The account ids in the page are replaced by a fixed placeholder number,
  because Facebook's page reads `USER_ID` as a number when it starts. Facebook and Instagram
  captures made between 2 and 16 September 2026 used a text placeholder
  and replay only briefly; capture them again.
- If a window from an earlier capture still holds the Facebook profile,
  the new capture cannot open its browser and fails with a message naming
  the window to close. The old window's SWM panel shows **Not connected**.
  Close it and start again.

## Instagram

**What it captures:** a profile's posts and reels, or a single post or
reel. Explore, hashtag, location, story and message pages are refused:
Instagram chooses what appears there, so they are not an archive of
anything.

**How it works:** a Chrome window opens for the whole capture; sign in or
clear a verification there when Instagram asks. Instagram's own web page
makes the requests, and SWM reads what it is sent. A post counts as the
profile's only when the profile's own listing returned it and it names no
other owner; the feed, suggestions and other profiles' posts that a
signed-in page also loads are never counted.

**Listing a profile** can be done two ways:

- **Browser**: scroll the profile and read what Instagram sends, page by
  page.
- **gallery-dl**: list the profile through Instagram's per-user interface,
  in Instagram's order with pinned flags and dates, using the browser's
  signed-in session. It can be faster, but Instagram may limit it. SWM
  runs gallery-dl as a separate program with every setting stated, reads
  its output as it arrives, stops it once it has enough, and resumes from
  where it was after a rate limit. If Instagram refuses gallery-dl while
  the browser is signed in, SWM lists through the browser instead and
  records why. gallery-dl's listing is kept under `evidence/listings/`;
  media are fetched through the browser where possible, and comments
  always are. Needs `pip install gallery-dl` (GPL-2.0, run as a separate
  program).

**Choosing what to capture:** latest N, date range, until stopped, and
since last capture.

**What it saves:**

- `media/`: the largest version Instagram served (not necessarily the
  original upload), named by its SHA-256 fingerprint, every image of a
  carousel in order;
- `raw/responses/`: every Instagram response a kept record was read from,
  with the session's own values removed; `raw/posts/`, `raw/profiles/` and
  `raw/comments/` hold the extracted items, and each record names the
  response it came from;
- `instagram-posts.jsonl`/`.csv`, `instagram-comments.jsonl`/`.csv`,
  `instagram-profiles.json`, `instagram-media.json`, `checksums.sha256`;
- `instagram-manifest.json`, `instagram-checkpoint.json`,
  `instagram-events.jsonl`, and readable pages in `pages/`;
- optionally a WARC. With the gallery-dl listing, the WARC holds the post
  pages opened for comments and the media fetched, but not the profile
  listing, which the browser did not do.

**Notes:** where the page states the length of a block of session data,
SWM updates that length after removing session values, because the page
rejects a block whose length does not match. Captures made before 5
September 2026 lack this and replay as an empty page; capture them again.

## X

**What it captures:** an account's posts, a single post with its
conversation, or what X shows for a hashtag or search.

**Targets**, one per line: `@handle` or a profile address (optionally
with `/with_replies` or `/media`), a post address, `#hashtag`, or
`search: words`. twitter.com addresses are treated as x.com. Home,
notifications, messages and other personal pages are refused.

**How it works:** X's web page names each of its data requests in the
address (`/i/api/graphql/<id>/UserOriginalsTimeline`), so SWM recognises
the ones it needs in the page's own traffic and reads the answers. X's
rate limits are waited out until the time X gives, and sign-ins or checks
are handed to you in the window.

A post counts as the account's only when one of the account's own
listings (identified by the account's numeric id) returned it and its
author is that account. X renames these listings from time to time: the
first capture, in September 2026, saw `UserOriginalsTimeline`,
`UserRepliesTimeline` and `UserVideoTimeline` where older tools knew
`UserTweets`, `UserTweetsAndReplies` and `UserMedia`. SWM accepts both.
If a capture finds nothing, every request the page made is recorded under
`client_anomaly` in the events, so the next rename is visible. The home
feed and recommendations the page also loads are kept in the WARC (replay
shows errors without them), counted in the manifest under
`operations_observed`, and never become records.

**What the records hold:**

- **Replies are posts** with their own id, author and media. Every post
  has a `relationship` (original, reply, repost, quote) and a
  `capture_role`: *target*, or *conversation_context* for other people's
  posts kept to explain a conversation, which never count towards the
  account's total.
- **Reposts** are kept as what the account chose to share, with the
  original post and its author under `original_post`; the account is
  never shown as the original's author. An option leaves them out.
- **Quotes** are the account's post, with the quoted post under
  `quoted_post`.
- **Media** is fetched by SWM: images at X's `orig` size, asked for with
  `name=orig` (falling back to
  `4096x4096`, `large` and the plain address), videos and GIFs as the
  highest-quality MP4 X offers. Each entry records the version the page
  showed, the one requested, what was fetched, and how.
- **Conversations** (optional, always on for a post target) open each
  post's page and keep what it replies to and the replies under it, graded
  like comments. Deleted or withheld posts are recorded as gaps with X's
  reason.
- **Searches and hashtags** keep every result with its author. The
  manifest describes them as what X showed this account, for this query,
  in this tab (Latest or Top), at this time.

**Choosing what to capture:** latest N (the pinned post recognised and
kept without counting), date range, until stopped, end of observed
timeline, and since last capture. X does not signal the end of a
timeline, so a capture that finds nothing new is reported as
`timeline_stalled`, never as the end of the account's posts. Post ids are
in time order, so "since last capture" compares ids and goes a little past
the previous capture's newest post.

**What it saves:** `x-posts.jsonl`/`.csv`, `x-users.json`, `x-media.json`,
`x-manifest.json`, `x-checkpoint.json`, `x-events.jsonl`,
`checksums.sha256`, `media/`, `raw/responses/` (only the responses a kept
record or media file came from), `raw/posts/` and `raw/users/`, `pages/`,
and optionally a WARC. In the WARC, the `x-csrf-token` and
`x-client-transaction-id` headers, the bearer token and the cookies are
removed from requests, and the signed-in account's id from the page's
session data.

**Replay notes:**

- X's page decides whether it is signed in from two cookies (`twid` and
  `ct0`). The cookies themselves are never archived, so for archives that
  start on X the replay page sets placeholder values (not your account's)
  before the page runs. The page then asks for what the archive holds.
- Replies that X did not send during the capture are not in the archive
  either.
- SWM never rewrites JavaScript. X's code builds an address as
  `?access_token=${…}` inside a template, and a removal that once landed
  there broke the code, so replay showed "Something went wrong. Try
  reloading." Captures made before that fix need to be run again.

The design is described in [docs/research/x-capture.md](research/x-capture.md).

## YouTube

**What it captures:** a channel's videos, Shorts, live streams and
community posts, a single video, or a playlist.

**Targets**, one per line: `@handle` or a channel address (with or
without a tab), a `/channel/UC…` address, a video address in any form
(`watch?v=`, `youtu.be`, `/shorts/`, `/live/`), a playlist address, or a
bare video or channel id. Personal pages (subscriptions, history, search
results) are refused.

**How it works:** two tools, each doing what it does best:

- **yt-dlp** lists the channel's tabs and playlists, reads each video's
  details and comments, and downloads the files. It keeps up with
  YouTube's player changes far better than SWM could on its own.
- **A browser** with a dedicated Chrome profile reads the **Posts** tab and
  post comments, which yt-dlp does not cover. It opens pages, scrolls and
  presses "View replies", and reads what YouTube sends its own page
  (`ytInitialData` in the page, and the `youtubei/v1/browse` and
  `youtubei/v1/next` requests that load more). It never makes those
  requests itself.

Their results are kept apart because they are different kinds of
evidence. `evidence/yt-dlp/<id>.info.json` is yt-dlp's reading of a video,
labelled *tool-derived metadata* in the manifest, and every video record
names the file it came from. `raw/responses/` holds YouTube's own
responses to the browser.

**Choosing what to capture:** two separate choices.

- **What to select:** latest N, date range, until stopped, end of
  listing, or since last capture. Only videos that pass are read in full.
- **What to keep:** the maximum resolution (from best down to none),
  thumbnails, captions, automatic captions, live chat replays, post images
  and comments.

**What the records hold:**

- **Videos** carry YouTube's availability terms as given (`public`,
  `unlisted`, `private`, `premium_only`, `subscriber_only`, `needs_auth`)
  plus `deleted`, `unavailable` and `unknown`. A private or deleted video
  in a listing is recorded as a gap, never as a video. Each file in
  `media/videos/<id>/` is described: the version YouTube served within
  the chosen resolution, joined by yt-dlp (never the original upload), the
  thumbnail, each caption track with its language, and the live chat.
- **Posts** carry their kind (text, image, images, poll, quiz, video,
  shared), images at the largest size offered in `media/posts/<id>/`, a
  poll's options without results (results need a vote, which SWM never
  casts), and both the relative time YouTube shows ("3 weeks ago") and an
  estimated date, marked as an estimate. A post by someone else that
  appears on the tab is skipped.
- **Comments** use one model for videos and posts (`target_type`,
  `target_id`, `parent_id`, `thread_root_id`, `reply_depth`), with
  YouTube's own `<parent>.<reply>` ids. Each item's comments are graded; besides the common
  grades they can be *disabled*, *blocked* or *stopped by you*. The default
  limit is 1,000 per item, newest first.
- **Playlists** keep their items in order, including those YouTube reports
  as private or deleted.

**Signing in:** YouTube sometimes answers "Sign in to confirm you're not a
bot", most often to data-centre addresses. The capture then pauses and
opens the sign-in page in the window. Once you have signed in, the
browser's session is lent to yt-dlp as a temporary cookie file, readable
only by your user, kept outside the capture and deleted when it ends. Use
a dedicated institutional Google account. The manifest records whether
the capture was signed in.

**Disk space:** below the warning level in Settings, the capture pauses
before the next file and continues once space is freed. Below a critical
level, a download in progress is stopped, the partial file kept, and the
download resumes later. **Stop and save** during a download also keeps the
partial file and says so.

**What it saves:** `youtube-videos.jsonl`/`.csv`,
`youtube-posts.jsonl`/`.csv`, `youtube-comments.jsonl`/`.csv`,
`youtube-channels.json`, `youtube-playlists.jsonl`,
`youtube-playlist-items.jsonl`, `youtube-media.json`,
`youtube-manifest.json`, `youtube-checkpoint.json`, `youtube-events.jsonl`,
`checksums.sha256`, `media/`, `evidence/yt-dlp/`, `raw/responses/`,
`pages/`, and optionally a WARC of the Posts tab and each video's watch
page as a person saw it.

**Replay notes:** video streams are never in the WARC; the downloaded
files are the preserved objects. During local replay, SWM puts a player
for the downloaded file in place of YouTube's own, with a note saying so;
the WARC is not changed. In another replay tool the page shows without
playback. The Posts tab reading was built from YouTube's documented
formats and a test sample; if a capture finds nothing, the page's requests
are recorded under `client_anomaly` in the events.

YouTube needs the extras listed under
[Optional extras](../README.md#optional-extras): yt-dlp, ffmpeg (to join
the separate video and audio YouTube serves above 720p) and Deno or
Node.js. The YouTube tab reports which it found. It will not start a video
capture without yt-dlp, and without ffmpeg it asks YouTube for single-file
versions instead. ffmpeg and Deno are found on the PATH or in the folder
named by `SWM_TOOLS_DIR`. The design is described in
[docs/research/youtube-capture.md](research/youtube-capture.md).

## Checking a capture

From the command line you can check what a capture holds:

```bash
python -m webarc.cli inspect warcs/example-session              # every captured address
python -m webarc.cli inspect warcs/example-session --hosts      # counts per host
python -m webarc.cli inspect warcs/example-session --grep video # addresses containing "video"
```

To share a capture for diagnosis, make a smaller copy without large
bodies:

```bash
python -m webarc.cli extract warcs/example-session small.warc.gz --max-mb 1
```

The smaller file is for checking and troubleshooting only; it is not a
replacement for the original.
