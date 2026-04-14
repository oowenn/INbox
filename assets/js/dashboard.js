import {
  clearResults,
  fetchNewestMessages,
  getDashboardSummary,
  getSankey,
  streamProcessBatch,
  wipeCache,
} from "./api.js";
import { renderSankeyChart } from "./sankey.js";

const state = {
  pullBusy: false,
  processBusy: false,
  mutateBusy: false,
  summary: null,
  sankey: null,
  selectedBranchId: "",
  selectedBranchPairs: [],
  branchSearchTerm: "",
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
  const busy = state.pullBusy || state.processBusy || state.mutateBusy;
  const ids = [
    "pull_btn",
    "process_btn",
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

  const inputIds = ["query_input", "pull_count_input", "process_count_input", "process_concurrency_input"];
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
  if (!tbody) return;

  if (!state.selectedBranchId) {
    tbody.innerHTML = '<tr><td colspan="2" class="table-empty">Select a branch to inspect pairs.</td></tr>';
    return;
  }

  const query = state.branchSearchTerm.trim().toLowerCase();
  const filtered = query
    ? state.selectedBranchPairs.filter((pair) => {
        const haystack = `${pair.company || ""} ${pair.role || ""}`.toLowerCase();
        return haystack.includes(query);
      })
    : state.selectedBranchPairs;

  if (!filtered.length) {
    const message = query
      ? "No pairs match this search."
      : "No pairs mapped to this selection.";
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
  const noun = pairs.length === 1 ? "pair" : "pairs";
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

  const noun = mergedPairs.length === 1 ? "pair" : "pairs";
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
    setSankeyStatus("No classified application paths yet.");
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
  setSankeyStatus(`Loaded ${payload.links.length} branches across ${payload.total_pairs} application pairs.`);
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
  const query = ($("query_input").value || "in:inbox").trim() || "in:inbox";

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
  const rawConcurrency = $("process_concurrency_input").value.trim();
  const concurrency = rawConcurrency ? clampInt(rawConcurrency, 0, 1, 16) : undefined;

  state.processBusy = true;
  setControlsBusy();
  setStatus("Starting processing batch...", { spinner: true });

  let donePayload = null;
  try {
    donePayload = await streamProcessBatch({
      count,
      concurrency,
      onEvent: (event) => {
        if (event.type === "start") {
          const attempted = event.attempted ?? 0;
          if (!attempted) {
            setStatus("No unprocessed cached emails found.");
            return;
          }
          setStatus(`Processing: 0/${attempted}`, { spinner: true });
        } else if (event.type === "progress") {
          setStatus(`Processing: ${event.finished ?? 0}/${event.total ?? 0}`, { spinner: true });
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
      ? ` ${donePayload.failed} failed (${donePayload.failed_ids.slice(0, 3).join(", ")}${donePayload.failed > 3 ? ", ..." : ""}).`
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

function handleClearResultsClick() {
  if (state.pullBusy || state.processBusy || state.mutateBusy) return;
  setConfirmVisible("wipe_confirm_row", false);
  setConfirmVisible("clear_confirm_row", true);
  setStatus('Press "Confirm" to clear stored classifications.');
}

function handleWipeCacheClick() {
  if (state.pullBusy || state.processBusy || state.mutateBusy) return;
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

function wireEvents() {
  $("pull_btn").addEventListener("click", handlePull);
  $("process_btn").addEventListener("click", handleProcess);
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
}

async function init() {
  wireEvents();
  setControlsBusy();
  await refreshDashboard();
  setControlsBusy();
}

void init();
