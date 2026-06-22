const JSON_HEADERS = {
  "Content-Type": "application/json",
};

function detailFromPayload(payload, fallback) {
  if (!payload || typeof payload !== "object") return fallback;
  if (!("detail" in payload)) return fallback;
  const detail = payload.detail;
  if (typeof detail === "string") return detail;
  try {
    return JSON.stringify(detail);
  } catch {
    return fallback;
  }
}

async function requestJson(url, options = {}, fallbackError = "Request failed") {
  const response = await fetch(url, options);
  const payload = await response.json().catch(() => null);
  if (!response.ok) {
    throw new Error(detailFromPayload(payload, fallbackError));
  }
  return payload ?? {};
}

export async function getDashboardSummary({ cycleStartYear } = {}) {
  const params = new URLSearchParams();
  if (Number.isFinite(Number(cycleStartYear))) {
    params.set("cycle_start_year", String(Number(cycleStartYear)));
  }
  const suffix = params.toString() ? `?${params}` : "";
  return requestJson(`/api/dashboard/summary${suffix}`, {}, "Failed to load dashboard summary");
}

export async function getSankey({ cycleStartYear } = {}) {
  const params = new URLSearchParams();
  if (Number.isFinite(Number(cycleStartYear))) {
    params.set("cycle_start_year", String(Number(cycleStartYear)));
  }
  const suffix = params.toString() ? `?${params}` : "";
  return requestJson(`/api/analytics/sankey${suffix}`, {}, "Failed to load sankey data");
}

export async function invalidateCycleGmailCount(cycleStartYear) {
  const y = Number(cycleStartYear);
  if (!Number.isFinite(y)) {
    throw new Error("cycleStartYear is required");
  }
  const params = new URLSearchParams({
    cycle_start_year: String(Math.max(2000, Math.min(2100, y))),
  });
  return requestJson(
    `/api/cycle/gmail-count?${params}`,
    { method: "DELETE" },
    "Failed to reset saved Gmail count"
  );
}

export async function getCycleEstimate({ cycleStartYear, query = "in:inbox" } = {}) {
  const y = Number(cycleStartYear);
  if (!Number.isFinite(y)) {
    throw new Error("cycleStartYear is required");
  }
  const params = new URLSearchParams({
    cycle_start_year: String(Math.max(2000, Math.min(2100, y))),
  });
  const q = String(query || "in:inbox").trim();
  if (q) params.set("query", q);
  return requestJson(`/api/cycle/estimate?${params}`, {}, "Failed to estimate cycle scope");
}

export async function getMonthlyCounts({ limitMonths = 48, cycleStartYear } = {}) {
  const clamped = Math.max(1, Math.min(240, Number(limitMonths) || 48));
  const params = new URLSearchParams({ limit_months: String(clamped) });
  if (Number.isFinite(Number(cycleStartYear))) {
    params.set("cycle_start_year", String(Number(cycleStartYear)));
  }
  return requestJson(
    `/api/analytics/monthly?${params}`,
    {},
    "Failed to load monthly counts"
  );
}

export async function getMessages({ limit = 500 } = {}) {
  const clamped = Math.max(1, Math.min(500, Number(limit) || 500));
  return requestJson(`/api/messages?limit=${clamped}`, {}, "Failed to load cached messages");
}

export async function fetchNewestMessages({ count, query }) {
  return requestJson(
    "/api/messages/fetch",
    {
      method: "POST",
      headers: JSON_HEADERS,
      body: JSON.stringify({ count, query }),
    },
    "Failed to pull messages"
  );
}

export async function clearResults() {
  return requestJson("/api/results/clear", { method: "POST" }, "Failed to clear results");
}

export async function correctMessageResult({
  gmailId,
  application,
  company,
  role,
  stage,
  interviewDate,
}) {
  return requestJson(
    "/api/results/correct",
    {
      method: "POST",
      headers: JSON_HEADERS,
      body: JSON.stringify({
        gmail_id: gmailId,
        application,
        company: company || null,
        role: role || null,
        stage,
        interview_date: interviewDate || null,
      }),
    },
    "Failed to save correction"
  );
}

export async function getClassificationFailures({ limit = 200, gmailId } = {}) {
  const clamped = Math.max(1, Math.min(500, Number(limit) || 200));
  const params = new URLSearchParams({ limit: String(clamped) });
  if (gmailId && String(gmailId).trim()) {
    params.set("gmail_id", String(gmailId).trim());
  }
  return requestJson(`/api/classification/failures?${params}`, {}, "Failed to load classification failures");
}

export async function wipeCache() {
  return requestJson("/api/messages/wipe", { method: "POST" }, "Failed to wipe cached emails");
}

function parseSseLine(line) {
  if (!line.startsWith("data: ")) return null;
  try {
    return JSON.parse(line.slice(6));
  } catch {
    return null;
  }
}

async function streamJsonEvents({ url, body, onEvent }) {
  const response = await fetch(url, {
    method: "POST",
    headers: {
      ...JSON_HEADERS,
      Accept: "text/event-stream",
    },
    body: JSON.stringify(body),
  });

  if (!response.ok) {
    const payload = await response.json().catch(() => null);
    throw new Error(detailFromPayload(payload, `${response.status} ${response.statusText}`));
  }

  if (!response.body) {
    throw new Error("Streaming is not supported by this browser response.");
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let doneEvent = null;

  while (true) {
    const { done, value } = await reader.read();
    if (value) {
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split("\n");
      buffer = lines.pop() ?? "";
      for (const line of lines) {
        const event = parseSseLine(line);
        if (!event) continue;
        if (
          event.type === "done" ||
          event.type === "load_done" ||
          event.type === "analyze_done" ||
          event.type === "count_done"
        ) {
          doneEvent = event;
        }
        onEvent?.(event);
      }
    }
    if (done) break;
  }

  if (buffer.trim()) {
    const event = parseSseLine(buffer.trim());
    if (event) {
      if (
        event.type === "done" ||
        event.type === "load_done" ||
        event.type === "analyze_done" ||
        event.type === "count_done"
      ) {
        doneEvent = event;
      }
      onEvent?.(event);
    }
  }

  return doneEvent;
}

export async function streamProcessBatch({ count, onEvent }) {
  const body = { count };

  const response = await fetch("/api/classify/batch/stream", {
    method: "POST",
    headers: {
      ...JSON_HEADERS,
      Accept: "text/event-stream",
    },
    body: JSON.stringify(body),
  });

  if (!response.ok) {
    const payload = await response.json().catch(() => null);
    throw new Error(detailFromPayload(payload, `${response.status} ${response.statusText}`));
  }

  if (!response.body) {
    throw new Error("Streaming is not supported by this browser response.");
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let doneEvent = null;

  while (true) {
    const { done, value } = await reader.read();
    if (value) {
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split("\n");
      buffer = lines.pop() ?? "";
      for (const line of lines) {
        const event = parseSseLine(line);
        if (!event) continue;
        if (event.type === "done") doneEvent = event;
        onEvent?.(event);
      }
    }
    if (done) break;
  }

  if (buffer.trim()) {
    const event = parseSseLine(buffer.trim());
    if (event) {
      if (event.type === "done") doneEvent = event;
      onEvent?.(event);
    }
  }

  return doneEvent;
}

export async function streamApplicationCycleRun({
  fetchLimit,
  classifyBatchSize = 250,
  cycleStartYear,
  concurrency,
  query = "in:inbox",
  onEvent,
} = {}) {
  const body = {
    classify_batch_size: Math.max(10, Math.min(500, Number(classifyBatchSize) || 250)),
    query: String(query || "in:inbox"),
  };
  if (fetchLimit != null && Number.isFinite(Number(fetchLimit))) {
    body.fetch_limit = Math.max(1, Math.min(10_000_000, Number(fetchLimit)));
  }
  if (Number.isFinite(Number(concurrency)) && Number(concurrency) > 0) {
    body.concurrency = Math.max(1, Math.min(16, Number(concurrency)));
  }
  if (Number.isFinite(Number(cycleStartYear)) && Number(cycleStartYear) >= 2000) {
    body.cycle_start_year = Math.max(2000, Math.min(2100, Number(cycleStartYear)));
  }
  return streamJsonEvents({
    url: "/api/cycle/run/stream",
    body,
    onEvent,
  });
}

export async function streamCycleCount({
  cycleStartYear,
  query = "in:inbox",
  fetchLimit,
  onEvent,
} = {}) {
  const body = {
    query: String(query || "in:inbox"),
  };
  if (fetchLimit != null && Number.isFinite(Number(fetchLimit))) {
    body.fetch_limit = Math.max(1, Math.min(10_000_000, Number(fetchLimit)));
  }
  if (Number.isFinite(Number(cycleStartYear)) && Number(cycleStartYear) >= 2000) {
    body.cycle_start_year = Math.max(2000, Math.min(2100, Number(cycleStartYear)));
  }
  let failed = null;
  const done = await streamJsonEvents({
    url: "/api/cycle/count/stream",
    body,
    onEvent: (event) => {
      if (event?.type === "count_failed") failed = event;
      onEvent?.(event);
    },
  });
  if (failed) {
    throw new Error(String(failed.error || "Gmail count failed"));
  }
  return done;
}

export async function streamCycleLoad({ cycleStartYear, query = "in:inbox", onEvent } = {}) {
  const body = { query: String(query || "in:inbox") };
  if (Number.isFinite(Number(cycleStartYear)) && Number(cycleStartYear) >= 2000) {
    body.cycle_start_year = Math.max(2000, Math.min(2100, Number(cycleStartYear)));
  }
  return streamJsonEvents({
    url: "/api/cycle/load/stream",
    body,
    onEvent,
  });
}

export async function streamCycleAnalyze({
  cycleStartYear,
  query = "in:inbox",
  onEvent,
} = {}) {
  const body = { query: String(query || "in:inbox") };
  if (Number.isFinite(Number(cycleStartYear)) && Number(cycleStartYear) >= 2000) {
    body.cycle_start_year = Math.max(2000, Math.min(2100, Number(cycleStartYear)));
  }
  return streamJsonEvents({
    url: "/api/cycle/analyze/stream",
    body,
    onEvent,
  });
}
