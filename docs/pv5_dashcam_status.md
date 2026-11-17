# PV5 dashcam status display

PV5 offroad home and stationary dashcam camera views show a read-only status panel.
Other vehicles keep their existing home layout. Settings, emergency alerts and SCC
HUD popups retain foreground priority. Moving, active-control, stale-state and stock
AEB/FCW conditions suppress the camera-view panel. This only governs display;
no upload admission, CAN, braking, steering or warning timing changes are made.

- Preparation/transfer activity observes the uploader's existing Linux flock through
  `/proc/locks`; it never acquires that lock. It cannot distinguish file preparation
  from bytes on the network, or identify the exact reason for an idle worker.
- Log and full-transfer progress count receiver-acknowledged entries in the two
  existing catalogs, matching directory inode/mtime against recordings currently
  retained on the device. Full transfer covers all available eligible road files;
  it does not imply that every segment originally contained a video. Video-only
  completion does not count as an rlog. Partially sent files are not completed.
- Percentages are segment counts, not bytes, estimated time or PC analysis progress.
  New recordings and normal deletion can change the denominator. An open recording
  can be included in the denominator and remains pending until closed and sent.
- SQLite is read-only/query-only (20 ms lock timeout, bounded query/size/row limits).
  Metadata collection runs on one nice-19 background thread, at most every 15 s
  while the panel is visible. Movement/alerts revoke its scan request. No media is
  decoded, no private configuration is read and no AI or network request is made.
- Unknown/locked/oversized catalogs and snapshots older than 45 s are not shown as
  zero or completed. The panel includes the snapshot age. A sample is not a live
  network health check.
- Power uses the existing hardwared `deviceState.powerDrawW` measurement in watts.
  CPU/GPU values are the maximum valid temperature reported in each existing list.
  Measurements older than 2 s, missing temperatures and unsupported/zero power
  are shown as an em dash. Recording status describes manager process state, not
  a guarantee that frames reached storage.

Validation: `test_pv5_dashboard.py` covers real Linux flock observation, read-only
catalogs, changing directories, missing/busy data, async rendering, freshness,
movement/emergency suppression and both screen sizes. PC layout previews use the
production render function with a drawing adapter, not a device GPU screenshot.
Deployment follows commit/push, exact published Git pull, and file-hash verification.
