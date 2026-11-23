"""Opt-in PV5 incident summaries. Never a model or vehicle-control input."""


def start_incident_reporting():
  # Default OFF: no SSL/network module, config file, subscriber or thread.
  try:
    from openpilot.common.params import Params
    if not Params().get_bool('Pv5IncidentReporting'):
      return None
    from openpilot.system.pv5_incidents.runtime import start_incident_reporting as start
    reporter = start()
  except Exception:
    return None
  if reporter is not None:
    try:
      from openpilot.system.pv5_incidents.memory_sender import start_memory_reporting
      start_memory_reporting(reporter)
    except Exception:
      pass  # Independent diagnostic. Keep the original reporter running on failure.
  return reporter
