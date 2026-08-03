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
    "completed", "failed", "blocked"
  ]);
  const normalised = String(value || "pending").toLowerCase();
  return allowed.has(normalised) ? normalised : "pending";
}

// Override the renderer used by every refresh. All values originating in the
// API or SQLite are escaped before entering an innerHTML template.
function crawlRow(c) {
  const id = Number(c.id);
  if (!Number.isSafeInteger(id) || id < 0) return "";

  const rawStatus = String(c.status || "pending");
  const status = escapeHtml(rawStatus);
  const statusCss = statusClass(rawStatus);
  const isRec = c.kind === "recording";
  const running = rawStatus === "running";
  const paused = rawStatus === "paused";
  const canPause = running;
  const canResume = paused;
  const canStop = running || paused || rawStatus === "stopping";
  const isOpen = openState.has(id);
  const t = c.totals || {};
  const visited = Number(t.visited) || 0;
  const queued = Number(t.queued) || 0;
  const failed = Number(t.failed) || 0;
  const bytes = Number(t.bytes) || 0;
  const seeds = Array.isArray(c.seeds) ? c.seeds : [];

  const seedRows = seeds.map(sd => {
    const seedStatus = escapeHtml(sd.status || "pending");
    const seedVisited = Number(sd.visited) || 0;
    const seedBytes = Number(sd.bytes) || 0;
    const current = sd.current_url
      ? `<div class="cur">▸ ${escapeHtml(sd.current_url)}</div>`
      : "";
    return `
      <div class="seed">
        <div>
          <div class="url">${escapeHtml(sd.seed_url)}</div>
          ${current}
        </div>
        <div class="st">${seedStatus} · ${seedVisited} pages · ${fmtBytes(seedBytes)}</div>
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
        <div class="name">${isRec ? '<span class="rec-chip">REC</span>' : ""}${name}</div>
        <div class="meta"><span class="id">#${id}</span> · ${isRec ? "recording session" : `${seedsTotal} seed(s)`} · ${created}</div>
      </div>
      <div class="counts">
        ${isRec
          ? `<b>${visited}</b> pages<br>${fmtBytes(bytes)}`
          : `<b>${visited}</b> pages · <b>${queued}</b> queued${failed ? ` · ${failed} failed` : ""}<br>${fmtBytes(bytes)}`}
      </div>
      <span class="badge b-${statusCss}">${status}</span>
    </div>
    <div class="actions">
      <button class="act" onclick="ctl(${id},'pause')" ${canPause ? "" : "disabled"}>Pause</button>
      <button class="act" onclick="ctl(${id},'resume')" ${canResume ? "" : "disabled"}>Resume</button>
      <button class="act danger" onclick="ctl(${id},'stop')" ${canStop ? "" : "disabled"}>Stop</button>
      <button class="act replay" onclick="replay(${id})" ${bytes > 0 ? "" : "disabled"}>Replay</button>
      <button class="act danger" onclick="del(${id})" ${(running || paused) ? "disabled" : ""}>Delete</button>
    </div>
    <div class="seeds">${seedRows}</div>
  </div>`;
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
