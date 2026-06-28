/* MAS FID Directory — internal app frontend (no build step, vanilla JS). */

// ---------------------------------------------------------------- helpers
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

function esc(s) {
  return String(s ?? "")
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

async function api(url, opts) {
  const res = await fetch(url, opts);
  let data = null;
  try { data = await res.json(); } catch { /* non-json */ }
  return { ok: res.ok, status: res.status, data };
}

let toastTimer = null;
function toast(msg, kind = "") {
  const t = $("#toast");
  t.className = "toast " + kind;
  t.innerHTML = msg;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.add("hidden"), 5000);
}

function selectedValues(sel) {
  return [...sel.selectedOptions].map((o) => o.value);
}

function populateSelect(sel, values) {
  const chosen = new Set(selectedValues(sel));
  sel.innerHTML = "";
  for (const v of values) {
    const o = document.createElement("option");
    o.value = v; o.textContent = v;
    if (chosen.has(v)) o.selected = true;
    sel.appendChild(o);
  }
}

function qs(params) {
  const p = new URLSearchParams();
  for (const s of params.sectors || []) p.append("sector", s);
  for (const l of params.licences || []) p.append("licence", l);
  if (params.q) p.set("q", params.q);
  if (params.page) p.set("page", params.page);
  if (params.page_size) p.set("page_size", params.page_size);
  if (params.sort) p.set("sort", params.sort);
  if (params.dir) p.set("dir", params.dir);
  return p.toString();
}

// ---------------------------------------------------------------- theme
function applyTheme(theme) {
  document.documentElement.setAttribute("data-theme", theme);
  updateThemeIcon(theme);
  setLogo(theme);
  if (lastDelta) renderChart(lastDelta.chart || []); // SVG picks up theme vars
}
function initTheme() {
  const saved = localStorage.getItem("theme") || "dark";
  applyTheme(saved);
  $("#themeToggle").addEventListener("click", () => {
    const cur = document.documentElement.getAttribute("data-theme");
    const next = cur === "dark" ? "light" : "dark";
    localStorage.setItem("theme", next);
    applyTheme(next);
  });
}
function updateThemeIcon(theme) {
  $("#themeToggle").textContent = theme === "dark" ? "🌙" : "☀️";
}
// Prefer the official PNG (prive-logo-<theme>.png); fall back to the bundled SVG.
function setLogo(theme) {
  const img = $("#brandLogo");
  if (!img) return;
  const svg = `/static/assets/prive-logo-${theme}.svg`;
  const png = `/static/assets/prive-logo-${theme}.png`;
  img.onerror = () => { img.onerror = null; img.src = svg; };
  img.src = png;
}

// ---------------------------------------------------------------- tabs
function initTabs() {
  $$(".tab").forEach((btn) => {
    btn.addEventListener("click", () => {
      $$(".tab").forEach((b) => b.classList.remove("active"));
      $$(".panel").forEach((p) => p.classList.remove("active"));
      btn.classList.add("active");
      $("#panel-" + btn.dataset.tab).classList.add("active");
      if (btn.dataset.tab === "directory") loadDirectory();
      if (btn.dataset.tab === "config") loadFiles();
    });
  });
}

// ---------------------------------------------------------------- delta
let lastDelta = null;

function renderBanner(data) {
  const b = $("#deltaBanner");
  const r = data.refresh;
  if (r && r.unchanged) {
    b.className = "banner warn";
    b.innerHTML = `<span class="icon">⚠️</span><div><strong>No update.</strong>
      The MAS directory is unchanged since <strong>${esc(r.date)}</strong> — no new snapshot stored.</div>`;
    return;
  }
  if (data.available && data.no_changes) {
    b.className = "banner warn";
    b.innerHTML = `<span class="icon">⚠️</span><div><strong>No week-over-week changes</strong>
      between <strong>${esc(data.old_date)}</strong> and <strong>${esc(data.new_date)}</strong>.</div>`;
    return;
  }
  if (!data.available) {
    b.className = "banner muted";
    b.innerHTML = `<span class="icon">ℹ️</span><div>${esc(data.message || "")}</div>`;
    return;
  }
  if (r && r.saved) {
    b.className = "banner info";
    let extra = r.deleted && r.deleted.length
      ? ` Auto-cleanup removed ${r.deleted.length} old file(s).` : "";
    b.innerHTML = `<span class="icon">✅</span><div><strong>Updated.</strong>
      New snapshot <strong>${esc(r.date)}</strong> stored.${extra}</div>`;
    return;
  }
  b.className = "banner hidden";
}

function renderDelta(data) {
  lastDelta = data;
  renderBanner(data);

  const meta = $("#refreshMeta");
  if (data.available) {
    meta.innerHTML = `Comparing <strong>${esc(data.old_date)}</strong> →
      <strong>${esc(data.new_date)}</strong>`;
  } else {
    meta.textContent = "";
  }

  // populate filter selects (preserve selection)
  if (data.all_sectors) populateSelect($("#deltaSector"), data.all_sectors);
  if (data.all_licence_types) populateSelect($("#deltaLicence"), data.all_licence_types);

  const body = $("#deltaBody");
  if (!data.available) { body.innerHTML = ""; return; }

  const netClass = data.net_change > 0 ? "success" : data.net_change < 0 ? "danger" : "";
  const netSign = data.net_change > 0 ? "+" : "";

  body.innerHTML = `
    <div class="kpi-row">
      <div class="kpi"><div class="label">Total institutions</div>
        <div class="value">${data.new_count.toLocaleString()}</div></div>
      <div class="kpi"><div class="label">Net change</div>
        <div class="value ${netClass}">${netSign}${data.net_change}</div></div>
      <div class="kpi"><div class="label">New this week</div>
        <div class="value success">${data.total_new}</div></div>
      <div class="kpi"><div class="label">Removed this week</div>
        <div class="value danger">${data.total_removed}</div></div>
    </div>

    <div class="card">
      <h3>Movements by sector (week-over-week)</h3>
      <div class="chart-legend">
        <span><span class="swatch" style="background:var(--success)"></span>New</span>
        <span><span class="swatch" style="background:var(--danger)"></span>Removed</span>
      </div>
      <div id="chart"></div>
    </div>

    <div class="card">
      <div class="section-title"><span class="dot new"></span>
        <h3 style="margin:0;">New institutions</h3>
        <span class="count">(${data.shown_new} shown)</span>
        <span class="grow"></span>
        <button class="btn btn-sm" id="copyDelta">Copy summary</button>
      </div>
      ${tableHTML(data.new_entries, data.columns)}
    </div>

    <div class="card">
      <div class="section-title"><span class="dot removed"></span>
        <h3 style="margin:0;">Removed institutions</h3>
        <span class="count">(${data.shown_removed} shown)</span>
      </div>
      ${tableHTML(data.removed_entries, data.columns)}
    </div>
  `;

  renderChart(data.chart || []);
  $("#copyDelta")?.addEventListener("click", () => copySummary(data));
}

function copySummary(data) {
  const fmt = (rows) => rows.map((r) =>
    `${r["Organisation Name"]} (${r["Licence Type/Status"]}) — ${r["Sector"]}`).join("\n") || "None.";
  const text =
    `MAS FID delta  ${data.old_date} → ${data.new_date}\n` +
    `New (${data.shown_new})\n${"=".repeat(50)}\n${fmt(data.new_entries)}\n\n` +
    `Removed (${data.shown_removed})\n${"=".repeat(50)}\n${fmt(data.removed_entries)}`;
  navigator.clipboard.writeText(text)
    .then(() => toast("Summary copied to clipboard.", "success"))
    .catch(() => toast("Copy failed.", "error"));
}

function tableHTML(rows, cols) {
  if (!rows || !rows.length) return `<div class="empty">None match current filters.</div>`;
  // Long, noisy columns: truncate to one line with the full text on hover.
  const clampCols = new Set(["Activity/Business Type", "Sub-Activity/Product"]);
  const wrapCols = new Set(["Address"]);
  const head = cols.map((c) => `<th>${esc(c)}</th>`).join("");
  const body = rows.map((r) => "<tr>" + cols.map((c) => {
    const v = r[c] ?? "";
    if (c === "Website" && v) {
      const href = v.startsWith("http") ? v : "http://" + v;
      return `<td><a href="${esc(href)}" target="_blank" rel="noopener">${esc(v)}</a></td>`;
    }
    if (clampCols.has(c)) {
      return `<td class="clamp" title="${esc(v)}">${esc(v)}</td>`;
    }
    return `<td class="${wrapCols.has(c) ? "wrap" : ""}">${esc(v)}</td>`;
  }).join("") + "</tr>").join("");
  return `<div class="table-wrap"><table class="data"><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>`;
}

// ---------------------------------------------------------------- chart (SVG, no deps)
function renderChart(chart) {
  const host = $("#chart");
  if (!host) return;
  const data = (chart || []).filter((d) => d.new || d.removed);
  if (!data.length) {
    host.innerHTML = `<div class="empty">No movements to plot.</div>`;
    return;
  }
  const M = { top: 16, right: 12, bottom: 78, left: 40 };
  const perGroup = 84;
  const innerW = data.length * perGroup;
  const W = innerW + M.left + M.right;
  const H = 300;
  const innerH = H - M.top - M.bottom;
  const maxV = Math.max(1, ...data.map((d) => Math.max(d.new, d.removed)));
  const y = (v) => M.top + innerH - (v / maxV) * innerH;
  const barW = 26, gap = 8;

  // y gridlines / ticks (integer)
  const ticks = [];
  const step = Math.max(1, Math.ceil(maxV / 5));
  for (let v = 0; v <= maxV; v += step) ticks.push(v);
  if (ticks[ticks.length - 1] !== maxV) ticks.push(maxV);

  let svg = `<svg class="chart-svg" viewBox="0 0 ${W} ${H}" preserveAspectRatio="xMidYMid meet">`;
  // gridlines + y labels
  for (const t of ticks) {
    const yy = y(t);
    svg += `<line class="grid" x1="${M.left}" y1="${yy}" x2="${W - M.right}" y2="${yy}"/>`;
    svg += `<text x="${M.left - 6}" y="${yy + 3}" text-anchor="end">${t}</text>`;
  }
  // x axis line
  svg += `<line class="axis" x1="${M.left}" y1="${M.top + innerH}" x2="${W - M.right}" y2="${M.top + innerH}"/>`;

  data.forEach((d, i) => {
    const gx = M.left + i * perGroup + (perGroup - (barW * 2 + gap)) / 2;
    const groups = [
      { v: d.new, color: "var(--success)", x: gx },
      { v: d.removed, color: "var(--danger)", x: gx + barW + gap },
    ];
    for (const g of groups) {
      const h = (g.v / maxV) * innerH;
      const yy = M.top + innerH - h;
      svg += `<rect x="${g.x}" y="${yy}" width="${barW}" height="${h}" rx="3" fill="${g.color}"/>`;
      if (g.v) svg += `<text class="bar-label" x="${g.x + barW / 2}" y="${yy - 4}" text-anchor="middle">${g.v}</text>`;
    }
    // sector label (wrapped onto two lines if long)
    const cx = M.left + i * perGroup + perGroup / 2;
    const label = d.sector || "—";
    const words = label.split(" ");
    let lines = [label];
    if (label.length > 12 && words.length > 1) {
      const mid = Math.ceil(words.length / 2);
      lines = [words.slice(0, mid).join(" "), words.slice(mid).join(" ")];
    }
    const ly = M.top + innerH + 16;
    lines.forEach((ln, k) => {
      svg += `<text x="${cx}" y="${ly + k * 13}" text-anchor="middle">${esc(ln)}</text>`;
    });
  });
  svg += `</svg>`;
  host.innerHTML = svg;
}

async function loadDelta(extra = {}) {
  const params = {
    sectors: selectedValues($("#deltaSector")),
    licences: selectedValues($("#deltaLicence")),
    q: $("#deltaQuery").value.trim(),
  };
  const { data } = await api("/api/delta?" + qs(params));
  renderDelta({ ...data, ...extra });
}

async function doRefresh() {
  const btn = $("#refreshBtn");
  btn.disabled = true;
  const orig = btn.innerHTML;
  btn.innerHTML = `<span class="spinner"></span> Fetching…`;
  try {
    const { ok, data } = await api("/api/refresh", { method: "POST" });
    if (!ok || !data || data.ok === false) {
      const msg = (data && data.error) || "Refresh failed.";
      $("#deltaBanner").className = "banner error";
      $("#deltaBanner").innerHTML =
        `<span class="icon">⛔</span><div><strong>Auto-refresh unavailable.</strong>
         ${esc(msg)} <a href="#" id="goConfig">Upload manually →</a></div>`;
      $("#goConfig")?.addEventListener("click", (e) => {
        e.preventDefault(); $$(".tab").find((t) => t.dataset.tab === "config")?.click();
      });
      toast("Auto-refresh unavailable — use manual upload.", "error");
      // still refresh the (possibly existing) delta view underneath
      await loadDelta();
      return;
    }
    renderDelta(data);
    if (data.refresh?.saved) toast(`Snapshot ${data.refresh.date} stored.`, "success");
    else if (data.refresh?.unchanged) toast("No update — directory unchanged.", "");
  } finally {
    btn.disabled = false;
    btn.innerHTML = orig;
  }
}

// ---------------------------------------------------------------- directory
let dirState = { page: 1, sort: "Organisation Name", dir: "asc" };

async function loadDirectory() {
  const params = {
    sectors: selectedValues($("#dirSector")),
    licences: selectedValues($("#dirLicence")),
    q: $("#dirQuery").value.trim(),
    page: dirState.page,
    page_size: 50,
    sort: dirState.sort,
    dir: dirState.dir,
  };
  const { data } = await api("/api/directory?" + qs(params));
  const body = $("#dirBody");
  const meta = $("#dirMeta");
  if (!data || !data.available) {
    meta.textContent = "";
    body.innerHTML = `<div class="empty">${esc((data && data.message) || "No data.")}</div>`;
    return;
  }
  populateSelect($("#dirSector"), data.all_sectors);
  populateSelect($("#dirLicence"), data.all_licence_types);

  meta.innerHTML = `Snapshot <strong>${esc(data.date)}</strong> · showing
    <strong>${data.total.toLocaleString()}</strong> of ${data.grand_total.toLocaleString()} institutions`;

  body.innerHTML = tableHTML(data.rows, data.columns) + `
    <div class="pager">
      <button class="btn btn-sm" id="prevPage" ${data.page <= 1 ? "disabled" : ""}>← Prev</button>
      <span class="meta">Page ${data.page} / ${data.pages}</span>
      <button class="btn btn-sm" id="nextPage" ${data.page >= data.pages ? "disabled" : ""}>Next →</button>
    </div>`;

  // clickable sortable headers
  $$("#dirBody thead th").forEach((th) => {
    const col = th.textContent.trim();
    th.style.cursor = "pointer";
    if (col === dirState.sort) th.innerHTML = `${esc(col)} ${dirState.dir === "asc" ? "▲" : "▼"}`;
    th.addEventListener("click", () => {
      if (dirState.sort === col) dirState.dir = dirState.dir === "asc" ? "desc" : "asc";
      else { dirState.sort = col; dirState.dir = "asc"; }
      dirState.page = 1; loadDirectory();
    });
  });
  $("#prevPage")?.addEventListener("click", () => { dirState.page--; loadDirectory(); });
  $("#nextPage")?.addEventListener("click", () => { dirState.page++; loadDirectory(); });
}

// ---------------------------------------------------------------- config / files
async function loadFiles() {
  const { data } = await api("/api/files");
  $("#dataDir").textContent = data.data_dir;
  $("#maxFiles").textContent = data.max_files;
  renderFiles(data.files, data.max_files);
}

function renderFiles(files, maxFiles) {
  const body = $("#filesBody");
  if (!files || !files.length) {
    body.innerHTML = `<div class="empty">No snapshots stored yet.</div>`;
    return;
  }
  const rows = files.map((f, i) => `
    <tr>
      <td><strong>${esc(f.date)}</strong>${i === 0 ? ' <span class="chip">latest</span>' : ""}</td>
      <td>${esc(f.name)}</td>
      <td>${f.size_kb.toLocaleString()} KB</td>
      <td>${esc(f.modified.replace("T", " "))}</td>
      <td><button class="btn btn-danger btn-sm" data-del="${esc(f.name)}">Delete</button></td>
    </tr>`).join("");
  body.innerHTML = `
    <div class="meta" style="margin-bottom:8px;">${files.length} / ${maxFiles} files stored</div>
    <div class="table-wrap"><table class="data">
      <thead><tr><th>Date</th><th>File</th><th>Size</th><th>Modified</th><th></th></tr></thead>
      <tbody>${rows}</tbody></table></div>`;
  $$("#filesBody [data-del]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      if (!confirm(`Delete ${btn.dataset.del}?`)) return;
      const { ok, data } = await api("/api/files/" + encodeURIComponent(btn.dataset.del), { method: "DELETE" });
      if (ok) { renderFiles(data.files, maxFiles); toast("File deleted.", "success"); }
      else toast((data && data.error) || "Delete failed.", "error");
    });
  });
}

function initUpload() {
  const dz = $("#dropzone");
  const input = $("#fileInput");
  dz.addEventListener("click", () => input.click());
  input.addEventListener("change", () => input.files.length && uploadFile(input.files[0]));
  ["dragenter", "dragover"].forEach((ev) =>
    dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.add("drag"); }));
  ["dragleave", "drop"].forEach((ev) =>
    dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.remove("drag"); }));
  dz.addEventListener("drop", (e) => {
    if (e.dataTransfer.files.length) uploadFile(e.dataTransfer.files[0]);
  });
}

async function uploadFile(file) {
  const msg = $("#uploadMsg");
  msg.innerHTML = `<span class="spinner"></span> Uploading ${esc(file.name)}…`;
  const fd = new FormData();
  fd.append("file", file);
  const { ok, data } = await api("/api/upload", { method: "POST", body: fd });
  if (ok && data.ok) {
    msg.innerHTML = `✅ Stored <strong>${esc(data.saved)}</strong> (${data.rows.toLocaleString()} rows).`
      + (data.deleted.length ? ` Removed ${data.deleted.length} old file(s).` : "");
    renderFiles(data.files, Number($("#maxFiles").textContent));
    toast("File uploaded.", "success");
    loadDelta(); // refresh delta view with the new file
  } else {
    msg.innerHTML = `⛔ ${esc((data && data.error) || "Upload failed.")}`;
    toast("Upload failed.", "error");
  }
}

// ---------------------------------------------------------------- init
function initDeltaFilters() {
  $("#deltaFilterToggle").addEventListener("click", () => {
    const f = $("#deltaFilters");
    f.style.display = f.style.display === "none" ? "block" : "none";
  });
  $("#deltaApply").addEventListener("click", () => loadDelta());
  $("#deltaReset").addEventListener("click", () => {
    $("#deltaSector").selectedIndex = -1;
    $("#deltaLicence").selectedIndex = -1;
    $("#deltaQuery").value = "";
    loadDelta();
  });
  $("#deltaQuery").addEventListener("keydown", (e) => e.key === "Enter" && loadDelta());
}

function initDirFilters() {
  $("#dirApply").addEventListener("click", () => { dirState.page = 1; loadDirectory(); });
  $("#dirReset").addEventListener("click", () => {
    $("#dirSector").selectedIndex = -1;
    $("#dirLicence").selectedIndex = -1;
    $("#dirQuery").value = "";
    dirState = { page: 1, sort: "Organisation Name", dir: "asc" };
    loadDirectory();
  });
  $("#dirQuery").addEventListener("keydown", (e) => {
    if (e.key === "Enter") { dirState.page = 1; loadDirectory(); }
  });
}

window.addEventListener("DOMContentLoaded", () => {
  initTheme();
  initTabs();
  initDeltaFilters();
  initDirFilters();
  initUpload();
  $("#refreshBtn").addEventListener("click", doRefresh);
  loadDelta(); // first paint
});
