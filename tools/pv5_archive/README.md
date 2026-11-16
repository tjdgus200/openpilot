# Private PV5 parked recording upload (default OFF)

This opt-in owner-PC path reuses the enrolled incident HMAC/HTTPS origin. It
does not use NAS route publication, publicly serve recordings, change recording
settings, alter vehicle controls/models, or enable inbound remote access.

## Eligibility and privacy

`Pv5IncidentReporting` remains the master opt-in. There must additionally be a
root/runtime-owned mode-0600 `/data/pv5-pc-archive.json` with exact version,
vehicle `KIA_PV5`, `enabled: true`, qualified modes, hashed trusted NetworkManager
Wi-Fi profile UUIDs, and the seven-module `module_identity()` digest. No key is
duplicated into this file. Unconfigured/mismatched code does not start a copier.

Qualify `detached_offroad` and `dashcam_stationary` separately on the actual
device before listing either mode. Home/offroad does not prove vehicle CAN
interlocks or onroad resource impact. Dashcam alone is not stationary proof.
Admission requires fresh existing state, cool/available resources, trusted
nonmetered Wi-Fi, two continuous seconds, and all mode-specific gates in
`archive_gate.py`. Cellular, ordinary unregistered hotspots, unknown/stale data,
movement, cruise, lateral/longitudinal control, AEB/critical alerts and system
disable events do not authorize bulk processing. This is separate from the tiny
legacy incident reporter's existing zero-speed policy and manual remote guard.

The sole transient nice-19/idle-I/O copier reads only closed existing road
segments without any logger/deleter lock or preservation hook. Existing recording
and deletion always win. No new onroad encoding/inference/recording is added.
The admitted observer reuses tombstoned's SubMaster; encoder segment changes wake
a completed stationary-dashcam scan without idle directory polling. Losing
admission closes the heartbeat pipe and kills only the copier's own reserved
process group, including its own short-lived nmcli/ffmpeg children. Already
submitted kernel I/O/packets cannot be recalled instantly.

Driver-camera files are excluded. Transmitted rlog copies preserve native
non-audio messages using the FULL cereal schema, dropping audio/microphone and
driver-camera encoded-media messages. Unknown schema/framing fails closed.
Container road video is probed; audio/data tracks are removed using video stream
copy (`-c:v copy -an -sn -dn`), never video reencoding. Elementary HEVC road files
have no audio container. Originals and recording settings remain untouched.
PC manifests require `privacy=no-audio-cabin-v1`; older raw copiers are refused.

## Explicit trial budgets and limitations

Detached-home-only configuration uses a 30-second HTTP timeout and up to
8 MiB/s transfer pacing. Permission EOF/freshness still terminates its dedicated
process group within the existing watchdog path; a longer network timeout does
not extend admission. Vehicle and mixed-mode configurations retain the original
6-second timeout and 256 KiB/s pacing. Real throughput depends on the link and
PC durable checkpoint latency, and is not promised to match the pacing ceiling.

On the observed AGNOS device, ffmpeg/ffprobe are absent and the OS runtime is
read-only. An optional native FFmpeg9.0.2 build lives only under
`/data/pv5-archive-tools/ffmpeg-9.0.2-8c3850283eb25fa0`. Its official source archive
signature and hash were checked before compilation; retain the source/build
manifest and LGPL notice. The build enables local file processing, the required
demuxers/parsers and MPEG-TS stream copy; network protocols are disabled.
`archive_sanitize.py` uses it only when no explicitly supplied or existing PATH
tool is available. Before execution it checks root ownership, directory/file
permissions, regular files, source identity, AArch64 ELF and each binary hash.
Missing optional tools retain the existing refusal behavior. No runtime download,
OS/global-PATH modification or recording change is introduced. The module digest
changes, so previous archive qualification configs must not silently carry over.

- One copier; 64-KiB authenticated/resumable chunks; 256 KiB/s average transfer.
- At most 4096 source segments, 32 entries/segment, 512 MiB/source file;
  2-MiB catalog. Completed immutable directories are cached.
- 64-MiB total generated privacy cache; at most 48 MiB/copy. Oversize, missing,
  reopened, deleted or corrupt sources are refused, never reported as uploaded.
- Copier: 300 wall seconds / 10 measured CPU seconds per burst, 12-second CPU
  rlimit and 192-MiB address-space cap, cores limited to the first four available.
  In-process CPU is paced to one second per twenty wall seconds. Helper CPU is
  counted when reaped, so this is not a strict subtree instantaneous CPU cap.
  Helpers inherit OS limits; actual peak RSS/CPU/I/O/cancel latency needs device
  measurement. ffmpeg reads at 0.25x, does not decode or reencode video.

Owner's2026-11-15 home exception: a config qualified **only** for
`detached_offroad` has no copier CPU/address-space/first-four-core limit or
CPU pacing, and home admission ignores CPU/RAM/free-percent numeric thresholds.
Thermal-green, real disconnection/stopped-process/freshness, trusted unmetered
Wi-Fi, permission EOF/expiry and the300-second resumable work burst remain.
File/chunk/cache/PC storage bounds remain. A vehicle or mixed-mode config keeps
all original resource limits. This does not qualify the vehicle mode.
- PC default 50-GiB reserved archive budget (including production per-segment
  analysis allowance and metadata), 10-GiB free floor, four ingress connections,
  configurable 1..8 analysis processes (default1), each 384-MiB analysis memory / 95 CPU seconds / 210 wall seconds.
  Completed originals are not automatically deleted. Storage pressure applies
  backpressure; the device's normal deleter may remove pending originals.

“All” means available eligible recorded road files, not guaranteed lossless
retention. Recording disabled, missing rlog, incomplete windows, erased files,
privacy/resource limits, unplugging or PC unavailability can leave gaps. No new
retention lock, unlimited spool, full-device RSS guarantee or safety proof is
implied. Cache corruption/schema mismatch fails closed, with local status.

## PC completion events and analysis

Owner risk-first policy (`risk-first-v1`): a token-free PC pass scans logs and
keeps same-radar-track distance/relative-speed discontinuities, a substantially
closer new target, rapid approach, meaningful abrupt/hard measured deceleration,
fresh qualified original SCC requests <= -3 m/s², recorded AEB, and system
disable events. SCC request acceleration is not measured deceleration; track
switches are not labelled radar defects. These are provisional review thresholds,
not vehicle-control settings or validated hazard boundaries.

Automatic AI dispatch additionally requires the `critical-errors-only-v1`
allowlist: coded critical warnings, faults, recorded stock AEB, or analysis
execution failure. Numeric radar/deceleration candidates remain available for
manual investigation but do not independently wake AI. Repeated same-version
faults are suppressed. Small acceleration noise, ordinary cruise releases and
mild SCC assist alone do not wake AI. Selected evidence windows cover +/-5 seconds, merge up to 20 s,
and are limited to eight windows per segment. Missing fields and capped windows
remain explicit in private results; parser failures remain visible operational
failures. No visual inference is run over ordinary recordings. AI review uses
the selected road-video/data windows and proposes explanations/improvements
only; video time alignment must be checked before causal claims.

`store.py` reserves space before accepting a manifest, verifies chunk-chain
integrity and fsync checkpoints, then seals each file. A sealed rlog can enter early data screening; full-media review requires all
files in its manifest to be sealed. Duplicate/lost ACKs resume without
fabricating faults. Private media root must be provisioned owner/SYSTEM-only;
the receiver must verify that ACL before enabling archive ingress.

`service.py` launches a bounded PC-only child on completion. `analyze.py` streams
the full schema (not opendbc's reduced CAN schema), counts topics, uses actual
service timestamps rather than initData boot time, and preserves recorded source
commit separately from uploader version. Initial review candidates: acceleration
<= -3 m/s², negative jerk <= -5 m/s³ while moving, cruise release, recorded AEB,
system-disable codes, recorded TTC <2 seconds and SCC assist activity. These
are heuristic review thresholds, not validated hazard/defect boundaries.
The first pass does not run visual semantic inference. Missing services, source
identity, road video and failed analysis are explicitly incomplete.

Normal uploads create no chat. Only the critical/error policy above can wake the
existing analyst chat with opaque result IDs, at most three per five
minutes and same-cause thirty-minute cooldown. Durable uncertain dispatches are
held, never blindly resent. Dispatch-time fault identities are stored separately
from mutable analysis results, so early failures do not masquerade as a later
fault already reviewed. Legacy history is used for fault deduplication only when
its stored cause still matches the result. An uncertain existing intent for the
same segment remains held for receipt reconciliation even if analysis changes.
Future analysis may propose improvements but cannot
edit/tune/commit/push/install/reboot. Discord receives only a short reviewed
cause/proposal/missing-data summary through `report.py`; actual POST+GET receipt
verification is required before completion. No media, raw logs, location, device
identifiers or credentials belong in those reports.

## PC checks and activation

Run isolated `python -m unittest discover -s tools/pv5_archive/tests` on Linux
for native group cancellation/source-deletion checks. Run privacy/service tests
on Windows with ffmpeg/ffprobe, pycapnp and zstandard for native AAC removal,
identical video-stream hash, untouched originals and bounded analysis-child
checks. No fixture goes into the production inbox. Existing incident tests must
remain green. PC tests are not device/vehicle safety or performance validation.

The current release intentionally contains no device activation installer and
does not create its private qualification config. An expired home/vehicle
session cannot be reused. Finish PC review and publication first, then use a
new explicit owner manual session for device version/tool inspection, bounded
resource/interlock qualification, and only then the separately qualified config.


## Log-first PC screening and detached-home fast copies

The PC can screen a sealed rlog while its road-video files are still uploading.
Only the verified log is exposed to that job. The result waits for media and
does not open an AI review at that stage; analysis execution failures remain reportable. When all files seal, a content-identity
cache reuses the numeric risk-first result, attaches media availability, and
applies the existing bounded event/cooldown policy. Claims are atomic; recovery
requeues interrupted early/full jobs without declaring partial media complete.
`AnalysisQueue(workers=4)` runs at most four existing isolated child processes;
1..8 is accepted, with each original child budget unchanged. Normal recordings
remain token-free PC work. The immutable public schema cache publishes complete
files atomically for concurrent Windows readers.

The prepared copier change uses two durable cursors only when the qualified
mode is exactly detached_offroad: sanitized rlogs first, then full road-file
manifests. Repeated log identities resume/skip already received bytes. In that
home-only mode, audio stripping still copies compressed video with no audio,
but omits the old 0.25x read pacing. Vehicle/mixed-mode pacing, all privacy,
source/cache/storage bounds, admission and permission cancellation remain.
A changed module identity requires an explicit qualified home update; PC code
installation alone does not install or qualify that device change.

Qualified detached-home-only transfers now use a separate durable log cursor
before the full road-file cursor. Logs retain stable filtered identities so the
PC resumes/skips already sealed bytes. Both phases share the original 300-second
burst start and cumulative transfer pacing; the second phase cannot reset that
budget. Detached-home audio stripping omits the old 0.25x readrate while retaining
compressed video stream-copy, no audio/cabin media, original files, cache/storage
bounds, source checks and permission-group cancellation. Vehicle and mixed-mode
configurations keep all previous behavior. A new module identity must be applied
in an explicitly approved fresh detached-home session; PC release alone does not
activate the device. No model/control/recording/remote-guard code changes.


## Negotiated detached-home batches (2026-11-16)

An authenticated manifest reply can advertise `max_batch_bytes=1048576`.
Only the detached-home-only copier uses `/v1/archive/batch/<file>/<offset>`;
vehicle/mixed configurations and old servers keep64-KiB requests. A batch is
at most sixteen original64-KiB units, with exactly the same chain and resumable
positions. Every source read retains all privacy/source/permission checks.
Trusted Wi-Fi is checked before each request; permission loss/EOF still kills
the sole copier group. The300-second burst, pacing, file/cache/PC quotas,
no-audio/cabin policy and seal verification are unchanged. Each acknowledged
batch receives one file fsync and one transactional DB checkpoint; a lost reply
reconciles through the next manifest, and an unacknowledged tail is discarded
before the next append. No parallel copier/encoder or new observer is introduced.
A new module identity and fresh approved home admission are required.
