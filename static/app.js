"use strict";

// ---------------------------------------------------------------------------
// Tiny helpers
// ---------------------------------------------------------------------------
const $ = (s, r = document) => r.querySelector(s);
const esc = (s) => (s == null ? "" : String(s)).replace(/[&<>"]/g,
  c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const money = (n) => "$" + (Number(n || 0)).toLocaleString();

async function api(path, opts) {
  const res = await fetch(path, Object.assign({ headers: { "Content-Type": "application/json" } }, opts));
  let data = null;
  try { data = await res.json(); } catch (e) { /* no body */ }
  if (!res.ok) throw Object.assign(new Error((data && data.error) || res.statusText), { data, status: res.status });
  return data;
}

function toast(msg, kind = "") {
  const t = document.createElement("div");
  t.className = "toast " + kind;
  t.textContent = msg;
  $("#toasts").appendChild(t);
  requestAnimationFrame(() => t.classList.add("show"));
  setTimeout(() => { t.classList.remove("show"); setTimeout(() => t.remove(), 250); }, 2600);
}

const STAGE_COLORS = {
  to_research: "#8a93a6", drafted: "#c98a00", finalized: "#7a5bd8", contacted: "#3b5bdb",
  replied: "#1f9d6b", call: "#0d9488", proposal: "#d4761a", won: "#10795a",
  do_not_contact: "#d64545", archived: "#9aa3b2",
};

let META = null;
let CURRENT_TAB = "today";

// ---------------------------------------------------------------------------
// Boot
// ---------------------------------------------------------------------------
async function boot() {
  META = await api("/api/meta");
  $("#tabs").addEventListener("click", (e) => {
    const b = e.target.closest("button[data-tab]");
    if (!b) return;
    setTab(b.dataset.tab);
  });
  $("#overlay").addEventListener("click", closeDrawer);
  $("#d-close").addEventListener("click", closeDrawer);
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeDrawer(); });
  renderTopstats();
  setTab("today");
}

async function renderTopstats() {
  try {
    const m = await api("/api/metrics");
    const active = m.counts.to_research + m.counts.drafted + m.counts.finalized;
    const convos = m.counts.replied + m.counts.call + m.counts.proposal;
    const el = $("#topstats");
    el.innerHTML = `
      <span class="pill ${m.due_today ? "alert" : ""}">Follow-ups due <b>${m.due_today}</b></span>
      <span class="pill">Conversations <b>${convos}</b></span>
      <span class="pill">Active <b>${active}</b></span>
      <span class="pill">Won <b>${m.counts.won}</b></span>`;
  } catch (e) { /* ignore */ }
}

function setTab(tab) {
  CURRENT_TAB = tab;
  [...$("#tabs").children].forEach(b => b.classList.toggle("active", b.dataset.tab === tab));
  ({ today: renderToday, board: renderBoard, table: renderTable, dashboard: renderDashboard, gmail: renderGmail }[tab])();
}

// ---------------------------------------------------------------------------
// Shared row / card renderers
// ---------------------------------------------------------------------------
function statusChip(s) {
  if (!s) return "";
  return `<span class="chip ${esc(s)}">${esc(s)}</span>`;
}
function reviewBadge(n) { return `<span class="rev">${n || 0}<small> rev</small></span>`; }

function leadRow(l, subtext) {
  return `<div class="lead-row" data-id="${l.id}">
    ${reviewBadge(l.review_count)}
    <div style="flex:1;min-width:0">
      <div class="name">${esc(l.company_name)}</div>
      <div class="sub">${subtext || ""}</div>
    </div>
    ${l.score_total != null ? `<span class="chip score">${l.score_total}/14</span>` : ""}
    ${statusChip(l.website_status)}
  </div>`;
}

function bindRows(container) {
  container.querySelectorAll(".lead-row[data-id], tr[data-id], .kcard[data-id]").forEach(r => {
    r.addEventListener("click", (e) => {
      if (e.target.closest("button,select,a")) return;
      openLead(r.dataset.id);
    });
  });
}

// ---------------------------------------------------------------------------
// TODAY
// ---------------------------------------------------------------------------
let researchOffset = 0;
async function renderToday() {
  const view = $("#view");
  view.innerHTML = `<div class="empty">Loading your queue…</div>`;
  const q = await api(`/api/queue?batch=25&offset=${researchOffset}`);

  const dueSub = (l) => `Follow-up due ${esc(l.follow_up_date)} · ${esc(l.stage_label)} · ${esc(l.next_action || "")}`;
  const activeSub = (l) => `${esc(l.stage_label)} · ${esc(l.next_action || "needs attention")}`;
  const readySub = (l) => `${esc(l.stage_label)} · ${esc(l.subject || "no subject yet")}`;
  const resSub = (l) => `${esc(l.geography || "")}${l.employees ? " · " + esc(l.employees) : ""}${l.rating ? " · ★" + l.rating : ""}`;

  const panel = (title, items, sub, extra = "") => `
    <section class="card-panel ${title === "Research next" ? "full" : ""}">
      <header><h2>${title}</h2><span class="count">${items.length}${extra}</span></header>
      <div class="body">
        ${items.length ? items.map(l => leadRow(l, sub(l))).join("") : `<div class="empty">Nothing here right now.</div>`}
      </div>
    </section>`;

  view.innerHTML = `
    <div class="queue-grid">
      ${panel("Follow-ups due", q.due, dueSub)}
      ${panel("Replies & conversations", q.active, activeSub)}
      ${panel("Ready to draft / finalize", q.ready, readySub)}
      <section class="card-panel full">
        <header><h2>Research next</h2>
          <span class="count">top ${q.research.length} by reviews · ${q.research_total} waiting</span></header>
        <div class="body">
          ${q.research.length ? q.research.map(l => leadRow(l, resSub(l))).join("") : `<div class="empty">No leads left to research.</div>`}
        </div>
        <div style="padding:10px 14px;display:flex;gap:8px;align-items:center;border-top:1px solid var(--line-soft)">
          <button class="btn sm" id="res-prev" ${researchOffset === 0 ? "disabled" : ""}>&larr; Previous</button>
          <button class="btn sm" id="res-next" ${researchOffset + 25 >= q.research_total ? "disabled" : ""}>Next batch &rarr;</button>
          <span class="sub" style="color:var(--muted);font-size:12.5px">Batch ${Math.floor(researchOffset / 25) + 1}</span>
        </div>
      </section>
    </div>`;
  $("#res-prev") && ($("#res-prev").onclick = () => { researchOffset = Math.max(0, researchOffset - 25); renderToday(); });
  $("#res-next") && ($("#res-next").onclick = () => { researchOffset += 25; renderToday(); });
  bindRows(view);
}

// ---------------------------------------------------------------------------
// BOARD
// ---------------------------------------------------------------------------
async function renderBoard() {
  const view = $("#view");
  view.innerHTML = `<div class="empty">Loading board…</div>`;
  const stages = META.pipeline_stages;
  const lists = await Promise.all(stages.map(s =>
    api(`/api/leads?stage=${s}&limit=80&order=reviews`).then(r => r.leads)));

  const col = (stage, leads) => `
    <div class="col" data-stage="${stage}">
      <header>
        <span class="stage-dot" style="background:${STAGE_COLORS[stage]}"></span>
        <span class="label">${esc(META.stage_labels[stage])}</span>
        <span class="n">${META.counts[stage] ?? leads.length}</span>
      </header>
      <div class="stack">
        ${leads.map(kcard).join("") || `<div class="empty" style="font-size:12.5px">—</div>`}
      </div>
    </div>`;

  view.innerHTML = `<div class="board">${stages.map((s, i) => col(s, lists[i])).join("")}</div>`;
  bindRows(view);
  setupDnD(view);
}

function kcard(l) {
  return `<div class="kcard" draggable="true" data-id="${l.id}">
    <div class="kname">${esc(l.company_name)}</div>
    <div class="krow">
      ${reviewBadge(l.review_count)}
      ${l.score_total != null ? `<span class="chip score">${l.score_total}/14</span>` : ""}
      ${statusChip(l.website_status)}
      ${l.priority_flag ? `<span class="chip flag">priority</span>` : ""}
    </div>
    <div class="ksub">${esc(l.geography || "")}${l.next_action ? " · " + esc(l.next_action) : ""}${l.follow_up_date ? " · due " + esc(l.follow_up_date) : ""}</div>
  </div>`;
}

let dragId = null;
function setupDnD(root) {
  root.querySelectorAll(".kcard").forEach(c => {
    c.addEventListener("dragstart", () => { dragId = c.dataset.id; c.style.opacity = ".5"; });
    c.addEventListener("dragend", () => { c.style.opacity = ""; });
  });
  root.querySelectorAll(".col").forEach(col => {
    col.addEventListener("dragover", (e) => { e.preventDefault(); col.classList.add("dragover"); });
    col.addEventListener("dragleave", () => col.classList.remove("dragover"));
    col.addEventListener("drop", async (e) => {
      e.preventDefault(); col.classList.remove("dragover");
      if (!dragId) return;
      await moveStage(dragId, col.dataset.stage, { refresh: renderBoard });
      dragId = null;
    });
  });
}

// ---------------------------------------------------------------------------
// TABLE
// ---------------------------------------------------------------------------
let tableState = { stage: "", status: "", q: "", order: "reviews", all: false };
async function renderTable() {
  const view = $("#view");
  const stageOpts = ["", ...META.pipeline_stages, ...META.terminal_stages]
    .map(s => `<option value="${s}" ${tableState.stage === s ? "selected" : ""}>${s ? META.stage_labels[s] : "All stages"}</option>`).join("");
  const statusOpts = ["", "alive", "stale", "blocked", "dead"]
    .map(s => `<option value="${s}" ${tableState.status === s ? "selected" : ""}>${s || "Any website status"}</option>`).join("");
  const orderOpts = [["reviews", "Most reviews"], ["score", "Best score"], ["name", "Name A–Z"], ["followup", "Follow-up date"]]
    .map(([v, t]) => `<option value="${v}" ${tableState.order === v ? "selected" : ""}>${t}</option>`).join("");

  view.innerHTML = `
    <div class="toolbar">
      <input type="search" id="t-q" placeholder="Search name, website, location, services…" value="${esc(tableState.q)}">
      <select id="t-stage">${stageOpts}</select>
      <select id="t-status">${statusOpts}</select>
      <select id="t-order">${orderOpts}</select>
      <label style="font-size:13px;color:var(--ink-soft);display:flex;gap:6px;align-items:center">
        <input type="checkbox" id="t-all" ${tableState.all ? "checked" : ""} style="width:auto"> show all (ignore 500 cap)
      </label>
      <span id="t-count" class="sub" style="margin-left:auto;color:var(--muted)"></span>
    </div>
    <div id="t-wrap"><div class="empty">Loading…</div></div>`;

  const run = async () => {
    const p = new URLSearchParams();
    if (tableState.stage) p.set("stage", tableState.stage);
    if (tableState.status) p.set("status", tableState.status);
    if (tableState.q) p.set("q", tableState.q);
    p.set("order", tableState.order);
    p.set("limit", tableState.all ? 10000 : 500);
    const { leads, count } = await api("/api/leads?" + p.toString());
    $("#t-count").textContent = `${count} shown`;
    $("#t-wrap").innerHTML = `
      <table class="grid"><thead><tr>
        <th class="num">Rev</th><th>Company</th><th>Stage</th><th>Site</th>
        <th>Location</th><th>Team</th><th class="num">★</th><th>Next action</th><th>Follow-up</th>
      </tr></thead><tbody>
      ${leads.map(l => `<tr data-id="${l.id}">
        <td class="num"><b>${l.review_count || 0}</b></td>
        <td>${esc(l.company_name)} ${l.score_total != null ? `<span class="chip score">${l.score_total}</span>` : ""}</td>
        <td><span class="stage-dot" style="display:inline-block;background:${STAGE_COLORS[l.stage]}"></span> ${esc(l.stage_label)}</td>
        <td>${statusChip(l.website_status)}</td>
        <td>${esc(l.geography || "")}</td>
        <td>${esc(l.employees || "")}</td>
        <td class="num">${l.rating || ""}</td>
        <td>${esc(l.next_action || "")}</td>
        <td>${esc(l.follow_up_date || "")}</td>
      </tr>`).join("")}
      </tbody></table>`;
    bindRows($("#t-wrap"));
  };

  let deb;
  $("#t-q").addEventListener("input", (e) => { tableState.q = e.target.value; clearTimeout(deb); deb = setTimeout(run, 250); });
  $("#t-stage").onchange = (e) => { tableState.stage = e.target.value; run(); };
  $("#t-status").onchange = (e) => { tableState.status = e.target.value; run(); };
  $("#t-order").onchange = (e) => { tableState.order = e.target.value; run(); };
  $("#t-all").onchange = (e) => { tableState.all = e.target.checked; run(); };
  run();
}

// ---------------------------------------------------------------------------
// DASHBOARD
// ---------------------------------------------------------------------------
async function renderDashboard() {
  const view = $("#view");
  view.innerHTML = `<div class="empty">Loading metrics…</div>`;
  const m = await api("/api/metrics");
  const f = m.funnel, r = m.rates;
  const maxF = Math.max(1, f.contacted, f.replied, f.call, f.proposal, f.won);
  const bar = (label, val) => `
    <div class="fbar"><div class="flabel">${label}</div>
      <div class="ftrack"><div class="ffill" style="width:${Math.max(2, val / maxF * 100)}%"></div></div>
      <div class="fval">${val}</div></div>`;

  const stat = (k, v, sub = "") => `<div class="stat"><div class="k">${k}</div><div class="v">${v}${sub ? ` <small>${sub}</small>` : ""}</div></div>`;

  view.innerHTML = `
    <div class="section-title">Pipeline</div>
    <div class="stat-row">
      ${stat("To research", m.counts.to_research)}
      ${stat("Drafted", m.counts.drafted)}
      ${stat("Finalized", m.counts.finalized)}
      ${stat("Contacted", m.counts.contacted)}
      ${stat("Replied", m.counts.replied)}
      ${stat("Proposal", m.counts.proposal)}
      ${stat("Won", m.counts.won)}
    </div>
    <div class="section-title">Commercial</div>
    <div class="stat-row">
      ${stat("Cash received", money(m.cash_received))}
      ${stat("Pipeline value", money(m.pipeline_value))}
      ${stat("Follow-ups due today", m.due_today)}
      ${stat("Do not contact", m.counts.do_not_contact)}
    </div>
    <div class="section-title">Funnel (SOP §21)</div>
    <div class="funnel">
      ${bar("Contacted", f.contacted)}
      ${bar("Replied", f.replied)}
      ${bar("Call", f.call)}
      ${bar("Proposal", f.proposal)}
      ${bar("Won", f.won)}
      <div class="stat-row" style="margin-top:16px">
        ${stat("Reply rate", r.reply_rate, "%")}
        ${stat("Call rate", r.call_rate, "%")}
        ${stat("Proposal rate", r.proposal_rate, "%")}
        ${stat("Win rate", r.win_rate, "%")}
      </div>
    </div>`;
}

// ---------------------------------------------------------------------------
// GMAIL BATCH
// ---------------------------------------------------------------------------
async function renderGmail() {
  const view = $("#view");
  view.innerHTML = `<div class="empty">Loading finalized batch…</div>`;
  const { batch } = await api("/api/gmail/batch");
  const accounts = META.gmail_accounts || [];

  const item = (l) => `
    <div class="gm-item">
      <div style="display:flex;align-items:center;gap:8px">
        <span class="subj">${esc(l.subject) || "<em>no subject</em>"}</span>
        ${reviewBadge(l.review_count)}
        ${l.has_links ? `<span class="chip flag">link found</span>` : `<span class="chip" style="background:var(--good-soft);color:var(--good)">link-free</span>`}
        ${l.gmail_draft_id ? `<span class="chip ghost">draft recorded</span>` : ""}
      </div>
      <div class="meta">${esc(l.company_name)} &rarr; <b>${esc(l.recipient_label)}</b>${l.gmail_account ? " · in " + esc(l.gmail_account) : ""}</div>
      <pre>${esc(l.message_draft) || "<em>no body</em>"}</pre>
    </div>`;

  view.innerHTML = `
    <div class="callout">
      <b>How drafts get made:</b> name the Gmail account below, then ask me in chat —
      e.g. <code>create Gmail drafts for the finalized batch in ${esc(accounts[0] || "your@gmail.com")}</code>.
      I create them through the in-chat Gmail connector (drafts only, never sent) and record each one here.
      You review &amp; send in Gmail, then hit <b>Confirm sent</b> on the lead to move it to Contacted.
    </div>
    <div class="gm-head">
      <div class="fld">
        <label>Approved Gmail accounts for this batch (comma-separated)</label>
        <input id="gm-accounts" style="min-width:360px" value="${esc(accounts.join(", "))}" placeholder="you@gmail.com, business@gmail.com">
      </div>
      <button class="btn" id="gm-save">Save accounts</button>
      <span class="sub" style="color:var(--muted);margin-left:auto">${batch.length} finalized lead(s) ready</span>
    </div>
    ${batch.length ? batch.map(item).join("") : `<div class="empty">No leads are Finalized yet. Finalize a lead (link-free) and it will appear here.</div>`}`;

  $("#gm-save").onclick = async () => {
    const accs = $("#gm-accounts").value.split(",").map(s => s.trim()).filter(Boolean);
    const r = await api("/api/settings/gmail_accounts", { method: "POST", body: JSON.stringify({ accounts: accs }) });
    META.gmail_accounts = r.gmail_accounts;
    toast("Accounts saved.", "good");
  };
}

// ---------------------------------------------------------------------------
// LEAD DRAWER
// ---------------------------------------------------------------------------
let CUR = null; // current lead

async function openLead(id) {
  CUR = await api(`/api/leads/${id}`);
  renderDrawer();
  $("#overlay").classList.add("show");
  $("#drawer").classList.add("show");
  $("#drawer").setAttribute("aria-hidden", "false");
}
function closeDrawer() {
  $("#overlay").classList.remove("show");
  $("#drawer").classList.remove("show");
  $("#drawer").setAttribute("aria-hidden", "true");
  CUR = null;
}

function f_input(field, label, val, opts = {}) {
  const t = opts.type || "text";
  return `<div class="field ${opts.wide ? "wide" : ""}">
    <label>${label}</label>
    <input data-field="${field}" type="${t}" value="${esc(val)}" ${opts.ph ? `placeholder="${esc(opts.ph)}"` : ""}>
    ${opts.hint ? `<span class="hint">${opts.hint}</span>` : ""}</div>`;
}
function f_area(field, label, val, hint) {
  return `<div class="field wide"><label>${label}</label>
    <textarea data-field="${field}">${esc(val)}</textarea>
    ${hint ? `<span class="hint">${hint}</span>` : ""}</div>`;
}
function f_select(field, label, val, options, wide) {
  return `<div class="field ${wide ? "wide" : ""}"><label>${label}</label>
    <select data-field="${field}">
      <option value="">—</option>
      ${options.map(o => `<option value="${o}" ${val === o ? "selected" : ""}>${o.replace(/_/g, " ")}</option>`).join("")}
    </select></div>`;
}

const SCORE_LABELS = {
  sc_agency_fit: "Agency fit", sc_size_maturity: "Size & maturity", sc_recent_activity: "Recent activity",
  sc_dev_gap: "Development gap", sc_work_relevance: "Work relevance", sc_access: "Access",
  sc_personalization: "Personalization evidence",
};
function scoreBand(total) {
  if (total == null) return ["", ""];
  if (total >= 11) return ["Contact priority", "var(--good-soft);color:var(--good)"];
  if (total >= 8) return ["Contact after review", "var(--accent-soft);color:var(--accent-ink)"];
  if (total >= 5) return ["Hold / low priority", "var(--warn-soft);color:var(--warn)"];
  return ["Exclude", "var(--bad-soft);color:var(--bad)"];
}

function renderDrawer() {
  const l = CUR;
  $("#d-name").textContent = l.company_name;
  const socials = [["in", l.social_linkedin], ["fb", l.social_facebook], ["ig", l.social_instagram],
    ["x", l.social_twitter], ["yt", l.social_youtube], ["be", l.social_behance], ["dr", l.social_dribbble]]
    .filter(([, u]) => u).map(([k, u]) => `<a href="${esc(u)}" target="_blank" rel="noopener">${k}</a>`).join(" · ");
  $("#d-sub").innerHTML = `
    <span class="stage-dot" style="background:${STAGE_COLORS[l.stage]}"></span>
    <b>${esc(l.stage_label)}</b> ${reviewBadge(l.review_count)} ${statusChip(l.website_status)}
    ${l.rating ? `★ ${l.rating}` : ""} ${l.website ? `· <a href="${esc(l.website)}" target="_blank" rel="noopener">site</a>` : ""}
    ${l.profile_url ? `· <a href="${esc(l.profile_url)}" target="_blank" rel="noopener">clutch</a>` : ""}
    ${socials ? "· " + socials : ""}`;

  const sc = (field) => `<div class="sc-item">
    <span class="sc-label">${SCORE_LABELS[field]}</span>
    <span class="sc-seg" data-score="${field}">
      ${[0, 1, 2].map(n => `<button data-v="${n}" class="${l[field] === n ? "on" : ""}">${n}</button>`).join("")}
    </span></div>`;

  const [bandText, bandStyle] = scoreBand(l.score_total);

  $("#d-body").innerHTML = `
    <fieldset class="group"><legend>Identity</legend>
      <div class="fgrid">
        ${f_input("company_name", "Company name", l.company_name)}
        ${f_input("website", "Website", l.website)}
        ${f_input("geography", "Location", l.geography)}
        ${f_input("employees", "Team size", l.employees)}
        ${f_input("founded", "Founded", l.founded)}
        ${f_input("campaign", "Campaign / batch", l.campaign, { ph: "2026-10 …" })}
      </div>
      ${l.services ? `<div class="field wide"><label>Services (scraped)</label><div class="hint">${esc(l.services)}</div></div>` : ""}
      ${l.clutch_summary ? `<div class="field wide"><label>Clutch summary (scraped)</label><div class="hint">${esc(l.clutch_summary)}</div></div>` : ""}
    </fieldset>

    <fieldset class="group"><legend>Research &amp; enrichment</legend>
      <div class="fgrid">
        ${f_select("dev_capability", "Development capability", l.dev_capability, META.dev_capability)}
        ${f_select("confidence", "Evidence confidence", l.confidence, META.confidence)}
        ${f_input("decision_maker", "Decision maker", l.decision_maker, { ph: "Name, role (if public)" })}
        ${f_input("contact_email", "Contact email", l.contact_email)}
        ${f_input("contact_route", "Contact route", l.contact_route, { ph: "email / form / partnerships page" })}
        <div class="field"><label>Contact is a form (blank To)</label>
          <select data-field="contact_is_form"><option value="0" ${!l.contact_is_form ? "selected" : ""}>No</option>
          <option value="1" ${l.contact_is_form ? "selected" : ""}>Yes — form only</option></select></div>
      </div>
      ${f_area("relevant_work", "Relevant work (one project + observation)", l.relevant_work)}
      ${f_area("tech_signals", "Technology signals (only what's shown)", l.tech_signals)}
      ${f_area("restrictions", "Restrictions", l.restrictions, "Region, no-contractors, no unsolicited, form-only…")}
      ${f_area("opportunity_hypothesis", "Opportunity hypothesis", l.opportunity_hypothesis, "A hypothesis — never claim a known need.")}
      ${f_area("pitch_angle", "Pitch angle (one primary offer)", l.pitch_angle)}
      ${f_area("evidence_notes", "Evidence notes (facts + source URLs + dates)", l.evidence_notes)}
    </fieldset>

    <fieldset class="group"><legend>Scorecard (§Appendix C)</legend>
      ${Object.keys(SCORE_LABELS).map(sc).join("")}
      <div class="sc-total">Total: <span id="sc-total">${l.score_total != null ? l.score_total : "—"}</span>/14
        <span class="sc-band" id="sc-band" style="background:${bandStyle}">${bandText}</span></div>
    </fieldset>

    <fieldset class="group"><legend>First-touch message</legend>
      ${f_input("subject", "Subject", l.subject, { wide: true, ph: "Plain, 4–8 words" })}
      ${f_area("message_draft", "Body", l.message_draft, "90–160 words, natural, one observation, one small paid ask, no links.")}
      <div id="linkcheck"></div>
    </fieldset>

    <fieldset class="group"><legend>Pipeline &amp; next action</legend>
      <div class="fgrid">
        ${f_input("next_action", "Next action", l.next_action)}
        ${f_input("follow_up_date", "Follow-up date", l.follow_up_date, { type: "date" })}
      </div>
    </fieldset>

    <fieldset class="group"><legend>Commercial</legend>
      <div class="fgrid">
        ${f_input("proposed_value", "Proposed value (USD)", l.proposed_value, { type: "number" })}
        ${f_input("payment_received", "Payment received (USD)", l.payment_received, { type: "number" })}
      </div>
      ${l.suppression_reason ? `<div class="field wide"><label>Suppression reason</label><div class="hint">${esc(l.suppression_reason)}</div></div>` : ""}
    </fieldset>

    <fieldset class="group"><legend>Activity</legend>
      <ul class="log">${(l.events || []).map(e => `<li>
        <span class="lk">${esc(e.type)}</span>
        <span style="flex:1">${esc(e.detail)}</span>
        <span class="lt">${esc(e.ts)}</span></li>`).join("") || "<li class='hint'>No activity yet.</li>"}</ul>
    </fieldset>`;

  // scorecard clicks
  $("#d-body").querySelectorAll(".sc-seg").forEach(seg => {
    seg.querySelectorAll("button").forEach(b => b.onclick = () => {
      const field = seg.dataset.score, v = Number(b.dataset.v);
      CUR[field] = (CUR[field] === v) ? null : v;
      seg.querySelectorAll("button").forEach(x => x.classList.toggle("on", Number(x.dataset.v) === CUR[field]));
      const total = ["sc_agency_fit", "sc_size_maturity", "sc_recent_activity", "sc_dev_gap", "sc_work_relevance", "sc_access", "sc_personalization"]
        .reduce((a, k) => CUR[k] == null ? a : a + CUR[k], null);
      $("#sc-total").textContent = total == null ? "—" : total;
      const [bt, bs] = scoreBand(total);
      $("#sc-band").textContent = bt; $("#sc-band").style.cssText = "background:" + bs;
    });
  });

  // live link check
  const lc = () => runLinkCheck($(`[data-field="subject"]`).value, $(`[data-field="message_draft"]`).value);
  $(`[data-field="message_draft"]`).addEventListener("input", debounce(lc, 200));
  $(`[data-field="subject"]`).addEventListener("input", debounce(lc, 200));
  lc();

  renderFooter();
}

function debounce(fn, ms) { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; }

let LINK_DIRTY = false;
async function runLinkCheck(subject, body) {
  const box = $("#linkcheck"); if (!box) return;
  const r = await api(`/api/leads/${CUR.id}/check_links`, { method: "POST", body: JSON.stringify({ subject, message_draft: body }) });
  LINK_DIRTY = !r.clean;
  box.className = "linkcheck " + (r.clean ? "clean" : "dirty");
  box.innerHTML = r.clean
    ? "✓ No links detected — safe to finalize."
    : "⚠ Links found (remove before finalizing):<ul>" + r.links.map(x => `<li><b>${esc(x.match)}</b> — ${esc(x.kind)}</li>`).join("") + "</ul>";
  const fb = $("#btn-finalize"); if (fb) fb.disabled = LINK_DIRTY;
}

function collectEdits() {
  const edits = {};
  $("#d-body").querySelectorAll("[data-field]").forEach(el => {
    let v = el.value;
    if (el.dataset.field === "contact_is_form") v = Number(v);
    edits[el.dataset.field] = v;
  });
  ["sc_agency_fit", "sc_size_maturity", "sc_recent_activity", "sc_dev_gap", "sc_work_relevance", "sc_access", "sc_personalization"]
    .forEach(k => edits[k] = CUR[k]);
  return edits;
}

async function saveLead(silent) {
  const edits = collectEdits();
  const r = await api(`/api/leads/${CUR.id}`, { method: "PATCH", body: JSON.stringify(edits) });
  CUR.score_total = r.score_total;
  Object.assign(CUR, edits);
  if (!silent) toast("Saved.", "good");
  return r;
}

function renderFooter() {
  const l = CUR;
  const stageSel = `<select id="stage-move" title="Move to stage">
    ${[...META.pipeline_stages, ...META.terminal_stages].map(s =>
      `<option value="${s}" ${s === l.stage ? "selected" : ""}>${META.stage_labels[s]}</option>`).join("")}</select>`;

  let actions = `<button class="btn primary" id="btn-save">Save</button>`;
  if (["to_research", "drafted"].includes(l.stage)) {
    actions += `<button class="btn" id="btn-finalize" ${LINK_DIRTY ? "disabled" : ""}>Finalize</button>`;
  }
  if (l.stage === "finalized") {
    actions += `<button class="btn primary" id="btn-contacted">Confirm sent → Contacted</button>`;
  }
  if (["contacted", "replied", "call", "proposal"].includes(l.stage)) {
    actions += `<button class="btn" id="btn-followup">Log follow-up sent</button>`;
    actions += `<button class="btn" id="btn-reply">Log reply</button>`;
  }
  actions += `<span style="margin-left:auto;display:flex;gap:8px;align-items:center">
    <span class="sub" style="color:var(--muted);font-size:12.5px">Move:</span>${stageSel}
    <button class="btn danger sm" id="btn-suppress">Do not contact</button></span>`;

  $("#d-footer").innerHTML = actions;
  $("#btn-save").onclick = () => saveLead();
  const fin = $("#btn-finalize"); if (fin) fin.onclick = finalizeLead;
  const con = $("#btn-contacted"); if (con) con.onclick = confirmSent;
  const fu = $("#btn-followup"); if (fu) fu.onclick = logFollowup;
  const rp = $("#btn-reply"); if (rp) rp.onclick = logReply;
  $("#btn-suppress").onclick = suppress;
  $("#stage-move").onchange = (e) => {
    const s = e.target.value;
    if (s === l.stage) return;
    if (s === "finalized") return finalizeLead();
    if (s === "contacted") return confirmSent();
    if (s === "do_not_contact") return suppress();
    moveStage(l.id, s, { note: "" });
  };
}

async function refreshDrawer() { CUR = await api(`/api/leads/${CUR.id}`); renderDrawer(); renderTopstats(); }

async function moveStage(id, stage, { note = "", confirm_sent, date, channel, suppression_reason, next_action, refresh } = {}) {
  try {
    await api(`/api/leads/${id}/stage`, {
      method: "POST",
      body: JSON.stringify({ stage, note, confirm_sent, date, channel, suppression_reason, next_action }),
    });
    toast(`Moved to ${META.stage_labels[stage]}.`, "good");
    if (refresh) refresh();
    else if (CUR && String(CUR.id) === String(id)) await refreshDrawer();
    renderTopstats();
  } catch (e) {
    if (e.data && e.data.links) {
      toast("Blocked: remove links first.", "bad");
    } else {
      toast(e.message || "Could not move stage.", "bad");
    }
    if (CUR) renderFooter();
  }
}

async function finalizeLead() {
  await saveLead(true);
  await moveStage(CUR.id, "finalized", { note: "Finalized for Gmail." });
}

async function confirmSent() {
  if (!confirm("Confirm this message was actually SENT from Gmail (or the form was submitted)?\n\nThis moves the lead to Contacted and schedules the first follow-up.")) return;
  const channel = prompt("Channel (Gmail account or 'contact form'):", CUR.gmail_account || "Gmail") || "Gmail";
  await moveStage(CUR.id, "contacted", { confirm_sent: true, channel, date: META.today });
}

async function suppress() {
  const reason = prompt("Suppression reason (opt-out, bounce, region, rejection, broken route, owner decision):", CUR.suppression_reason || "");
  if (!reason) return;
  await moveStage(CUR.id, "do_not_contact", { suppression_reason: reason });
}

async function logFollowup() {
  await api(`/api/leads/${CUR.id}/followup`, { method: "POST" });
  toast("Follow-up logged.", "good");
  await refreshDrawer();
}

async function logReply() {
  const note = prompt("Reply summary (what did they say?):", "");
  if (note === null) return;
  await saveLead(true);
  await api(`/api/leads/${CUR.id}`, { method: "PATCH", body: JSON.stringify({ reply_notes: note }) });
  await moveStage(CUR.id, "replied", { note: note });
}

boot().catch(e => { $("#view").innerHTML = `<div class="empty">Failed to load: ${esc(e.message)}</div>`; });
