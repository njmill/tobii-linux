# Offline camera/face calibration experiment

> Development note: offline tools and capture files referenced here are local
> research artifacts under `.tmp/` and are not shipped with the public runtime.
> Runtime modules live in `scripts/runtime/`; the explanations and measured
> limitations below are retained for contributors.

This experiment tests camera/face hypotheses using the recorded sparse
landmarks. It does not install a runtime profile or modify camera output.

## Reproduce

Use the existing MediaPipe environment, which provides NumPy, OpenCV, and
Matplotlib:

```bash
.venv/mediapipe/bin/python .tmp/development/scripts/recon/experiment-head-geometry.py \
  .tmp/head-position-comparison-bidirectional.jsonl \
  --neutral 0:0.8 --train 0:10 \
  --validate-nod 10:14 --validate-nod 14:16.8 \
  --lean 16.8:21.4 --lean 21.4:26.3 \
  --eye-samples .tmp/head-position-comparison-bidirectional-eye-samples.csv \
  --report .tmp/head-geometry-experiment.json \
  --csv .tmp/head-geometry-experiment.csv \
  --plot .tmp/head-geometry-experiment.png
```

These windows describe this recording only. Training contains the first down/up
pair. The second downward and upward nods are evaluated separately; the final
two leans are independent checks. Neutral-template observations must be inside
training. Windows cannot overlap, and training must precede evaluation.

Exit status 0 means the selected hypothesis passed all evaluation checks;
status 1 means it failed validation, with a completed report; status 2 means an
input/dependency/experiment error. No status installs or enables a profile.

## Method

The baseline fits a fixed reference point to the existing MediaPipe matrices.
The other 24 hypotheses combine six focal-length/image-width ratios
(`0.5, 0.75, 1, 1.5, 2, 3`) with four relative face-depth scales
(`0.5, 1, 1.5, 2`). Use `--focal-ratios` and `--depth-scales` to specify a different
grid explicitly. This is a bounded hypothesis search, not a measured lens
calibration.

A personalized ten-point 3D template is reconstructed from neutral training
landmarks. Relative landmark z is used as a shape hypothesis, not assumed to be
measured metric depth. The experiment assumes a centered principal point,
square pixels, and zero lens distortion. It uses OpenCV iterative `solvePnP`
with a consistent initial orientation, carrying the previous pose forward for
subsequent frames. A candidate cannot improve its score by dropping difficult
frames. The camera frame is converted back to the existing MediaPipe matrix
convention before reference-point fitting.

OpenCV's PnP estimator minimizes landmark reprojection error given a 3D model
and camera assumptions; small reprojection error does not establish accurate
depth. See the [OpenCV pose documentation](https://docs.opencv.org/4.x/d5/d1f/calib3d_solvePnP.html).

The reference offset is fitted on training data only. Metric scale is anchored
using stable, time-matched USB depth during the neutral training interval.
Candidates with invalid geometry, unsupported metric scale, or training
reprojection 95th percentile above three pixels are rejected.

**Selection is locked before evaluating held-out motion:** choose the lowest
training 3D RMS position spread, breaking ties by candidate ID. All fitted
hypotheses receive evaluation diagnostics, but the report never selects a
different winner because it happened to do better on validation.

For each held-out nod the candidate must reduce depth span by at least 60%,
reduce 3D RMS spread by at least 50%, and keep residual depth span within 25 mm.
Both pitch directions must be represented. Leans must preserve 80–120% of the
baseline image-derived depth range after metric anchoring. This comparison
uses the baseline, not each candidate's own uncorrected pose, to prevent a
wrong camera scale from passing simply by preserving its own distorted range.
These remain internal consistency tests, not external physical-angle or
millimeter ground truth.

The JSON report records trace/USB-input SHA-256 hashes, dependency versions,
windows, assumptions, fit parameters, rejected hypotheses, training metrics,
the locked selection, and separate nod/lean results. The CSV contains estimated
positions for every distinct face observation and fitted hypothesis. Fixed
OpenCV thread count and random seed make reruns comparable.

## Recorded result

The 25-hypothesis run fitted 18 candidates; none passed all held-out checks.
The selected personalized-PnP hypothesis used focal length equal to 3x image
width and a template depth multiplier of 1.5. Its training 3D RMS spread was
11.1 mm; its training reprojection 95th percentile was 1.80 pixels.

| Held-out movement | Baseline depth span | Selected depth span | Result |
| --- | ---: | ---: | --- |
| Downward nod, 10–14 s | 65.3 mm | 19.9 mm | Passed |
| Upward nod, 14–16.8 s | 40.7 mm | 48.1 mm | Failed |
| First lean | 298.6 mm | 310.6 mm | 104.0% preserved |
| Second lean | 292.3 mm | 307.6 mm | 105.2% preserved |

The selected focal ratio is at the search boundary, reinforcing that it should
not be interpreted as recovered physical intrinsics. The experiment improves
downward consistency and retains lean motion, but still fails upward tracking.
The existing runtime and shadow-only profile remain unchanged.

Next experiments need stronger camera/face-shape constraints or a more capable
multi-view shape model. Further search must preserve these independent motion
checks; lowering the acceptance limits or selecting by held-out results would
not establish an improvement. The current capture is retained for that work.

## Tests

```bash
.venv/mediapipe/bin/python -m unittest discover -s tests -q
```

The numeric tests recover known synthetic perspective motion, check that the
template ignores held-out landmarks, prevent selection by held-out scores,
reject missing geometry/overlapping windows, and reject candidates that drop a
failed pose. `make test` uses the system Python and skips the four NumPy/OpenCV
experiment tests if those modules are unavailable there.
