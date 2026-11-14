# PV5 official 0.11.2 sound assets

The owner approved adopting the official new sounds after the read-only review.
Import seven original WAVs from commaai/openpilot **v0.11.2**, immutable commit
`6939d172e85e3b91f16d90cc8de72e8f938cc196`. The asset directory includes the
original license and a SHA-256/size/source manifest. Legacy/localized files remain.

| Existing event | New asset |
| --- | --- |
| engage / engage2 / longEngaged | engage.wav |
| disengage / disengage2 / longDisengaged | disengage.wav |
| refuse | refuse.wav |
| prompt / promptRepeat | warning.wav |
| promptDistracted | dm_warning.wav |
| warningSoft | critical.wav |
| warningImmediate | dm_critical.wav |

Only a valid, real `KIA_PV5` CarParams selects this profile. Unknown, invalid,
notCar and other vehicles retain the existing profile. Carrot navigation/voice,
readiness, radarCutin's dormant chime and the FCW `stopStop` voice keep their
existing assets. Shared standard event IDs use the new tone wherever already
requested, including the SCC single warning prompt and the navigation cue that
already shares promptDistracted; no new sound event is introduced.

## Policy and resources

Preserve every alert predicate, priority, finite/infinite play count, existing
engage-volume setting, DM gain floor, critical gain normalization, timeout
fallback and exact silent-SCC exception. Keep the retired cut-in chime silent.
The new WAV durations differ, so repeating waveform periods naturally differ;
this is not a claim that the old acoustic cadence is identical. Do not port the
official DM escalation/lockout, four-second volume ramp, maximum-stage switch,
new preAlert or dm_critical_max policy as part of this asset change.

The existing startup/language loader prepares both profiles. The existing
20Hz loop uses its already subscribed CarParams and switches only preloaded
dictionary references between sounds. Callback samples and gains are captured
as one published pair, retaining a matching gain during a profile transition.
No playback/profile-path file I/O, new subscriber, worker, timer, inference,
transport or logging stream is added. Missing, malformed, truncated or
unallocatable official assets retain the complete existing warning pack and
report the load error through existing logging.

Alias buffers share the same float32 PCM when their per-event gains match.
Different primary engage gain can require two additional arrays. This is bounded
extra startup memory, not zero CPU/RAM cost. Existing explicit language changes
also use the existing loader. Host measurements do not establish device timing.

## Validation and delivery scope

Use exact WAV hashes/PCM formats; actual loader and audio methods without a
speaker or device; PV5/unknown/notCar/other-car and three language cases;
single/repeating playback, interruption, gains and SCC red-silent sequences;
malformed files and failed allocations; existing DM/navigation/audio regression.
Compare other-car output bit for bit with `ae956005b`.

The supplied preview renders one loop of each new software tone with the test
engage gain and current volume at100%, with silence between clips. It is a PC
PCM sample, not a physical comma speaker or in-car loudness test. Official
upstream documentation says this family was designed for comma four's speaker;
C3X audibility and recognition in real cabin noise remain unverified.

Final host checks: **131 passed, 0 failed/skipped**. Other-car/localized playback
matched the old code exactly in **630 PCM cases**. The software preview is
10.471 seconds including gaps and includes only one loop of each selected tone.
Additional retained float32 PCM is1,069,712 bytes in7 shared arrays at the test
engage gain; a different primary engage gain requires1,453,712 bytes in9 arrays.
These counts exclude dictionaries, transient decode buffers and full process RSS.

No car/model/planner/CAN/DM timing/HUD rendering or alert wire format changes.
The five already verified model-paired Jetson HUD pins stay unchanged: the HUD
does not run comma soundd and needs no audio runtime update for this change.
No vehicle connection, installation, restart or hardware validation is performed.

Sources: [official release](https://blog.comma.ai/0112release/#new-sounds),
[pinned official soundd](https://github.com/commaai/openpilot/blob/6939d172e85e3b91f16d90cc8de72e8f938cc196/openpilot/selfdrive/ui/soundd.py).
