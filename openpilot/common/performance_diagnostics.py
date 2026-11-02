"""Optional performance instrumentation; scheduling and health are independent."""
import os

# Private PC/device diagnosis only. Production does not collect/display samples.
# Read once at process startup, never from a model or rendering loop.
ENABLED = os.environ.get('CARROT_PERF_DIAGNOSTICS') == '1'
