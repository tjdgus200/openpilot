# Official server updates and isolated comma relay updates

This fork keeps the official Jetson update mechanism and adds a separate,
opt-in personal channel. The branch's signed `comma_host_release.json` selects
the personal channel; official branches continue to send `host_release.json`.
An invalid personal manifest never falls back silently to another model.

| Boundary | Official Carrot branch | This personal branch |
| --- | --- | --- |
| Downloader | Jetson, configured HTTPS NAS | Comma, pinned HTTPS URLs |
| Jetson transport | Existing Internet download | Chunked USB transfer from comma |
| Trust root | Existing official public key | Separately provisioned personal Ed25519 public key |
| Executable sources | `releases/<commit>` | `personal/releases/<signed-manifest-digest>` |
| ONNX and TensorRT cache | `cache/` | `personal/cache/<signed-manifest-digest>/` |
| Incomplete files | Existing official updater files | `personal/incoming/<signed-manifest-digest>/` |
| Activation | Existing stopped-service transaction | Same serialized transaction and candidate probe |

The shared activation journal owns the single `current` pointer; two updater
processes cannot activate independently. The boot gate holds the update lock,
freezes a selection while staging/probing, rejects another selection during that
transaction, and requires a fresh peer selection before granting runtime start.
The personal receiver is absent from the running inference server.

## First use

The older receiver already on a Jetson cannot accept a new upload protocol or
trust a new key merely because comma asks it to. **One PC recovery/offline
installation of `install_comma_relay.py` and the public key is required.** This
updates only the existing protected DATA runtime's small updater. The official
key, current release, QSPI, JetPack, APP, identity and Wi-Fi settings are retained.
Use the verified package and exact public-key fingerprint in its `INSTALL.txt`.
An existing different personal key is rejected rather than replaced.

After provisioning, boot both devices with this branch installed on comma and
USB connected. Leave comma offroad with Internet access. Comma downloads the
signed host bundle and hash-pinned ONNX into `/data/jetson-comma-relay/`, checks
their complete lengths/hashes, then sends them over USB. Jetson independently
checks its separately installed trust key, signature, runtime ABI, disk space,
offsets, length and SHA-256. The Jetson does not need Internet for this channel.
Subsequent boots with the same active release do not download/upload again and
can start onroad normally. New personal updates require offroad; a transition
to onroad or a disconnected link cancels approval to start the new runtime.

The Jetson extracts only bounded, regular files into a new personal release,
builds/loads TensorRT and performs finite synthetic inference before switching
`current` atomically. Failure retains the old runtime but does **not** run an old
model against a new comma contract. An interrupted transaction is recovered at
the next boot. Interrupted transfers can resume; corrupt objects are discarded
for a clean retry. Official artifacts/caches are never deleted to make space.

## Returning to official / returning to this branch

Install the desired official Carrot branch on comma, then restart Jetson with
comma USB attached. Its signed official manifest selects the official path and
original cache. An uncached official version still requires **Jetson Internet**,
as before. The personal receiver/key remains installed but inactive; it does
not change official signature checks or NAS URL restrictions. Restart Jetson
when switching branches in either direction; a running inference session is
not hot-swapped. Registered old `local` experiment profiles remain supported.

## Publishing the next personal release

Keep the private signing key on the PC, outside the Git checkout; never put it
in the branch, release assets, comma or Jetson. Only the public key is shipped.
Commit the host code first, then build its immutable bundle:

```sh
python tools/jetlink/build_host_bundle.py --regular-files /outside/repo/host.tar.gz
python tools/jetlink/prepare_comma_release.py \
  --bundle /outside/repo/host.tar.gz --model /outside/repo/model.onnx \
  --key /private/pc/comma-relay-ed25519.pem --output /outside/repo/relay-package \
  --bundle-url https://github.com/tjdgus200/openpilot/releases/download/UNIQUE-TAG/host.tar.gz \
  --model-url https://upload.shind0.synology.me/models/RELEASE/model.onnx
```

`--generate-key` is for initial provisioning only and refuses to overwrite an
existing key. Publish `host.tar.gz` at that immutable release URL and commit the
generated `comma_host_release.json` and `comma-relay-public.pem` under
`openpilot/selfdrive/modeld/jetlink/`. The manifest commit comes after the host
commit intentionally: a bundle cannot contain its own cryptographic hash. A
source-only checkout without a personal manifest retains the prior `local`
selection; distributed branch tips include the signed personal manifest.

## Verification scope

PC tests exercise independent signatures, malformed/tampered selection, missing
provisioning, broken transfers, offsets, length/hash failures, symlinks/traversal,
space exhaustion, official/personal commit collisions, offroad cancellation,
selection races, candidate rejection, interrupted activation recovery, cache
separation, actual sender/receiver protocol roundtrip and official restoration.
No vehicle controls or model math change in this update.

This is update isolation, **not a security sandbox for arbitrary signed Python**:
signed code runs as the existing Jetson runtime account with its existing OS
permissions. Keep the personal signer trusted. PC tests cannot establish
electrical power-loss durability, real USB stability, TensorRT build/performance
on this device or closed-loop driving safety. Those hardware checks remain
required before treating the new deployment as verified on the car.
