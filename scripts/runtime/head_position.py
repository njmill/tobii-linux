"""Calibrated MediaPipe head reference, independent of the dashboard and USB.

MediaPipe's canonical model uses nominal centimeters. The scale here is an
empirical mm/model-unit anchor, not a claim of calibrated camera intrinsics.
"""

from collections import deque
from dataclasses import asdict, dataclass
import json
import math
from pathlib import Path
from statistics import median


AXIS_SIGNS = (-1.0, 1.0, -1.0)  # ET5 stream 050e image axes -> native eye axes


def rigid_matrix(values):
    if values is None or len(values) != 16 or not all(math.isfinite(v) for v in values):
        raise ValueError("missing or non-finite face matrix")
    if any(abs(values[12 + i] - expected) > 1e-4 for i, expected in enumerate((0., 0., 0., 1.))):
        raise ValueError("invalid homogeneous matrix")
    columns = [[values[4 * i + j] for i in range(3)] for j in range(3)]
    lengths = [math.sqrt(sum(v * v for v in col)) for col in columns]
    if any(not .9 < length < 1.1 for length in lengths):
        raise ValueError("non-rigid face matrix scale")
    columns = [[v / length for v in col] for col, length in zip(columns, lengths)]
    if any(abs(sum(a * b for a, b in zip(columns[i], columns[j]))) > .02
           for i in range(3) for j in range(i + 1, 3)):
        raise ValueError("non-orthogonal face matrix")
    a, b, c = columns
    determinant = (a[0] * (b[1] * c[2] - b[2] * c[1])
                   - b[0] * (a[1] * c[2] - a[2] * c[1])
                   + c[0] * (a[1] * b[2] - a[2] * b[1]))
    if determinant < .98:
        raise ValueError("reflected face matrix")
    rotation = [[columns[j][i] for j in range(3)] for i in range(3)]
    return rotation, [values[3], values[7], values[11]]


def reference_point(matrix, offset):
    rotation, translation = rigid_matrix(matrix)
    return tuple(translation[i] + sum(rotation[i][j] * offset[j] for j in range(3)) for i in range(3))


def fit_reference(matrices):
    """Fit a symmetric reference point during an explicitly labeled still nod.

    x is fixed at zero: pitch alone cannot identify a lateral pivot reliably.
    Both y and z movement constrain the fit; a depth-only fit can worsen y.
    """
    if len(matrices) < 30:
        raise ValueError("need at least 30 distinct face observations")
    rows = [rigid_matrix(matrix) for matrix in matrices]
    # Remove the unknown fixed world position independently on all three axes.
    means = [[sum(rotation[i][j] for rotation, _ in rows) / len(rows) for j in (1, 2)] for i in range(3)]
    center = [sum(t[i] for _, t in rows) / len(rows) for i in range(3)]
    aa = ab = bb = ay = by = 0.0
    for rotation, translation in rows:
        for i in range(3):
            a, b = rotation[i][1] - means[i][0], rotation[i][2] - means[i][1]
            y = center[i] - translation[i]
            aa += a * a
            ab += a * b
            bb += b * b
            ay += a * y
            by += b * y
    det = aa * bb - ab * ab
    if aa + bb < .1 or det <= 1e-6 * (aa + bb) ** 2:
        raise ValueError("insufficient independent rotation for a reference fit")
    offset = (0.0, (ay * bb - by * ab) / det, (by * aa - ay * ab) / det)
    if math.sqrt(sum(v * v for v in offset)) > 20.0:
        raise ValueError("fitted reference is outside the 20-model-unit head region")
    return offset


@dataclass
class HeadPositionProfile:
    offset: tuple[float, float, float]
    mm_per_unit: float
    validation: dict
    image_stream: str = "050e"
    version: int = 1

    def validate(self):
        if self.version != 1 or self.image_stream != "050e":
            raise ValueError("unsupported head-position profile or image stream")
        if len(self.offset) != 3 or not all(math.isfinite(v) for v in self.offset):
            raise ValueError("invalid reference offset")
        if abs(self.offset[0]) > 1e-6 or math.sqrt(sum(v * v for v in self.offset)) > 20:
            raise ValueError("invalid symmetric head reference")
        if not math.isfinite(self.mm_per_unit) or not 5 <= self.mm_per_unit <= 30:
            raise ValueError("metric scale outside supported range")
        if not isinstance(self.validation, dict) or self.validation.get("passed") is not True:
            raise ValueError("profile has not passed held-out nod/lean validation")

    def require_live(self):
        self.validate()
        if self.validation.get("live_approved") is not True:
            raise ValueError("profile is shadow-only: needs passing upward/downward held-out coverage")

    @classmethod
    def load(cls, path):
        data = json.loads(Path(path).read_text())
        profile = cls(**data)
        profile.offset = tuple(profile.offset)
        profile.validate()
        return profile

    def save(self, path):
        self.validate()
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(path.suffix + ".tmp")
        temp.write_text(json.dumps(asdict(self), indent=2, sort_keys=True) + "\n")
        temp.replace(path)


class HeadPositionEstimator:
    def __init__(self, profile):
        profile.validate()
        self.profile = profile
        self.initial_scale = profile.mm_per_unit
        self.scale = self.initial_scale
        self.recenter()

    def recenter(self):
        self.origin = None
        self.last_time = None
        self.position = (0.0, 0.0, 0.0)
        self.status = "waiting for face reference"
        self.rest = deque()

    def update(self, matrix, sample_time, now, *, eye_depth_mm=None, neutral_angles=False):
        if not 0 <= now - sample_time <= .35:
            self.status = "held: stale face"
            self.rest.clear()
            return self.position
        try:
            point = reference_point(matrix, self.profile.offset)
        except (ValueError, TypeError):
            self.status = "held: invalid face matrix"
            self.rest.clear()
            return self.position
        if self.last_time is not None and sample_time <= self.last_time:
            return self.position
        dt = 0.0 if self.last_time is None else sample_time - self.last_time
        if dt > .2:
            self.rest.clear()
        self.last_time = sample_time
        if self.origin is None:
            self.origin = point
        delta = tuple(AXIS_SIGNS[i] * (point[i] - self.origin[i]) for i in range(3))
        self.position = tuple(v * self.scale for v in delta)
        self.status = "calibrated face reference"
        # Online refinement is ONLY scale, near the existing neutral, after both
        # estimators have settled. Never learn a new pivot or recenter from
        # arbitrary motion: those operations can erase a deliberate held lean.
        at_rest = neutral_angles and math.sqrt(sum(v * v for v in self.position)) < 5.0
        if at_rest and eye_depth_mm is not None and math.isfinite(eye_depth_mm) and 250 < eye_depth_mm < 1500 and matrix[11] < -1:
            self.rest.append((sample_time, eye_depth_mm, -matrix[11]))
            while self.rest and self.rest[0][0] < sample_time - 2.0:
                self.rest.popleft()
            if len(self.rest) >= 30 and sample_time - self.rest[0][0] >= 1.5:
                depths = [v[1] for v in self.rest]
                face_depths = [v[2] for v in self.rest]
                if (max(depths) - min(depths)) / median(depths) < .02 and (max(face_depths) - min(face_depths)) / median(face_depths) < .02:
                    target = median(depths) / median(face_depths)
                    target = max(.9 * self.initial_scale, min(1.1 * self.initial_scale, target))
                    change = (target - self.scale) * -math.expm1(-dt / 30.0)
                    limit = self.initial_scale * .002 * dt
                    self.scale += max(-limit, min(limit, change))
                    self.position = tuple(v * self.scale for v in delta)
                    self.status = "refining metric scale at neutral"
        else:
            self.rest.clear()
        return self.position

    def snapshot(self):
        return {"version": 1, "status": self.status, "offset_model": self.profile.offset,
                "mm_per_unit": self.scale, "reference_model": self.origin,
                "position_mm": self.position, "sample_time": self.last_time,
                "rest_samples": len(self.rest)}
