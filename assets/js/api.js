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

export async function streamProcessBatch({ count, concurrency, onEvent }) {
  const body = { count };
  if (Number.isFinite(concurrency) && concurrency > 0) {
    body.concurrency = Math.floor(concurrency);
  }

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
