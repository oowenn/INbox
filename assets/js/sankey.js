const NODE_COLORS = {
  Applications: "#9c9793",
  Received: "#577aa6",
  "Online Assessment": "#f2a53b",
  Interview: "#ea8da0",
  Offer: "#84c9cc",
  Rejection: "#e36363",
  Unknown: "#b9b4d8",
};

function nodeColor(label) {
  return NODE_COLORS[label] || "#8d9aad";
}

function hexToRgba(hex, alpha) {
  const normalized = hex.replace("#", "");
  const value = Number.parseInt(normalized, 16);
  const r = (value >> 16) & 255;
  const g = (value >> 8) & 255;
  const b = value & 255;
  return `rgba(${r}, ${g}, ${b}, ${alpha})`;
}

export async function renderSankeyChart({ element, payload, onLinkClick }) {
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
  const linkColors = payload.links.map((link) => {
    const targetLabel = labels[link.target] || "";
    return hexToRgba(nodeColor(targetLabel), 0.38);
  });

  const trace = {
    type: "sankey",
    arrangement: "snap",
    valueformat: "d",
    node: {
      pad: 15,
      thickness: 22,
      line: { color: "#f6f7fa", width: 0.8 },
      label: labels,
      color: labels.map(nodeColor),
      hovertemplate: "%{label}<extra></extra>",
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
    const edgeId = point?.customdata;
    if (!edgeId) return;
    onLinkClick?.(edgeId);
  });
}
