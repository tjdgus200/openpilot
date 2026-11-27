# PV5 Jetson lane-marking replay and shadow mode

The shadow decision is recorded in `modelV2.meta.pv5LaneReview` with its side in
`pv5LaneReviewSide`. Steering admission continues to use the existing OEM policy.
Previously installed Jetson packages do not advertise this capability.

A fresh OEM camera code 8 immediately before a physical turn signal can arm a
one-second observation. The request starts when the same side switches to a
fresh hidden/unknown display state (0 or 1). A fresh same-side OEM BSD report,
the existing side-object checks and valid radar data are required. A conflicting
marking, changed signal, invalid data or timeout cancels the observation.

The lab Jetson reuses camera 0 of the existing 512×256 road warp. After sending
the main inference reply, it can accept one frame into an idle worker, at most
four times per second. The worker projects the same frame's estimated lane
boundary into the road view and checks the Xiaoge masks along its near section.
An unobservable or ambiguous marking produces an unknown result. The next
negotiated reply can carry the source frame, side, marking and confidence in
ten bytes. Comma checks the frame against the capture times for this request.

## CarrotWeb

Under **Settings → PV5 → Driving Assist**, **Jetson Lane Marking Observation
(Experimental)** controls the persistent `Pv5JetsonLaneReview` setting. It
defaults to off. Modeld reads it at the existing 250 ms settings poll. Switching
it off clears the observation history and stops new lane-review requests.

During a supported Jetson package update, the candidate probe prepares and
checks a local TensorRT plan while the inference service is stopped. A Jetson
without a validated plan does not advertise lane review. Turning on the web
switch permits observation when the paired Jetson advertises the capability
and the vehicle conditions above are met. Results are recorded in the driving
log. Normal operation requires no terminal command or service environment edit.

## Offline evidence

The replay paired archived narrow-camera video with the recorded model
boundaries and calibration for the same camera frame. The 512×256 luma input
was reconstructed from compressed video. The tested samples included 69
distinct frames from 17 segments, with 73 evaluations across overlapping sets.

| Selected samples | Original full-camera pipeline | Proposed road-warp pipeline |
| --- | ---: | ---: |
| Correct dashed | 10/10 | 4/10 |
| Correct solid | 4/4 | 2/4 |
| Reported dashed without a clear dashed boundary | 10/25 | 0/25 |

The remaining eight primary cases were unknown. In one solid-to-dashed
transition the recorded boundary probabilities stayed below 0.5 on all three
later labelled dashed frames, so the auxiliary network was skipped. OpenCV and
ONNX Runtime on PC agreed on all 73 decisions. These observations do not
establish on-device latency, GPU memory cost or a reliable visual fallback for
automatic lane changes.
