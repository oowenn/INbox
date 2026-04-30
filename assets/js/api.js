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

export async function getDashboardSummary() {
  return requestJson("/api/dashboard/summary", {}, "Failed to load dashboard summary");
}

export async function getSankey() {
  return requestJson("/api/analytics/sankey", {}, "Failed to load sankey data");
}

export async function getMonthlyCounts({ limitMonths = 48 } = {}) {
  const clamped = Math.max(1, Math.min(240, Number(limitMonths) || 48));
  return requestJson(
    `/api/analytics/monthly?limit_months=${clamped}`,
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
  fetchLimit = 12000,
  classifyBatchSize = 250,
  concurrency,
  query = "in:inbox",
  onEvent,
} = {}) {
  const body = {
    fetch_limit: Math.max(100, Math.min(100000, Number(fetchLimit) || 12000)),
    classify_batch_size: Math.max(10, Math.min(500, Number(classifyBatchSize) || 250)),
    query: String(query || "in:inbox"),
  };
  if (Number.isFinite(Number(concurrency)) && Number(concurrency) > 0) {
    body.concurrency = Math.max(1, Math.min(16, Number(concurrency)));
  }

  const response = await fetch("/api/cycle/run/stream", {
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
