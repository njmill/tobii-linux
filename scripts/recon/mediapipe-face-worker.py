#!/usr/bin/env python3
"""Long-lived MediaPipe Face Landmarker worker for Tobii IR PGM frames.

Protocol:
  stdin:  /path/to/frame.pgm<TAB>timestamp_ms\n
  stdout: one JSON object per input line

This runs in a Python 3.11/3.12 venv because the main dashboard may run on
Python 3.13, where MediaPipe wheels are commonly unavailable.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def read_pgm(path: Path) -> tuple[int, int, bytes]:
    with path.open("rb") as f:
        if f.readline().strip() != b"P5":
            raise ValueError(f"not a P5 PGM: {path}")
        line = f.readline()
        while line.startswith(b"#"):
            line = f.readline()
        width, height = [int(part) for part in line.split()]
        max_value = int(f.readline().strip())
        data = f.read(width * height)
    if max_value != 255:
        raise ValueError(f"unsupported PGM max value {max_value}: {path}")
    if len(data) != width * height:
        raise ValueError(f"short PGM frame: {path}")
    return width, height, data


def rotation_to_ypr_euler(matrix: list[float]) -> tuple[float, float, float]:
    # Matrix is row-major 4x4. This is the original generic Euler
    # decomposition. It is useful as a fallback, but on the ET5 IR stream it can
    # couple pitch and yaw enough that pitch collapses at high yaw.
    r00, r01, r02 = matrix[0], matrix[1], matrix[2]
    r10, r11, r12 = matrix[4], matrix[5], matrix[6]
    r20, r21, r22 = matrix[8], matrix[9], matrix[10]
    sy = math.sqrt(r00 * r00 + r10 * r10)
    singular = sy < 1e-6
    if singular:
        pitch = math.atan2(-r12, r11)
        yaw = math.atan2(-r20, sy)
        roll = 0.0
    else:
        pitch = math.atan2(r21, r22)
        yaw = math.atan2(-r20, sy)
        roll = math.atan2(r10, r00)
    return math.degrees(yaw), math.degrees(pitch), math.degrees(roll)


def rotation_to_ypr_forward(matrix: list[float]) -> tuple[float, float, float]:
    # Derive yaw/pitch from the transformed face-forward axis. This keeps pitch
    # observable while yawed because both angles come from the same direction
    # vector instead of separate Euler terms. Roll still comes from the original
    # in-plane basis and remains dashboard-tunable.
    r00, r01, r02 = matrix[0], matrix[1], matrix[2]
    r10, r11, r12 = matrix[4], matrix[5], matrix[6]
    _r20, _r21, r22 = matrix[8], matrix[9], matrix[10]

    forward_x = r02
    forward_y = r12
    forward_z = r22
    yaw = math.atan2(forward_x, forward_z)
    pitch = math.atan2(-forward_y, math.sqrt(forward_x * forward_x + forward_z * forward_z))
    roll = math.atan2(r10, r00)
    return math.degrees(yaw), math.degrees(pitch), math.degrees(roll)


def rotation_to_ypr(matrix: list[float], mode: str) -> tuple[float, float, float]:
    if mode == "euler":
        return rotation_to_ypr_euler(matrix)
    return rotation_to_ypr_forward(matrix)


def average_landmark(landmarks, indices: list[int]) -> tuple[float, float, float] | None:
    points = [landmarks[index] for index in indices if index < len(landmarks)]
    if not points:
        return None
    return (
        sum(point.x for point in points) / len(points),
        sum(point.y for point in points) / len(points),
        sum(point.z for point in points) / len(points),
    )


def sub3(a: tuple[float, float, float], b: tuple[float, float, float]) -> tuple[float, float, float]:
    return a[0] - b[0], a[1] - b[1], a[2] - b[2]


def cross3(a: tuple[float, float, float], b: tuple[float, float, float]) -> tuple[float, float, float]:
    return (
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    )


def normalize3(v: tuple[float, float, float]) -> tuple[float, float, float] | None:
    length = math.sqrt(v[0] * v[0] + v[1] * v[1] + v[2] * v[2])
    if length < 1e-6:
        return None
    return v[0] / length, v[1] / length, v[2] / length


def landmark_normal_ypr(landmarks) -> tuple[float, float, float] | None:
    # Estimate a face plane directly from stable MediaPipe mesh points. This is
    # intentionally independent of the transformation matrix because the matrix
    # pitch can lose magnitude when pitch and yaw are combined on the ET5 IR feed.
    left_eye = average_landmark(landmarks, [33, 133, 159, 145, 468, 469, 470, 471, 472])
    right_eye = average_landmark(landmarks, [263, 362, 386, 374, 473, 474, 475, 476, 477])
    forehead = average_landmark(landmarks, [10, 151, 109, 338])
    chin = average_landmark(landmarks, [152, 175, 199])
    if left_eye is None or right_eye is None or forehead is None or chin is None:
        return None
    right = sub3(right_eye, left_eye)
    down = sub3(chin, forehead)
    normal = normalize3(cross3(right, down))
    if normal is None:
        return None

    # Try both possible normal directions and pick the one facing the camera.
    # MediaPipe face z is negative toward the camera in the normalized image
    # coordinate system, so a front-facing normal should generally have z < 0.
    if normal[2] > 0.0:
        normal = (-normal[0], -normal[1], -normal[2])

    yaw = math.atan2(normal[0], -normal[2])
    pitch = math.atan2(normal[1], math.sqrt(normal[0] * normal[0] + normal[2] * normal[2]))
    # Roll from the eye line is generally stable and useful for diagnostics.
    roll = math.atan2(right[1], right[0])
    return math.degrees(yaw), math.degrees(pitch), math.degrees(roll)


def hybrid_pitch(matrix_pitch: float, landmark_pitch: float) -> float:
    # The two MediaPipe pose signals fail in different ways on the ET5 IR feed.
    # Landmark-plane pitch tends to be steadier, but can lose magnitude when a
    # strong yaw hides one side of the face. If both estimates agree on the
    # direction, keep the stronger magnitude; if they disagree, prefer landmarks
    # because they are tied directly to the visible mesh.
    if not math.isfinite(matrix_pitch) or not math.isfinite(landmark_pitch):
        return landmark_pitch
    if matrix_pitch == 0.0 or landmark_pitch == 0.0:
        return landmark_pitch
    if math.copysign(1.0, matrix_pitch) == math.copysign(1.0, landmark_pitch):
        return matrix_pitch if abs(matrix_pitch) > abs(landmark_pitch) else landmark_pitch
    return landmark_pitch


def stretch_gray(np, gray):
    lo = np.percentile(gray, 1.0)
    hi = np.percentile(gray, 99.5)
    if hi <= lo:
        hi = lo + 1.0
    return np.clip((gray.astype(np.float32) - lo) * 255.0 / (hi - lo), 0, 255).astype(np.uint8)


def preprocess_frame(path: Path, preprocess: str, upscale: float):
    import numpy as np

    width, height, data = read_pgm(path)
    gray = np.frombuffer(data, dtype=np.uint8).reshape((height, width))
    if preprocess == "stretch":
        gray = stretch_gray(np, gray)
    elif preprocess == "clahe":
        import cv2

        clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
        gray = clahe.apply(gray)
    elif preprocess != "raw":
        raise ValueError(f"unknown preprocess: {preprocess}")
    if upscale != 1.0:
        import cv2

        gray = cv2.resize(gray, None, fx=upscale, fy=upscale, interpolation=cv2.INTER_CUBIC)
    rgb = np.repeat(gray[:, :, None], 3, axis=2)
    return rgb, width, height


class FaceWorker:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        try:
            import mediapipe as mp
            from mediapipe.tasks import python
            from mediapipe.tasks.python import vision
        except Exception as exc:  # noqa: BLE001 - this is a worker diagnostic.
            raise SystemExit(f"failed to import mediapipe worker deps: {exc}") from exc

        model = Path(args.model)
        if not model.exists():
            raise SystemExit(f"missing model: {model}; run make mediapipe-fetch-model")

        self.mp = mp
        base_options = python.BaseOptions(model_asset_path=str(model))
        options = vision.FaceLandmarkerOptions(
            base_options=base_options,
            running_mode=vision.RunningMode.VIDEO,
            num_faces=1,
            output_face_blendshapes=args.blendshapes,
            output_facial_transformation_matrixes=True,
            min_face_detection_confidence=args.min_detection_confidence,
            min_face_presence_confidence=args.min_presence_confidence,
            min_tracking_confidence=args.min_tracking_confidence,
        )
        self.landmarker = vision.FaceLandmarker.create_from_options(options)
        self.last_timestamp_ms = -1

    def process(self, path: Path, timestamp_ms: int) -> dict:
        start = time.perf_counter()
        rgb, original_width, original_height = preprocess_frame(path, self.args.preprocess, self.args.upscale)
        if timestamp_ms <= self.last_timestamp_ms:
            timestamp_ms = self.last_timestamp_ms + 1
        self.last_timestamp_ms = timestamp_ms

        image = self.mp.Image(image_format=self.mp.ImageFormat.SRGB, data=rgb)
        result = self.landmarker.detect_for_video(image, timestamp_ms)
        elapsed = (time.perf_counter() - start) * 1000.0
        present = bool(result.face_landmarks)
        out: dict = {
            "path": str(path),
            "timestamp_ms": timestamp_ms,
            "present": present,
            "latency_ms": elapsed,
            "preprocess": self.args.preprocess,
            "upscale": self.args.upscale,
            "landmarks": [],
            "matrix": None,
            "yaw": None,
            "pitch": None,
            "roll": None,
            "matrix_yaw": None,
            "matrix_pitch": None,
            "matrix_roll": None,
            "landmark_yaw": None,
            "landmark_pitch": None,
            "landmark_roll": None,
            "blendshapes": [],
        }
        if not present:
            return out
        landmarks = result.face_landmarks[0]
        out["landmarks"] = [
            {
                "index": i,
                "x": lm.x * original_width,
                "y": lm.y * original_height,
                "z": lm.z * original_width,
                "visibility": getattr(lm, "visibility", 0.0),
                "presence": getattr(lm, "presence", 0.0),
            }
            for i, lm in enumerate(landmarks)
        ]
        matrices = getattr(result, "facial_transformation_matrixes", None) or []
        landmark_pose = landmark_normal_ypr(landmarks)
        if landmark_pose is not None:
            out["landmark_yaw"], out["landmark_pitch"], out["landmark_roll"] = landmark_pose
        if matrices:
            matrix = [float(v) for row in matrices[0] for v in row]
            out["matrix"] = matrix
            yaw, pitch, roll = rotation_to_ypr(matrix, self.args.rotation_mode)
            out["matrix_yaw"] = yaw
            out["matrix_pitch"] = pitch
            out["matrix_roll"] = roll
            if self.args.pose_source == "landmark-normal" and landmark_pose is not None:
                yaw, pitch, roll = landmark_pose
            elif self.args.pose_source == "hybrid" and landmark_pose is not None:
                pitch = hybrid_pitch(pitch, landmark_pose[1])
            out["yaw"] = yaw
            out["pitch"] = pitch
            out["roll"] = roll
            out["translation"] = [matrix[3], matrix[7], matrix[11]]
        elif landmark_pose is not None:
            out["yaw"], out["pitch"], out["roll"] = landmark_pose
        if self.args.blendshapes and result.face_blendshapes:
            out["blendshapes"] = [
                {"category": item.category_name, "score": float(item.score)}
                for item in result.face_blendshapes[0]
            ]
        return out


def self_test() -> int:
    try:
        import cv2  # noqa: F401
        import mediapipe  # noqa: F401
        import numpy  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        print(f"self_test=failed error={exc}", file=sys.stderr)
        return 1
    print("self_test=ok")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", type=Path, default=ROOT / "assets" / "mediapipe" / "face_landmarker.task")
    parser.add_argument("--preprocess", choices=("raw", "stretch", "clahe"), default="clahe")
    parser.add_argument("--upscale", type=float, default=1.0)
    parser.add_argument("--min-detection-confidence", type=float, default=0.35)
    parser.add_argument("--min-presence-confidence", type=float, default=0.35)
    parser.add_argument("--min-tracking-confidence", type=float, default=0.35)
    parser.add_argument("--rotation-mode", choices=("forward", "euler"), default="forward")
    parser.add_argument("--pose-source", choices=("matrix", "landmark-normal", "hybrid"), default="hybrid")
    parser.add_argument("--blendshapes", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()

    if args.self_test:
        return self_test()

    worker = FaceWorker(args)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            path_text, timestamp_text = line.split("\t", 1)
            result = worker.process(Path(path_text), int(float(timestamp_text)))
        except Exception as exc:  # noqa: BLE001 - keep worker alive if one frame fails.
            result = {"present": False, "error": str(exc), "line": line}
        print(json.dumps(result, separators=(",", ":")), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
