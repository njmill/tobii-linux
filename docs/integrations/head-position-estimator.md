# Calibrated MediaPipe head position

> Development note: offline tools and capture files referenced here are local
> research artifacts under `.tmp/` and are not shipped with the public runtime.
> Runtime modules live in `scripts/runtime/`; the explanations and measured
> limitations below are retained for contributors.

The dashboard can use MediaPipe for rotation and position, keeping the native
Tobii path for gaze and as a slow metric reference. This avoids using delayed
USB eye-midpoint depth as the immediate camera-position signal. The dashboard
remains available for calibration, recentering, and diagnostics.

## Reference point and calibration

A face transformation gives rotation `R` and translation `t`. Tracking the
face origin directly can move the camera during a nod. The new estimator tracks
a fixed point in the face's own coordinate system:

```text
head reference = t + R × calibrated_offset
position = axis_alignment × (head reference − neutral_reference) × metric_scale
```

The symmetric offset has x fixed at zero; nods alone cannot reliably identify
a lateral pivot. A least-squares fit minimizes 3D reference movement during a
labeled nod. An independent nod must then reduce depth span by at least 60%,
reduce 3D RMS spread by at least 50%, and have no more than 25 mm residual depth
span at the anchored scale. Separate near-fixed-pitch leans must preserve
80–120% of their image-derived depth range. Training and validation windows
cannot overlap. Profiles that fail these fit checks are not written. Live use
also requires held-out coverage at least 15 degrees above and below neutral.
Partial coverage or a failed additional trace check makes a candidate
shadow-only; `face` mode rejects it and `auto` calculates it without using it
for camera position.

This point is an empirical tracking reference, not an anatomical measurement.
Residual movement can remain. For the recorded ET5 example, training on the
first nod reduced the second nod's depth span from approximately 82 mm to
17 mm; the two leans preserved 98% and 96% of their image-derived depth ranges.
However, the same reference worsened depth span in the earlier up/down capture
from about 68 mm to 123 mm. The candidate is therefore **shadow-only**, not a
validated live fix. These results do not establish physical millimeter accuracy
against an external reference, or prove performance on combined yaw/pitch.

MediaPipe's canonical geometry uses nominal centimeters, but its virtual camera
is not a measured ET5 camera calibration. The estimator anchors mm/model-unit
scale using the neutral absolute USB eye depth and the simultaneous face
matrix's depth. It does not use the changing face-box area as distance. See
[MediaPipe's geometry metadata](https://github.com/google-ai-edge/mediapipe/blob/master/mediapipe/modules/face_geometry/protos/geometry_pipeline_metadata.proto).

The fixed image/native axis alignment is `(-x, +y, -z)` for the current unmirrored
`050e` path. The observed USB/image correlations support this alignment; this
profile should not be transferred to a different camera, image orientation,
person, or stream without fresh validation. The runtime rejects other streams.

## Online refinement

The calibrated offset and neutral reference do not change during play. Recenter
is explicit. Background refinement changes only metric scale, and only when:

- head angles are within 5 degrees of the existing neutral;
- estimated head position is within 5 mm of that neutral;
- recent eye-origin observations are valid and fresh;
- both image and USB depth have settled for at least 1.5 seconds, with less than
  2% depth variation over the short window.

Scale refinement has a 30-second time constant, a maximum rate of 0.2% of the
initial scale per second, and a total bound of ±10% from the validated profile.
It remains session-local; it does not rewrite the saved calibration. A held lean
outside the neutral region cannot update scale or gradually become the new
neutral. Missing/stale/non-rigid face data holds the last image-derived position;
it never switches to a different position source mid-motion.

This is bounded refinement, not unconstrained learning of nod-versus-lean from
ordinary movement. Initial reference fitting needs labeled movements. No extra
neural network training or OpenSeeFace installation is involved.

## Build a profile from a labeled capture

Keep head-only SC Input Mix at 1.00 and record a neutral pause, two separate
nods, and two separate leans with nearly fixed head angle. Note their times in
seconds since the first packet. Use a captured USB CSV from the same runtime
for a stable absolute distance anchor. The calibration tool accepts a measured
`--reference-distance-mm` instead of `--eye-samples` when available.

For the existing labeled capture:

```bash
python3 .tmp/development/scripts/recon/calibrate-head-position.py \
  .tmp/translation-comparison-after-gate-fix.jsonl \
  --train-nod 0:5.5 --validate-nod 6:10.5 \
  --lean 10.5:14.8 --lean 14.8:19.3 \
  --eye-samples .tmp/translation-comparison-after-gate-fix-eye-samples.csv \
  --check-trace .tmp/pitch-comparison.jsonl \
  --output .tmp/head-position-candidate.json \
  --csv .tmp/head-position-candidate.csv
```

The times above describe that recording only; choose appropriate labeled windows
for a different capture. The CSV evaluates the same `HeadPositionEstimator` used
by the live dashboard on every distinct recorded face observation. The report
contains training/held-out metrics and lean-range preservation. Do not use a
window containing deliberate leaning as a stationary nod.

Install a candidate as the local runtime profile to enable comparison. A
shadow-only candidate cannot automatically become the live source:

```bash
cp .tmp/head-position-candidate.json .tmp/sc-tobii-native-runtime/head-position.json
```

A shadow-only candidate has been installed for the current workspace. It is ignored
user calibration data, not a default profile for other people. `make runtime-clean`
clears this profile along with other calibrations; normal restarts retain it.

## Runtime modes

Normal quicklaunch and `make runtime` use `auto`: a live-approved local profile
selects image-derived head position; a shadow-only candidate is calculated and
recorded while eye-origin position remains the live source. Without a profile,
eye-origin position remains the fallback. An invalid existing profile is
reported before USB or worker startup. Explicit `face` mode requires a
live-approved profile, not just a successful fit on one motion direction.

```bash
# Normal runtime using a validated local profile when present
make runtime

# Record a comparison, including the calibrated reference and scale
SC_TOBII_TRACKING_TRACE="$PWD/.tmp/head-position-comparison.jsonl" make runtime

# Calculate the candidate but continue sending the eye-origin position
SC_HEAD_POSITION_MODE=shadow make runtime

# Return to the previous eye-origin position path
SC_HEAD_POSITION_MODE=eye make runtime
```

`SC_HEAD_POSITION_PROFILE` overrides the profile path. The dashboard exposes the
same controls as `--head-position-mode` and `--head-position-profile`. Its
calibration panel shows the active position source. Restart the dashboard and
recenter while facing forward after installing a new profile. Rotation gains,
position gains, gaze calibration, and game settings remain unchanged.

New traces also preserve ten sparse face landmarks and original image dimensions
for investigating camera geometry; they do not contain images. The old matrix-
only traces cannot identify a different camera model from landmarks they did
not retain. This is the next investigation after the upward validation failure.

New traces record `context.head_position`: the reference point, calibrated
offset, current scale, position, measurement time, and refinement status. Offline
replay checks both the geometric position calculation and the output filter:

```bash
python3 .tmp/development/scripts/recon/replay-tracking.py .tmp/head-position-comparison.jsonl
```

The report includes `head_position_rows` and `max_head_position_error_mm`.
Older traces lack this stage and continue to replay their original output
filter. Matrix recordings cannot re-run a different landmark model without the
corresponding camera frames.

The current candidate is not activated for camera position. The next useful
comparison is a single capture with repeated down/neutral/up/neutral movements,
followed by forward/backward leans with nearly fixed head angle. Both the native
position and shadow estimate will be recorded, along with the sparse landmark
geometry. Use a new trace name to preserve the earlier captures.

## Bidirectional landmark capture (2026-10-30)

The requested capture contains 3,291 packets over 27.42 seconds. Both packet
filter replay and the recorded geometric shadow calculation reproduce exactly.
The image dimensions and sparse landmarks are present. Live translation remains
the eye-origin path; the reference is correctly shadow-only.

The existing candidate reduces downward nod motion but still moves by roughly
6–7 cm during upward looks. A fresh reference fit trained jointly on the first
down/up pair and checked on the second pair also fails: held-out nod depth span
changes from about 79 mm to 67 mm, short of the required 60% reduction. Neither
candidate was promoted. A regression test now rejects a synthetic
direction-dependent geometry error that a fixed pivot cannot explain.

Offline checks also tried perspective pose fits to the ten landmarks, using
both the canonical face mesh and a neutral-frame personalized shape across
several focal-length assumptions. Low pixel reprojection error alone did not
produce a reference passing the nod-position limits. These experiments stayed
in ignored `.tmp/` files and are not a new live pose solver.

The captured geometry supports moving the next investigation upstream to the
camera/face model rather than fitting another constant pivot. Position and
head-pivot calibration are not uniquely recoverable from arbitrary unlabelled
movement; the labeled up/down/lean windows must remain independent validation
data for any subsequent adaptation. The capture, report, and plot are preserved
as `.tmp/head-position-comparison-bidirectional.jsonl`,
`.tmp/head-position-comparison-analysis.json`, and
`.tmp/head-position-comparison-analysis.png`.

The camera/face hypotheses now have a repeatable offline benchmark. See
[Offline camera/face calibration experiment](head-geometry-experiment.md) for
the training/evaluation split, command, selected hypothesis, and failed upward
validation. It does not promote the shadow profile or change runtime output.
