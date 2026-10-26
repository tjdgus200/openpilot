# PV5 daily source sync, 2026-10-27

Fixed upstream: `c1a28aff4bc0fc9ec7299ab01a0fb4f5c4a4cf43`.
Starting target: `cb94b635d400d9d43f9d4ddddb1da01b74227e99`.

The complete tree comparison initially had 63 differing paths. Production
differences belong to the six retained PV5 features and the owner's subsequent
OBD reception, brightness and unverified charging-observation instructions.
The remaining paths are their tests, packaging, CI coverage and documentation.
Driving, CAN/DBC, planning, cereal, model contracts/binaries, manager/process.py,
and USB/AIO/SHM/parser/watchdog implementations match the fixed upstream.
The existing signed manifests also match upstream; the additional PV5 SHA-256
manifest remains the selected package. None of the six protected files changes.

Fully imports `4ad26a8d2a10c2c79ffc05489166953aa9929f50`: qcomgpsd uses the
existing spawn context to avoid inheriting the manager's IPC state. TICI and
onroad/external-GPS conditions, supervision and the separate PV5 OBD enrollment
predicate remain. Its real-child tests cover crash/restart, duplicate starts,
stop and fork-state inheritance using a harmless worker, not a modem.

Partially imports `c1a28aff4bc0fc9ec7299ab01a0fb4f5c4a4cf43`: the upstream
review document and its validation provenance. That document describes the
official branches' power policy and historical test results; it does not set
PV5 policy or establish PV5 test results. Its AGENTS.md addition is excluded
because rejection of unconditional input cutoff conflicts with this owner's
independent-power requirement. Similarly, the empty-port-only part of
`5d2437da6ce221b682595876b86dff96051612f1` remains excluded; its recovery/parser
changes are already present. Older applied records were checked for duplicate
work; the previous run's applied list is empty. No older change is reapplied
merely because Git lacks a merge ancestry link.

Retained behavior:

1. BSD physical square corners, 72px inner joins, fading rails/halo, alpha96,
   fresh OEM OR local ONNX, alerts last; steady without a same-side turn request,
   two frames on/two off at 10fps with one. The October 27 brightness override
   covers dark pulse frames and returns to automatic brightness on expiry/end.
2. W at a 0.5s sampling target, temperature and CPU/MEM/GPU/inference diagnostics,
   stale/missing suppression, outside the inference path.
3. Battery mode8, validated passive profile and separately enrolled BLE reader,
   bounded sequential reads, per-field expiry and unverified heatmap provenance.
   No automatic field confirmation, compressor guess or new CAN command.
4. Unconditional USB_ICL cut at early normal launch and before Jetlink, with
   callback reassertion. No role/VBUS change or boot-ROM isolation claim.
5. Offroad OR fresh valid Park/zero update permission, no running onroad engine
   replacement, permission-loss suspension, integrity checks and rollback.
6. Existing own package source `b624a1c2e184579f6f6dc58f7e9edb42c1eb1aa1`,
   64805205 bytes, SHA-256
   `83c0e0217694945b7f294ab2ed5aa07a745a131a29fece38724499219849ee4a`.

The GPS change requires no new Jetson host package, model or wire protocol.
Official Wi-Fi, normal automatic brightness/offroad gates, local ONNX and
same-request AIO remain; retired PV5 async writers and Wi-Fi HUD stay absent.
PC validation uses the owner-side validator and additional Linux lifecycle,
OBD/heatmap, screen-settings and update tests. Exact results are recorded by
the daily runner, not inferred from the imported upstream review. No device
connection, installation, restart, charging calibration, live timing, long-term
stability or driving-safety validation is performed by this sync.
