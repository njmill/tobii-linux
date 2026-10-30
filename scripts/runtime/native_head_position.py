"""Native eye-midpoint head reference: metric stabilization plus rotation arc.

Uses Tobii positions and MediaPipe ROTATION only. No image-derived translation
or face size enters this estimator. A calibrated rigid eye offset is expressed
in the head frame; alignment maps the camera rotation into native coordinates.
"""

from collections import deque
from dataclasses import asdict, dataclass
import json
import math
from pathlib import Path

from head_position import rigid_matrix


def transpose(a):
    return [list(row) for row in zip(*a)]


def multiply(a, b):
    return [[sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3)] for i in range(3)]


def transform(a, v):
    return tuple(sum(a[i][j] * v[j] for j in range(3)) for i in range(3))


def separation(left, right):
    return math.sqrt(sum((a - b) ** 2 for a, b in zip(left, right)))


def native_basis(left, right):
    # 050e camera/model X opposes the native left->right eye vector. This proper
    # rotation agrees with the measured image/native axis signs (-x,+y,-z).
    x = [a - b for a, b in zip(left, right)]
    length = math.sqrt(sum(v * v for v in x))
    if not 20 < length < 100:
        raise ValueError('invalid neutral eye separation')
    x = [v / length for v in x]
    y = [-x[i] * x[1] + (1. if i == 1 else 0.) for i in range(3)]
    length = math.sqrt(sum(v * v for v in y))
    if length < .5:
        raise ValueError('eye line cannot establish native up')
    y = [v / length for v in y]
    z = [x[1]*y[2]-x[2]*y[1], x[2]*y[0]-x[0]*y[2], x[0]*y[1]-x[1]*y[0]]
    return transpose([x, y, z])


def stabilized_eye(left, right, reference_separation):
    if len(left) != 3 or len(right) != 3 or not all(math.isfinite(v) for v in (*left, *right)):
        raise ValueError('non-finite eye positions')
    distance = separation(left, right)
    if not 20 < distance <= 100:
        raise ValueError('implausible eye separation')
    scale = reference_separation / distance
    if not .5 <= scale <= 2.:
        raise ValueError('eye scale outside calibrated range')
    center = tuple((a + b) * .5 for a, b in zip(left, right))
    if not 100 < center[2] < 2500:
        raise ValueError('eye depth outside supported range')
    return tuple(v * scale for v in center), scale


def head_reference(left, right, matrix, alignment, offset, reference_separation):
    rotation, _ = rigid_matrix(matrix)
    point, scale = stabilized_eye(left, right, reference_separation)
    arc = transform(multiply(alignment, rotation), offset)
    return tuple(point[i] - arc[i] for i in range(3)), point, scale


@dataclass
class NativeHeadProfile:
    alignment: list
    offset_mm: tuple
    eye_separation_mm: float
    validation: dict
    version: int = 1
    image_stream: str = '050e'

    def validate(self):
        if self.version != 1 or self.image_stream != '050e':
            raise ValueError('unsupported native head-position profile')
        if len(self.alignment) != 3 or any(len(row) != 3 for row in self.alignment):
            raise ValueError('invalid rotation alignment')
        flat = [v for row in self.alignment for v in (*row, 0.)] + [0., 0., 0., 1.]
        rigid_matrix(flat)
        if len(self.offset_mm) != 3 or not all(math.isfinite(v) for v in self.offset_mm):
            raise ValueError('invalid eye/head reference offset')
        radius = math.sqrt(sum(v*v for v in self.offset_mm))
        if abs(self.offset_mm[0]) > 1e-6 or not 20 <= radius <= 250:
            raise ValueError('reference outside supported symmetric head region')
        if not math.isfinite(self.eye_separation_mm) or not 35 <= self.eye_separation_mm <= 85:
            raise ValueError('invalid neutral eye separation')
        if not isinstance(self.validation, dict) or self.validation.get('passed') is not True:
            raise ValueError('native reference did not pass held-out validation')

    def require_live(self):
        self.validate()
        if self.validation.get('live_approved') is not True:
            raise ValueError('native profile is shadow-only')

    @classmethod
    def load(cls, path):
        profile = cls(**json.loads(Path(path).read_text()))
        profile.offset_mm = tuple(profile.offset_mm)
        profile.validate()
        return profile

    def save(self, path):
        self.validate()
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + '.tmp')
        temporary.write_text(json.dumps(asdict(self), indent=2, sort_keys=True) + '\n')
        temporary.replace(path)


class NativeHeadEstimator:
    def __init__(self, profile):
        profile.validate()
        self.profile = profile
        self.recenter()

    def recenter(self):
        self.orientations = deque(maxlen=64)
        self.origin = None
        self.position = (0., 0., 0.)
        self.last_eye_time = None
        self.status = 'waiting for paired eyes and orientation'
        self.last_observation = None

    def observe_orientation(self, matrix, sample_time, now):
        if not math.isfinite(sample_time) or not 0 <= now - sample_time <= .35:
            return
        try:
            rigid_matrix(matrix)
        except (ValueError, TypeError):
            return
        if self.orientations and sample_time <= self.orientations[-1][0]:
            return
        self.orientations.append((sample_time, tuple(matrix)))

    def update_eye(self, left, right, sample_time, now, valid=True):
        if not valid:
            self.status = 'held: binocular eyes unavailable'
            return self.position
        if not math.isfinite(sample_time) or not 0 <= now - sample_time <= .15:
            self.status = 'held: stale eye sample'
            return self.position
        if self.last_eye_time is not None and sample_time <= self.last_eye_time:
            return self.position
        # Causal pairing: never use an orientation from after the eye sample.
        pose = next(((t, m) for t, m in reversed(self.orientations) if t <= sample_time), None)
        if pose is None or sample_time - pose[0] > .080:
            self.status = 'held: no time-matched orientation'
            return self.position
        pose_time, matrix = pose
        try:
            reference, stabilized, scale = head_reference(left, right, matrix, self.profile.alignment,
                self.profile.offset_mm, self.profile.eye_separation_mm)
        except (ValueError, TypeError) as exc:
            self.status = f'held: {exc}'
            return self.position
        if self.origin is None:
            self.origin = reference
        self.position = tuple(reference[i] - self.origin[i] for i in range(3))
        self.last_eye_time = sample_time
        self.status = 'native compensated head'
        self.last_observation = dict(left=tuple(left), right=tuple(right), matrix=matrix,
            eye_time=sample_time, pose_time=pose_time, stabilized_midpoint_mm=stabilized, scale=scale)
        return self.position

    def snapshot(self):
        return dict(version=1, status=self.status, alignment=self.profile.alignment,
            offset_mm=self.profile.offset_mm, eye_separation_mm=self.profile.eye_separation_mm,
            origin_mm=self.origin, position_mm=self.position, observation=self.last_observation)
