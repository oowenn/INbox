import {
  clearResults,
  fetchNewestMessages,
  getClassificationFailures,
  getDashboardSummary,
  getMonthlyCounts,
  getMessages,
  getSankey,
  streamApplicationCycleRun,
  streamProcessBatch,
  wipeCache,
} from "./api.js";
import { renderSankeyChart } from "./sankey.js";

const state = {
  pullBusy: false,
  processBusy: false,
  cycleBusy: false,
  mutateBusy: false,
  summary: null,
  sankey: null,
  selectedBranchId: "",
  selectedBranchPairs: [],
  branchSearchTerm: "",
  showEmails: false,
  emailsLoading: false,
  classifiedMessages: [],
};

const $ = (id) => document.getElementById(id);

const STAGE_COLORS = {
  Received: "#577aa6",
  "Online Assessment": "#f2a53b",
  Interview: "#ea8da0",
  Offer: "#84c9cc",
  Pending: "#9aa5b3",
  Rejection: "#e36363",
  Unknown: "#b9b4d8",
};

function escapeHtml(text) {
  const div = document.createElement("div");
  div.textContent = String(text ?? "");
  return div.innerHTML;
}

function formatBranchLabel(edgeId) {
  const left = String(edgeId ?? "");
  return left
    .replace("Rejected after Received", "Rejected")
    .replace("Rejected after Online Assessment", "Rejected")
    .replace("Rejected after Interview", "Rejected")
    .replace("Rejected after Offer", "Rejected")
    .replace("Rejected after Unknown", "Rejected")
    .replace("Pending after Received", "Pending")
    .replace("Pending after Online Assessment", "Pending")
    .replace("Pending after Interview", "Pending")
    .replace("Pending after Offer", "Pending")
    .replace("Pending after Unknown", "Pending");
}

function stageColor(stage) {
  return STAGE_COLORS[stage] || "#8d9aad";
}

function formatMonthLabel(month) {
  const raw = String(month || "").trim();
  if (!raw) return "";
  const parts = raw.split("-");
  if (parts.length !== 2) return raw;
  return `${parts[0].slice(2)}-${parts[1]}`; // YY-MM
}

function parseMonthStart(month) {
  const parts = String(month || "").split("-");
  if (parts.length !== 2) return null;
  const year = Number(parts[0]);
  const monthIdx = Number(parts[1]) - 1;
  if (!Number.isFinite(year) || !Number.isFinite(monthIdx)) return null;
  return new Date(Date.UTC(year, monthIdx, 1));
}

function addMonths(date, delta) {
  return new Date(Date.UTC(date.getUTCFullYear(), date.getUTCMonth() + delta, 1));
}

function monthKey(date) {
  const year = date.getUTCFullYear();
  const month = String(date.getUTCMonth() + 1).padStart(2, "0");
  return `${year}-${month}`;
}

function formatMonthTick(dateLike) {
  const date = new Date(dateLike);
  if (Number.isNaN(date.getTime())) return "";
  return new Intl.DateTimeFormat("en-US", {
    month: "short",
    year: "numeric",
    timeZone: "UTC",
  }).format(date);
}

function expandMonthlySeries(rows) {
  const data = Array.isArray(rows) ? rows : [];
  if (!data.length) return null;

  const countsByMonth = new Map();
  for (const row of data) {
    const key = String(row.month || "").trim();
    if (!key) continue;
    countsByMonth.set(key, Number(row.count || 0));
  }
  const monthKeys = [...countsByMonth.keys()].sort();
  if (!monthKeys.length) return null;

  const firstReal = parseMonthStart(monthKeys[0]);
  const lastReal = parseMonthStart(monthKeys[monthKeys.length - 1]);
  if (!firstReal || !lastReal) return null;

  const start = addMonths(firstReal, -1);
  const end = addMonths(lastReal, 1);
  const months = [];
  const x = [];
  const y = [];

  for (let current = start; current <= end; current = addMonths(current, 1)) {
    const key = monthKey(current);
    months.push(key);
    x.push(current.toISOString());
    y.push(Number(countsByMonth.get(key) || 0));
  }

  return {
    months,
    x,
    y,
    firstRealMonth: monthKey(firstReal),
  };
}

async function renderTimelineChart(rows) {
  const chart = $("timeline_chart");
  const scroll = $("timeline_scroll");
  const status = $("timeline_status");
  if (!chart || !scroll || !status) return;
  if (!window.Plotly) {
    status.textContent = "Plotly is not available.";
    return;
  }

  const series = expandMonthlySeries(rows);
  if (!series) {
    status.textContent = "No dated emails found yet.";
    chart.innerHTML = "";
    if (window.Plotly) window.Plotly.purge(chart);
    return;
  }

  const monthWidth = 72;
  const paddedMonthCount = series.months.length;
  const minWidth = Math.max(scroll.clientWidth + monthWidth * 2, paddedMonthCount * monthWidth);
  const firstRealIndex = Math.max(0, series.months.indexOf(series.firstRealMonth));
  status.textContent = `Loaded ${paddedMonthCount - 2} months of data with one padded month on each side.`;

  const monthLabels = series.x.map(formatMonthTick);
  const trace = {
    type: "scatter",
    mode: "lines+markers",
    x: series.x,
    y: series.y,
    text: monthLabels,
    line: { color: "#577aa6", width: 3, shape: "linear" },
    marker: { color: "#577aa6", size: 7 },
    hovertemplate: "%{text}<br>%{y} emails<extra></extra>",
  };

  const layout = {
    width: minWidth,
    height: 260,
    margin: { l: 48, r: 18, t: 14, b: 42 },
    paper_bgcolor: "#ffffff",
    plot_bgcolor: "#ffffff",
    font: { size: 12, color: "#1f2430" },
    xaxis: {
      type: "date",
      tickmode: "array",
      tickvals: series.x,
      ticktext: monthLabels,
      showgrid: true,
      gridcolor: "#eef1f5",
      zeroline: false,
      fixedrange: true,
    },
    yaxis: {
      rangemode: "tozero",
      showgrid: true,
      gridcolor: "#eef1f5",
      zeroline: false,
      fixedrange: true,
      title: { text: "Emails" },
    },
    showlegend: false,
  };

  await window.Plotly.react(chart, [trace], layout, { displayModeBar: false, responsive: false });
  scroll.scrollLeft = Math.max(0, firstRealIndex * monthWidth);
}

async function toggleTimeline() {
  const card = $("timeline_card");
  const body = $("timeline_body");
  const btn = $("timeline_toggle_btn");
  if (!card || !body || !btn) return;

  const showing = !body.hasAttribute("hidden");
  if (showing) {
    body.setAttribute("hidden", "");
    btn.textContent = "Show";
    return;
  }

  body.removeAttribute("hidden");
  btn.textContent = "Hide";

  const status = $("timeline_status");
  if (status) status.textContent = "Loading...";
  try {
    const payload = await getMonthlyCounts({ limitMonths: 60 });
    renderTimelineChart(payload?.months || []);
  } catch (err) {
    if (status) status.textContent = err.message || String(err);
  }
}

function formatMessageDate(message) {
  const internalMs = Number(message?.internalDate || 0);
  if (Number.isFinite(internalMs) && internalMs > 0) {
    try {
      return new Date(internalMs).toLocaleString();
    } catch {
      // Fall back to header date below.
    }
  }
  const dateHeader = String(message?.date || "").trim();
  return dateHeader || "Unknown date";
}

function updateShowEmailsButton() {
  const btn = $("show_emails_btn");
  if (!btn) return;
  if (state.emailsLoading) {
    btn.textContent = "Loading...";
    btn.disabled = true;
    return;
  }
  btn.disabled = false;
  btn.textContent = state.showEmails ? "Hide Emails" : "Show Emails";
}

function formatClassificationBlock(title, result, { tiny = false } = {}) {
  if (!result) {
    return (
      `<div class="result-col">` +
      `<div class="result-col-title">${escapeHtml(title)}</div>` +
      `<div class="result-col-body muted">(none)</div>` +
      `</div>`
    );
  }
  const application = String(result.application || "no");
  const stage = String(result.stage || "Unknown");
  const company = String(result.company || "").trim() || "(Unknown Company)";
  const role = String(result.role || "").trim() || "(Unknown Role)";
  const interviewDate = String(result.interview_date || "").trim();
  return (
    `<div class="result-col">` +
    `<div class="result-col-title">${escapeHtml(title)}</div>` +
    `<div class="result-col-tags">` +
    `<span class="email-item-stage ${tiny ? "tiny" : ""}" style="background: ${stageColor(stage)};">${escapeHtml(
      `${application} · ${stage}`
    )}</span>` +
    `</div>` +
    `<div class="result-col-body">` +
    `company: ${escapeHtml(company)}<br/>` +
    `role: ${escapeHtml(role)}${interviewDate ? `<br/>interview_date: ${escapeHtml(interviewDate)}` : ""}` +
    `</div>` +
    `</div>`
  );
}

function renderEmailsList() {
  const list = $("emails_list");
  const meta = $("emails_meta");
  if (!list) return;

  const items = Array.isArray(state.classifiedMessages) ? state.classifiedMessages : [];
  if (meta) {
    const noun = items.length === 1 ? "email" : "emails";
    meta.textContent = `Showing ${items.length} processed ${noun} (newest first).`;
  }

  if (!items.length) {
    list.innerHTML = '<div class="table-empty">No processed emails found yet.</div>';
    return;
  }

  list.innerHTML = items
    .map((message, idx) => {
      const result = message.result || {};
      const extraction = message.extraction || null;
      const stage = String(result.stage || "Unknown");
      const stageTag = `${result.application || "no"} · ${stage}`;
      const subject = String(message.subject || "").trim() || "(No subject)";
      const sender = String(message.sender || "").trim() || "(Unknown sender)";
      const dateText = formatMessageDate(message);
      const body = String(message.body || "").trim();
      const snippet = String(message.snippet || "").trim();
      const fullText = body || snippet;
      const hasFinal = Boolean(message.result);
      const topTag = hasFinal ? stageTag : "no final result";

      return (
        `<article class="email-item">` +
        `<div class="email-item-head">` +
        `<div class="email-item-subject">#${idx + 1} ${escapeHtml(subject)}</div>` +
        `<span class="email-item-stage" style="background: ${hasFinal ? stageColor(stage) : stageColor("Unknown")};">${escapeHtml(
          topTag
        )}</span>` +
        `</div>` +
        `<div class="email-item-meta">${escapeHtml(dateText)} · ${escapeHtml(sender)}</div>` +
        `<div class="email-compare">` +
        formatClassificationBlock("Extraction (raw)", extraction, { tiny: true }) +
        formatClassificationBlock("Final (stored)", message.result, { tiny: true }) +
        `</div>` +
        (fullText
          ? `<details class="email-preview-wrap"><summary>Message body</summary><pre class="email-preview">${escapeHtml(fullText)}</pre></details>`
          : "") +
        `</article>`
      );
    })
    .join("");
}

async function loadClassifiedEmails({ silent = false } = {}) {
  if (state.emailsLoading) return;
  state.emailsLoading = true;
  updateShowEmailsButton();
  const list = $("emails_list");
  const meta = $("emails_meta");
  if (list && !silent) {
    list.innerHTML = '<div class="table-empty">Loading classified emails...</div>';
  }
  if (meta && !silent) {
    meta.textContent = "Loading...";
  }

  try {
    const payload = await getMessages({ limit: 500 });
    const messages = Array.isArray(payload?.messages) ? payload.messages : [];
    state.classifiedMessages = messages.filter(
      (message) => message && (message.result || message.extraction)
    );
    renderEmailsList();
    if (!silent) {
      const noun = state.classifiedMessages.length === 1 ? "email" : "emails";
      setStatus(`Loaded ${state.classifiedMessages.length} processed ${noun}.`);
    }
  } catch (err) {
    const errorText = err?.message || String(err);
    if (list) list.innerHTML = `<div class="table-empty">${escapeHtml(errorText)}</div>`;
    if (meta) meta.textContent = "Failed to load.";
    if (!silent) {
      setStatus(errorText, { error: true });
    }
  } finally {
    state.emailsLoading = false;
    updateShowEmailsButton();
  }
}

async function handleShowEmailsToggle() {
  hideAllConfirmRows();
  const panel = $("emails_panel");
  if (!panel) return;

  if (state.showEmails) {
    state.showEmails = false;
    panel.hidden = true;
    updateShowEmailsButton();
    return;
  }

  state.showEmails = true;
  panel.hidden = false;
  updateShowEmailsButton();
  await loadClassifiedEmails({ silent: false });
}

function sankeyDerivedStageCounts() {
  if (!state.sankey || !Array.isArray(state.sankey.links) || !Array.isArray(state.sankey.nodes)) {
    return null;
  }

  const labels = state.sankey.nodes.map((node) => String(node.label || ""));
  const counts = {
    Received: 0,
    Rejection: 0,
    Pending: 0,
    "Online Assessment": 0,
    Interview: 0,
    Offer: 0,
  };

  for (const link of state.sankey.links) {
    const source = labels[Number(link.source)] || "";
    const target = labels[Number(link.target)] || "";
    const value = Number(link.value || 0);
    if (!Number.isFinite(value) || value <= 0) continue;

    if (source === "Received") counts.Received += value;
    if (target === "Online Assessment") counts["Online Assessment"] += value;
    if (target === "Interview") counts.Interview += value;
    if (target === "Offer") counts.Offer += value;
    if (target.startsWith("Rejected")) counts.Rejection += value;
    if (target.startsWith("Pending")) counts.Pending += value;
  }

  const ordered = ["Received", "Rejection", "Pending", "Online Assessment", "Interview", "Offer"];
  return ordered
    .map((stage) => ({ stage, count: counts[stage] || 0 }))
    .filter((item) => item.count > 0);
}

function clampInt(value, fallback, min, max) {
  const parsed = Number.parseInt(value, 10);
  if (!Number.isFinite(parsed)) return fallback;
  return Math.max(min, Math.min(max, parsed));
}

function setStatus(text, { error = false, spinner = false } = {}) {
  const row = $("status_row");
  const textEl = $("status_text");
  const spin = $("status_spinner");
  if (textEl) textEl.textContent = text || "";
  if (row) row.className = error ? "status-row error" : "status-row";
  if (spin) spin.hidden = !spinner;
}

function setSankeyStatus(text, isError = false) {
  const el = $("sankey_status");
  if (!el) return;
  el.textContent = text || "";
  el.className = isError ? "subtle-text error" : "subtle-text";
}

function setControlsBusy() {
  const busy = state.pullBusy || state.processBusy || state.cycleBusy || state.mutateBusy;
  const ids = [
    "pull_btn",
    "process_btn",
    "cycle_run_btn",
    "clear_results_btn",
    "wipe_cache_btn",
    "refresh_btn",
    "clear_results_confirm_btn",
    "clear_results_cancel_btn",
    "wipe_cache_confirm_btn",
    "wipe_cache_cancel_btn",
  ];
  for (const id of ids) {
    const el = $(id);
    if (el) el.disabled = busy;
  }

  const inputIds = [
    "query_input",
    "pull_count_input",
    "process_count_input",
    "process_concurrency_input",
    "cycle_fetch_limit_input",
    "cycle_batch_size_input",
  ];
  for (const id of inputIds) {
    const el = $(id);
    if (el) el.disabled = busy;
  }
}

function setConfirmVisible(id, visible) {
  const el = $(id);
  if (el) el.hidden = !visible;
}

function hideAllConfirmRows() {
  setConfirmVisible("clear_confirm_row", false);
  setConfirmVisible("wipe_confirm_row", false);
}

function renderSummary(summary) {
  state.summary = summary;
  $("metric_cached").textContent = String(summary.cached_total ?? 0);
  $("metric_classified").textContent = String(summary.classified_total ?? 0);
  $("metric_unprocessed").textContent = String(summary.unprocessed_total ?? 0);
  $("metric_yes").textContent = String(summary.application_yes_total ?? 0);
  $("metric_no").textContent = String(summary.application_no_total ?? 0);

  const stageCounts =
    sankeyDerivedStageCounts() ||
    (Array.isArray(summary.stage_counts) ? summary.stage_counts : []);
  renderStageBreakdownChart(stageCounts);
}

function renderStageBreakdownChart(stageCounts) {
  const chart = $("stage_chart");
  if (!chart) return;

  if (!Array.isArray(stageCounts) || !stageCounts.length) {
    chart.innerHTML = '<div class="table-empty">No stage data yet.</div>';
    return;
  }

  const sorted = [...stageCounts].sort((a, b) => {
    const countDiff = Number(b.count || 0) - Number(a.count || 0);
    if (countDiff !== 0) return countDiff;
    return String(a.stage || "").localeCompare(String(b.stage || ""));
  });
  const maxCount = Math.max(...sorted.map((item) => Number(item.count || 0)), 1);

  chart.innerHTML = sorted
    .map((item) => {
      const stage = String(item.stage || "Unknown");
      const count = Number(item.count || 0);
      const pct = count > 0 ? Math.max(8, (count / maxCount) * 100) : 0;
      const color = stageColor(stage);
      return (
        `<div class="stage-row">` +
        `<div class="stage-row-label">${escapeHtml(stage)}</div>` +
        `<div class="stage-bar-track">` +
        `<div class="stage-bar-fill" style="width: ${pct}%; background: ${color};"></div>` +
        `</div>` +
        `<div class="stage-bar-value">${count}</div>` +
        `</div>`
      );
    })
    .join("");
}

function renderBranchPairTable() {
  const tbody = $("branch_pairs_body");
  const countEl = $("branch_table_count");
  if (!tbody) return;

  if (!state.selectedBranchId) {
    tbody.innerHTML = '<tr><td colspan="2" class="table-empty">Select a branch to inspect companies.</td></tr>';
    if (countEl) countEl.textContent = "";
    return;
  }

  const totalCount = state.selectedBranchPairs.length;
  const query = state.branchSearchTerm.trim().toLowerCase();
  const filtered = query
    ? state.selectedBranchPairs.filter((pair) => {
        const haystack = `${pair.company || ""} ${pair.role || ""}`.toLowerCase();
        return haystack.includes(query);
      })
    : state.selectedBranchPairs;

  if (countEl) {
    const shownCount = filtered.length;
    countEl.textContent =
      totalCount === shownCount ? `${shownCount} rows` : `${shownCount} of ${totalCount} rows`;
  }

  if (!filtered.length) {
    const message = query
      ? "No companies match this search."
      : "No companies mapped to this selection.";
    tbody.innerHTML = `<tr><td colspan="2" class="table-empty">${message}</td></tr>`;
    return;
  }

  tbody.innerHTML = filtered
    .map(
      (pair) =>
        `<tr><td>${escapeHtml(pair.company || "(Unknown Company)")}</td><td>${escapeHtml(pair.role || "(Unknown Role)")}</td></tr>`
    )
    .join("");
}

function renderBranchPairs(edgeId) {
  const title = $("branch_title");
  const searchInput = $("branch_search_input");
  if (!title) return;

  if (!edgeId || !state.sankey) {
    state.selectedBranchId = "";
    state.selectedBranchPairs = [];
    state.branchSearchTerm = "";
    if (searchInput) searchInput.value = "";
    title.textContent = "Click any branch in the Sankey chart.";
    renderBranchPairTable();
    return;
  }

  const pairs = state.sankey.branch_pairs?.[edgeId] || [];
  state.selectedBranchId = edgeId;
  state.selectedBranchPairs = pairs;
  state.branchSearchTerm = "";
  if (searchInput) searchInput.value = "";
  const noun = pairs.length === 1 ? "company" : "companies";
  title.textContent = `${formatBranchLabel(edgeId)} (${pairs.length} ${noun})`;
  renderBranchPairTable();
}

function renderBranchPairsForNode(nodeLabel) {
  const title = $("branch_title");
  const searchInput = $("branch_search_input");
  if (!title || !nodeLabel || !state.sankey) return;

  const nodes = Array.isArray(state.sankey.nodes) ? state.sankey.nodes : [];
  const links = Array.isArray(state.sankey.links) ? state.sankey.links : [];
  const labels = nodes.map((node) => String(node.label || ""));

  const connectedEdgeIds = [];
  const merged = new Map();
  for (const link of links) {
    const sourceLabel = labels[Number(link.source)];
    const targetLabel = labels[Number(link.target)];
    if (sourceLabel !== nodeLabel && targetLabel !== nodeLabel) continue;

    const edgeId = String(link.id || "");
    if (!edgeId) continue;
    connectedEdgeIds.push(edgeId);

    const pairs = state.sankey.branch_pairs?.[edgeId] || [];
    for (const pair of pairs) {
      const company = String(pair.company || "(Unknown Company)");
      const role = String(pair.role || "(Unknown Role)");
      const key = `${company}\u0000${role}`;
      if (!merged.has(key)) merged.set(key, { company, role });
    }
  }

  const mergedPairs = [...merged.values()];
  state.selectedBranchId = `node:${nodeLabel}`;
  state.selectedBranchPairs = mergedPairs;
  state.branchSearchTerm = "";
  if (searchInput) searchInput.value = "";

  const noun = mergedPairs.length === 1 ? "company" : "companies";
  const branchWord = connectedEdgeIds.length === 1 ? "branch" : "branches";
  const label = formatBranchLabel(nodeLabel);
  title.textContent = `${label} (${mergedPairs.length} ${noun} across ${connectedEdgeIds.length} ${branchWord})`;
  renderBranchPairTable();
}

async function renderSankey(payload) {
  state.sankey = payload;
  const chart = $("sankey_chart");
  if (!chart) return;

  if (!payload?.links?.length) {
    if (window.Plotly) window.Plotly.purge(chart);
    chart.innerHTML = "";
    renderBranchPairs("");
    setSankeyStatus("No displayable paths yet (need at least one non-Unknown stage; Interview requires an interview_date).");
    return;
  }

  await renderSankeyChart({
    element: chart,
    payload,
    onSelection: (selection) => {
      if (selection?.type === "edge") {
        renderBranchPairs(selection.edgeId);
      } else if (selection?.type === "node") {
        renderBranchPairsForNode(selection.nodeLabel);
      }
    },
  });
  renderBranchPairs("");
  setSankeyStatus(
    `Loaded ${payload.links.length} branches across ${payload.total_pairs} companies (Interview counted only when interview_date exists).`
  );
}

async function refreshDashboard({ keepStatus = false } = {}) {
  if (!keepStatus) {
    setStatus("Refreshing dashboard...", { spinner: true });
  }
  setSankeyStatus("Loading sankey...");
  try {
    const [summary, sankey] = await Promise.all([getDashboardSummary(), getSankey()]);
    // Update sankey state first so stage chart derivation can include Pending immediately.
    state.sankey = sankey;
    renderSummary(summary);
    await renderSankey(sankey);
    if (state.showEmails) {
      await loadClassifiedEmails({ silent: true });
    }
    if (!keepStatus) {
      setStatus("Dashboard refreshed.");
    }
  } catch (err) {
    setStatus(err.message || String(err), { error: true });
    setSankeyStatus(err.message || String(err), true);
  }
}

async function handlePull() {
  hideAllConfirmRows();
  const count = clampInt($("pull_count_input").value, 100, 1, 500);
  $("pull_count_input").value = String(count);
  const query = "in:inbox";

  state.pullBusy = true;
  setControlsBusy();
  setStatus(`Pulling ${count} newest emails...`, { spinner: true });
  try {
    const payload = await fetchNewestMessages({ count, query });
    await refreshDashboard({ keepStatus: true });
    setStatus(
      `Pull complete: ${payload.fetched_new} new, ${payload.duplicates} duplicates skipped. Total cached: ${payload.total_cached}.`
    );
  } catch (err) {
    setStatus(err.message || String(err), { error: true });
  } finally {
    state.pullBusy = false;
    setControlsBusy();
  }
}

async function handleProcess() {
  hideAllConfirmRows();
  const count = clampInt($("process_count_input").value, 100, 1, 500);
  $("process_count_input").value = String(count);

  state.processBusy = true;
  setControlsBusy();
  setStatus("Starting processing batch...", { spinner: true });

  let donePayload = null;
  try {
    donePayload = await streamProcessBatch({
      count,
      onEvent: (event) => {
        if (event.type === "start") {
          const attempted = event.attempted ?? 0;
          if (!attempted) {
            setStatus("No unprocessed cached emails found.");
            return;
          }
          setStatus(`Processing: 0/${attempted}`, { spinner: true });
        } else if (event.type === "progress") {
          const base = `Processing: ${event.finished ?? 0}/${event.total ?? 0}`;
          if (event.ok === false && event.error) {
            const snippet = String(event.error.message || "").slice(0, 120);
            setStatus(`${base} — last error: ${event.error.type || "?"} ${snippet}`, { spinner: true });
          } else {
            setStatus(base, { spinner: true });
          }
        } else if (event.type === "done") {
          donePayload = event;
        }
      },
    });

    await refreshDashboard({ keepStatus: true });

    if (!donePayload || !donePayload.attempted) {
      setStatus("No unprocessed cached emails found.");
      return;
    }

    const failNote = donePayload.failed
      ? ` ${donePayload.failed} failed (${donePayload.failed_ids.slice(0, 3).join(", ")}${donePayload.failed > 3 ? ", ..." : ""}). Errors are stored in the DB and listed under Classification failures.`
      : "";
    setStatus(
      `Processing complete: ${donePayload.processed}/${donePayload.attempted} with concurrency ${donePayload.concurrency_used}.${failNote}`
    );
  } catch (err) {
    setStatus(err.message || String(err), { error: true });
  } finally {
    state.processBusy = false;
    setControlsBusy();
  }
}

async function handleCycleRun() {
  hideAllConfirmRows();
  const fetchInput = $("cycle_fetch_limit_input");
  const batchInput = $("cycle_batch_size_input");
  const fetchLimit = clampInt(fetchInput?.value, 12000, 100, 100000);
  const classifyBatchSize = clampInt(batchInput?.value, 250, 10, 500);
  if (fetchInput) fetchInput.value = String(fetchLimit);
  if (batchInput) batchInput.value = String(classifyBatchSize);

  state.cycleBusy = true;
  setControlsBusy();
  setStatus("Starting application-cycle run...", { spinner: true });

  let donePayload = null;
  try {
    donePayload = await streamApplicationCycleRun({
      fetchLimit,
      classifyBatchSize,
      query: "in:inbox",
      onEvent: (event) => {
        if (event.type === "run_start") {
          setStatus(
            `Cycle run #${event.run_id}: scanning since ${event.cycle_start_date} (limit ${event.fetch_limit})...`,
            { spinner: true }
          );
          return;
        }
        if (event.type === "fetch_start") {
          setStatus(
            `Cycle run #${event.run_id}: ${event.new_candidates} new candidates (${event.duplicates} already cached).`,
            { spinner: true }
          );
          return;
        }
        if (event.type === "fetch_progress") {
          setStatus(
            `Cycle run #${event.run_id}: fetched ${event.fetched_new}/${event.total} new (${event.failed} fetch failures).`,
            { spinner: true }
          );
          return;
        }
        if (event.type === "fetch_done") {
          setStatus(
            `Cycle run #${event.run_id}: fetch complete (${event.fetched_new} new, ${event.duplicates} duplicates). Classifying...`,
            { spinner: true }
          );
          return;
        }
        if (event.type === "classify_batch_start") {
          const attempted = Number(event.attempted || 0);
          if (!attempted) {
            setStatus(`Cycle run #${event.run_id}: no more unprocessed cycle emails.`);
            return;
          }
          setStatus(
            `Cycle run #${event.run_id}: classifying batch ${event.batch_index} (${attempted} emails)...`,
            { spinner: true }
          );
          return;
        }
        if (event.type === "classify_progress") {
          const base =
            `Cycle run #${event.run_id}: batch ${event.batch_index} ` +
            `${event.finished}/${event.total} ` +
            `(yes ${event.classify_yes_total}, no ${event.classify_no_total}, missing company ${event.classify_yes_missing_company_total})`;
          if (event.ok === false && event.error) {
            const snippet = String(event.error.message || "").slice(0, 120);
            setStatus(`${base} — last error: ${event.error.type || "?"} ${snippet}`, { spinner: true });
          } else {
            setStatus(base, { spinner: true });
          }
          return;
        }
        if (event.type === "classify_batch_done") {
          setStatus(
            `Cycle run #${event.run_id}: batch ${event.batch_index} done. Totals processed ${event.classify_processed_total}/${event.classify_attempted_total}.`,
            { spinner: true }
          );
          return;
        }
        if (event.type === "done") {
          donePayload = event;
        }
      },
    });

    await refreshDashboard({ keepStatus: true });

    if (!donePayload) {
      setStatus("Cycle run ended without a completion payload.", { error: true });
      return;
    }

    const failNote = donePayload.classify_failed
      ? ` ${donePayload.classify_failed} classification failures recorded (${donePayload.failed_ids.slice(0, 3).join(", ")}${donePayload.classify_failed > 3 ? ", ..." : ""}).`
      : "";
    const fetchFailNote = donePayload.fetch_failed
      ? ` ${donePayload.fetch_failed} fetch failures were logged to classification failures.`
      : "";
    setStatus(
      `Cycle run #${donePayload.run_id} ${donePayload.status}: ${donePayload.classify_processed}/${donePayload.classify_attempted} classified, yes=${donePayload.classify_yes}, no=${donePayload.classify_no}, missing-company=${donePayload.classify_yes_missing_company}.${fetchFailNote}${failNote}`
    );
  } catch (err) {
    setStatus(err.message || String(err), { error: true });
  } finally {
    state.cycleBusy = false;
    setControlsBusy();
  }
}

function handleClearResultsClick() {
  if (state.pullBusy || state.processBusy || state.cycleBusy || state.mutateBusy) return;
  setConfirmVisible("wipe_confirm_row", false);
  setConfirmVisible("clear_confirm_row", true);
  setStatus('Press "Confirm" to clear stored classifications.');
}

function handleWipeCacheClick() {
  if (state.pullBusy || state.processBusy || state.cycleBusy || state.mutateBusy) return;
  setConfirmVisible("clear_confirm_row", false);
  setConfirmVisible("wipe_confirm_row", true);
  setStatus('Press "Confirm" to wipe cached emails for this user.');
}

async function confirmClearResults() {
  state.mutateBusy = true;
  setControlsBusy();
  setStatus("Clearing stored classifications...", { spinner: true });
  try {
    const payload = await clearResults();
    hideAllConfirmRows();
    await refreshDashboard({ keepStatus: true });
    const noun = payload.deleted === 1 ? "result" : "results";
    setStatus(`Cleared ${payload.deleted} classification ${noun}.`);
  } catch (err) {
    setStatus(err.message || String(err), { error: true });
  } finally {
    state.mutateBusy = false;
    setControlsBusy();
  }
}

async function confirmWipeCache() {
  state.mutateBusy = true;
  setControlsBusy();
  setStatus("Wiping cached emails for this user...", { spinner: true });
  try {
    const payload = await wipeCache();
    hideAllConfirmRows();
    await refreshDashboard({ keepStatus: true });
    setStatus(
      `Wipe complete: ${payload.deleted_user_links} user links, ${payload.deleted_orphan_messages} orphan messages, ${payload.deleted_orphan_results} orphan results deleted.`
    );
  } catch (err) {
    setStatus(err.message || String(err), { error: true });
  } finally {
    state.mutateBusy = false;
    setControlsBusy();
  }
}

async function handleLoadFailures() {
  const btn = $("load_failures_btn");
  const tbody = $("failures_tbody");
  if (!tbody) return;
  if (btn) {
    btn.disabled = true;
    btn.textContent = "Loading…";
  }
  try {
    const payload = await getClassificationFailures({ limit: 300 });
    const rows = Array.isArray(payload.failures) ? payload.failures : [];
    if (!rows.length) {
      tbody.innerHTML = '<tr><td colspan="4" class="table-empty">No recorded failures.</td></tr>';
      return;
    }
    tbody.innerHTML = rows
      .map((row) => {
        const when = escapeHtml(row.created_at || "");
        const gid = escapeHtml(row.gmail_id || "");
        const sub = escapeHtml(row.subject || "(no subject)");
        const typ = escapeHtml(row.error_type || "");
        const msg = escapeHtml(row.error_message || "");
        return (
          `<tr>` +
          `<td class="failures-when">${when}</td>` +
          `<td class="failures-msg"><div class="failures-gid">${gid}</div><div class="failures-sub">${sub}</div></td>` +
          `<td class="failures-type">${typ}</td>` +
          `<td class="failures-detail"><pre class="failures-pre">${msg}</pre></td>` +
          `</tr>`
        );
      })
      .join("");
  } catch (err) {
    tbody.innerHTML = `<tr><td colspan="4" class="table-empty">${escapeHtml(err.message || String(err))}</td></tr>`;
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = "Load recent";
    }
  }
}

function wireEvents() {
  $("pull_btn").addEventListener("click", handlePull);
  $("process_btn").addEventListener("click", handleProcess);
  $("cycle_run_btn").addEventListener("click", handleCycleRun);
  $("show_emails_btn").addEventListener("click", handleShowEmailsToggle);
  $("refresh_btn").addEventListener("click", () => refreshDashboard({ keepStatus: false }));

  $("clear_results_btn").addEventListener("click", handleClearResultsClick);
  $("clear_results_confirm_btn").addEventListener("click", confirmClearResults);
  $("clear_results_cancel_btn").addEventListener("click", () => {
    setConfirmVisible("clear_confirm_row", false);
    setStatus("");
  });

  $("wipe_cache_btn").addEventListener("click", handleWipeCacheClick);
  $("wipe_cache_confirm_btn").addEventListener("click", confirmWipeCache);
  $("wipe_cache_cancel_btn").addEventListener("click", () => {
    setConfirmVisible("wipe_confirm_row", false);
    setStatus("");
  });

  $("branch_search_input").addEventListener("input", (event) => {
    state.branchSearchTerm = String(event.target?.value || "");
    renderBranchPairTable();
  });

  const failuresBtn = $("load_failures_btn");
  if (failuresBtn) {
    failuresBtn.addEventListener("click", () => void handleLoadFailures());
  }

  const timelineBtn = $("timeline_toggle_btn");
  if (timelineBtn) {
    timelineBtn.addEventListener("click", () => void toggleTimeline());
  }
}

async function init() {
  wireEvents();
  updateShowEmailsButton();
  setControlsBusy();
  await refreshDashboard();
  setControlsBusy();
}

void init();
