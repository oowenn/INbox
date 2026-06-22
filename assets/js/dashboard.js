import {
  correctMessageResult,
  getCycleEstimate,
  getDashboardSummary,
  getMessages,
  getMonthlyCounts,
  getSankey,
  invalidateCycleGmailCount,
  streamCycleAnalyze,
  streamCycleCount,
  streamCycleLoad,
} from "./api.js";
import { renderSankeyChart } from "./sankey.js";

const MIN_CYCLE_YEAR = 2000;
const STAGES = ["Received", "Online Assessment", "Interview", "Rejection", "Offer", "Unknown"];
const MESSAGES_PAGE_SIZE = 100;

function currentCycleStartYear() {
  const now = new Date();
  return now.getMonth() + 1 >= 6 ? now.getFullYear() : now.getFullYear() - 1;
}

const state = {
  cycleBusy: false,
  cycleScanBusy: false,
  summary: null,
  sankey: null,
  cycleReadyToAnalyze: 0,
  cycleLoadedYear: null,
  cycleEstimatedCostUsd: 0,
  cycleEstimateYear: null,
  cycleNeedsExactScan: true,
  cycleExactCount: null,
  cycleCachedTotal: 0,
  cycleNotAnalyzedTotal: 0,
  selectedCycleStartYear: currentCycleStartYear(),
  selectedBranchId: "",
  selectedBranchPairs: [],
  branchSearchTerm: "",
  messages: [],
  messagesLimit: MESSAGES_PAGE_SIZE,
  messagesFilter: "all",
  messagesSearchTerm: "",
  editingGmailId: "",
  messagesSaving: false,
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

function cycleYearLabel(year) {
  if (!Number.isFinite(year)) return "";
  const endYear = year + 1;
  return `${year}\u2013${endYear}`;
}

function isAllCyclesSelected() {
  return state.selectedCycleStartYear == null;
}

function selectedCycleStartYear() {
  return isAllCyclesSelected() ? undefined : state.selectedCycleStartYear;
}

function ensureCycleYearSelectOptions(select) {
  const current = currentCycleStartYear();
  if (select.dataset.cycleMaxYear === String(current) && select.options.length > 0) {
    return;
  }
  select.dataset.cycleMaxYear = String(current);
  select.innerHTML = "";
  const optAll = document.createElement("option");
  optAll.value = "";
  optAll.textContent = "All cycles";
  select.appendChild(optAll);
  for (let y = current; y >= MIN_CYCLE_YEAR; y -= 1) {
    const opt = document.createElement("option");
    opt.value = String(y);
    opt.textContent = cycleYearLabel(y);
    select.appendChild(opt);
  }
}

function syncCycleYearSelectValue() {
  const select = $("cycle_year_select");
  if (!select) return;
  const v = state.selectedCycleStartYear == null ? "" : String(state.selectedCycleStartYear);
  if (select.value !== v) {
    select.value = v;
  }
}

function cyclePickerSubtitleText() {
  const selected = state.selectedCycleStartYear;
  if (selected == null) return "";
  const current = currentCycleStartYear();
  if (selected === current) return "(current cycle)";
  if (selected === MIN_CYCLE_YEAR) return "(earliest cycle)";
  return "";
}

function syncCyclePickerSubtitle() {
  const el = $("cycle_picker_subtitle");
  if (!el) return;
  const text = cyclePickerSubtitleText();
  el.textContent = text;
  el.hidden = text === "";
}

function refreshCyclePicker() {
  const select = $("cycle_year_select");
  const prevBtn = $("cycle_prev_btn");
  const nextBtn = $("cycle_next_btn");
  const current = currentCycleStartYear();
  const selected = state.selectedCycleStartYear;
  const navBusy = state.cycleBusy || state.cycleScanBusy;

  if (select) {
    ensureCycleYearSelectOptions(select);
    syncCycleYearSelectValue();
    select.disabled = navBusy;
  }
  syncCyclePickerSubtitle();

  if (prevBtn) {
    prevBtn.disabled =
      navBusy || selected == null || (typeof selected === "number" && selected <= MIN_CYCLE_YEAR);
  }
  if (nextBtn) {
    nextBtn.disabled =
      navBusy || (typeof selected === "number" && selected >= current);
  }
}

function setCycleResultSummary(text = "") {
  const el = $("cycle_result_summary");
  if (el) el.textContent = text;
}

async function selectCycleStartYear(nextYearOrNull) {
  const current = currentCycleStartYear();
  const normalized =
    nextYearOrNull == null
      ? null
      : Math.max(MIN_CYCLE_YEAR, Math.min(current, Number(nextYearOrNull) || current));
  if (normalized === state.selectedCycleStartYear) {
    syncCycleYearSelectValue();
    return;
  }
  state.selectedCycleStartYear = normalized;
  state.cycleReadyToAnalyze = 0;
  state.cycleLoadedYear = null;
  state.cycleEstimatedCostUsd = 0;
  state.cycleEstimateYear = null;
  state.cycleNeedsExactScan = true;
  state.cycleExactCount = null;
  state.cycleCachedTotal = 0;
  state.cycleNotAnalyzedTotal = 0;
  setCycleResultSummary("");
  refreshCyclePicker();
  updateAnalyzeButtonState();
  await refreshDashboard({ keepStatus: false });
  const timelineBody = $("timeline_body");
  if (timelineBody && !timelineBody.hasAttribute("hidden")) {
    const status = $("timeline_status");
    if (status) status.textContent = "Loading...";
    try {
      const payload = await getMonthlyCounts({
        limitMonths: 60,
        cycleStartYear: selectedCycleStartYear(),
      });
      await renderTimelineChart(payload?.months || []);
    } catch (err) {
      if (status) status.textContent = err.message || String(err);
    }
  }
  await refreshCycleEstimate();
}

function formatCycleStartDate(isoDate) {
  const raw = String(isoDate || "").trim();
  const parts = raw.split("-");
  if (parts.length !== 3) return raw;
  const y = Number(parts[0]);
  const m = Number(parts[1]);
  const d = Number(parts[2]);
  if (!Number.isFinite(y) || !Number.isFinite(m) || !Number.isFinite(d)) return raw;
  return new Intl.DateTimeFormat("en-US", {
    month: "short",
    day: "numeric",
    year: "numeric",
    timeZone: "UTC",
  }).format(new Date(Date.UTC(y, m - 1, d)));
}

function formatUsd(amount) {
  const value = Number(amount || 0);
  if (!Number.isFinite(value) || value <= 0) return "$0.00";
  if (value < 0.01) return "<$0.01";
  return value.toLocaleString(undefined, {
    style: "currency",
    currency: "USD",
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
}

function updateAnalyzeButtonState() {
  const analyzeBtn = $("cycle_analyze_btn");
  const estimateBtn = $("cycle_estimate_btn");
  const selectedYear = selectedCycleStartYear();
  const hasCycle = selectedYear != null;
  const busy = state.cycleBusy || state.cycleScanBusy;
  const currentCycle = currentCycleStartYear();
  const isCurrent = hasCycle && selectedYear === currentCycle;
  const estimateIsFresh =
    hasCycle &&
    state.cycleEstimateYear === selectedYear &&
    !state.cycleNeedsExactScan;
  const noPendingWork = estimateIsFresh && Number(state.cycleNotAnalyzedTotal || 0) <= 0;

  if (estimateBtn) {
    // Historical cycles are stable; keep Estimate enabled for current cycle so users can refresh as new mail arrives.
    estimateBtn.disabled = busy || !hasCycle || (noPendingWork && !isCurrent);
  }
  if (analyzeBtn) {
    analyzeBtn.disabled = busy || !hasCycle || noPendingWork;
  }
}

function applyCycleEstimateSummary(est) {
  const ready = Number(est.ready_to_analyze ?? 0);
  const cached = Number(est.cached_total ?? 0);
  const notAnalyzed = Number(est.not_analyzed_total ?? 0);
  const estimatedCost = Number(est.estimated_cost_usd ?? 0);
  const exactRaw = est.gmail_exact_list_count;
  const exact =
    exactRaw != null && Number.isFinite(Number(exactRaw)) ? Math.max(0, Number(exactRaw)) : null;
  const selectedYear = selectedCycleStartYear();
  const analyzed = Number(state.summary?.classified_total ?? 0);
  const foundCount = exact != null ? exact : Math.max(cached, analyzed + notAnalyzed);

  state.cycleReadyToAnalyze = ready;
  state.cycleEstimatedCostUsd = estimatedCost;
  state.cycleLoadedYear = null;
  state.cycleEstimateYear = selectedYear ?? null;
  state.cycleNeedsExactScan = Boolean(est.needs_exact_list_scan);
  state.cycleExactCount = exact;
  state.cycleCachedTotal = cached;
  state.cycleNotAnalyzedTotal = Math.max(0, notAnalyzed);
  updateAnalyzeButtonState();

  if (state.cycleNeedsExactScan) {
    setCycleResultSummary("Estimate Cost to find cycle email count and estimated analysis cost.");
    return;
  }

  if (selectedYear === currentCycleStartYear() && analyzed > 0) {
    setCycleResultSummary(
      `Analyzed ${analyzed.toLocaleString()} emails. Found ${notAnalyzed.toLocaleString()} new · Est. cost ${formatUsd(estimatedCost)}`
    );
    return;
  }

  if (foundCount > 0 || cached > 0 || notAnalyzed > 0) {
    setCycleResultSummary(
      `Found ${foundCount.toLocaleString()} emails in this cycle · Est. up to ${formatUsd(estimatedCost)} to analyze`
    );
    return;
  }

  setCycleResultSummary("Estimate Cost to find cycle email count and estimated analysis cost.");
}

async function refreshCycleEstimate() {
  if (isAllCyclesSelected()) {
    state.cycleReadyToAnalyze = 0;
    state.cycleEstimatedCostUsd = 0;
    state.cycleEstimateYear = null;
    state.cycleNeedsExactScan = true;
    state.cycleExactCount = null;
    state.cycleCachedTotal = 0;
    state.cycleNotAnalyzedTotal = 0;
    setCycleResultSummary("");
    updateAnalyzeButtonState();
    return;
  }
  const targetYear = selectedCycleStartYear();
  if (targetYear == null) return;

  try {
    const est = await getCycleEstimate({ cycleStartYear: targetYear, query: "in:inbox" });
    if (state.selectedCycleStartYear !== targetYear) return;
    applyCycleEstimateSummary(est);
  } catch (err) {
    const up = Number(state.summary?.unprocessed_total ?? 0);
    const cached = Number(state.summary?.cached_total ?? 0);
    state.cycleReadyToAnalyze = up;
    state.cycleEstimatedCostUsd = 0;
    state.cycleEstimateYear = null;
    state.cycleNeedsExactScan = true;
    state.cycleExactCount = null;
    state.cycleCachedTotal = cached;
    state.cycleNotAnalyzedTotal = Math.max(0, up);
    updateAnalyzeButtonState();
    setCycleResultSummary(
      err?.message
        ? `${err.message}`
        : cached > 0 || up > 0
          ? `Found ${Math.max(cached, up).toLocaleString()} emails in this cycle · Est. up to ${formatUsd(state.cycleEstimatedCostUsd)} to analyze`
          : "Estimate Cost to find cycle email count and estimated analysis cost."
    );
  }
}

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

  const totalsByMonth = new Map();
  const receivedByMonth = new Map();
  for (const row of data) {
    const key = String(row.month || "").trim();
    if (!key) continue;
    totalsByMonth.set(key, Number(row.count || 0));
    receivedByMonth.set(key, Number(row.received_count || 0));
  }
  const monthKeys = [...totalsByMonth.keys()].sort();
  if (!monthKeys.length) return null;

  const firstReal = parseMonthStart(monthKeys[0]);
  const lastReal = parseMonthStart(monthKeys[monthKeys.length - 1]);
  if (!firstReal || !lastReal) return null;

  const months = [];
  const x = [];
  const totalY = [];
  const receivedY = [];

  for (const key of monthKeys) {
    const dt = parseMonthStart(key);
    if (!dt) continue;
    const total = Number(totalsByMonth.get(key) || 0);
    const received = Number(receivedByMonth.get(key) || 0);
    // Drop total=0 months entirely (and we no longer pad with synthetic 0 months).
    if (!Number.isFinite(total) || total <= 0) continue;
    months.push(key);
    x.push(dt.toISOString());
    totalY.push(total);
    // Drop received=0 points by rendering them as null.
    receivedY.push(Number.isFinite(received) && received > 0 ? received : null);
  }

  return {
    months,
    x,
    totalY,
    receivedY,
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
  const monthCount = series.months.length;
  const minWidth = Math.max(scroll.clientWidth, monthCount * monthWidth);
  status.textContent = `Loaded ${monthCount} months.`;

  const monthLabels = series.x.map(formatMonthTick);
  const totalTrace = {
    type: "scatter",
    mode: "lines+markers",
    x: series.x,
    y: series.totalY,
    text: monthLabels,
    line: { color: "#577aa6", width: 3, shape: "linear" },
    marker: { color: "#577aa6", size: 7 },
    name: "Total emails",
    hovertemplate: "%{text}<br>%{y} total emails<extra></extra>",
  };

  const receivedTrace = {
    type: "scatter",
    mode: "lines+markers",
    x: series.x,
    y: series.receivedY,
    text: monthLabels,
    line: { color: "#84c9cc", width: 3, shape: "linear" },
    marker: { color: "#84c9cc", size: 7 },
    name: "Applications",
    hovertemplate: "%{text}<br>%{y} Applications<extra></extra>",
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
    showlegend: true,
    legend: { orientation: "h", x: 0, y: 1.18, yanchor: "top" },
  };

  await window.Plotly.react(chart, [totalTrace, receivedTrace], layout, {
    displayModeBar: false,
    responsive: false,
  });
  scroll.scrollLeft = 0;
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
    const payload = await getMonthlyCounts({
      limitMonths: 60,
      cycleStartYear: selectedCycleStartYear(),
    });
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
  const busy = state.cycleBusy || state.cycleScanBusy;
  const ids = ["cycle_estimate_btn", "cycle_year_select", "cycle_prev_btn", "cycle_next_btn"];
  for (const id of ids) {
    const el = $(id);
    if (el) el.disabled = busy;
  }
  refreshCyclePicker();
  updateAnalyzeButtonState();
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
  $("metric_yes").textContent = String(summary.application_yes_total ?? 0);
  $("metric_no").textContent = String(summary.application_no_total ?? 0);
  $("metric_corrected").textContent = String(summary.manually_corrected_total ?? 0);

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
    `Loaded ${payload.links.length} branches across ${payload.total_pairs} applications (Interview counted only when interview_date exists).`
  );
}

async function refreshDashboard({ keepStatus = false } = {}) {
  const cycleStartYear = selectedCycleStartYear();
  if (!keepStatus) {
    setStatus("Refreshing dashboard...", { spinner: true });
  }
  setSankeyStatus("Loading sankey...");
  try {
    const [summary, sankey] = await Promise.all([
      getDashboardSummary({ cycleStartYear }),
      getSankey({ cycleStartYear }),
    ]);
    // Update sankey state first so stage chart derivation can include Pending immediately.
    state.sankey = sankey;
    renderSummary(summary);
    await renderSankey(sankey);
    if (!keepStatus) {
      setStatus("Dashboard refreshed.");
    }
  } catch (err) {
    setStatus(err.message || String(err), { error: true });
    setSankeyStatus(err.message || String(err), true);
  }
}

async function handleEstimateCost() {
  hideAllConfirmRows();
  if (isAllCyclesSelected()) {
    setStatus("Choose a specific cycle first.");
    return;
  }
  const cycleStartYear = selectedCycleStartYear();
  if (cycleStartYear == null) return;
  state.cycleScanBusy = true;
  setControlsBusy();
  setStatus("Estimating cycle cost…", { spinner: true });
  try {
    await invalidateCycleGmailCount(cycleStartYear);
    await streamCycleCount({
      cycleStartYear,
      query: "in:inbox",
      onEvent: (event) => {
        if (event?.type === "count_progress") {
          const counted = Number(event.counted ?? 0);
          setCycleResultSummary(`Counting… ${counted.toLocaleString()} emails found so far`);
        }
      },
    });
    if (state.selectedCycleStartYear !== cycleStartYear) return;
    await refreshCycleEstimate();
    setStatus("Cost estimate updated.", {});
  } catch (err) {
    setStatus(err.message || String(err), { error: true });
  } finally {
    state.cycleScanBusy = false;
    setControlsBusy();
  }
}

async function handleCycleAnalyze() {
  hideAllConfirmRows();
  if (isAllCyclesSelected()) {
    setStatus("Choose a specific cycle before running analysis.");
    return;
  }
  const cycleStartYear = selectedCycleStartYear();

  state.cycleBusy = true;
  setControlsBusy();
  try {
    setStatus("Loading emails for this cycle…", { spinner: true });
    const loadPayload = await streamCycleLoad({
      cycleStartYear,
      query: "in:inbox",
      onEvent: (event) => {
        if (event.type === "load_start") {
          const cycleStart = formatCycleStartDate(event.cycle_start_date);
          setStatus(`Loading emails since ${cycleStart}…`, { spinner: true });
          return;
        }
        if (event.type === "load_fetch_start") {
          setStatus(
            `Found ${event.listed} emails in this cycle. Loading ${event.new_candidates} new…`,
            { spinner: true }
          );
          return;
        }
        if (event.type === "load_fetch_progress") {
          setStatus(`Loaded ${event.fetched_new}/${event.total} new emails…`, { spinner: true });
          return;
        }
      },
    });

    if (!loadPayload) {
      setStatus("Load finished without a completion payload.", { error: true });
      return;
    }

    await refreshDashboard({ keepStatus: true });
    await refreshCycleEstimate();

    setStatus("Analyzing emails…", { spinner: true });
    const analyzeDone = await streamCycleAnalyze({
      cycleStartYear,
      query: "in:inbox",
      onEvent: (event) => {
        if (event.type === "analyze_start") {
          const ready = Number(event.ready_to_analyze || 0);
          if (ready <= 0) {
            setStatus("No emails are waiting for analysis in this cycle.");
          } else {
            setStatus(`Analyzing up to ${ready} emails…`, { spinner: true });
          }
          return;
        }
        if (event.type === "analyze_batch_start") {
          const attempted = Number(event.attempted || 0);
          if (attempted > 0) {
            setStatus(`Analyzing batch ${event.batch_index} (${attempted} emails)…`, { spinner: true });
          }
          return;
        }
        if (event.type === "analyze_progress") {
          setStatus(`Analyzing batch ${event.batch_index}: ${event.finished}/${event.total}`, {
            spinner: true,
          });
          return;
        }
      },
    });

    await refreshDashboard({ keepStatus: true });

    if (!analyzeDone) {
      setStatus("Analysis finished without a completion payload.", { error: true });
      return;
    }

    const listed = Number(loadPayload.listed || 0);
    const loaded = Number(loadPayload.fetched_new || 0);
    const found = Number(analyzeDone.application_related_found || 0);
    const analyzed = Number(analyzeDone.analyzed || 0);
    const missingCompany = Number(analyzeDone.missing_company || 0);
    const failures = Number(analyzeDone.failed || 0);
    const remaining = Number(analyzeDone.remaining_unprocessed || 0);
    state.cycleReadyToAnalyze = remaining;
    state.cycleEstimatedCostUsd = 0;
    updateAnalyzeButtonState();

    if (analyzed > 0) {
      setStatus(
        `Done: listed ${listed.toLocaleString()} in Gmail (${loaded.toLocaleString()} newly fetched). Found ${found} application-related emails out of ${analyzed} analyzed.`
      );
    } else {
      setStatus(
        `Done: listed ${listed.toLocaleString()} in Gmail (${loaded.toLocaleString()} newly fetched). No messages required classification.`
      );
    }

    const notes = [];
    notes.push(`${listed.toLocaleString()} matched in Gmail · ${loaded.toLocaleString()} newly loaded`);
    notes.push(`Found ${found} application-related out of ${analyzed} analyzed`);
    if (missingCompany > 0) notes.push(`${missingCompany} still need company detection`);
    if (failures > 0) notes.push(`${failures} had processing issues`);
    if (remaining > 0) notes.push(`${remaining} remain unprocessed`);
    setCycleResultSummary(notes.join(" \u00b7 "));
    await refreshCycleEstimate();
  } catch (err) {
    setStatus(err.message || String(err), { error: true });
  } finally {
    state.cycleBusy = false;
    setControlsBusy();
  }
}

function filteredMessages() {
  const query = state.messagesSearchTerm.trim().toLowerCase();
  return state.messages.filter((message) => {
    const application = message.result?.application;
    if (state.messagesFilter === "yes" && application !== "yes") return false;
    if (state.messagesFilter === "no" && application !== "no") return false;
    if (!query) return true;
    const haystack = [
      message.subject,
      message.sender,
      message.result?.company,
      message.result?.role,
    ]
      .filter(Boolean)
      .join(" ")
      .toLowerCase();
    return haystack.includes(query);
  });
}

function stageOptionsHtml(selectedStage) {
  return STAGES.map(
    (stage) =>
      `<option value="${escapeHtml(stage)}" ${stage === selectedStage ? "selected" : ""}>${escapeHtml(stage)}</option>`
  ).join("");
}

function renderMessageEditRow(message) {
  const result = message.result || { application: "no", company: "", role: "", stage: "Unknown", interview_date: "" };
  const stage = STAGES.includes(result.stage) ? result.stage : "Unknown";
  return (
    `<tr data-gmail-id="${escapeHtml(message.id)}" class="message-edit-row">` +
    `<td colspan="8" class="message-edit-cell">` +
    `<div class="message-edit-form">` +
    `<label>Application` +
    `<select data-field="application">` +
    `<option value="yes" ${result.application === "yes" ? "selected" : ""}>Yes</option>` +
    `<option value="no" ${result.application !== "yes" ? "selected" : ""}>No</option>` +
    `</select></label>` +
    `<label>Company<input data-field="company" type="text" value="${escapeHtml(result.company || "")}" /></label>` +
    `<label>Role<input data-field="role" type="text" value="${escapeHtml(result.role || "")}" /></label>` +
    `<label>Stage<select data-field="stage">${stageOptionsHtml(stage)}</select></label>` +
    `<label data-interview-date-field ${stage === "Interview" ? "" : "hidden"}>Interview date` +
    `<input data-field="interview_date" type="date" value="${escapeHtml(result.interview_date || "")}" /></label>` +
    `<button type="button" data-action="save" data-gmail-id="${escapeHtml(message.id)}">Save</button>` +
    `<button type="button" data-action="cancel" data-gmail-id="${escapeHtml(message.id)}">Cancel</button>` +
    `</div></td></tr>`
  );
}

function renderMessageRow(message) {
  if (state.editingGmailId === message.id) {
    return renderMessageEditRow(message);
  }
  const result = message.result;
  const corrected = result?.manually_corrected_at
    ? '<span class="message-corrected-badge">Corrected</span>'
    : "";
  const application = result ? (result.application === "yes" ? "Yes" : "No") : "Not analyzed";
  const company = result?.company || "-";
  const role = result?.role || "-";
  const stage = result?.stage || "-";
  const editBtn = result
    ? `<button type="button" data-action="edit" data-gmail-id="${escapeHtml(message.id)}">Edit</button>`
    : "";
  return (
    `<tr data-gmail-id="${escapeHtml(message.id)}">` +
    `<td>${escapeHtml(formatMessageDate(message))}</td>` +
    `<td>${escapeHtml(message.sender || "")}</td>` +
    `<td>${escapeHtml(message.subject || "")}</td>` +
    `<td>${escapeHtml(application)}${corrected}</td>` +
    `<td>${escapeHtml(company)}</td>` +
    `<td>${escapeHtml(role)}</td>` +
    `<td>${escapeHtml(stage)}</td>` +
    `<td>${editBtn}</td>` +
    `</tr>`
  );
}

function renderMessagesList() {
  const tbody = $("messages_body");
  const status = $("messages_status");
  if (!tbody) return;

  const visible = filteredMessages();
  if (status) {
    status.textContent = `Showing ${visible.length} of ${state.messages.length} loaded messages.`;
  }

  if (!visible.length) {
    tbody.innerHTML = '<tr><td colspan="8" class="table-empty">No messages match this filter.</td></tr>';
    return;
  }

  tbody.innerHTML = visible.map(renderMessageRow).join("");
}

async function loadMessages() {
  const status = $("messages_status");
  if (status) status.textContent = "Loading...";
  try {
    const payload = await getMessages({ limit: state.messagesLimit });
    state.messages = Array.isArray(payload?.messages) ? payload.messages : [];
    renderMessagesList();
  } catch (err) {
    if (status) status.textContent = err.message || String(err);
  }
}

function startEditingMessage(gmailId) {
  state.editingGmailId = gmailId;
  renderMessagesList();
}

function cancelEditingMessage() {
  state.editingGmailId = "";
  renderMessagesList();
}

async function saveMessageCorrection(gmailId) {
  if (state.messagesSaving) return;
  const row = document.querySelector(`tr.message-edit-row[data-gmail-id="${gmailId}"]`);
  if (!row) return;

  const field = (name) => row.querySelector(`[data-field="${name}"]`)?.value ?? "";
  const application = field("application");
  const stage = field("stage");
  const interviewDate = stage === "Interview" ? field("interview_date") : "";

  state.messagesSaving = true;
  try {
    await correctMessageResult({
      gmailId,
      application,
      company: field("company").trim(),
      role: field("role").trim(),
      stage,
      interviewDate,
    });
    state.editingGmailId = "";
    await loadMessages();
    await refreshDashboard({ keepStatus: true });
    setStatus("Correction saved.");
  } catch (err) {
    setStatus(err.message || String(err), { error: true });
  } finally {
    state.messagesSaving = false;
  }
}

function wireEvents() {
  const onClick = (id, handler) => {
    const el = $(id);
    if (el) el.addEventListener("click", handler);
  };
  const onInput = (id, handler) => {
    const el = $(id);
    if (el) el.addEventListener("input", handler);
  };

  onClick("cycle_estimate_btn", handleEstimateCost);
  onClick("cycle_analyze_btn", handleCycleAnalyze);
  onClick("cycle_prev_btn", () => {
    if (state.selectedCycleStartYear == null) return;
    void selectCycleStartYear(state.selectedCycleStartYear - 1);
  });
  onClick("cycle_next_btn", () => {
    if (state.selectedCycleStartYear == null) {
      void selectCycleStartYear(currentCycleStartYear());
      return;
    }
    void selectCycleStartYear(state.selectedCycleStartYear + 1);
  });
  const cycleSelect = $("cycle_year_select");
  if (cycleSelect) {
    cycleSelect.addEventListener("change", () => {
      const raw = cycleSelect.value;
      if (raw === "") {
        void selectCycleStartYear(null);
        return;
      }
      const y = Number.parseInt(raw, 10);
      if (!Number.isFinite(y)) return;
      void selectCycleStartYear(y);
    });
  }

  onInput("branch_search_input", (event) => {
    state.branchSearchTerm = String(event.target?.value || "");
    renderBranchPairTable();
  });

  onClick("timeline_toggle_btn", () => void toggleTimeline());

  const filterSelect = $("messages_filter_select");
  if (filterSelect) {
    filterSelect.addEventListener("change", () => {
      state.messagesFilter = filterSelect.value || "all";
      renderMessagesList();
    });
  }
  onInput("messages_search_input", (event) => {
    state.messagesSearchTerm = String(event.target?.value || "");
    renderMessagesList();
  });
  onClick("messages_load_more_btn", () => {
    state.messagesLimit += MESSAGES_PAGE_SIZE;
    void loadMessages();
  });

  const messagesBody = $("messages_body");
  if (messagesBody) {
    messagesBody.addEventListener("click", (event) => {
      const target = event.target.closest("[data-action]");
      if (!target) return;
      const gmailId = target.dataset.gmailId || "";
      if (!gmailId) return;
      if (target.dataset.action === "edit") startEditingMessage(gmailId);
      if (target.dataset.action === "cancel") cancelEditingMessage();
      if (target.dataset.action === "save") void saveMessageCorrection(gmailId);
    });
    messagesBody.addEventListener("change", (event) => {
      if (event.target?.dataset?.field !== "stage") return;
      const row = event.target.closest("tr");
      const field = row?.querySelector("[data-interview-date-field]");
      if (field) field.hidden = event.target.value !== "Interview";
    });
  }
}

async function init() {
  wireEvents();
  refreshCyclePicker();
  updateAnalyzeButtonState();
  setControlsBusy();
  await refreshDashboard();
  await refreshCycleEstimate();
  await loadMessages();
  setControlsBusy();
}

void init();
