# PV5 selective Carrot sync — 2026-11-14 KST

Baseline: `0523f3765dbeb04d9aabc2c50ecc5061697efcb7`. Reviewed upstream
`ajouatom/openpilot:carrot-wip` through `16df8e63c06988171d62abef608e4a6959ccd10a`.
This is a selective port, not a branch merge.

| Upstream | Decision |
| --- | --- |
| f89f40a37 | Exclude Ioniq 5 PE B1 firmware/manual/model indicators; no PV5 scope expansion or flashing. |
| e5f4ac8a1 | Adapt close slow-queue radar/vision handoff to explicit PV5-only runtime and replay gates. Preserve existing PV5 stationary shielding. Exclude another vehicle's private validation fixture. |
| 9c79e9bcb | Port compact model summary and details dialog, translations, generated assets and input-preservation tests. Do not import the absent assistant feature or unrelated verification endpoints. |
| 080366c3e | Exclude separate SD/NVMe automatic firmware image/build/deployment path; prerequisite image builder absent from PV5. No new firmware policy. |
| 51b6a4c5d | Port PC installer SD/SSD wording and repeated-boot power regression test. No new image or physical boot claim. |
| 3903ab988 | Exclude image-cache builder change with the separate firmware image path. |
| 33a9da829 | Port preparation phases, model warp-preparation status and bounded transition-only diagnostics. |
| d3268a253 | Read the complete existing status snapshot before sampling time, avoiding future-timestamp misclassification during atomic replacement. |
| 91eb1e86b | Preserve explicit-time callers while allowing `_fresh` to sample time after reading; retain PV5 `preparing` and snapshot APIs. |
| 977ebb826 | Port first/repeated PC preparation command-name correction. |
| 16df8e63c | Evidence-only upstream AGENTS record; do not import another owner's approvals or hardware results. |

## Behavioral limits

Close handoff requires distinct measured front-radar targets, a qualified vision
match, path/body overlap, distance at most 8m, inferred ego speed at most 5m/s,
and a continuous 0.20s sequence. ID changes, gaps over 0.10s, missing vision or
position discontinuity restart confirmation. Existing stricter handoffs remain.
The new constructor flag defaults OFF and only existing PV5 refinements enable it;
unknown/mixed replay metadata and SCC fallback retain their old policy.
This does not restore the removed staged cut-in braking or its repeated popups.

USB/network preparation receives a display-only 22s lease covering bounded setup;
normal reports retain 3s, HELLO 8s, and existing update preparation 35s. HUD frame,
model-result, control, CAN and driver-monitoring freshness are unchanged. Real
errors still override preparation. Transition diagnostics reuse the existing
logger, suppress identical repeats and bound error text to 300 characters.

The model dialog changes presentation only. Selection still saves the next-boot
choice; boot-frozen models, ownership tokens, updater permissions, hashes and
rollback remain. No worker, timer, subscription, inference pass or polling loop
is added. Extra scalar radar confirmation and transition logging have not had
physical CPU/RSS/latency measurements.

## PC verification and deployment limits

Focused radar/status/installer checks: 633 passed. Existing SCC/HUD/model/update
checks: 313 passed. Additional replay/daemon-ordering checks: 318 passed (overlap
between groups). Full web checks after restoring PC dependencies/sparse sources:
853 passed, zero failures/skips; the Python groups cover 1,209 unique cases. The initial web run had three environment errors;
a replay indentation error introduced during adaptation was found and corrected
before source publication. Final actual-log replay covers 1,199 frames; leadOne
and leadTwo selections are identical before/after in the owner's supplied case.
Do not claim this port improves braking in that particular case.

Synthetic mirrored close-offset cases confirm the new 0.20s handoff; default-OFF,
parallel, interrupted ID, missing vision and gapped observations are covered. A
graph of this fixture is not a vehicle trajectory or brake-response simulation.
The actual DOM renderer checks narrow/landscape layouts, native default/high
contrast, dialog viewport bounds and preserved focus during inventory refresh.
Carrot Web's native tokens are dark; a light preference is not a separate theme.
Visual inspection caught an undefined upstream surface-color token making the
dialog transparent; the component uses the existing `--md-surface-cont` token.

The NAS automatic workflow for upstream e5f4ac8a1 completed successfully; public
health identifies that exact source. The existing actual upload-result viewer and
its recalculated 1,199-frame/seven-series response returned HTTP 200. This verifies
the upstream NAS deployment, not that the complete PV5 branch runs on the NAS.
No manual NAS restart, vehicle access, install, reboot or firmware flash occurred.
Unknown periodic load warning and physical HUD disconnect cause remain separate.

Five model-paired bundles must retain neural/runtime identity and pass isolated
native checks plus public size/hash verification before pinning. Detailed receipts
and sanitized PC plots are in the companion workspace task directory.

## Verified paired publication

Five isolated native bundle checks passed; all 20 public assets were downloaded
and matched by complete SHA-256 and byte size before pinning. Model/runtime
policy fields match the baseline exactly; only profile/source/bundle locators change.

| Model | Package source | Public assets verified |
| --- | --- | --- |
| n16 | `d56e5bfd9bb2a48215089b4b0c15b8559402eb39` | 4 |
| flip-flop | `0b830a04a42f899b984589c15fd1ead5409007b0` | 4 |
| ctv3m | `3a103f742e267dd870a5aef4cbfbe2cfc046fa82` | 4 |
| light-speed | `c8dfaa183683ed521e4ca96db027956ca5adf4a8` | 4 |
| light-speed-2 | `51ac6bc9b6838ebe36e0b12941b36d083a7e6010` | 4 |

In an additional public-matcher stationary fixture (existing PV5 stationary
shield enabled), closer-object selection moved from 0.25s to 0.20s. This
function-level PC input is not a controller/CAN or physical braking measurement.
