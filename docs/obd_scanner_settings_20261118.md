# OBD2 scanner web settings

Entry: CarrotWeb → Settings → Device → OBD2 Scanner.

The dialog enables the existing radio, scans for 30 seconds, lists separate devices by name and address, and explicitly enrolls one selected target. Other Bluetooth devices remain visible with compatibility unknown. It does not pair/trust arbitrary devices or create a second OBD reader. The existing real-PV5 manager predicate, GATT/model/manufacturer/ELM checks and fixed battery queries remain responsible for actual collection.

All mutations retain the existing same-origin, JSON and fresh stationary/disengaged guard. Enrollment rechecks this guard after the BlueZ snapshot await. A second target cannot overwrite the saved target; repeat selection preserves confirmed fields. Exclusive mode-0600 configuration creation does not replace a concurrent enrollment. Removal affects only the selected OBD configuration and lets its existing reader/manager own teardown. HID configuration and radio state are preserved.

UI distinguishes registration, BLE connection and fresh matching OBD reception. Unknown/stale/other-target snapshots cannot show receiving. Failure snapshots now carry their existing session configuration identity, allowing the UI to attribute a failure without exposing raw diagnostic payloads. Calibration is not inferred from connection success.

PC validation (2026-11-18):
- API/storage: 4 passed, including existing HID API regression, cross-origin/content type, stale/moving rejection, post-await guard, calibration preservation and snapshot identity/age.
- Existing OBD battery/reconnect/status: 108 passed.
- Web settings: 250 passed (includes 2 new OBD model tests).
- Web build successful; actual Edge with fixture API at 390px and 1100px passed enable/scan/ambiguous candidates/select/waiting/receiving/guard/remove/close and no script errors. This is PC browser validation, not a live BLE or vehicle test.
- Initial environment failures: missing native Params pytest conftest, xdist defaults and jeepney; used isolated test scope and task-local dependency. Browser close assertion initially raced its asynchronous close event; waited for detach and passed.

Not committed/pushed/installed on the comma device in this task. No real adapter pairing, vehicle restart, remote-profile deployment or existing uploader changes. Prior remote profile and offload worktree changes remain separate.
