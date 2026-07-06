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


def rotation_to_ypr(matrix: list[float]) -> tuple[float, float, float]:
    # Matrix is row-major 4x4. Decompose the upper-left 3x3 as yaw(Y),
    # pitch(X), roll(Z). Final sign mapping is dashboard-tunable later.
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
        if matrices:
            matrix = [float(v) for row in matrices[0] for v in row]
            out["matrix"] = matrix
            yaw, pitch, roll = rotation_to_ypr(matrix)
            out["yaw"] = yaw
            out["pitch"] = pitch
            out["roll"] = roll
            out["translation"] = [matrix[3], matrix[7], matrix[11]]
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
