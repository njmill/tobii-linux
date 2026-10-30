"""Tracking data types, native I/O, inference, mapping and persistence. No GUI dependency."""
import argparse
import csv
import json
import math
import os
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass
from pathlib import Path
from statistics import median
from typing import Callable
from capture_control import DemandReceiver
from pose_runtime import PacketFilter, TrackingTrace, filter_settings, timed_alpha
from head_position import HeadPositionEstimator, HeadPositionProfile
from native_head_position import NativeHeadEstimator, NativeHeadProfile


CALIBRATION_TARGETS = [
    ("center", 0.5, 0.5),
    ("top-center", 0.5, 0.05),
    ("right-center", 0.95, 0.5),
    ("bottom-center", 0.5, 0.95),
    ("left-center", 0.05, 0.5),
    ("top-left", 0.08, 0.08),
    ("top-right", 0.92, 0.08),
    ("bottom-left", 0.08, 0.92),
    ("bottom-right", 0.92, 0.92),
]


BG = "#070a0e"


PANEL = "#0d1218"


STROKE = "#26313d"


TEXT = "#dbe7f3"


MUTED = "#91a0b2"


BLUE = "#53b7ff"


GREEN = "#46d97d"


YELLOW = "#f8d66d"


RED = "#ff7b7b"


TOBII_ET5_MARKER_SPACING_MM = 184.0


DEFAULT_WINDOW_GEOMETRY = "1680x1050"


TUNING_ATTRS = (
    "opentrack_yaw_scale",
    "opentrack_pitch_scale",
    "opentrack_roll_scale",
    "opentrack_x_scale",
    "opentrack_y_scale",
    "opentrack_z_scale",
    "opentrack_output_smoothing",
    "opentrack_motion_smoothing",
    "opentrack_prediction_ms",
    "opentrack_stillness_deadband_deg",
    "opentrack_stillness_velocity_dps",
    "opentrack_max_angle_step",
    "opentrack_max_translation_step",
    "opentrack_yaw_curve",
    "opentrack_pitch_curve",
    "opentrack_curve_knee_deg",
    "blink_hold_s",
    "mediapipe_yaw_output_scale",
    "mediapipe_pitch_output_scale",
    "mediapipe_roll_output_scale",
    "mediapipe_pitch_yaw_comp",
    "opentrack_max_output_angle",
    "harness",
    "tobii_roll_source",
    "tobii_head_pose_source",
)


# Physical gain baseline; UI and version-2 tuning expose its multipliers.
TUNING_GAIN_BASELINES = {
    "opentrack_yaw_scale": 11.75,
    "opentrack_pitch_scale": -8.25,
    "opentrack_roll_scale": 1.0,
    "opentrack_x_scale": 1.0,  # Baseline disables X; zero remains explicit/off.
    "opentrack_y_scale": -0.5,
    "opentrack_z_scale": 1.0,
    "mediapipe_yaw_output_scale": 0.08,
    "mediapipe_pitch_output_scale": 0.10,
    "mediapipe_roll_output_scale": 0.08,
}


def tuning_multiplier(attr: str, physical_value: float) -> float:
    return physical_value / TUNING_GAIN_BASELINES.get(attr, 1.0)


def tuning_physical_value(attr: str, multiplier: float) -> float:
    return multiplier * TUNING_GAIN_BASELINES.get(attr, 1.0)


DEFAULT_TUNING_VALUES: dict[str, float | str] = {
    "opentrack_yaw_scale": 11.75,
    "opentrack_pitch_scale": -8.25,
    "opentrack_roll_scale": 1.0,
    "opentrack_x_scale": 0.0,
    "opentrack_y_scale": -0.5,
    "opentrack_z_scale": 1.0,
    "opentrack_output_smoothing": 0.03,
    "opentrack_motion_smoothing": 0.05,
    "opentrack_prediction_ms": 45.0,
    "opentrack_stillness_deadband_deg": 0.18,
    "opentrack_stillness_velocity_dps": 10.0,
    "opentrack_max_angle_step": 180.0,
    "opentrack_max_translation_step": 0.20,
    "opentrack_yaw_curve": 1.0,
    "opentrack_pitch_curve": 1.0,
    "opentrack_curve_knee_deg": 18.0,
    "blink_hold_s": 0.20,
    "mediapipe_yaw_output_scale": 0.08,
    "mediapipe_pitch_output_scale": 0.10,
    "mediapipe_roll_output_scale": 0.08,
    "mediapipe_pitch_yaw_comp": 0.0,
    "opentrack_max_output_angle": 160.0,
    "harness": "tobii",
    "tobii_roll_source": "pose",
    "tobii_head_pose_source": "face",
}


@dataclass
class EyeSample:
    elapsed_ms: float
    monotonic_ns: int
    sample_index: int
    gaze_valid: int
    gaze_x: float
    gaze_y: float
    validity_l: int
    validity_r: int
    eye_present_l: int
    eye_present_r: int
    pupil_l: float | None
    pupil_r: float | None
    left: tuple[float, float, float]
    right: tuple[float, float, float]

    @property
    def valid(self) -> bool:
        return (
            self.eye_present_l == 1
            and self.eye_present_r == 1
            and all(math.isfinite(value) for value in (*self.left, *self.right))
            and self.eye_distance > 20.0
        )

    @property
    def center(self) -> tuple[float, float, float]:
        return tuple((self.left[i] + self.right[i]) * 0.5 for i in range(3))

    @property
    def eye_delta(self) -> tuple[float, float, float]:
        return tuple(self.right[i] - self.left[i] for i in range(3))

    @property
    def eye_distance(self) -> float:
        dx, dy, dz = self.eye_delta
        return math.sqrt(dx * dx + dy * dy + dz * dz)


@dataclass
class EyeBaseline:
    left: tuple[float, float, float]
    right: tuple[float, float, float]
    center: tuple[float, float, float]
    delta: tuple[float, float, float]
    eye_distance: float
    yaw_ref: float
    roll_ref: float


@dataclass
class EyePose:
    yaw: float
    pitch_proxy: float
    roll: float
    tx: float
    ty: float
    tz: float
    eye_distance: float
    eye_distance_delta: float


@dataclass
class PoseConfidence:
    eye: float = 0.0
    face: float = 0.0
    blended: float = 0.0
    eye_distance_stable: float = 0.0
    velocity_ok: float = 0.0
    pitch: float = 0.0


@dataclass
class HQFrameStatus:
    enabled: bool = False
    running: bool = False
    last_frame_monotonic: float = 0.0
    frames: int = 0
    private_frames: int = 0
    kind0001: int = 0
    kind0002: int = 0
    kind0003: int = 0
    kind_other: int = 0
    latest_kind: int = 0
    latest_avg: float = 0.0
    latest_neighbor_diff: float = 0.0
    fps: float = 0.0
    errors: int = 0
    timeouts: int = 0
    last_error: str = ""
    session_dir: str = ""


@dataclass
class MediaPipeFaceSample:
    frame: int
    seen_monotonic: float
    confidence: float
    yaw: float
    pitch: float
    roll: float
    tx: float
    ty: float
    tz: float
    face_width: float
    face_height: float
    landmarks: int
    latency_ms: float
    error: str = ""
    matrix: tuple[float, ...] | None = None
    matrix_pitch: float | None = None
    landmark_pitch: float | None = None
    image_width: int = 0
    image_height: int = 0
    pose_landmarks: tuple[tuple[int, float, float, float], ...] = ()

    @property
    def usable(self) -> bool:
        return self.confidence > 0.0 and all(math.isfinite(value) for value in (self.yaw, self.pitch, self.roll))


@dataclass
class MonitorInfo:
    name: str
    x: int
    y: int
    width: int
    height: int
    primary: bool = False


@dataclass
class ScreenCalibrationProfile:
    version: int
    created_unix: float
    monitor_name: str
    monitor_x: int
    monitor_y: int
    width_px: int
    height_px: int
    marker_spacing_mm: float
    marker_spacing_px: float
    px_per_mm: float
    mm_per_px: float
    device_center_x_px: float
    device_center_y_px: float

    @classmethod
    def from_json(cls, data: object) -> "ScreenCalibrationProfile | None":
        if not isinstance(data, dict):
            return None
        try:
            return cls(
                version=int(data.get("version", 1)),
                created_unix=float(data.get("created_unix", 0.0)),
                monitor_name=str(data.get("monitor_name", "")),
                monitor_x=int(data.get("monitor_x", 0)),
                monitor_y=int(data.get("monitor_y", 0)),
                width_px=int(data.get("width_px", 0)),
                height_px=int(data.get("height_px", 0)),
                marker_spacing_mm=float(data.get("marker_spacing_mm", TOBII_ET5_MARKER_SPACING_MM)),
                marker_spacing_px=float(data.get("marker_spacing_px", 0.0)),
                px_per_mm=float(data.get("px_per_mm", 0.0)),
                mm_per_px=float(data.get("mm_per_px", 0.0)),
                device_center_x_px=float(data.get("device_center_x_px", 0.0)),
                device_center_y_px=float(data.get("device_center_y_px", 0.0)),
            )
        except (TypeError, ValueError):
            return None

    def to_json(self) -> dict[str, float | int | str]:
        return {
            "version": self.version,
            "created_unix": self.created_unix,
            "monitor_name": self.monitor_name,
            "monitor_x": self.monitor_x,
            "monitor_y": self.monitor_y,
            "width_px": self.width_px,
            "height_px": self.height_px,
            "marker_spacing_mm": self.marker_spacing_mm,
            "marker_spacing_px": self.marker_spacing_px,
            "px_per_mm": self.px_per_mm,
            "mm_per_px": self.mm_per_px,
            "device_center_x_px": self.device_center_x_px,
            "device_center_y_px": self.device_center_y_px,
        }


class SmoothValue:
    def __init__(self, alpha: float) -> None:
        self.alpha = alpha
        self.value: float | None = None

    def update(self, value: float) -> float:
        if self.value is None:
            self.value = value
        else:
            self.value += self.alpha * (value - self.value)
        return self.value

    def reset(self) -> None:
        self.value = None


class PoseSmoother:
    def __init__(self, alpha: float) -> None:
        self.alpha = alpha
        self.filters = {name: SmoothValue(alpha) for name in ("yaw", "pitch", "roll", "tx", "ty", "tz", "dist", "ddist")}

    def update(self, pose: EyePose, dt: float = 1.0 / 60.0) -> EyePose:
        for filt in self.filters.values():
            filt.alpha = timed_alpha(self.alpha, dt, 60.0)
        return EyePose(
            yaw=self.filters["yaw"].update(pose.yaw),
            pitch_proxy=self.filters["pitch"].update(pose.pitch_proxy),
            roll=self.filters["roll"].update(pose.roll),
            tx=self.filters["tx"].update(pose.tx),
            ty=self.filters["ty"].update(pose.ty),
            tz=self.filters["tz"].update(pose.tz),
            eye_distance=self.filters["dist"].update(pose.eye_distance),
            eye_distance_delta=self.filters["ddist"].update(pose.eye_distance_delta),
        )

    def reset(self) -> None:
        for filt in self.filters.values():
            filt.reset()

    def set_alpha(self, alpha: float) -> None:
        self.alpha = alpha
        for filt in self.filters.values():
            filt.alpha = alpha


def blend_pose(a: EyePose, b: EyePose, b_weight: float) -> EyePose:
    b_weight = clamp(b_weight, 0.0, 1.0)
    a_weight = 1.0 - b_weight
    return EyePose(
        yaw=a.yaw * a_weight + b.yaw * b_weight,
        pitch_proxy=a.pitch_proxy * a_weight + b.pitch_proxy * b_weight,
        roll=a.roll * a_weight + b.roll * b_weight,
        tx=a.tx * a_weight + b.tx * b_weight,
        ty=a.ty * a_weight + b.ty * b_weight,
        tz=a.tz * a_weight + b.tz * b_weight,
        eye_distance=a.eye_distance * a_weight + b.eye_distance * b_weight,
        eye_distance_delta=a.eye_distance_delta * a_weight + b.eye_distance_delta * b_weight,
    )


def load_screen_calibration(path: Path | None) -> ScreenCalibrationProfile | None:
    if path is None or not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return ScreenCalibrationProfile.from_json(data)


def save_screen_calibration(path: Path | None, profile: ScreenCalibrationProfile) -> bool:
    if path is None:
        return False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        tmp_path.write_text(json.dumps(profile.to_json(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
        tmp_path.replace(path)
        return True
    except OSError:
        return False


def load_gaze_calibration(path: Path | None) -> tuple[list[float], list[float]] | None:
    if path is None or not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    x_coeffs = data.get("x_coeffs")
    y_coeffs = data.get("y_coeffs")
    if not isinstance(x_coeffs, list) or not isinstance(y_coeffs, list):
        return None
    try:
        x_values = [float(value) for value in x_coeffs]
        y_values = [float(value) for value in y_coeffs]
    except (TypeError, ValueError):
        return None
    if len(x_values) != 6 or len(y_values) != 6:
        return None
    if not all(math.isfinite(value) for value in (*x_values, *y_values)):
        return None
    return (x_values, y_values)


def save_gaze_calibration(path: Path | None, affine: tuple[list[float], list[float]]) -> bool:
    if path is None:
        return False
    x_coeffs, y_coeffs = affine
    data = {
        "version": 1,
        "created_unix": time.time(),
        "x_coeffs": [float(value) for value in x_coeffs],
        "y_coeffs": [float(value) for value in y_coeffs],
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        tmp_path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        tmp_path.replace(path)
        return True
    except OSError:
        return False


def delete_file_quietly(path: Path | None) -> None:
    if path is None:
        return
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass


class OpenTrackUdpBridge:
    def __init__(self, target: str, args: argparse.Namespace) -> None:
        self.addr = self.parse_udp_target(target)
        self.extra_addrs = self.parse_extra_targets(getattr(args, "stock_gaze_udp", ""))
        self.args = args
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sent = 0
        self.last_error = ""
        self.last_send_monotonic = 0.0
        self.last_packet: tuple[float, ...] | None = None
        self.last_pose_source = "none"
        self.latest_values: tuple[float, ...] | None = None
        self.latest_pose_source = "none"
        self.filter = PacketFilter(args)
        self.sample_time = 0.0
        self.gaze_time: float | None = None
        self.mapping_source = "eye binocular"
        self.context = {}
        self.generation = 0
        trace_path = getattr(args, "tracking_trace", None)
        self.trace = TrackingTrace(trace_path, args) if trace_path else None
        self.lock = threading.Lock()
        self.closed = threading.Event()
        self.thread = threading.Thread(target=self.pump, name="tobii-udp-pump", daemon=True)
        self.thread.start()

    def retarget(self, target: str) -> None:
        addr = self.parse_udp_target(target)
        extra_addrs = self.parse_extra_targets(getattr(self.args, "stock_gaze_udp", ""))
        with self.lock:
            self.addr = addr
            self.extra_addrs = extra_addrs
            self.last_error = ""

    @staticmethod
    def parse_udp_target(target: str) -> tuple[str, int]:
        if ":" in target:
            host, port_text = target.rsplit(":", 1)
        else:
            host, port_text = "127.0.0.1", target
        return (host, int(port_text))

    @classmethod
    def parse_extra_targets(cls, targets: str) -> list[tuple[str, int]]:
        addrs: list[tuple[str, int]] = []
        for raw_target in (targets or "").split(","):
            target = raw_target.strip()
            if not target:
                continue
            try:
                addrs.append(cls.parse_udp_target(target))
            except ValueError:
                continue
        return addrs

    def reset(self) -> None:
        with self.lock:
            self.filter.reset()
            self.latest_values = None
            self.generation += 1

    def send(
        self, pose: EyePose, gaze: tuple[float, float] | None = None, *,
        sample_time: float | None = None, gaze_time: float | None = None,
        context: dict | None = None,
    ) -> None:
        pose_source = getattr(self.args, "pose_source", "")
        source_code = {
            "eye binocular": 1.0,
            "blended eye+face": 1.5,
            "MediaPipe head primary": 2.5,
            "MediaPipe yaw/pitch fallback": 2.5,
            "hold last pose": 3.0,
        }.get(pose_source, 0.0)
        yaw_pose = pose.yaw
        pitch_pose = pose.pitch_proxy
        roll_pose = pose.roll
        if pose_source != "hold last pose":
            self.mapping_source = pose_source
        if self.mapping_source.startswith("MediaPipe"):
            yaw_pose *= self.args.mediapipe_yaw_output_scale
            pitch_pose *= self.args.mediapipe_pitch_output_scale
            roll_pose *= self.args.mediapipe_roll_output_scale
        yaw_out = yaw_pose * self.args.opentrack_yaw_sign * self.args.opentrack_yaw_scale
        pitch_out = pitch_pose * self.args.opentrack_pitch_sign * self.args.opentrack_pitch_scale
        roll_out = roll_pose * self.args.opentrack_roll_sign * self.args.opentrack_roll_scale
        if getattr(self.args, "harness", "") == "tobii" and getattr(self.args, "tobii_roll_source", "zero") == "zero":
            roll_out = 0.0
        yaw_packet = self.apply_angle_curve(yaw_out, self.args.opentrack_yaw_curve, self.args.opentrack_curve_knee_deg)
        pitch_packet = self.apply_angle_curve(pitch_out, self.args.opentrack_pitch_curve, self.args.opentrack_curve_knee_deg)
        max_output_angle = abs(getattr(self.args, "opentrack_max_output_angle", 160.0))
        if max_output_angle > 0:
            yaw_packet = clamp(yaw_packet, -max_output_angle, max_output_angle)
            pitch_packet = clamp(pitch_packet, -max_output_angle, max_output_angle)
            roll_out = clamp(roll_out, -max_output_angle, max_output_angle)
        gaze_x, gaze_y, gaze_valid = (0.5, 0.5, 0.0)
        if gaze is not None and math.isfinite(gaze[0]) and math.isfinite(gaze[1]):
            gaze_x = clamp(gaze[0], 0.0, 1.0)
            gaze_y = clamp(gaze[1], 0.0, 1.0)
            gaze_valid = 1.0
        values = (
            (pose.tx / 10.0) * self.args.opentrack_x_sign * self.args.opentrack_x_scale,
            (pose.ty / 10.0) * self.args.opentrack_y_sign * self.args.opentrack_y_scale,
            (pose.tz / 10.0) * self.args.opentrack_z_sign * self.args.opentrack_z_scale,
            yaw_packet,
            pitch_packet,
            roll_out,
            source_code,
            float(self.sent),
            gaze_x,
            gaze_y,
            gaze_valid,
            float(self.sent),
        )
        with self.lock:
            self.latest_values = values
            self.latest_pose_source = pose_source
            self.sample_time = time.monotonic() if sample_time is None else sample_time
            self.gaze_time = gaze_time
            self.context = context or {}

    def pump(self) -> None:
        next_send = time.monotonic()
        while not self.closed.is_set():
            period = 1.0 / clamp(getattr(self.args, "opentrack_send_hz", 120.0), 30.0, 240.0)
            now = time.monotonic()
            if now < next_send:
                self.closed.wait(next_send - now)
                continue
            next_send = max(next_send + period, now)
            with self.lock:
                values = self.latest_values
                if values is None:
                    continue
                send_values = self.filter.update(values, self.sample_time, now, self.gaze_time)
                frame_id = float(self.sent)
                send_values = (*send_values[:7], frame_id, send_values[8], send_values[9], send_values[10], frame_id)
                packet = struct.pack("=" + "d" * len(send_values), *send_values)
                addr = self.addr
                extra_addrs = list(self.extra_addrs)
                pose_source = self.latest_pose_source
                trace_row = None
                if self.trace is not None:
                    trace_row = {
                        "kind": "packet", "time": now,
                        "sample_time": self.sample_time, "gaze_time": self.gaze_time,
                        "generation": self.generation, "input": values,
                        "output": send_values, "filter_settings": filter_settings(self.args),
                        "context": self.context,
                    }
            try:
                targets = [addr]
                for extra_addr in extra_addrs:
                    if extra_addr not in targets:
                        targets.append(extra_addr)
                for target_addr in targets:
                    self.sock.sendto(packet, target_addr)
                with self.lock:
                    self.sent += 1
                    self.last_send_monotonic = time.monotonic()
                    self.last_packet = send_values
                    self.last_pose_source = pose_source
                    self.last_error = ""
                    if self.trace is not None:
                        try:
                            self.trace.write(trace_row)
                        except (OSError, ValueError) as exc:
                            self.last_error = f"tracking trace disabled: {exc}"
                            self.trace.close()
                            self.trace = None
            except OSError as exc:
                with self.lock:
                    self.last_error = str(exc)

    def apply_angle_curve(self, value: float, curve: float, knee: float) -> float:
        curve = max(curve, 1.0)
        knee = max(knee, 1.0)
        magnitude = abs(value)
        if magnitude < 1e-6 or curve == 1.0:
            return value
        curved = (magnitude ** curve) / (knee ** (curve - 1.0))
        return math.copysign(curved, value)

    def close(self) -> None:
        self.closed.set()
        self.thread.join()
        self.sock.close()
        if self.trace is not None:
            self.trace.close()
            self.trace = None


class CsvTail:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.offset = 0
        self.header: list[str] | None = None
        self.partial = ""

    def read_new(self) -> list[EyeSample]:
        if not self.path.exists():
            return []
        with self.path.open("r", encoding="utf-8", newline="") as f:
            f.seek(self.offset)
            chunk = f.read()
            self.offset = f.tell()
        if not chunk:
            return []

        text = self.partial + chunk
        if not text.endswith("\n"):
            text, self.partial = text.rsplit("\n", 1) if "\n" in text else ("", text)
        else:
            self.partial = ""

        lines = [line for line in text.splitlines() if line.strip()]
        if not lines:
            return []
        if self.header is None:
            self.header = next(csv.reader([lines.pop(0)]))
        samples = []
        for values in csv.reader(lines):
            if len(values) != len(self.header):
                continue
            try:
                samples.append(parse_sample(dict(zip(self.header, values))))
            except (KeyError, ValueError):
                continue
        return samples


class MediaPipeWorkerClient:
    def __init__(self, root_dir: Path, args: argparse.Namespace) -> None:
        self.root_dir = root_dir
        self.args = args
        self.proc: subprocess.Popen[str] | None = None
        self.lifecycle_lock = threading.Lock()
        self.closed = False

    def start(self) -> None:
        with self.lifecycle_lock:
            if self.closed:
                raise RuntimeError("MediaPipe worker is closed")
            if self.proc is not None:
                return
            cmd = [
                str(self.args.mediapipe_python),
                str(self.root_dir / "scripts" / "runtime" / "mediapipe-face-worker.py"),
                "--model",
                str(self.args.mediapipe_model),
                "--preprocess",
                self.args.mediapipe_preprocess,
                "--upscale",
                str(self.args.mediapipe_upscale),
                "--rotation-mode",
                self.args.mediapipe_rotation_mode,
                "--pose-source",
                self.args.mediapipe_pose_source,
                "--min-detection-confidence",
                str(self.args.mediapipe_min_detection_confidence),
                "--min-presence-confidence",
                str(self.args.mediapipe_min_presence_confidence),
                "--min-tracking-confidence",
                str(self.args.mediapipe_min_tracking_confidence),
            ]
            self.proc = subprocess.Popen(
                cmd,
                cwd=self.root_dir,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )

    def process(self, path: Path, elapsed_ms: float) -> dict[str, object]:
        self.start()
        proc = self.proc
        if proc is None:
            raise RuntimeError("MediaPipe worker stopped")
        if proc.poll() is not None:
            stderr = proc.stderr.read() if proc.stderr else ""
            raise RuntimeError(f"mediapipe worker exited rc={proc.returncode}: {stderr.strip()[-240:]}")
        assert proc.stdin is not None
        assert proc.stdout is not None
        proc.stdin.write(f"{path}\t{int(elapsed_ms)}\n")
        proc.stdin.flush()
        line = proc.stdout.readline()
        if not line:
            stderr = proc.stderr.read() if proc.stderr else ""
            raise RuntimeError(f"mediapipe worker produced no output: {stderr.strip()[-240:]}")
        return json.loads(line)

    def close(self) -> None:
        with self.lifecycle_lock:
            self.closed = True
            if self.proc is None:
                return
            if self.proc.poll() is None:
                try:
                    if self.proc.stdin is not None:
                        self.proc.stdin.close()
                    self.proc.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    self.proc.terminate()
                    try:
                        self.proc.wait(timeout=1.0)
                    except subprocess.TimeoutExpired:
                        self.proc.kill()
                        self.proc.wait(timeout=1.0)
            for stream in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
                if stream is not None:
                    stream.close()
            self.proc = None


class MediaPipeFaceBridge:
    def __init__(self, root_dir: Path, args: argparse.Namespace, log_dir: Path) -> None:
        self.root_dir = root_dir
        self.args = args
        self.log_dir = log_dir
        self.worker = MediaPipeWorkerClient(root_dir, args)
        self.thread: threading.Thread | None = None
        self.processor_thread: threading.Thread | None = None
        self.pending_frame_path: Path | None = None
        self.pending_frame_elapsed_ms = 0.0
        self.pending_condition = threading.Condition()
        self.stop = threading.Event()
        self.frames_seen = 0
        self.frames_sent = 0
        self.frames_present = 0
        self.last_frame_monotonic = 0.0
        self.last_result_monotonic = 0.0
        self.last_error = ""
        self.latest_sample: MediaPipeFaceSample | None = None
        self.preview = None
        self.lock = threading.Lock()

    def start(self) -> bool:
        model = Path(self.args.mediapipe_model)
        python = Path(self.args.mediapipe_python)
        if not python.exists():
            self.last_error = f"missing MediaPipe python {python}"
            return False
        if not model.exists():
            self.last_error = f"missing MediaPipe model {model}"
            return False
        self.log_dir.mkdir(parents=True, exist_ok=True)
        return True

    def attach_mux_stdout(self, stdout) -> None:
        self.thread = threading.Thread(target=self._run_frame_bridge, args=(stdout,), name="mediapipe-frame-bridge", daemon=True)
        self.processor_thread = threading.Thread(target=self._run_frame_processor, name="mediapipe-frame-processor", daemon=True)
        self.processor_thread.start()
        self.thread.start()

    def terminate(self) -> None:
        self.stop.set()
        with self.pending_condition:
            self.pending_condition.notify_all()
        self.worker.close()
        for thread in (self.thread, self.processor_thread):
            if thread is not None and thread is not threading.current_thread():
                thread.join(timeout=1.0)

    def snapshot(self) -> MediaPipeFaceSample | None:
        with self.lock:
            return self.latest_sample

    def proc_status(self) -> str:
        proc = self.worker.proc
        if proc is None:
            return "not started"
        rc = proc.poll()
        return "running" if rc is None else f"exited {rc}"

    def _run_frame_bridge(self, stdout) -> None:
        for raw_line in stdout:
            if self.stop.is_set():
                break
            line = raw_line.decode("utf-8", errors="replace").strip()
            prefix = "private_frame_dump path="
            if not line.startswith(prefix):
                continue
            self.frames_seen += 1
            self.last_frame_monotonic = time.monotonic()
            path = Path(line[len(prefix) :])
            if not path.is_absolute():
                path = self.root_dir / path
            wanted_stream = self.args.mediapipe_image_stream.lower().removeprefix("0x")
            if wanted_stream and f"stream{wanted_stream}" not in path.name.lower():
                continue
            with self.pending_condition:
                # Keep only the freshest frame. MediaPipe can be slower than the
                # mux frame stream; blocking this stdout reader can stall gaze too.
                self.pending_frame_path = path
                self.pending_frame_elapsed_ms = time.monotonic() * 1000.0
                self.pending_condition.notify()

    def _run_frame_processor(self) -> None:
        while not self.stop.is_set():
            with self.pending_condition:
                while self.pending_frame_path is None and not self.stop.is_set():
                    self.pending_condition.wait(timeout=0.25)
                if self.stop.is_set():
                    break
                path = self.pending_frame_path
                elapsed_ms = self.pending_frame_elapsed_ms
                self.pending_frame_path = None
            if path is not None:
                self.feed_frame(path, elapsed_ms)

    def feed_frame(self, path: Path, elapsed_ms: float | None = None) -> None:
        try:
            try:
                frame_bytes = path.read_bytes()
            except OSError:
                frame_bytes = b""
            result = self.worker.process(path, elapsed_ms if elapsed_ms is not None else time.monotonic() * 1000.0)
            self.frames_sent += 1
            now = time.monotonic()
            sample = self.sample_from_result(result, (elapsed_ms / 1000.0) if elapsed_ms is not None else now)
            with self.lock:
                self.latest_sample = sample
                self.preview = (frame_bytes, result.get("landmarks", []))
            if sample.usable:
                self.frames_present += 1
                self.last_result_monotonic = now
                self.last_error = ""
            elif sample.error:
                self.last_error = sample.error[:160]
        except (RuntimeError, OSError, ValueError, json.JSONDecodeError) as exc:
            self.last_error = str(exc)

    def sample_from_result(self, result: dict[str, object], now: float) -> MediaPipeFaceSample:
        valid_angles = all(
            isinstance(result.get(axis), (int, float)) and math.isfinite(result[axis])
            for axis in ("yaw", "pitch", "roll")
        )
        if not bool(result.get("present")) or not valid_angles:
            return MediaPipeFaceSample(
                frame=self.frames_sent,
                seen_monotonic=now,
                confidence=0.0,
                yaw=0.0,
                pitch=0.0,
                roll=0.0,
                tx=0.0,
                ty=0.0,
                tz=0.0,
                face_width=0.0,
                face_height=0.0,
                landmarks=0,
                latency_ms=float(result.get("latency_ms") or 0.0),
                error=str(result.get("error") or "no valid face pose"),
            )
        translation = result.get("translation") if isinstance(result.get("translation"), list) else []
        tx = float(translation[0]) if len(translation) > 0 else 0.0
        ty = float(translation[1]) if len(translation) > 1 else 0.0
        tz = float(translation[2]) if len(translation) > 2 else 0.0
        landmarks = result.get("landmarks")
        face_width = 0.0
        face_height = 0.0
        if isinstance(landmarks, list) and landmarks:
            xs = [float(item.get("x", 0.0)) for item in landmarks if isinstance(item, dict)]
            ys = [float(item.get("y", 0.0)) for item in landmarks if isinstance(item, dict)]
            if xs and ys:
                face_width = max(xs) - min(xs)
                face_height = max(ys) - min(ys)
        return MediaPipeFaceSample(
            frame=self.frames_sent,
            seen_monotonic=now,
            confidence=0.90,
            yaw=float(result.get("yaw") or 0.0),
            pitch=float(result.get("pitch") or 0.0),
            roll=float(result.get("roll") or 0.0),
            tx=tx,
            ty=ty,
            tz=tz,
            face_width=face_width,
            face_height=face_height,
            landmarks=len(landmarks) if isinstance(landmarks, list) else 0,
            latency_ms=float(result.get("latency_ms") or 0.0),
            matrix=tuple(result["matrix"]) if isinstance(result.get("matrix"), list) else None,
            matrix_pitch=result.get("matrix_pitch"),
            landmark_pitch=result.get("landmark_pitch"),
            image_width=int(result.get("image_width") or 0),
            image_height=int(result.get("image_height") or 0),
            pose_landmarks=tuple(
                (int(item["index"]), float(item["x"]), float(item["y"]), float(item["z"]))
                for item in (landmarks or [])
                if isinstance(item, dict) and item.get("index") in (1, 4, 33, 133, 263, 362, 61, 291, 152, 10)
                and all(isinstance(item.get(key), (int, float)) and math.isfinite(item[key]) for key in ("x", "y", "z"))
            ),
        )


class HQFrameWorker:
    def __init__(self, root_dir: Path, args: argparse.Namespace, out_dir: Path) -> None:
        self.root_dir = root_dir
        self.args = args
        self.out_dir = out_dir
        self.proc: subprocess.Popen[str] | None = None
        self.thread: threading.Thread | None = None
        self.log_path = out_dir / "hq-frame-worker.log"
        self.status = HQFrameStatus(enabled=True)
        self.frame_times: deque[float] = deque(maxlen=120)
        self.lock = threading.Lock()

    def start(self) -> bool:
        self.out_dir.mkdir(parents=True, exist_ok=True)
        probe = self.root_dir / "build" / "tobii-uvc-probe"
        if not probe.exists():
            self.status.last_error = f"missing {probe}"
            return False
        cmd = [
            str(probe),
            "--read-ep",
            "--frames-only",
            "--vs-only",
            "--reads",
            str(0 if self.args.hq_mode == "continuous" else self.args.hq_reads),
            "--timeout-ms",
            str(self.args.hq_timeout_ms),
            "--chunk-size",
            str(self.args.hq_chunk_size),
            "--sleep-ms",
            str(self.args.hq_sleep_ms),
            "--no-vs-commit",
            "--no-raw",
            "--out",
            str(self.out_dir),
        ]
        if self.args.hq_xu_set:
            cmd.extend(["--xu-set", self.args.hq_xu_set])
        log_file = self.log_path.open("w", encoding="utf-8")
        self.proc = subprocess.Popen(
            cmd,
            cwd=self.root_dir,
            stdout=subprocess.PIPE,
            stderr=log_file,
            text=True,
            bufsize=1,
            start_new_session=True,
        )
        log_file.close()
        self.status.running = True
        self.thread = threading.Thread(target=self._pump_stdout, name="hq-frame-worker", daemon=True)
        self.thread.start()
        return True

    def terminate(self) -> None:
        if self.proc is None:
            return
        if self.proc.poll() is None:
            try:
                os.killpg(self.proc.pid, signal.SIGTERM)
                self.proc.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                os.killpg(self.proc.pid, signal.SIGKILL)
        with self.lock:
            self.status.running = False

    def snapshot(self) -> HQFrameStatus:
        with self.lock:
            return HQFrameStatus(**self.status.__dict__)

    def _pump_stdout(self) -> None:
        import re

        assert self.proc is not None
        assert self.proc.stdout is not None
        private_re = re.compile(r"private_header kind=0x([0-9a-fA-F]+)")
        frame_re = re.compile(r"uvc_frame\[[0-9]+\]=.* avg=([0-9.\-]+) neighbor_diff=([0-9.\-]+)")
        endpoint_re = re.compile(r"ep82_read\[[0-9]+\]=bytes=([0-9]+)")
        for raw_line in self.proc.stdout:
            now = time.monotonic()
            line = raw_line.strip()
            with self.lock:
                if "endpoint_read dir=" in line:
                    marker = "endpoint_read dir="
                    start = line.find(marker) + len(marker)
                    end = line.find(" ", start)
                    self.status.session_dir = line[start:] if end < 0 else line[start:end]
                if "timeout" in line:
                    self.status.timeouts += 1
                if "error" in line.lower() and "errors=0" not in line.lower():
                    self.status.errors += 1
                    self.status.last_error = line[:160]
                endpoint = endpoint_re.search(line)
                if endpoint is not None:
                    try:
                        size = int(endpoint.group(1))
                        if size < 10:
                            self.status.errors += 1
                    except ValueError:
                        pass
                private = private_re.search(line)
                if private is not None:
                    try:
                        kind = int(private.group(1), 16)
                    except ValueError:
                        kind = 0
                    self.status.private_frames += 1
                    self.status.latest_kind = kind
                    if kind == 1:
                        self.status.kind0001 += 1
                    elif kind == 2:
                        self.status.kind0002 += 1
                    elif kind == 3:
                        self.status.kind0003 += 1
                    else:
                        self.status.kind_other += 1
                frame = frame_re.search(line)
                if frame is not None:
                    self.status.frames += 1
                    self.status.last_frame_monotonic = now
                    self.frame_times.append(now)
                    try:
                        self.status.latest_avg = float(frame.group(1))
                        self.status.latest_neighbor_diff = float(frame.group(2))
                    except ValueError:
                        pass
                    if len(self.frame_times) >= 2:
                        elapsed = self.frame_times[-1] - self.frame_times[0]
                        self.status.fps = (len(self.frame_times) - 1) / elapsed if elapsed > 0 else 0.0
        with self.lock:
            self.status.running = False
            rc = None
            if self.proc is not None:
                try:
                    rc = self.proc.wait(timeout=0.25)
                except subprocess.TimeoutExpired:
                    rc = self.proc.poll()
            self.status.last_error = f"exited rc={rc}"


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def ellipsize(text: str, max_chars: int) -> str:
    if max_chars <= 3 or len(text) <= max_chars:
        return text
    return text[: max_chars - 3] + "..."


def angle_delta_deg(value: float, neutral: float) -> float:
    return ((value - neutral + 180.0) % 360.0) - 180.0


def solve_linear(matrix: list[list[float]], vector: list[float]) -> list[float] | None:
    size = len(vector)
    rows = [matrix[i][:] + [vector[i]] for i in range(size)]
    for col in range(size):
        pivot = max(range(col, size), key=lambda row: abs(rows[row][col]))
        if abs(rows[pivot][col]) < 1e-9:
            return None
        rows[col], rows[pivot] = rows[pivot], rows[col]
        divisor = rows[col][col]
        for item in range(col, size + 1):
            rows[col][item] /= divisor
        for row in range(size):
            if row == col:
                continue
            factor = rows[row][col]
            for item in range(col, size + 1):
                rows[row][item] -= factor * rows[col][item]
    return [rows[i][size] for i in range(size)]


def gaze_features(raw_x: float, raw_y: float) -> list[float]:
    x = clamp(raw_x, -0.5, 1.5)
    y = clamp(raw_y, -0.5, 1.5)
    return [1.0, x, y, x * y, x * x, y * y]


def fit_axis(points: list[tuple[float, float, float]]) -> list[float] | None:
    dimension = len(gaze_features(0.0, 0.0))
    normal = [[0.0 for _ in range(dimension)] for _ in range(dimension)]
    rhs = [0.0 for _ in range(dimension)]
    for raw_x, raw_y, target in points:
        row = gaze_features(raw_x, raw_y)
        for i in range(dimension):
            rhs[i] += row[i] * target
            for j in range(dimension):
                normal[i][j] += row[i] * row[j]
    for i in range(1, dimension):
        normal[i][i] += 0.00001
    return solve_linear(normal, rhs)


def fit_gaze_calibration(points: list[tuple[float, float, float, float]]) -> tuple[list[float], list[float]] | None:
    if len(points) < len(gaze_features(0.0, 0.0)):
        return None
    x_points = [(raw_x, raw_y, target_x) for raw_x, raw_y, target_x, _target_y in points]
    y_points = [(raw_x, raw_y, target_y) for raw_x, raw_y, _target_x, target_y in points]
    x_coeffs = fit_axis(x_points)
    y_coeffs = fit_axis(y_points)
    if x_coeffs is None or y_coeffs is None:
        return None
    return (x_coeffs, y_coeffs)


def fnum(row: dict[str, str], key: str) -> float:
    value = row.get(key, "")
    return float(value) if value else float("nan")


def inum(row: dict[str, str], key: str, default: int = 0) -> int:
    value = row.get(key, "")
    return int(value) if value else default


def read_pgm(path: Path) -> tuple[int, int, bytes]:
    data = path.read_bytes()
    if not data.startswith(b"P5"):
        raise ValueError("not a P5 PGM")
    pos = 2
    tokens: list[bytes] = []
    while len(tokens) < 3:
        while pos < len(data) and data[pos] in b" \t\r\n":
            pos += 1
        if pos < len(data) and data[pos] == ord("#"):
            while pos < len(data) and data[pos] not in b"\r\n":
                pos += 1
            continue
        start = pos
        while pos < len(data) and data[pos] not in b" \t\r\n":
            pos += 1
        tokens.append(data[start:pos])
    while pos < len(data) and data[pos] in b" \t\r\n":
        pos += 1
    width = int(tokens[0])
    height = int(tokens[1])
    max_value = int(tokens[2])
    if max_value != 255:
        raise ValueError("unsupported PGM max value")
    pixels = data[pos : pos + width * height]
    if len(pixels) != width * height:
        raise ValueError("truncated PGM")
    return width, height, pixels


def resize_gray_nearest(pixels: bytes, width: int, height: int, out_width: int, out_height: int) -> bytes:
    out = bytearray(out_width * out_height)
    for y in range(out_height):
        sy = min(height - 1, int(y * height / out_height))
        for x in range(out_width):
            sx = min(width - 1, int(x * width / out_width))
            out[y * out_width + x] = pixels[sy * width + sx]
    return bytes(out)


def contrast_stretch_gray(pixels: bytes) -> bytes:
    if not pixels:
        return pixels
    hist = [0] * 256
    for value in pixels:
        hist[value] += 1
    total = len(pixels)
    low_target = max(0, int(total * 0.02))
    high_target = max(0, int(total * 0.995))
    accum = 0
    low = 0
    for value, count in enumerate(hist):
        accum += count
        if accum >= low_target:
            low = value
            break
    accum = 0
    high = 255
    for value, count in enumerate(hist):
        accum += count
        if accum >= high_target:
            high = value
            break
    if high <= low:
        return pixels
    scale = 255.0 / float(high - low)
    out = bytearray(len(pixels))
    for index, value in enumerate(pixels):
        out[index] = int(clamp((value - low) * scale, 0.0, 255.0))
    return bytes(out)


def gray_to_rgb(pixels: bytes) -> bytes:
    out = bytearray(len(pixels) * 3)
    j = 0
    for value in pixels:
        out[j] = value
        out[j + 1] = value
        out[j + 2] = value
        j += 3
    return bytes(out)


def parse_sample(row: dict[str, str]) -> EyeSample:
    pupil_l = fnum(row, "pupil_l_mm")
    pupil_r = fnum(row, "pupil_r_mm")
    return EyeSample(
        elapsed_ms=fnum(row, "elapsed_ms"),
        monotonic_ns=inum(row, "monotonic_ns"),
        sample_index=inum(row, "sample_index"),
        gaze_valid=inum(row, "gaze_valid"),
        gaze_x=fnum(row, "gaze_x_norm"),
        gaze_y=fnum(row, "gaze_y_norm"),
        validity_l=inum(row, "validity_l", -1),
        validity_r=inum(row, "validity_r", -1),
        eye_present_l=inum(row, "eye_present_l", -1),
        eye_present_r=inum(row, "eye_present_r", -1),
        pupil_l=pupil_l if math.isfinite(pupil_l) else None,
        pupil_r=pupil_r if math.isfinite(pupil_r) else None,
        left=(
            fnum(row, "eye_origin_l_x_mm"),
            fnum(row, "eye_origin_l_y_mm"),
            fnum(row, "eye_origin_l_z_mm"),
        ),
        right=(
            fnum(row, "eye_origin_r_x_mm"),
            fnum(row, "eye_origin_r_y_mm"),
            fnum(row, "eye_origin_r_z_mm"),
        ),
    )


def median_point(points: list[tuple[float, float, float]]) -> tuple[float, float, float]:
    return tuple(median(point[i] for point in points) for i in range(3))


def make_baseline(samples: list[EyeSample]) -> EyeBaseline | None:
    good = [sample for sample in samples if sample.valid]
    if len(good) < 5:
        return None
    left = median_point([sample.left for sample in good])
    right = median_point([sample.right for sample in good])
    center = tuple((left[i] + right[i]) * 0.5 for i in range(3))
    delta = tuple(right[i] - left[i] for i in range(3))
    eye_distance = math.sqrt(sum(value * value for value in delta))
    lateral = max(abs(delta[0]), 1.0)
    yaw_ref = math.degrees(math.atan2(delta[2], lateral))
    roll_ref = math.degrees(math.atan2(delta[1], lateral))
    return EyeBaseline(left=left, right=right, center=center, delta=delta, eye_distance=eye_distance, yaw_ref=yaw_ref, roll_ref=roll_ref)


def derive_pose(sample: EyeSample, baseline: EyeBaseline, sensor_distance_mm: float) -> EyePose:
    center = sample.center
    delta = sample.eye_delta
    lateral = max(abs(delta[0]), 1.0)
    yaw = math.degrees(math.atan2(delta[2], lateral)) - baseline.yaw_ref
    roll = math.degrees(math.atan2(delta[1], lateral)) - baseline.roll_ref
    tx = center[0] - baseline.center[0]
    ty = center[1] - baseline.center[1]
    tz = center[2] - baseline.center[2]
    pitch_proxy = math.degrees(math.atan2(-ty, max(sensor_distance_mm + tz, 100.0)))
    eye_distance = sample.eye_distance
    return EyePose(
        yaw=yaw,
        pitch_proxy=pitch_proxy,
        roll=roll,
        tx=tx,
        ty=ty,
        tz=tz,
        eye_distance=eye_distance,
        eye_distance_delta=eye_distance - baseline.eye_distance,
    )


def linear_fit(pairs: deque[tuple[float, float]]) -> tuple[float, float] | None:
    if not pairs:
        return None
    if len(pairs) < 8:
        raw_avg = sum(pair[0] for pair in pairs) / len(pairs)
        target_avg = sum(pair[1] for pair in pairs) / len(pairs)
        return (1.0, target_avg - raw_avg)
    raw_values = [pair[0] for pair in pairs]
    target_values = [pair[1] for pair in pairs]
    raw_mean = sum(raw_values) / len(raw_values)
    target_mean = sum(target_values) / len(target_values)
    denom = sum((value - raw_mean) ** 2 for value in raw_values)
    if denom < 1e-6:
        return (1.0, target_mean - raw_mean)
    slope = sum((raw_values[i] - raw_mean) * (target_values[i] - target_mean) for i in range(len(raw_values))) / denom
    slope = clamp(slope, -3.0, 3.0)
    intercept = target_mean - slope * raw_mean
    return (slope, intercept)


def build_sampler(root_dir: Path, skip_build: bool) -> None:
    if not skip_build:
        subprocess.run(["make", "build/tobii-gaze-native"], cwd=root_dir, check=True)


def build_mux_sampler(root_dir: Path, skip_build: bool) -> None:
    if not skip_build:
        subprocess.run(["make", "build/tobii-ttp-mux"], cwd=root_dir, check=True)


def build_hq_frame_worker(root_dir: Path, skip_build: bool) -> None:
    if not skip_build:
        subprocess.run(["make", "build/tobii-uvc-probe"], cwd=root_dir, check=True)


def write_runtime_pidfile(root_dir: Path, name: str, pid: int) -> None:
    runtime_dir = Path(os.environ.get("SC_TOBII_NATIVE_RUNTIME_DIR", root_dir / ".tmp" / "sc-tobii-native-runtime"))
    pid_dir = runtime_dir / "pids"
    try:
        pid_dir.mkdir(parents=True, exist_ok=True)
        (pid_dir / f"{name}.pid").write_text(f"{pid}\n", encoding="utf-8")
    except OSError:
        pass


def load_tuning(args: argparse.Namespace) -> None:
    tuning_file = getattr(args, "tuning_file", None)
    if tuning_file is None or not tuning_file.exists():
        return
    try:
        data = json.loads(tuning_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(data, dict):
        return
    version = data.get("tuning_version", 1)
    if version not in (1, 2):
        print(f"ignoring unsupported tuning version: {version}")
        return
    for attr in TUNING_ATTRS:
        value = data.get(attr)
        if attr == "harness" and value in ("tobii", "trackir"):
            setattr(args, attr, value)
        elif attr == "tobii_roll_source" and value in ("zero", "pose", "eye"):
            setattr(args, attr, "pose" if value == "eye" else value)
        elif attr == "tobii_head_pose_source" and value in ("face", "blend", "eye"):
            setattr(args, attr, value)
        elif isinstance(value, (int, float)):
            physical = tuning_physical_value(attr, float(value)) if version == 2 else float(value)
            setattr(args, attr, physical)


