export function scannerCandidates(devices = [], selected = null) {
  return [...devices].sort((a, b) => Number(b.address === selected) - Number(a.address === selected) ||
    Number(isObdCandidate(b)) - Number(isObdCandidate(a)) || (b.rssi ?? -999) - (a.rssi ?? -999) ||
    String(a.address).localeCompare(String(b.address)));
}
export function isObdCandidate(device) {
  return /obd|elm|vlink|veepeak|jxsl|jinxu/i.test(device.name || "") ||
    (device.uuids || []).some(uuid => String(uuid).toLowerCase() === "e7810a71-73ae-499d-8c15-faa9aef0c3f2");
}
export function connectionLabel(obd = {}) {
  if (obd.configError) return "configuration_error";
  if (!obd.registered) return "not_registered";
  if (obd.fresh && ["receiving", "diagnostic_receiving"].includes(obd.state)) return "receiving";
  return ["connecting", "unavailable", "disconnected", "stopped"].includes(obd.state) ? obd.state : "waiting";
}
