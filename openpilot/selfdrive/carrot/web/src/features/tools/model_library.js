"use strict";
const EN = {
  title: "External driving model", internal: "Internal model: CD210 (fixed)",
  models_title: "Driving models", internal_title: "Internal driving model",
  internal_next: "Internal model for the next boot",
  internal_saved: "Saved. Reboot comma to apply.",
  internal_preparing: "Preparing the internal model",
  internal_fallback: "Using CD210 as the fallback",
  help: "One choice for eGPU and Jetson. Choosing saves the next-boot selection. Reboot comma and Jetson while parked with ignition off and internet available. First use downloads files; Jetson also prepares its engine.",
  change: "Change model", close: "Close", pending_apply: "Apply after restart",
  current: "This boot", next: "Next boot", saved: "Saved. Reboot comma and Jetson to apply.",
  noo: "NOO supported", no_noo: "NOO not supported", shared: "N16 supports navigation inputs; Flip Flop, CTV3M, Light speed and Light speed v2 do not. Model changes take effect after reboot.",

};
function t(key) { return typeof getUIText === "function" ? getUIText(`model_library_${key}`, EN[key]) : EN[key]; }
function text(tag, value) { const e = document.createElement(tag); e.textContent = value; return e; }
function modelSelect(labelText, models, selected, optionText) {
  const label = text("label", labelText);
  const select = document.createElement("select");
  select.setAttribute("aria-label", labelText);
  for (const model of models) {
    const option = text("option", optionText(model));
    option.value = model.id;
    select.append(option);
  }
  select.value = selected;
  label.append(select);
  return { label, select };
}

export function renderLibrary(library, force = false) {
  if (!library?.models) return;
  const root = document.getElementById("externalModelLibrary");
  if (!root) return;
  root.hidden = false;
  const dialog = document.getElementById("modelLibraryDialog");
  const editor = document.getElementById("modelLibraryEditor");
  // Keep the submitted choice stable until its response arrives.
  if (editor.dataset.saving === "true") return;
  const internal = library.internal_model;
  const internalName = internal?.models.find(m => m.id === internal.effective)?.name || library.internal || "CD210";
  const effective = library.models.find(m => m.id === library.effective);
  const selected = library.models.find(m => m.id === library.selected);
  const effectiveName = effective?.name || library.effective_name || library.effective;
  if (!root.querySelector("button")) {
    const summary = text("div", "");
    summary.className = "model-library__summary";
    summary.append(text("strong", ""), text("p", ""));
    const button = text("button", "");
    button.id = "btnChangeModel";
    button.type = "button";
    button.className = "btn";
    button.setAttribute("aria-haspopup", "dialog");
    button.setAttribute("aria-controls", "modelLibraryDialog");
    button.addEventListener("click", () => dialog.showModal());
    root.append(summary, button);
    document.getElementById("btnCloseModelLibrary").addEventListener("click", () => dialog.close());
  }
  root.querySelector("strong").textContent = selected?.name || library.selected;
  const card = document.getElementById("egpuModelCard");
  const internalSummary = internal
    ? `${t("internal_title")} · ${internalName}${internal.reboot_required ? ` · ${t("pending_apply")}` : ""}`
    : t("internal");
  const state = card && !card.hidden ? document.getElementById("egpuModelState").textContent : internalSummary;
  const amount = card && !card.hidden ? document.getElementById("egpuModelAmount").textContent : "";
  root.querySelector("p").textContent = library.reboot_required
    ? `${t("pending_apply")} · ${t("current")} ${effectiveName} · ${state}`
    : [state, amount].filter(Boolean).join(" · ");
  root.dataset.state = card?.dataset.state || "";
  root.querySelector("button").textContent = t("change");
  document.getElementById("modelLibraryTitle").textContent = t(internal ? "models_title" : "title");
  document.getElementById("btnCloseModelLibrary").textContent = t("close");
  const signature = JSON.stringify([library.models, library.selected, library.effective,
    library.effective_name, library.effective_noo, library.reboot_required, internal,
    t(internal ? "models_title" : "title")]);
  if (!force && editor.dataset.signature === signature) return;
  editor.dataset.signature = signature;

  const externalChoice = modelSelect(t("next"), library.models, library.selected,
    model => `${model.name} · ${t(model.noo ? "noo" : "no_noo")}`);
  const effectiveNoo = effective?.noo ?? library.effective_noo;
  const children = [
    internal ? text("h3", t("title")) : text("p", t("internal")),
    text("p", t("help")),
    text("p", `${t("current")} ${effectiveName} · ${t(effectiveNoo ? "noo" : "no_noo")}`),
    externalChoice.label,
    text("p", t("shared")),
  ];
  if (library.reboot_required) children.push(text("p", t("saved")));

  const choices = [{ ...externalChoice, action: "select", saved: "saved" }];
  if (internal) {
    const internalChoice = modelSelect(t("internal_next"), internal.models, internal.selected, model => model.name);
    children.push(text("h3", t("internal_title")), text("p", `${t("current")} ${internalName}`));
    if (internal.state === "preparing") children.push(text("p", t("internal_preparing")));
    if (internal.state === "fallback") children.push(text("p", t("internal_fallback")));
    children.push(internalChoice.label);
    if (internal.reboot_required) children.push(text("p", t("internal_saved")));
    choices.push({ ...internalChoice, action: "select_internal", saved: "internal_saved" });
  }
  editor.replaceChildren(...children);

  for (const choice of choices) {
    choice.select.addEventListener("change", async () => {
      if (editor.dataset.saving === "true") return;
      editor.dataset.saving = "true";
      for (const { select } of choices) select.disabled = true;
      let nextLibrary = library;
      try {
        const response = await postJson("/api/egpu/model/library", { action: choice.action, model: choice.select.value });
        nextLibrary = response.library;
        if (typeof showAppToast === "function") showAppToast(t(choice.saved));
      } catch (error) {
        if (typeof showAppToast === "function") showAppToast(error?.message || String(error), { tone: "error" });
      } finally {
        delete editor.dataset.saving;
        renderLibrary(nextLibrary, true);
      }
      if (nextLibrary !== library) globalThis.CarrotEgpuModel?.refresh();
    });
  }
}
