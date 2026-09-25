// Dashboard safety and tab-state fixes loaded after the main dashboard script.
// Kept separate so the controls remain readable while the legacy single-file
// dashboard is gradually modularised.

function escapeHtml(value) {
  return String(value == null ? "" : value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function statusClass(value) {
  const allowed = new Set([
    "pending", "running", "paused", "stopping", "stopped",
    "completed", "failed", "blocked", "waiting"
  ]);
  const normalised = String(value || "pending").toLowerCase();
  return allowed.has(normalised) ? normalised : "pending";
}

// Override the renderer used by every refresh. All values originating in the
// API or SQLite are escaped before entering an innerHTML template.
function facebookPhaseLabel(phase) {
  return ({
    waiting_to_start: "Waiting",
    scrolling: "Collecting",
    scrolling_paused: "Paused",
    collecting: "Collecting",
    paused: "Paused",
    verification_required: "Needs you",
    finished: "Finished",
  })[String(phase || "")] || "Status";
}

function crawlRow(c) {
  const id = Number(c.id);
  if (!Number.isSafeInteger(id) || id < 0) return "";

  const rawStatus = String(c.status || "pending");
  const status = escapeHtml(rawStatus);
  const statusCss = statusClass(rawStatus);
  const isRec = c.kind === "recording";
  const isFacebook = c.kind === "facebook";
  const isInstagram = c.kind === "instagram";
  const isX = c.kind === "x";
  const isYouTube = c.kind === "youtube";
  // Instagram, X and YouTube jobs walk a list of targets and report the same way
  const isTargeted = isInstagram || isX || isYouTube;
  const isSocial = isFacebook || isTargeted;
  const running = rawStatus === "running";
  const paused = rawStatus === "paused";
  const blocked = rawStatus === "blocked";
  const waiting = rawStatus === "waiting";
  const canPause = running;
  const canResume = paused || blocked;
  const canStop = running || paused || blocked || rawStatus === "stopping";
  const usage = c.resources && typeof c.resources === "object" ? c.resources : null;
  const usageLine = usage
    ? `<br><span title="this job's worker, browser and helpers">${Math.round(Number(usage.cpu_percent_of_machine) || 0)}% CPU · ${fmtBytes(Number(usage.rss_bytes) || 0)} RAM</span>`
    : "";
  const isOpen = openState.has(id);
  const t = c.totals || {};
  const visited = Number(t.visited) || 0;
  const queued = Number(t.queued) || 0;
  const failed = Number(t.failed) || 0;
  const bytes = Number(t.bytes) || 0;
  const hasWarc = Number(c.warc_files) > 0;
  const dd = c.dedup && typeof c.dedup === "object" ? c.dedup : null;
  const reused = dd ? (Number(dd.revisits_within_job) || 0) + (Number(dd.revisits_across_jobs) || 0) : 0;
  const ch = c.changes || null;
  const changeParts = ch ? ["new", "changed", "unchanged", "gone"].filter(k => Number(ch[k])).map(k => `${Number(ch[k])} ${k}`) : [];
  const changeLine = changeParts.length
    ? ` · pages: <a href="/api/crawls/${id}/changes" target="_blank" rel="noopener" title="Compared by words and links with the collection's last capture of each page; opens the page-by-page report">${changeParts.join(", ")}${Number(ch.not_visited) ? `, ${Number(ch.not_visited)} not visited` : ""}</a>`
    : "";
  const dedupLine = (reused
    ? ` · ${reused} reused${Number(dd.revisits_across_jobs) ? ` (${Number(dd.revisits_across_jobs)} held by other jobs)` : ""}`
    : "") + changeLine;
  const seeds = Array.isArray(c.seeds) ? c.seeds : [];
  const fb = isSocial && seeds[0] && seeds[0].details
    ? seeds[0].details : {};

  const seedRows = seeds.map(sd => {
    const seedStatus = escapeHtml(sd.status || "pending");
    const seedVisited = Number(sd.visited) || 0;
    const seedBytes = Number(sd.bytes) || 0;
    const current = sd.current_url
      ? `<div class="cur">▸ ${escapeHtml(sd.current_url)}</div>`
      : "";
    const details = sd.details || {};
    const th = details.theme && typeof details.theme === "object" ? details.theme : null;
    const themeDetail = th && !isSocial ? `
      <div class="cur">theme: ${Number(th.kept) || 0} kept · ${Number(th.rejected) || 0} left out · ${Number(th.unsure) || 0} for review${th.links_skipped != null ? ` · ${Number(th.links_skipped) || 0} links skipped` : ""}</div>` : "";
    const facebookDetail = isFacebook ? `
      <div class="cur">${escapeHtml(details.message || details.phase || "")}</div>
      <div class="cur">newest: ${escapeHtml(details.newest_post || "not yet observed")} · oldest: ${escapeHtml(details.oldest_post || "not yet observed")} · pagination failures: ${Number(details.pagination_failures) || 0}</div>` : "";
    const targetRows = isTargeted && Array.isArray(fb.targets) ? fb.targets.map(t => `
      <div class="cur">${escapeHtml(t.label || "")} · ${escapeHtml(t.status || "pending")} · ${Number(t.posts_selected) || 0} selected of ${Number(t.posts_encountered) || 0} seen${t.reason ? ` · ${escapeHtml(t.reason)}` : ""}</div>`).join("") : "";
    if (isTargeted && sd !== seeds[0]) return "";
    const dl = isYouTube && fb.download && typeof fb.download === "object" && fb.download.title ? fb.download : null;
    const downloadLine = dl ? `
      <div class="cur">downloading: ${escapeHtml(dl.title)}${dl.percent != null ? ` · ${Number(dl.percent)}%` : ""}${dl.downloaded_bytes != null ? ` · ${fmtBytes(Number(dl.downloaded_bytes))}${dl.total_bytes ? ` of ${fmtBytes(Number(dl.total_bytes))}` : ""}` : ""}${dl.speed ? ` · ${fmtBytes(Number(dl.speed))}/s` : ""}</div>` : "";
    const instagramDetail = isTargeted ? `
      <div class="cur">viewer: ${escapeHtml(fb.viewer || "unknown")} · newest: ${escapeHtml(fb.newest_post || "not yet")} · oldest: ${escapeHtml(fb.oldest_post || "not yet")} · rate-limit waits: ${Number(fb.rate_limit_waits) || 0} · media failed: ${Number(fb.media_failed) || 0}${isYouTube ? ` · disk holds: ${Number(fb.disk_holds) || 0}` : ""}</div>
      ${downloadLine}${targetRows}` : "";
    return `
      <div class="seed">
        <div>
          <div class="url">${isTargeted ? `${Number(fb.targets_done) || 0} of ${Number(fb.targets_total) || seeds.length} targets` : escapeHtml(sd.seed_url)}</div>
          ${isTargeted ? "" : current}
          ${themeDetail}
          ${facebookDetail}
          ${instagramDetail}
        </div>
        <div class="st">${seedStatus} · ${seedVisited} ${isSocial ? "posts" : "pages"} · ${fmtBytes(seedBytes)}</div>
      </div>`;
  }).join("");

  const name = escapeHtml(c.name || "Unnamed job");
  const created = escapeHtml(c.created_at || "");
  const seedsTotal = Number(c.seeds_total) || 0;

  return `
  <div class="crawl ${isOpen ? "open" : ""}" data-id="${id}">
    <div class="row" onclick="toggle(${id})">
      <div class="gutter g-${statusCss}"></div>
      <div>
        <div class="name">${isRec ? '<span class="rec-chip">REC</span>' : isFacebook ? '<span class="fb-chip">FB</span>' : isInstagram ? '<span class="fb-chip ig-chip">IG</span>' : isX ? '<span class="fb-chip x-chip">X</span>' : isYouTube ? '<span class="fb-chip yt-chip">YT</span>' : ""}${name}</div>
        <div class="meta"><span class="id">#${id}</span> · ${c.collection ? `<span class="theme-chip coll-chip" title="collection">${escapeHtml(c.collection.name)}</span> · ` : ""}${c.theme ? `<span class="theme-chip" title="theme-based selection">theme: ${escapeHtml(c.theme)}</span> · ` : ""}${isRec ? "recording session" : isFacebook ? "Facebook Page capture" : isInstagram ? `Instagram capture · ${seedsTotal} target(s)` : isX ? `X capture · ${seedsTotal} target(s)` : isYouTube ? `YouTube capture · ${seedsTotal} target(s)` : `${seedsTotal} seed(s)`} · ${created}</div>
      </div>
      <div class="counts">
        ${isRec
          ? `<b>${visited}</b> pages<br>${fmtBytes(bytes)}`
          : isYouTube
            ? `<b>${Number(fb.videos_exported) || 0}</b> videos · <b>${Number(fb.channel_posts_exported) || 0}</b> posts · <b>${Number(fb.comments_exported) || 0}</b> comments · <b>${Number(fb.media_captured) || 0}</b> files<br>${Number(fb.warc_files) ? "WARC · " : ""}${fmtBytes(bytes)}`
          : isX
            ? `<b>${Number(fb.posts_exported) || 0}</b> posts${Number(fb.reposts_exported) ? ` (${Number(fb.reposts_exported)} reposts)` : ""} · <b>${Number(fb.context_posts) || 0}</b> context · <b>${Number(fb.media_captured) || 0}</b>${Number(fb.media_expected) ? `/${Number(fb.media_expected)}` : ""} media<br>${Number(fb.warc_files) ? "WARC · " : ""}${fmtBytes(bytes)}`
          : isInstagram
            ? `<b>${Number(fb.posts_exported) || 0}</b> posts · <b>${Number(fb.comments_exported) || 0}</b>${Number(fb.comments_available) ? `/${Number(fb.comments_available)}` : ""} comments · <b>${Number(fb.media_captured) || 0}</b>${Number(fb.media_expected) ? `/${Number(fb.media_expected)}` : ""} media<br>${Number(fb.warc_files) ? "rendered WARC · " : ""}${fmtBytes(bytes)}`
          : isFacebook
            ? `<b>${Number(fb.posts_exported) || 0}</b> posts · <b>${Number(fb.comments_exported) || 0}</b>${Number(fb.comments_available) ? `/${Number(fb.comments_available)}` : ""} comments · <b>${Number(fb.media_captured) || 0}</b> media${Number(fb.album_photos_viewed) ? ` · <b>${Number(fb.album_photos_viewed)}</b> album photos` : ""}<br>${Number(fb.graphql_responses) || 0} API responses${Number(fb.pagination_failures) ? ` · ${Number(fb.pagination_failures)} failed` : ""} · ${fmtBytes(bytes)}`
          : `<b>${visited}</b> pages · <b>${queued}</b> queued${failed ? ` · ${failed} failed` : ""}<br>${fmtBytes(bytes)}${dedupLine}`}${usageLine}
      </div>
      <span class="badge b-${statusCss}">${status}</span>
    </div>
    ${waiting ? `<div class="fb-phase">
      <span class="fb-phase-label">Waiting</span>
      <span>Created, not started: this machine was short of CPU, memory or disk space. It starts by itself once every resource is above its warning level, or now if you say so.</span>
    </div>` : ""}
    ${isSocial && (fb.message || fb.phase) ? `<div class="fb-phase">
      <span class="fb-phase-label">${escapeHtml(facebookPhaseLabel(fb.phase))}</span>
      <span>${escapeHtml(fb.message || "")}</span>
    </div>` : ""}
    ${!isSocial ? warcIndexLine(id, c.warc_index) : ""}
    <div class="actions">
      <div class="act-group" role="group" aria-label="Run controls">
        <button class="act" onclick="ctl(${id},'pause')" ${canPause ? "" : "disabled"}>${isFacebook ? "Pause scrolling" : "Pause"}</button>
        <button class="act" onclick="ctl(${id},'resume')" ${canResume ? "" : "disabled"}>${isSocial && blocked ? "I have resolved it — continue" : isFacebook ? "Resume scrolling" : "Resume"}</button>
        ${waiting ? `<button class="act" onclick="startNow(${id})">Start now</button>
        <button class="act stop" onclick="ctl(${id},'stop')">Cancel</button>` : `<button class="act stop" onclick="ctl(${id},'stop')" ${canStop ? "" : "disabled"}>${isSocial ? "Stop and save" : "Stop"}</button>`}
        ${rawStatus === "stopping" ? `<button class="act stop" onclick="forceStop(${id})" title="End the worker now if it is not answering">Force stop</button>` : ""}
        ${isFacebook ? `<button class="act" onclick="continueFacebook(${id})" ${["stopped", "failed"].includes(rawStatus) ? "" : "disabled"}>Continue</button>` : ""}
      </div>
      <div class="act-group" role="group" aria-label="Open the capture">
        ${isFacebook ? `<button class="act replay" onclick="replay(${id},'pages')" ${Number(fb.posts_exported) > 0 ? "" : "disabled"}>Open pages</button>
        <button class="act replay" onclick="replay(${id},'warc')" ${hasWarc ? "" : "disabled"} title="Shows the Page as it first loaded">Replay WARC</button>`
        : isTargeted ? `<button class="act replay" onclick="replay(${id},'pages')" ${Number(fb.posts_exported) > 0 || Number(fb.users_exported) > 0 ? "" : "disabled"}>Open pages</button>
        <button class="act replay" onclick="replay(${id},'warc')" ${Number(fb.warc_files) > 0 ? "" : "disabled"} title="How ${isX ? "X" : isYouTube ? "YouTube" : "Instagram"} presented the captured posts">Replay WARC</button>`
        : `<button class="act replay" onclick="replay(${id})" ${hasWarc ? "" : "disabled"}>Replay</button>`}
      </div>
      <details class="more" ${moreOpen.has(id) ? "open" : ""} ontoggle="moreToggled(${id}, this.open)">
        <summary class="act" aria-label="More actions for this job">More ▾</summary>
        <div class="menu" role="menu">
          <button role="menuitem" onclick="editMetadata(${id})" title="Describe this capture: title, creator, subject, rights…">Metadata${Number(c.metadata_fields) ? ` · ${Number(c.metadata_fields)}` : ""}</button>
          ${isSocial ? `<button role="menuitem" onclick="indexCapture(${id})" ${(running || paused || blocked || rawStatus === "stopping") ? "disabled" : ""} title="Index the captured posts, comments and profiles as search documents in warc-indexer's schema, beside the capture">${c.index && c.index.documents != null ? `Re-index · ${Number(c.index.documents)}` : "Index"}</button>`
          : (() => {
            // crawls and recordings: the warc-indexer jar over the WARC files.
            // When Java or the jar is missing the item stays live: clicking
            // it opens Settings › Indexer to say where they are.
            const wi = c.warc_index || null;
            const indexing = wi && wi.status === "running";
            const cap = (typeof warcIndexerCapability !== "undefined" && warcIndexerCapability) || {available: true};
            const off = indexing || !hasWarc || running || paused || blocked || rawStatus === "stopping";
            const title = !cap.available ? escapeHtml((cap.reason || "warc-indexer is unavailable") + " Click to set it up.")
              : "Run warc-indexer over this job's WARC files; each gets a <name>.jsonl of search documents beside it";
            const label = indexing ? "Indexing…"
              : !cap.available ? "Index WARC · set up"
              : wi && wi.status === "done" && wi.documents != null ? `Re-index WARC · ${Number(wi.documents)}`
              : wi && wi.status === "failed" ? "Index WARC · retry" : "Index WARC";
            return `<button role="menuitem" onclick="indexWarc(${id})" ${off ? "disabled" : ""} title="${title}">${label}</button>`;
          })()}
          ${c.has_selection ? `<button role="menuitem" onclick="openSelection(${id})" title="What the theme kept, held for review and left out, with the reasons">Selection</button>` : ""}
          ${ch && changeParts.length ? `<button role="menuitem" onclick="window.open('/api/crawls/${id}/changes', '_blank')" title="Page-by-page report against the collection's earlier captures">Page changes</button>` : ""}
        </div>
      </details>
      <span class="spacer"></span>
      <button class="act ghost danger" onclick="del(${id})" ${(running || paused || blocked || (c.warc_index && c.warc_index.status === "running")) ? "disabled" : ""} ${c.warc_index && c.warc_index.status === "running" ? 'title="Wait for the indexer to finish"' : 'title="Delete this job; you are asked to confirm, and told what depends on it"'}>Delete</button>
    </div>
    <div class="seeds">${seedRows}</div>
  </div>`;
}

// Which jobs' More menus are open: the list is rebuilt every two seconds
// and must not close a menu the curator has just opened.
const moreOpen = new Set();
function moreToggled(id, open) { if (open) moreOpen.add(id); else moreOpen.delete(id); }

// What a crawl's or recording's card says about its warc-indexer run: the
// progress while it goes, the outcome after, and the error with a link to
// the jar's log when it failed.
function warcIndexLine(id, wi) {
  if (!wi || !wi.status) return "";
  const n = v => Number(v || 0).toLocaleString();
  if (wi.status === "running") {
    const p = wi.progress || {};
    const where = p.files_total > 1 ? `file ${p.file_index || 0} of ${p.files_total}` : (p.current_warc ? escapeHtml(p.current_warc) : "starting");
    const secs = Number(p.elapsed_seconds || 0);
    const when = secs >= 90 ? `${Math.round(secs / 60)} min` : `${secs} s`;
    return `<div class="fb-phase"><span class="fb-phase-label">Indexing</span>
      <span>${where} · ${n(p.documents)} documents so far · ${when}</span></div>`;
  }
  if (wi.status === "failed") {
    const detail = wi.error_detail ? `<details><summary>What the indexer said</summary><pre>${escapeHtml(wi.error_detail)}</pre></details>` : "";
    return `<div class="fb-phase warc-index-failed"><span class="fb-phase-label">Indexing failed</span>
      <span>${escapeHtml(wi.error || "no reason recorded")} <a href="/api/crawls/${id}/warc-index/log" target="_blank" rel="noopener">Open the log</a></span>${detail}</div>`;
  }
  if (wi.status === "done") {
    return `<div class="fb-phase"><span class="fb-phase-label">Indexed</span>
      <span>${n(wi.documents)} documents from ${n(wi.warcs)} WARC file${Number(wi.warcs) === 1 ? "" : "s"}, beside the WARC${wi.finished_at ? ` · ${escapeHtml(wi.finished_at)}` : ""}</span></div>`;
  }
  return "";
}

// The page uses .mode-tab for both job-type tabs and editor tabs. Scope editor
// updates to #new-form so switching Guided/Raw never clears Automated/Record.
async function switchMode(mode) {
  if (mode === editorMode) return;
  setMessage("");
  try {
    if (mode === "raw") {
      await renderRawFromForm();
      $("#mode-note").textContent = "Complete control for advanced settings.";
    } else {
      await importRawIntoForm();
      $("#mode-note").textContent = "Recommended for most crawls.";
    }
  } catch (error) {
    setMessage(error.message, "err");
    return;
  }
  editorMode = mode;
  $$("#new-form .mode-tab").forEach(tab => {
    const active = tab.dataset.mode === mode;
    tab.classList.toggle("active", active);
    tab.setAttribute("aria-selected", active ? "true" : "false");
  });
  $("#guided-panel").classList.toggle("active", mode === "guided");
  $("#raw-panel").classList.toggle("active", mode === "raw");
}

function restoreEditorTabs(mode = "guided") {
  $$("#new-form .mode-tab").forEach(tab => {
    const active = tab.dataset.mode === mode;
    tab.classList.toggle("active", active);
    tab.setAttribute("aria-selected", active ? "true" : "false");
  });
}

// resetExample() ran before this script loaded and its original click handler is
// already registered. Restore the correct tab state after that handler runs.
restoreEditorTabs("guided");
switchJob("crawl");
$("#reset-btn").addEventListener("click", () => {
  restoreEditorTabs("guided");
  switchJob("crawl");
});
