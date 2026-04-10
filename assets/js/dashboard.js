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
};

const $ = (id) => document.getElementById(id);

function escapeHtml(text) {
  const div = document.createElement("div");
  div.textContent = String(text ?? "");
  return div.innerHTML;
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

  const list = $("stage_breakdown");
  if (!list) return;
  const stageCounts = Array.isArray(summary.stage_counts) ? summary.stage_counts : [];
  if (!stageCounts.length) {
    list.innerHTML = "<li>No application=yes stage data yet.</li>";
    return;
  }

  list.innerHTML = stageCounts
    .map((item) => `<li>${escapeHtml(item.stage)}: ${Number(item.count ?? 0)}</li>`)
    .join("");
}

function renderBranchPairs(edgeId) {
  const title = $("branch_title");
  const list = $("branch_pairs");
  if (!title || !list) return;

  if (!edgeId || !state.sankey) {
    title.textContent = "Click any branch in the Sankey chart.";
    list.innerHTML = "";
    return;
  }

  const pairs = state.sankey.branch_pairs?.[edgeId] || [];
  const noun = pairs.length === 1 ? "pair" : "pairs";
  title.textContent = `${edgeId} (${pairs.length} ${noun})`;
  if (!pairs.length) {
    list.innerHTML = "<li>No company/role pairs mapped to this branch.</li>";
    return;
  }

  list.innerHTML = pairs
    .map(
      (pair) =>
        `<li>${escapeHtml(pair.company || "(Unknown Company)")} | ${escapeHtml(pair.role || "(Unknown Role)")}</li>`
    )
    .join("");
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
    onLinkClick: (edgeId) => renderBranchPairs(edgeId),
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
}

async function init() {
  wireEvents();
  setControlsBusy();
  await refreshDashboard();
  setControlsBusy();
}

void init();
