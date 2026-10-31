# Upstream Light speed incident follow-up

**2026-10-31 follow-up:** the supplied Light speed eGPU recording misses the
50 ms loop budget and repeatedly invalidates camera odometry/locationd inputs.
See [the measured incident and upstream revert review](light_speed_locationd_review_20261031.md).
PR39184 restores the existing Flip Flop pair already retained here; it does not
supply a replacement Light speed runtime pair. Earlier desktop/CI compatibility
results are not evidence of physical eGPU throughput.

This is an upstream USB AMD eGPU investigation, not a PV5 Orin measurement.
PV5 retains its four model-specific packages and the approved Light speed pair; see
[PV5 Light speed integration](pv5_light_speed_20261101.md). The earlier upstream
signed-bundle delivery/CI record is not adopted as a PV5 release record.
