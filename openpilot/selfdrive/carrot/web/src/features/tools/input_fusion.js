"use strict";

export function fusionRows(report) {
  return [0, 1].map((id) => {
    const row = report?.rows?.find((item) => item?.id === id);
    const valid = row && Number.isInteger(row.n) && row.n > 0 && row.n <= 6000
      && [row.mean_ms, row.p99_ms, row.max_ms, row.seconds].every((v) => Number.isFinite(v) && v >= 0)
      && Number.isInteger(row.over50) && row.over50 >= 0 && row.over50 <= row.n
      && row.max_ms >= Math.max(row.mean_ms, row.p99_ms);
    return {id, active: report?.effective === id,
      ready: !!valid && row.n >= 1000 && row.seconds >= 60,
      cells: valid ? [String(row.n), row.mean_ms.toFixed(3), row.p99_ms.toFixed(3),
        row.max_ms.toFixed(3), String(row.over50)] : ["--", "--", "--", "--", "--"]};
  });
}

export function renderInputFusion(element, report, text) {
  const document = element.ownerDocument || globalThis.document;
  const title = document.createElement("strong");
  title.textContent = text("fusion_title");
  const note = document.createElement("p");
  note.textContent = text(report ? `fusion_${report.state}` : "fusion_unavailable");
  const table = document.createElement("table");
  table.style.fontSize = "12px";
  table.style.width = "100%";
  const head = document.createElement("tr");
  for (const label of [text("quality_candidate"), "N", "AVG ms", "P99 ms", "MAX ms", ">50ms"]) {
    const th = document.createElement("th");
    th.textContent = label;
    head.appendChild(th);
  }
  table.appendChild(head);
  for (const item of fusionRows(report)) {
    const row = document.createElement("tr");
    if (item.active) row.style.fontWeight = "bold";
    for (const label of [`${item.active ? "● " : ""}${text(item.id ? "fusion_gpu" : "fusion_cpu")}`, ...item.cells]) {
      const cell = document.createElement("td");
      cell.textContent = label;
      row.appendChild(cell);
    }
    table.appendChild(row);
  }
  const controls = document.createElement("p");
  for (const mode of [0, 1]) {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = text(mode ? "fusion_gpu" : "fusion_cpu");
    button.disabled = report?.change_allowed !== true || report?.effective === mode;
    button.addEventListener("click", async () => {
      button.disabled = true;
      try {
        await globalThis.setParam("JetsonInputFusion", mode);
      } catch {
        if (typeof globalThis.showAppToast === "function") globalThis.showAppToast(text("fusion_change_failed"));
      }
    });
    controls.appendChild(button);
  }
  const failures = document.createElement("p");
  failures.textContent = `${text("fusion_failures")}: ${Number.isInteger(report?.failed) ? report.failed : "--"}`;
  const scope = document.createElement("p");
  scope.textContent = text("fusion_scope");
  const wrap = document.createElement("div");
  wrap.style.overflowX = "auto";
  wrap.appendChild(table);
  element.replaceChildren(title, note, wrap, controls, failures, scope);
}
