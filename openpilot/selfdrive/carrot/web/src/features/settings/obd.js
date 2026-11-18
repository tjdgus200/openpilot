import { scannerCandidates, isObdCandidate, connectionLabel } from "./obd_model.js";

const words = {
  ko: { title: "OBD2 스캐너", close: "닫기", on: "블루투스 켜기", scan: "스캐너 검색", scanning: "검색 중… (최대 30초)",
    help: "정차·오픈파일럿 해제 상태에서 스캐너를 선택하세요. 다른 휴대폰 앱에 연결돼 있다면 먼저 해당 연결을 해제하세요.",
    compatibility: "PV5 배터리 리더는 JINXUSOLU JXSL-MIC34LE의 BLE 통신을 지원합니다. 이름만으로 호환성을 확정하지 않으며, 연결 시 장치를 확인합니다. 일반 ELM327·클래식 블루투스 기기의 동작은 보장하지 않습니다.",
    blocked: "정차·오픈파일럿 해제 상태가 확인되어야 설정할 수 있습니다.", unavailable: "블루투스 어댑터를 확인하세요.",
    empty: "검색된 장치가 없습니다. 스캐너 전원을 확인한 뒤 검색하세요.", connect: "선택·연결", forget: "등록 해제", registered: "등록된 스캐너",
    replace: "다른 스캐너를 사용하려면 현재 등록을 먼저 해제하세요.", confirm: "스캐너 등록을 해제할까요? 이 스캐너의 수신과 저장된 필드 확인 설정이 해제됩니다.",
    not_registered: "스캐너 미등록", waiting: "등록됨 · 데이터 수신 대기", connecting: "스캐너 연결 확인 중", receiving: "데이터 수신 중",
    disconnected: "연결 끊김 · 재연결 대기", stopped: "리더 정지 · 차량 실행 조건 대기", configuration_error: "등록 설정을 읽을 수 없습니다. 기존 설정은 보존됩니다.",
    failed: "연결·통신 확인 실패 · 스캐너 전원, 다른 앱 연결 및 호환성을 확인하세요.",
    reader: "등록한 스캐너는 실제 PV5 차량 실행 시 기존 리더가 자동 연결합니다. 데이터 수신과 센서값 보정 확인은 별개입니다.",
    candidate: "OBD 후보", other: "기타 블루투스 장치 · 호환성 미확인", unnamed: "이름 없는 장치", bluetooth: "블루투스 연결됨",
    error: "요청을 처리하지 못했습니다. 상태를 새로 확인해 주세요.", refresh: "새로고침", saved: "등록했습니다. 실제 연결 상태를 확인 중입니다." },
  en: { title: "OBD2 scanner", close: "Close", on: "Enable Bluetooth", scan: "Scan scanners", scanning: "Scanning… (up to 30s)",
    help: "Select a scanner while stationary with openpilot disengaged. Disconnect it from other phone apps first.",
    compatibility: "The PV5 battery reader supports JINXUSOLU JXSL-MIC34LE over BLE. Identity is checked on connection; a name alone does not prove compatibility. Generic ELM327 and classic Bluetooth devices are not guaranteed to work.",
    blocked: "Setup requires a fresh stationary, disengaged state.", unavailable: "Check the Bluetooth adapter.", empty: "No devices found. Power on the scanner and scan again.",
    connect: "Select and connect", forget: "Remove registration", registered: "Registered scanner", replace: "Remove the current registration before selecting another scanner.",
    confirm: "Remove this scanner registration, including its saved field confirmations?", not_registered: "No scanner registered", waiting: "Registered · waiting for data",
    connecting: "Checking scanner connection", receiving: "Receiving data", disconnected: "Disconnected · waiting to reconnect", stopped: "Reader stopped · waiting for vehicle operation",
    configuration_error: "Cannot read registration. Existing configuration is preserved.", failed: "Connection or communication failed. Check power, other app connections and compatibility.",
    reader: "The existing reader automatically connects during actual PV5 vehicle operation. Receiving data does not confirm sensor calibration.", candidate: "OBD candidate",
    other: "Other Bluetooth device · compatibility unknown", unnamed: "Unnamed device", bluetooth: "Bluetooth connected", error: "Request failed. Refresh the status.", refresh: "Refresh", saved: "Registered. Checking actual connection status." },
};
const t = key => words[typeof LANG !== "undefined" && LANG === "ko" ? "ko" : "en"][key] || key;
const el = (tag, text, cls) => { const node = document.createElement(tag); if (text) node.textContent = text; if (cls) node.className = cls; return node; };
let active;
async function api(operation = "obd", payload) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), operation === "radio" ? 70000 : 15000);
  try {
    const response = await fetch(`/api/bluetooth/${operation}`, { signal: controller.signal, cache: "no-store", ...(payload !== undefined ? {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
    } : {}) });
    if (!response.ok) throw new Error(response.status === 409 ? t("blocked") : t("error"));
    return await response.json();
  } finally { clearTimeout(timeout); }
}
export function openObdSettings() {
  if (active?.open) return;
  const dialog = el("dialog", null, "obd-dialog"); active = dialog;
  dialog.setAttribute("aria-label", t("title"));
  let state = null, busy = false, timer, disposed = false, scanUntil = 0, signature = "";
  const header = el("header"), heading = el("h2", t("title")), close = el("button", t("close"), "btn"); close.type = "button"; close.onclick = () => dialog.close();
  header.append(heading, close);
  const status = el("p"), error = el("p", null, "obd-error"), notice = el("p"), actions = el("div", null, "obd-actions"), list = el("div", null, "obd-list"), registration = el("div");
  status.setAttribute("role", "status"); error.setAttribute("role", "alert");
  function button(label, action, mutation = true) {
    const b = el("button", label, "btn"); b.type = "button"; if (mutation) b.dataset.obdMutation = "1";
    b.onclick = () => run(action); return b;
  }
  const radio = button(t("on"), () => api("radio", { enabled: true }));
  const scan = button(t("scan"), async () => { await api("scan", {}); scanUntil = Date.now() + 30000; });
  const refresh = button(t("refresh"), async () => {}, false); actions.append(radio, scan, refresh);
  function lock() {
    dialog.querySelectorAll('[data-obd-mutation]').forEach(b => { b.disabled = busy || !state?.runtime?.stationary || b.dataset.blocked === "1"; });
    refresh.disabled = busy;
    scan.disabled ||= !state?.radioEnabled || !state?.available || Date.now() < scanUntil || state?.adapters?.some(a => a.discovering);
    radio.hidden = state?.radioEnabled === true;
  }
  function render() {
    const obd = state?.obd || {};
    const scanning = Date.now() < scanUntil || state?.adapters?.some(a => a.discovering);
    scan.textContent = t(scanning ? "scanning" : "scan");
    status.textContent = state ? t(connectionLabel(obd) === "unavailable" ? "failed" : connectionLabel(obd)) : t("error");
    notice.textContent = !state?.runtime?.stationary ? t("blocked") : !state?.available ? t("unavailable") : "";
    const key = JSON.stringify([obd.registered, obd.address, obd.configError, state?.devices]);
    if (key !== signature) {
      signature = key; registration.replaceChildren(); list.replaceChildren();
      if (obd.registered) {
        const remove = button(t("forget"), async () => { if (confirm(t("confirm"))) await api("obd-forget", { address: obd.address }); });
        registration.append(el("strong", t("registered")), el("p", obd.address), remove, el("p", t("replace")));
      }
      const devices = scannerCandidates(state?.devices || [], obd.address);
      if (!devices.length) list.append(el("p", t("empty")));
      for (const device of devices) {
        const card = el("section", null, "obd-device");
        card.append(el("strong", device.name || t("unnamed")), el("div", device.address), el("small", t(isObdCandidate(device) ? "candidate" : "other")));
        if (device.connected) card.append(el("div", t("bluetooth")));
        if (device.address === obd.address) card.append(el("div", t("registered")));
        else {
          const select = button(t("connect"), async () => { await api("obd-connect", { address: device.address }); });
          select.dataset.blocked = obd.registered || obd.configError ? "1" : "0";
          card.append(select);
        }
        list.append(card);
      }
    }
    lock();
  }
  async function load() {
    try { const next = await api(); if (!disposed) { state = next; render(); } }
    catch (e) { if (!disposed) { state = null; render(); } throw e; }
  }
  async function run(action) {
    if (busy || disposed) return;
    busy = true; lock(); error.textContent = "";
    try { await action(); await load(); }
    catch (e) { if (!disposed) { error.textContent = e.message; try { await load(); } catch {} } }
    finally { busy = false; if (!disposed) lock(); }
  }
  dialog.append(header, el("p", t("help")), status, notice, error, actions, registration, list, el("p", t("compatibility")), el("p", t("reader")));
  dialog.addEventListener("close", () => { disposed = true; clearTimeout(timer); dialog.remove(); });
  document.body.append(dialog); dialog.showModal(); lock();
  const poll = async () => {
    if (disposed) return;
    if (!busy && !document.hidden) await run(async () => {});
    if (!disposed) timer = setTimeout(poll, 2000);
  }; poll();
}
export function installObdSettings() {
  document.addEventListener("click", event => { if (event.target.closest?.("#btnSettingsObd")) openObdSettings(); });
}
