const NODE_COLORS = {
  Received: "#577aa6",
  "Online Assessment": "#f2a53b",
  Interview: "#ea8da0",
  Offer: "#84c9cc",
  "Pending after Received": "#9aa5b3",
  "Pending after Online Assessment": "#9aa5b3",
  "Pending after Interview": "#9aa5b3",
  "Pending after Unknown": "#9aa5b3",
  "Rejected after Received": "#e36363",
  "Rejected after Online Assessment": "#e36363",
  "Rejected after Interview": "#e36363",
  "Rejected after Unknown": "#e36363",
  Unknown: "#b9b4d8",
};
const REJECTION_COLOR = "#e36363";

const NODE_X = {
  Received: 0.02,
  "Rejected after Received": 0.18,
  "Pending after Received": 0.18,
  "Online Assessment": 0.42,
  "Rejected after Online Assessment": 0.68,
  "Pending after Online Assessment": 0.68,
  Interview: 0.82,
  "Rejected after Interview": 0.94,
  "Pending after Interview": 0.94,
  Offer: 0.96,
  Unknown: 0.02,
  "Rejected after Unknown": 0.18,
  "Pending after Unknown": 0.18,
};

const NODE_Y = {
  Received: 0.62,
  "Rejected after Received": 0.18,
  "Pending after Received": 0.52,
  "Online Assessment": 0.74,
  "Rejected after Online Assessment": 0.14,
  "Pending after Online Assessment": 0.46,
  Interview: 0.76,
  "Rejected after Interview": 0.1,
  "Pending after Interview": 0.4,
  Offer: 0.8,
  Unknown: 0.86,
  "Rejected after Unknown": 0.36,
  "Pending after Unknown": 0.66,
};

function nodeColor(label) {
  if (typeof label === "string" && label.startsWith("Rejected")) {
    return REJECTION_COLOR;
  }
  return NODE_COLORS[label] || "#8d9aad";
}

function displayNodeLabel(label) {
  if (typeof label === "string" && label.startsWith("Rejected")) {
    return "Rejected";
  }
  if (typeof label === "string" && label.startsWith("Pending")) {
    return "Pending";
  }
  return label;
}

function hexToRgba(hex, alpha) {
  if (typeof hex !== "string" || !hex.startsWith("#")) {
    return `rgba(141, 154, 173, ${alpha})`;
  }
  const normalized = hex.replace("#", "");
  const value = Number.parseInt(normalized, 16);
  const r = (value >> 16) & 255;
  const g = (value >> 8) & 255;
  const b = value & 255;
  return `rgba(${r}, ${g}, ${b}, ${alpha})`;
}

export async function renderSankeyChart({ element, payload, onSelection }) {
  if (!element) return;
  if (!window.Plotly) {
    throw new Error("Plotly is not available.");
  }

  const hasLinks = Array.isArray(payload?.links) && payload.links.length > 0;
  if (!hasLinks) {
    window.Plotly.purge(element);
    element.innerHTML = "";
    return;
  }

  const labels = payload.nodes.map((node) => node.label);
  const displayLabels = labels.map(displayNodeLabel);
  const payloadNodes = payload.nodes || [];
  const nodeX = labels.map((label, idx) => {
    const fromPayload = payloadNodes[idx]?.x;
    if (Number.isFinite(fromPayload)) return fromPayload;
    if (Number.isFinite(NODE_X[label])) return NODE_X[label];
    const fallback = 0.08 + (idx / Math.max(1, labels.length - 1)) * 0.84;
    return Math.min(0.97, Math.max(0.03, fallback));
  });
  const nodeY = labels.map((label, idx) => {
    const fromPayload = payloadNodes[idx]?.y;
    if (Number.isFinite(fromPayload)) return fromPayload;
    if (Number.isFinite(NODE_Y[label])) return NODE_Y[label];
    return 0.4;
  });
  const linkColors = payload.links.map((link) => {
    const targetLabel = labels[link.target] || "";
    return hexToRgba(nodeColor(targetLabel), 0.38);
  });

  const trace = {
    type: "sankey",
    arrangement: "fixed",
    valueformat: "d",
    node: {
      pad: 18,
      thickness: 18,
      line: { color: "#f6f7fa", width: 0.8 },
      label: displayLabels,
      color: labels.map(nodeColor),
      x: nodeX,
      y: nodeY,
      customdata: labels,
      hovertemplate: "%{customdata}<extra></extra>",
    },
    link: {
      source: payload.links.map((link) => link.source),
      target: payload.links.map((link) => link.target),
      value: payload.links.map((link) => link.value),
      color: linkColors,
      customdata: payload.links.map((link) => link.id),
      hovertemplate: "%{customdata}<br>%{value} pair(s)<extra></extra>",
    },
  };

  const layout = {
    margin: { l: 10, r: 10, t: 10, b: 10 },
    paper_bgcolor: "#ffffff",
    plot_bgcolor: "#ffffff",
    font: { size: 13, color: "#1f2430" },
  };

  if (typeof element.removeAllListeners === "function") {
    element.removeAllListeners("plotly_click");
  }

  await window.Plotly.react(element, [trace], layout, { displayModeBar: false, responsive: true });

  element.on("plotly_click", (event) => {
    const point = event?.points?.[0];
    if (!point) return;

    const pointIndex = Number.isFinite(point.pointNumber) ? Number(point.pointNumber) : -1;
    const edgeIdByCustomData = typeof point?.customdata === "string" ? point.customdata : "";
    const edgeId =
      edgeIdByCustomData.includes(" -> ")
        ? edgeIdByCustomData
        : Number.isFinite(point?.source) && Number.isFinite(point?.target) && pointIndex >= 0
          ? payload?.links?.[pointIndex]?.id
          : "";
    if (typeof edgeId === "string" && edgeId) {
      onSelection?.({ type: "edge", edgeId });
      return;
    }

    const nodeLabelByIndex = pointIndex >= 0 ? payload?.nodes?.[pointIndex]?.label : undefined;
    const nodeLabelByCustomData =
      typeof point?.customdata === "string" && point.customdata
        ? point.customdata
        : undefined;
    const nodeLabelByLabel =
      typeof point?.label === "string" && point.label ? point.label : undefined;
    const nodeLabel = nodeLabelByIndex || nodeLabelByCustomData || nodeLabelByLabel || "";
    if (nodeLabel) {
      onSelection?.({ type: "node", nodeLabel: String(nodeLabel) });
    }
  });
}
