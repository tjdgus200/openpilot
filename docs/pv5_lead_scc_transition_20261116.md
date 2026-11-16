# PV5 target takeover and SCC entry transition — PC validation

Baseline: 4c95cda2e05651c445c3de065204822e548e6b19. Local branch: codex/pv5-lead-scc-transition.
This change is implemented and PC-tested, not pushed or installed. No vehicle connection, service restart, tuning activation, or live safety qualification was performed.

## Behavior

- PV5 refinement path only: when a fresh coherent nearer visual lead exists, require 0.20 s of continuous evidence before a nonurgent, >2 m farther measured front-radar target replaces it. Every output uses the current visual sample; no stale radar target is held. Confirmation restarts after candidate identity/continuity loss.
- Closer targets, rapid closing (TTC <=5 s or required constant deceleration >=0.5 m/s²), lead braking <=−1 m/s², nonfinite geometry, lost/uncertain vision, SCC/corner sources bypass this added confirmation. These are conservative bypass thresholds, not new safety detector definitions.
- Apply confirmation only to the final leadOne output, after the existing cut-in/cut-out and auxiliary target pipeline. Any current cut-in/risk candidate or cut-out confidence bypasses it. Internal detector inputs and leadTwo/risk outputs remain unchanged.
- Existing default-OFF, qualified PV5 SCC assist: prepare before the hard −2.0 m/s² raw-request threshold using a smoothstep blend in −1.5..−2.0 m/s². Preparation alone is limited to 5 m/s³; a stronger comma request bypasses that downward limit. An admitted raw request <=−2.0 uses the original immediate stock path in the same call. Do not slew-limit that urgent entry to make a graph smoother.
- Preparation may introduce earlier mild braking and active feedback within this band. If the OEM request retreats, release uses the existing 1 m/s³ bound without entering the stock latch. Invalid source/driver admission still clears immediately. A weak request >=−1.5 cannot start fresh assistance.
- Preserve original CRC/counter/source age, driver/pedal/CAN gates, acceleration clamps, stationary hold, gas-override handling, CAN jerk fields, aReqValue slew, Panda code and control mismatch monitoring. DECEL_JERK remains the existing CAN jerk-limit constant; it is not an unused constant.

## Validation and evidence

Final suite: 4,023 passed, 0 failed, 2 skipped. The two skips are optional native CAN extension counter comparisons; the Python CAN parser/packer and existing native Panda checks ran. General test_hyundai.py was not collected because hypothesis is unavailable. Three existing pytest plugin-configuration warnings remain. Test Params is an in-memory substitute, not a live device configuration.

Historical target replay: two existing 60-second records, three modes (1/2/3), 7,197 replayed frames. The problem neighborhood changes from visual→radar→visual→radar to visual→radar (3 transitions→1). The final sustained farther takeover waits about 202 ms; this deliberate nonurgent wait is not described as zero delay. All non-leadOne output fields match baseline at all frames. Changed leadOne frames: 11 per mode in the first record, 5 per mode in the second, always a current nearer visual lead replacing a farther radar output. The nearer-target switch near 55.9 s and side-target case near 23.5 s retain their timing. Full-minute numeric history is used for tracker state; no new video interpretation was performed.

SCC replay uses recorded carControl timestamps, original received camera CAN through CameraSccSource (CRC/counter/freshness), recorded available admission fields, and fixed recorded planner inputs. Missing wheel-speed/paddle/live-TCS admission details are not reconstructed: they are an explicit healthy-input assumption for arbitration replay and separately checked in fixtures. It is not a closed-loop vehicle replay or an exact reproduction of the old recorded carOutput scheduling.

| Fixed-input SCC case | Entry command step before | After | Urgent weakened ticks |
|---|---:|---:|---:|
| active0 | 0.868678 m/s² | 0.127418 m/s² | 0 / 142 |
| active1 | 1.753265 m/s² | 1.647382 m/s² | 0 / 204 |

At both entry calls the final urgent command equals baseline. The second event has too little preparation time, so most of its step intentionally remains. The disabled/driver-braking case has identical output at all 3,592 ticks. These are command differences, not measured jerk, passenger comfort or emergency stopping-distance results. Recording source commits be8c40120650fa234bfc0ecbd85682d6647eb0be and 8644216bef268c16e2c82cb694f0e01cb4db3476 are distinct from uploader commits and the implementation baseline.

Additional regressions exercise sudden closer/urgent radar, lost/weak vision, irregular time, nonfinite values, source/counter/CRC rejection, preparation abort, stronger base braking, and preparation→urgent source→controller→CAN packets. No added tick of delay occurred in the tested urgent cases; real ECU scheduling and actual cut-in/emergency braking behavior remain unvalidated.

Initial failures were retained in task artifacts: removed DECEL_JERK broke three imports and was restored; one threshold expectation was updated for the explicit pre-entry behavior. Broader harness initially lacked Params put_int (50 failures) and two generated DBCs (6 failures); these were fixed only in the test environment, then rerun. No product logic was weakened to accommodate those failures.

## Artifacts

Task directory: A:/PV5/work/lead-scc-transition-20261116
- final-tests.xml and run_suite.py: final test evidence and invocation
- scc-replay-summary.json / replay_scc.py: bounded numerical replay summary and method
- lead-pipeline-parity.json / verify_pipeline.py: exact non-primary output comparison
- comparison.png: recorded-input before/after graph
- completion.json: scoped completion evidence and source hashes

No code commit/push, device install/reboot, operational policy edit, new instrumentation, or original data deletion is included. Required next validation before claiming vehicle behavior is resolved: isolated full-controller/ECU timing and controlled vehicle tests, particularly abrupt threshold crossing, aborted preparation, and genuine cut-in cases. Never suppress real hard braking merely to eliminate the remaining step.
