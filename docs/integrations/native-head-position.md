# Native eye position with rotation compensation

> Development note: offline tools and capture files referenced here are local
> research artifacts under `.tmp/` and are not shipped with the public runtime.
> Runtime modules live in `scripts/runtime/`; the explanations and measured
> limitations below are retained for contributors.

The native estimator uses Tobii binocular eye origins for position and the
MediaPipe transformation matrix for **rotation only**. It estimates a fixed head
reference behind the eyes, removing the arc the eyes follow during rotation.
MediaPipe translation, face-box size, and game output are not inputs to this
calculation. This supersedes the unsuccessful image-position candidate described
in [head-position-estimator.md](head-position-estimator.md).

## Calculation and timing

For eye midpoint `E`, native-to-camera rotation alignment `Q`, observed camera
rotation `R`, and calibrated head-to-eye offset `r`:

```text
scale = neutral_eye_separation / observed_eye_separation
head_reference = scale * E - Q * R * r
position = head_reference - reference_at_recenter
```

The separation normalization addresses a common scale variation in the decoded
3D eye origins. During deliberate leans their reported separation changed from
about 38 to 94 mm, despite the person's fixed physical eye separation. It also
substantially reduces the eye-depth lag relative to image motion in the recorded
captures. This is a geometric constraint, not an independent distance sensor or
proof of millimeter accuracy. Without this step, subtracting fresh rotation from
the lagging eye depth left large pitch-dependent residuals.

Calibration estimates the symmetric head-to-eye Y/Z offset in native millimeters
from labeled stationary-head nods. Camera alignment uses the neutral eye line,
native up, and neutral camera rotation. This assumes a near-forward neutral
posture; it is not a full extrinsic camera calibration. The fitted offset stays
fixed during use. Recenter resets the output origin and sample histories, without
learning from deliberate leans or changing the saved geometry.

Each eye observation uses the latest already-available rotation whose timestamp
is **at or before** the eye timestamp, with at most 80 ms separation. Eye samples
older than 150 ms and rotations older than 350 ms are rejected. Invalid eyes,
implausible geometry, missing rotations, and eye loss hold the **whole corrected
position**. The runtime never subtracts a changing rotation from a frozen eye
midpoint. Rotation itself still follows the existing head-rotation path.

## Runtime selection

The default `--head-position-mode auto` prefers a validated native profile at
`.tmp/sc-tobii-native-runtime/native-head-position.json`. Without a native
profile, existing selection behavior remains available. The profile is personal
runtime state, not committed defaults for every user.

The supervisor accepts:

```bash
# Normal launch; uses the saved native profile automatically.
make runtime

# Compare with uncompensated native eye position.
SC_HEAD_POSITION_MODE=eye make services

# Record the candidate but continue sending uncompensated eye position.
SC_HEAD_POSITION_MODE=native-shadow make services
```

Override the file with `SC_NATIVE_HEAD_POSITION_PROFILE`. Explicit `native`
requires a profile approved by the calibration checks; `native-shadow` can load
a passing but shadow-only profile. `runtime-clean` removes this calibration too.
Restart the service after backend code/profile changes; the dashboard is a remote client.
The startup log reports `head_position=native mode=native`. Translation status
shows `native compensated head` or the reason the position is held.

## Reproduce the current calibration

The ignored local inputs are the previously recorded bidirectional capture and
its matching eye CSV. No hardware or game process is needed:

```bash
python3 .tmp/development/scripts/recon/calibrate-native-head.py \
  .tmp/head-position-comparison-bidirectional.jsonl \
  --eye-samples .tmp/head-position-comparison-bidirectional-eye-samples.csv \
  --neutral 0:0.8 --train 0:10 \
  --validate-nod 10:14 --validate-nod 14:16.8 \
  --lean 16.8:21.4 --lean 21.4:26.3 \
  --output .tmp/native-head-position-candidate.json \
  --report .tmp/native-head-position-report.json \
  --csv .tmp/native-head-position-candidate.csv
```

Only training observations determine the offset. Held-out nods must cover both
pitch directions, reduce depth span by at least 60% to at most 25 mm, and reduce
3D RMS spread by at least 50%. Visible-eye coverage must exceed 40% of each nod
window. Held-out leans must retain 80–120% of both raw and scale-stabilized depth
range with nearly fixed pitch. Overlapping windows and insufficient motion are
rejected. Failed validation writes a report and does not export a new profile.

The current offset is approximately `(0, 94.4, 54.6)` mm, with neutral separation
59.2 mm. In the held-out capture:

| Movement | Stabilized eye depth span | Corrected head depth span |
| --- | ---: | ---: |
| Looking down | 55.5 mm | 10.6 mm |
| Looking up, eyes visible | 55.7 mm | 13.4 mm |
| Lean 1 | 316.1 mm | 309.9 mm |
| Lean 2 | 307.4 mm | 301.4 mm |

At the upward extreme, binocular tracking is lost; the runtime holds position.
Those missing observations are excluded from geometry metrics, not interpreted
as accurate tracked positions. Only about 55% of that upward window is paired.

Applying the same fixed profile to the older independent capture leaves about
24.4 and 25.2 mm depth span during its downward nods, while preserving its two
leans. This improves on the roughly 56–65 mm raw depth spans, but shows that the
held-out 11–13 mm result is not a guarantee across sessions. Yaw/roll compensation
and physical distance accuracy still need live validation. A camera/device
mounting change may invalidate the alignment.

## Next live check

Restart the service with the saved profile, open the dashboard, and recenter facing
forward. Compare down/up nods, deliberate leans, then nods while leaning. Also
check yaw and roll for unwanted translation. Keep Star Citizen Input Mix at
1.00 head for comparison. To record a fresh numeric trace:

```bash
SC_TOBII_TRACKING_TRACE="$PWD/.tmp/native-head-comparison.jsonl" make services
```

The trace includes the native eye observations, paired rotation, normalization,
reference origin, corrected position, and hold status. `replay-tracking.py`
checks the geometry and timestamp pairing as well as the output filter. This
implementation is a live-test candidate; recorded improvements do not establish
that the remaining forward motion or perceived delay is fully resolved.
