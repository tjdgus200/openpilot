"""Coded critical/error routing only. Never changes in-car alert/control behavior."""
PROFILE = "critical-errors-only-v1"
FAULT_ALERTS = frozenset((
  "accFaulted", "actuatorsApiUnavailable", "calibrationInvalid", "cameraFrameRate", "cameraMalfunction",
  "canBusMissing", "canError", "commIssue", "commIssueAvgFreq", "controlsMismatch", "cruiseMismatch",
  "deviceFalling", "fanMalfunction", "locationdPermanentError", "locationdTemporaryError", "lowMemory",
  "modeldLagging", "outOfSpace", "overheat", "paramsdPermanentError", "paramsdTemporaryError",
  "posenetInvalid", "processNotRunning", "radarFault", "radarTempUnavailable", "relayMalfunction",
  "selfdrivedLagging", "sensorDataInvalid", "steerTempUnavailable", "steerUnavailable", "usbError",
  "vehicleSensorsInvalid",
))
CRITICAL_ALERTS = frozenset(("aeb", "stockAeb", "fcw", "stockFcw", "impactDetected",
                            "driverDistracted3", "driverUnresponsive3", "pv5SccDecel"))
REPORTABLE_ALERTS = FAULT_ALERTS | CRITICAL_ALERTS


def reportable(fact):
  if not isinstance(fact, dict):
    return False
  if fact.get("kind") == "process_exit":
    code = fact.get("code")
    return type(code) is int and -128 <= code <= 255 and code not in (0, -2, -15)
  return fact.get("kind") == "system_alert" and type(fact.get("code")) is str and fact["code"] in REPORTABLE_ALERTS


def archive_reviewable(summary):
  return (summary.get("status") == "analysis_failed"
          or bool(set(summary.get("error_codes", [])) & REPORTABLE_ALERTS)
          or any(row.get("kind") == "recorded_stock_aeb" for row in summary.get("candidates", [])))


def fault_key(summary):
  """Repeated diagnostic faults in the same recorded version add no new cause."""
  codes = set(summary.get("error_codes", []))
  if codes & CRITICAL_ALERTS or any(row.get("kind") == "recorded_stock_aeb" for row in summary.get("candidates", [])):
    return None  # Distinct urgent driving episodes retain the existing cooldown.
  faults = tuple(sorted(codes & FAULT_ALERTS))
  if faults:
    return (str(summary.get("source_commit", "unknown")), faults)
  if summary.get("status") == "analysis_failed":
    return ("PC_analysis_failed", str(summary.get("failure", "unknown")))
  return None
