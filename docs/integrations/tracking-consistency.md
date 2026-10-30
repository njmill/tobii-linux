# Tracking consistency: first implementation stage

> Development note: offline tools and capture files referenced here are local
> research artifacts under `.tmp/` and are not shipped with the public runtime.
> Runtime modules live in `scripts/runtime/`; the explanations and measured
> limitations below are retained for contributors.

The dashboard remains the calibration and diagnostic interface. The goal is
consistent tracking without routinely adjusting its sliders. This first stage
fixes timing and tracking-loss behavior and adds numeric recording/replay. It
does not replace the pose estimator or remove the UI.

## Changes

- Pose filtering runs once per fresh face estimate, rather than once per gaze
  row reusing that estimate. Eye-only pose uses the last eye observation in a
  batch. Baseline and gaze processing still consume every row.
- Filter response uses elapsed time, so update frequency does not set its speed.
- Output velocity comes from timestamped measurements, including zero velocity
  when an axis stops. Updates to other axes cannot preserve old yaw velocity.
- Prediction expires after 160 ms without a newer pose measurement. Output
  converges to the last measured pose. The final rotation limit also applies
  after prediction.
- Recenter resets output filter and prediction history, including cached face
  holds. A held pose keeps its preceding source's mapping scales.
- Invalid gaze clears the retained gaze position. Gaze also expires after
  250 ms, independently of pose and UDP transmission. Expired gaze is sent as
  centered and invalid; it must not remain a valid edge-of-screen gaze.
- Face freshness includes inference latency: its timestamp is taken when the
  mux frame notification is read, rather than when inference completes. This
  is a receipt timestamp, not a hardware exposure timestamp; USB/stdout backlog
  can still add unmeasured latency.
- When configured for live UDP, the Wine provider holds its last pose if UDP
  stops, or neutral before the first packet. It never substitutes the synthetic
  test waveform. Explicit probes without live UDP retain synthetic motion.

Existing saved gains, response curves, and prediction preferences remain in
use. These changes do not establish that the underlying face estimator is
geometrically symmetric or that SC's camera response is independent of gaze.

## Record a comparison

Stop the existing dashboard/runtime before starting another instance. Then run:

```bash
SC_TOBII_TRACKING_TRACE="$PWD/.tmp/tracking-comparison.jsonl" make runtime
```

The dashboard also accepts `--tracking-trace /path/to/trace.jsonl` directly.
Recording is opt-in and overwrites the specified file at startup. Use a new
filename for each comparison. Stop the runtime normally to flush the file.

The JSONL trace contains numeric face angles and transformation matrix, neutral
angles, neutral-relative face angles, the eye/fallback pose, filtered pose,
mapped packet inputs, gaze freshness, effective output-filter settings, and
emitted packets. It contains no camera images. Neutral-relative face angles in
the trace precede sign/gain and pitch/yaw compensation. Raw face estimates are
the worker's selected hybrid estimates; its matrix is recorded separately.

Only raw input estimates and metadata necessary to diagnose the existing
pipeline are recorded. This is not a recording of every camera frame, and it
cannot rerun MediaPipe with a different model. At 120 packets/second traces can
grow quickly; use short comparisons.

For the first comparison:

1. Face forward, recenter, and stay still briefly.
2. Turn left, hold, return to center, then repeat on the right. Repeat several
   times slowly and then faster, using comparable physical turn angles.
3. Repeat while slightly nodding or leaning, to expose cross-axis effects.
4. Include a brief face/gaze tracking loss and reacquisition.
5. For the head-only camera comparison, disable SC's gaze-driven camera
   contribution/Camera Boost. Then repeat with the preferred gaze behavior.

Do not infer physical accuracy from matching output endpoints alone: an external
angle reference or repeatable physical targets are needed to assess estimator
error. Holding at each endpoint helps distinguish filter lag from a persistent
offset.

## Replay without hardware or the game

```bash
python3 .tmp/development/scripts/recon/replay-tracking.py .tmp/tracking-comparison.jsonl \
  --output .tmp/tracking-comparison.csv
```

Replay uses recorded measurement/send times, reset generations, and effective
filter settings. It executes the same `PacketFilter` used by the sender and
compares its output with the recorded packets. It does not open USB, start
Wine, or send network packets. Packet counters are excluded from comparison.
Success means replay error is below `1e-9`; otherwise the command exits nonzero.
The CSV exposes input/output yaw, output pitch/roll, gaze validity, and error.

Replay currently validates the output-filter stage, not recomputation of the
entire estimator, calibration, or source-fusion stage. Earlier-stage estimates
are recorded for comparison. A future estimator change needs its own replay
input contract and recorded reference movements.

## Automated checks

```bash
make test
```

The tests cover left/right reflection, stationary yaw with motion on other axes,
prediction expiry, gaze invalidity/expiry, elapsed-time filtering, repeated face
observations, recenter resets, held-pose mapping, trace replay, and actual local
UDP packet/trace agreement. They use synthetic numeric observations and require
neither a tracker nor a graphical display. Importing the dashboard requires
Python's Tk module, but no Tk window is created.

## Remaining work

Measure raw-estimator versus transmitted-pose symmetry before changing camera
geometry or tuning defaults. Subsequent stages can address relative rotation
composition, face-quality estimates, pitch compensation, and depth estimation.
Then extract acquisition/estimation from the Tk event loop into a runtime that
the dashboard can observe and configure. `make headless` currently still uses
Tk with a hidden window.

## Pitch follow-up

Head-only in-game testing improved yaw consistency but exposed a slow pitch
response followed by a sudden increase. Two processing mechanisms can produce
this: the quadratic pitch response suppresses small movements and amplifies
large ones, and the hybrid pitch selector could switch by the full matrix
angle when the landmark pitch crossed camera zero.

Pitch now defaults to a linear curve (`opentrack_pitch_curve=1.0`), with
yaw-dependent amplification disabled (`mediapipe_pitch_yaw_comp=0.0`). Hybrid
pitch continuously averages the matrix and landmark-plane estimates instead of
selecting by sign/magnitude. This removes the mathematical discontinuity; it
does not establish the accuracy of either underlying estimate. Matrix-only and
landmark-normal diagnostic modes remain available.

Existing saved tuning overrides these new defaults. For an existing profile,
set the dashboard's **pitch curve** to **1.00** and **mp p/y** to **0.00** once,
or edit those two fields in `sc-tuning.json` while the runtime is stopped. Do not
reset the other gains. Restart the runtime and recenter facing forward so the
new hybrid estimator has a matching neutral reference.

For a new comparison, use a separate trace:

```bash
SC_TOBII_TRACKING_TRACE="$PWD/.tmp/pitch-comparison.jsonl" make runtime
```

Keep SC's camera Input Mix at 1.00. Slowly nod down, hold, return to neutral,
then nod up and hold. Repeat three times, initially without yaw, then repeat
with modest yaw. Record whether any sudden camera movement remains. New traces
include `face.matrix_pitch`, `face.landmark_pitch`, and the effective pitch
mapping settings alongside the selected face pitch and transmitted output.
Older traces lack the two separate pitch signals and cannot re-run this
estimator change. Replay still checks the downstream output filter exactly.

## Translation during pitch

The pitch capture exposed another problem: in all three downward holds, the
reported depth exactly matched the face-box fallback calculation. The smaller
face box during a nod supplied about -30 to -35 mm of artificial depth, which
the saved 4x depth gain amplified to roughly -12 to -14 cm before SC's own
position response.

Translation now comes only from fresh binocular eye-origin observations with
plausible geometry. During unreliable eye tracking, position holds its last
trusted value while face rotation continues. Before the first trusted position,
translation is neutral. A valid zero depth remains zero; it no longer selects a
different depth estimator. Recenter clears the position cache. The cache is
separate from the face/fallback pose so fallback positions cannot feed back
into it as apparent eye observations.

The old face translation/depth sliders have been removed. Their legacy command
line options are accepted for compatibility but have no effect. Saved eye
translation gains remain unchanged, so deliberate leaning still works while
reliable eye-origin data is available. If the eyes cannot be tracked, intentional
leaning pauses too; the runtime does not infer distance from face size.

New trace contexts identify the translation source (`eye origins`, `held eye
origins`, or `neutral`), last trusted position, and its measurement time. Their
`eye_pose` field always contains an actual eye-origin-derived pose. In older
traces this field could instead contain the face fallback after eye loss.

For verification, restart and recenter, then record a slow nod followed by a
deliberate forward/backward lean:

```bash
SC_TOBII_TRACKING_TRACE="$PWD/.tmp/pitch-translation-comparison.jsonl" make runtime
```

Pure rotation can physically move the eyes along an arc. Eye-origin estimation
can also be imperfect. This change removes the demonstrated face-box depth
artifact; it does not compensate all rotation-induced eye motion about the
neck/head pivot. A remaining translation during nodding should be measured in
the new trace before adding geometric compensation.

## Translation latency follow-up

The old translation step limit acted as a speed governor: the saved 0.2 cm per
60 Hz reference step limited packet position to 12 cm/s. A 20 cm output move
took 1.5 seconds to reach 90% in a deterministic test. With a 4x position gain,
even a modest physical lean could take seconds to reach its target or return.

Output filter version 2 gives position an 80 ms time constant, without velocity
prediction or a travel-speed cap. Its 90% step response is about 184 ms,
independent of distance and direction. Tests require response within 250 ms at
30, 60, 120, and 240 Hz. This is the output-filter stage alone: tracking loss,
the upstream pose filter, and SC can still add latency. Rotation filtering and
saved position gains are unchanged. The obsolete translation-step slider has
been removed; its command-line option remains accepted for compatibility.

New traces identify output filter version 2. Replay selects version 1 for older
traces without that field, so the original captures still reproduce exactly.
The replay CSV now includes input/output translation in centimeters, translation
source, age of the last trusted eye position, and eye observation status.

To diagnose remaining forward movement during a nod, record the current code:

```bash
SC_TOBII_TRACKING_TRACE="$PWD/.tmp/translation-comparison.jsonl" make runtime
```

After restarting and recentering, remain still briefly, nod down and return
without deliberately leaning, then hold your head orientation steady and lean
forward/backward. Repeat twice and pause at each endpoint. This separates
rotation-associated eye motion from position changes and exposes whether any
delay comes from held tracking, upstream filtering, or output filtering. The
older pitch capture predates the removal of face-box depth and cannot establish
the source of the remaining forward translation.

## Position-gate follow-up

The current-code translation capture reproduced a second source of delay:
31.7% of output rows used a held position, mostly because eye separation was
different from the startup estimate. Four lean/recovery intervals rejected
position observations continuously for 1.26–1.36 seconds. The raw midpoint
continued moving; the accepted position remained flat. Output filter version 2
followed the accepted target without the former travel-speed cap.

The eye-distance stability score remains available for orientation confidence,
but no longer gates translation. Position still requires both eyes to be
present, finite coordinates, fresh/in-order measurements, and a broad 20–100 mm
eye-separation sanity range. This avoids treating a small difference from the
startup eye-separation estimate as proof that all midpoint motion is invalid.
New traces include measured eye separation and the rejection reason.

The same capture confirmed that the remaining nod-related forward motion is
present in the measured eye midpoint, independent of the removed face-size
fallback. It can contain physical eye motion about the head/neck pivot and
estimation error; this capture cannot separate those causes. A simple fixed
pitch-pivot fit on the first nod did not generalize to the second nod, so it was
not applied to the runtime. Rotation/translation decoupling remains unresolved
and needs a consistent geometric head reference, rather than a directional
gain or an offset fitted to one movement.

## Fresh capture after the position-gate fix

The explicitly ordered two-nod/two-lean capture has 2,435 packets over 20.28
seconds and replays exactly. Translation accepted 99.4% of the observations;
the remaining 15 output rows were brief missing-eye intervals. No startup
eye-distance rejection or long position hold remains in this capture. The
saved depth gain is 1.0.

Both nods still move the measured USB eye midpoint forward by approximately
6–7 cm. The output follows that measurement; the face-box fallback is absent.

During the two deliberate leans, the face transformation's translation starts
changing before USB eye depth. Comparing the halfway points of each lean gives
USB-versus-face differences of 0.66/0.63 seconds forward and 0.57/0.58 seconds
on return. The transmitted position trails the USB midpoint by another
0.10–0.12 seconds. These are relative timing differences between estimators,
not absolute sensor latency measured against external ground truth. The USB
CSV's raw-eye-origin columns show the same depth behavior as the origin columns
used by the dashboard; simply choosing those raw columns will not resolve it.

The capture and its matching raw USB CSV subset are preserved in ignored files
named `translation-comparison-after-gate-fix.jsonl` and
`translation-comparison-after-gate-fix-eye-samples.csv` under `.tmp/`. The plot
`translation-comparison-latest-analysis.png` separates the nods from the leans
and compares normalized estimator timing without claiming MediaPipe's nominal
translation units are calibrated millimeters.

Remaining position work is upstream of the output filter: establish a
rotation-aware head reference and validate an image-derived position estimate
against metric/physical references. Neither restoring face-box depth nor
switching uncalibrated matrix translation straight into the game establishes
accurate 6DoF tracking.

The next implementation adds a calibrated image-derived reference with labeled
nod/lean validation and bounded scale refinement. See
[Calibrated MediaPipe head position](head-position-estimator.md) for the current
behavior, calibration command, comparison modes, and measured limitations.

## Native rotation compensation

The current implementation uses native eye position with common-scale
stabilization and a rotation-compensated head reference. The older image-position
candidate remains separate. See [native head position](native-head-position.md)
for the calculation, held-out results, live testing, and rollback settings.

## Movement baseline and unity tuning

The tested movement settings are now the default starting point and the target
of **Reset Tuning**. Gain sliders show multiples of that baseline: `1.00` keeps
the tested response, `0.50` halves it, `2.00` doubles it, and `0.00` disables it.
Negative multipliers reverse its direction.

The physical baseline is yaw `11.75`, pitch `-8.25`, roll `1.00`, Y `-0.50`,
Z `1.00`, with MediaPipe yaw/pitch/roll scales `0.08 / 0.10 / 0.08`. Each of
these displays `1.00`. Both angle curves remain linear at `1.00`. X translation
and pitch/yaw compensation stay explicitly disabled at `0.00`; a disabled
baseline cannot provide a meaningful multiplicative gain. X can still be enabled
using its slider. Smoothing, timing, and limits retain their existing units.

Saved tuning uses `tuning_version: 2` and stores gain multipliers. Older unversioned
files retain their original physical meaning and convert when saved. Command-line
scale options and supervisor `SC_TOBII_*_SCALE` overrides retain their physical
units for compatibility. Numeric traces also retain physical runtime settings,
so existing replay remains valid. Restart the dashboard to load the new controls.
