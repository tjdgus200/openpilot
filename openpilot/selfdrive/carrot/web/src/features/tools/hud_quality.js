"use strict";

export const HUD_QUALITY_LABELS = ["H264 auto", "H264 4M", "H264 8M", "H264 12M", "TurboJPEG Q68", "TurboJPEG Q85", "TurboJPEG Q92", "Pillow Q85"];

export function qualityRows(report) {
  const rows = Array.isArray(report?.rows) ? report.rows.slice(0, 8) : [];
  const result = HUD_QUALITY_LABELS.map((label, id) => {
    const row = rows.find((value) => value?.id === id);
    const valid = row && Number.isInteger(row.n) && row.n > 0 && row.n <= 6000
      && [row.mean_ms, row.p99_ms, row.max_ms, row.server_p99_ms, row.seconds].every((n) => Number.isFinite(n) && n >= 0)
      && Number.isInteger(row.over50) && row.over50 >= 0 && row.over50 <= row.n;
    const ready = !!valid && row.n >= 1000 && row.seconds >= 60;
    return {id, label, active: report?.effective === id, best: ready && report?.best === id,
      ready,
      cells: valid ? [String(row.n), `${row.mean_ms.toFixed(1)} / ${row.p99_ms.toFixed(1)}`,
        `${row.over50} (${(row.over50 / row.n * 100).toFixed(2)}%)`, row.server_p99_ms.toFixed(1)] : ["--", "--", "--", "--"]};
  });
  if (result.filter((row) => row.ready).length < 2) result.forEach((row) => { row.best = false; });
  return result;
}

export function renderQualityComparison(element, report, text) {
  if (typeof element?.replaceChildren !== "function") return;
  const document = element.ownerDocument || globalThis.document;
  const title = document.createElement("strong");
  title.textContent = text("quality_title");
  const note = document.createElement("p");
  note.textContent = text(report ? `quality_${report.state}` : "quality_unavailable");
  const scope = document.createElement("p");
  scope.textContent = text("quality_scope");
  const wrap = document.createElement("div");
  wrap.style.overflowX = "auto";
  const table = document.createElement("table");
  table.style.fontSize = "12px";
  table.style.width = "100%";
  const head = document.createElement("tr");
  for (const label of [text("quality_candidate"), "N", "GPU AVG/P99 ms", "GPU >50ms", "Server P99 ms"]) {
    const cell = document.createElement("th");
    cell.textContent = label;
    head.appendChild(cell);
  }
  table.appendChild(head);
  for (const value of qualityRows(report)) {
    const row = document.createElement("tr");
    row.dataset.profile = String(value.id);
    if (value.active) row.style.fontWeight = "bold";
    if (value.best) row.style.color = "#6ee7a0";
    for (const label of [`${value.active ? "● " : ""}${value.best ? "★ " : ""}${value.label}`, ...value.cells]) {
      const cell = document.createElement("td");
      cell.textContent = label;
      row.appendChild(cell);
    }
    table.appendChild(row);
  }
  wrap.appendChild(table);
  element.replaceChildren(title, note, wrap, scope);
}
