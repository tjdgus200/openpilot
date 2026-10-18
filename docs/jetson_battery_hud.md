# PV5 battery overview for Jetson HUD

Select **External HUD Screen Mode / ClusterHudScreenMode = 8** for the battery information panel. Mode 0 and existing navigation/report modes retain their behavior. Camera/3D driving information and vehicle alerts remain visible, and panel swapping still applies.

The implementation includes the page, bounded passive decoding, per-field expiry, five-minute temperature history and the existing Wi-Fi snapshot extension. **It does not include a validated PV5 signal profile, a live vehicle measurement, or a newly published Jetson deployment bundle.** Updating comma alone cannot replace the renderer in an already installed, pinned Jetson bundle.

## Displayed information

- SOC; maximum/minimum battery temperature and their difference.
- Total battery draw/regeneration (positive draw, negative regeneration).
- Cell voltage spread.
- Battery coolant inlet temperature, only after identifying the actual sensor.
- Explicit battery thermal mode and the vehicle's Battery Care electrical power.
- Maximum battery temperature change over approximately five minutes.

Pack voltage/current, auxiliary voltage and SOH are excluded from the screen. Intermediate pack voltage/current may be used on comma to calculate total power. That calculation is not Battery Care power. Thermal power is never obtained by subtracting arbitrary vehicle loads or by assuming all compressor power belongs to battery cooling. A nonzero power reading alone does not identify cooling versus heating.

## Data path and cost controls

`comma receive-only CAN -> low-priority HUD worker -> existing authenticated Wi-Fi snapshot -> Jetson renderer -> USB panel`

- No new inference messages, Cap'n Proto service, high-priority worker, Jetson CAN subscription or Bluetooth connection.
- No diagnostic requests, ISO-TP flow control, safety-rule changes or thermal-control commands.
- CAN reception opens only while page 8 is selected and an explicitly validated profile matches `carParams.carFingerprint == KIA_PV5`.
- Profile files are read at most every five seconds, limited to 8 KiB and 16 signals. No profile is enabled by default.
- Each HUD tick reads at most 16 queued CAN events, stopping admission after 2 ms. A single event is limited to 512 inspected frames. This is an admission budget, not a guaranteed worst-case execution time.
- At most 1 KiB per configured ECU reassembly; sequence mismatch and one-second reassembly timeout discard the incomplete reply. Returned TX echoes and invalid/over-one-second-old CAN events are excluded.
- Telemetry reports update at most once per second and add only nine bounded fields to the existing HUD snapshot. Intermediate voltage/current/cell inputs are not forwarded. The existing 10 FPS Jetson output setting is unchanged.
- Each metric retains its original receive age, expires after five seconds, and cannot be revived by a fresh unrelated field. Receiver and sender monotonic clocks need not share an origin; unmeasured network transit before receipt is not claimed as an exact latency measurement.
- The five-minute history retains at most 302 samples, requires a baseline 298–302 seconds earlier, and resets after an over-five-second temperature gap. No SOH estimate or healthy/faulty classification is generated.

## Before enabling a profile

On a parked vehicle, compare comma-visible receive traffic with Car Scanner disconnected and while it displays the battery sensors. Public Ioniq 5/EV6 signal definitions are research references, not PV5 verification. Confirm every bus, responding ECU, DID, payload length, offset, signedness, unit, scaling, power sign and sensor meaning against the actual vehicle. Do not rename an unidentified `Inlet Temp` as battery coolant temperature. Independently verify heating/cooling states and the Battery Care component boundary.

Use the bounded `tools/jetlink/capture_bms_visible_can.py` diagnostic for the candidate BMS addresses. It never polls a diagnostic ECU; no observed response does not prove the sensor is absent. Comma cannot see every vehicle bus, and a multi-frame reply may require an already connected external scanner to request and flow-control it. Keep raw captures local.

Install a reviewed JSON profile at `/data/jetlink/pv5-battery-profile.json` on comma only after validation. The repository intentionally supplies no enabled example or guessed PV5 offsets. Do not set a validation flag on an untested mapping just to populate the display.

## Profile format

The root fields are `version` (integer 1), `vehicle` (exactly `KIA_PV5`), `validated` (boolean true), a nonempty `validation_reference` identifying the reviewed local comparison, and `signals` (at most 16 unique names). Do not include VIN, credentials or raw capture contents.

Each signal specifies `name`, physical receive `bus` (0–7), 11-bit `response_id`, 16-bit `did`, zero-based `offset`, `size` (1/2/4 bytes), optional boolean `signed`, and optional numeric `scale`/`bias`. **Offsets start after the positive `62 DID-high DID-low` prefix**, not at the CAN or ISO-TP header. Integers are big-endian. Numeric value = decoded integer × scale + bias. A `thermal_mode` signal instead maps raw decimal strings through a `modes` object to `idle`, `cooling`, `heating` or `mixed`; do not invent a mapping from temperature.

For an unsigned bitfield, optional positive `mask` and nonnegative `shift` select `(raw & mask) >> shift` before mapping/scaling. Optional `invalid_raw` lists up to 16 verified unavailable/sentinel integers, tested before bitfield extraction. Identify those sentinels on the vehicle; an in-range number alone does not prove the sensor is reporting. Signed bitfield extraction, zero scales and unknown profile keys are rejected, so a misspelled conversion option cannot silently select a different interpretation.

Displayed names: `soc_percent`, `max_temp_c`, `min_temp_c`, `pack_power_kw`, `cell_delta_mv`, `coolant_inlet_c`, `thermal_mode`, `thermal_power_kw`. Temperature change is computed on comma. Optional internal names `pack_voltage_v`/`pack_current_a` and `max_cell_v`/`min_cell_v` derive total power/cell spread only when the two input timestamps agree within 100 ms. Internal values are not sent to Jetson. Explicit total-power/spread signals take precedence over derivation. Unknown and malformed fields cannot create a displayed value.

Switch to any other screen mode to close the passive receiver and discard its history. Returning to mode 0 restores the existing navigation/report selection. Removing the profile also disables reception at the next profile check; per-field expiry still applies during that interval.

## Verification and deployment limits

PC regression tests cover receive-only operation, invalid/stale/echo rejection, frame sequencing and timeout, bounded buffering, profile/vehicle/page gating, clock translation, per-field expiry, temperature-history warm-up/reset, missing values and vehicle-alert precedence. Fixtures deliberately use invented ECU IDs and DIDs and do not validate PV5 signal meanings.

PC verification on 2026-10-19: 208 focused battery/HUD/configuration/navigation/report regression cases passed. Four hidden-window renders checked dark/light synthetic and missing-data pages using the actual renderer. New battery files passed Ruff, and a changed-file comparison found no additional lint findings beyond 84 pre-existing findings in the larger touched modules. Korean/English user-documentation checks and generated settings Wiki checks passed (195 settings, 587 generated files, zero structural issues). The PC environment reports three existing pytest configuration warnings. None of these checks measures on-device CPU/GPU headroom or validates actual PV5 values.

The generated settings Wiki overview includes option 8 and its prerequisites from the localized catalog. This feature-branch change does not publish unmerged features into the official project's live Wiki. A compatible Jetson HUD bundle and an actual PV5 capture/comparison remain required before claiming working live vehicle values or unchanged driving latency.

Research references: [OVMS Ioniq 5/EV6 metrics](https://docs.openvehicles.com/en/latest/components/vehicle_hyundai_ioniq5/docs/index.html) and [Kia's Battery Care/electricity-use definitions](https://ownersmanual.kia.com/full_webhelp/CV1/2023/en_US/topics/t00067.html). Neither is a validated PV5 CAN mapping.
