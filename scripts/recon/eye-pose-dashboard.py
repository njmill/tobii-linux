#!/usr/bin/env python3
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
from dataclasses import dataclass
from pathlib import Path
from statistics import median
from tkinter import BOTH, Button, Canvas, Frame, Label, StringVar, Tk, Toplevel
from typing import Callable


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

DEFAULT_TUNING_VALUES: dict[str, float | str] = {
    "opentrack_yaw_scale": 8.0,
    "opentrack_pitch_scale": -14.0,
    "opentrack_roll_scale": 1.0,
    "opentrack_x_scale": 1.0,
    "opentrack_y_scale": 1.0,
    "opentrack_z_scale": 1.0,
    "opentrack_output_smoothing": 0.03,
    "opentrack_motion_smoothing": 0.05,
    "opentrack_prediction_ms": 45.0,
    "opentrack_stillness_deadband_deg": 0.18,
    "opentrack_stillness_velocity_dps": 10.0,
    "opentrack_max_angle_step": 180.0,
    "opentrack_max_translation_step": 0.20,
    "opentrack_yaw_curve": 2.0,
    "opentrack_pitch_curve": 2.0,
    "opentrack_curve_knee_deg": 18.0,
    "blink_hold_s": 0.20,
    "mediapipe_yaw_output_scale": 0.15,
    "mediapipe_pitch_output_scale": 0.10,
    "mediapipe_roll_output_scale": 0.15,
    "mediapipe_pitch_yaw_comp": 1.0,
    "opentrack_max_output_angle": 160.0,
    "harness": "tobii",
    "tobii_roll_source": "pose",
    "tobii_head_pose_source": "face",
}

HEAD_CALIBRATION_BINS = (
    ("yaw_left", "yaw L"),
    ("yaw_center", "yaw C"),
    ("yaw_right", "yaw R"),
    ("pitch_down", "pitch D"),
    ("pitch_center", "pitch C"),
    ("pitch_up", "pitch U"),
    ("roll_left", "roll L"),
    ("roll_center", "roll C"),
    ("roll_right", "roll R"),
    ("near", "near"),
    ("far", "far"),
)

HEAD_CALIBRATION_PHASES = (
    (
        "neutral",
        "Sit naturally, face the center, and keep your head still.",
        ("yaw_center", "pitch_center", "roll_center"),
    ),
    ("yaw_left", "Slowly turn your head left and pause at your comfortable limit.", ("yaw_left",)),
    ("yaw_right", "Slowly turn your head right and pause at your comfortable limit.", ("yaw_right",)),
    ("pitch_up", "Return to center, then slowly tilt your head up and hold the highest stable angle.", ("pitch_up",)),
    ("pitch_down", "Return to center, then slowly tilt your head down and pause at your comfortable limit.", ("pitch_down",)),
    ("roll_left", "Return to center, then tilt your head toward your left shoulder and pause.", ("roll_left",)),
    ("roll_right", "Return to center, then tilt your head toward your right shoulder and pause.", ("roll_right",)),
)


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
class HeadCalibrationProfile:
    yaw_left_scale: float = 1.0
    yaw_right_scale: float = 1.0
    yaw_left_sign: float = -1.0
    yaw_right_sign: float = 1.0
    pitch_up_scale: float = 1.0
    pitch_down_scale: float = 1.0
    pitch_up_sign: float = 1.0
    pitch_down_sign: float = -1.0
    roll_left_scale: float = 1.0
    roll_right_scale: float = 1.0
    roll_left_sign: float = -1.0
    roll_right_sign: float = 1.0
    face_yaw_neutral: float | None = None
    face_pitch_neutral: float | None = None
    face_roll_neutral: float | None = None
    pitch_up_mid_raw: float | None = None
    pitch_up_mid_value: float | None = None
    pitch_up_max_raw: float | None = None
    pitch_up_max_value: float | None = None
    pitch_down_raw: float | None = None
    pitch_down_value: float | None = None
    created: float = 0.0


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
    landmarks: int
    latency_ms: float
    error: str = ""

    @property
    def usable(self) -> bool:
        return self.confidence > 0.0 and all(math.isfinite(value) for value in (self.yaw, self.pitch, self.roll))


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
        self.filters = {name: SmoothValue(alpha) for name in ("yaw", "pitch", "roll", "tx", "ty", "tz", "dist", "ddist")}

    def update(self, pose: EyePose) -> EyePose:
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


def profile_has_pitch_map(profile: HeadCalibrationProfile) -> bool:
    return (
        profile.pitch_up_mid_raw is not None
        and profile.pitch_up_mid_value is not None
        and profile.pitch_up_max_raw is not None
        and profile.pitch_up_max_value is not None
    )


def scaled_pose(pose: EyePose, profile: HeadCalibrationProfile, scale_pitch: bool = True) -> EyePose:
    if pose.yaw == 0.0:
        yaw_scale = 1.0
    elif profile.yaw_left_sign * pose.yaw > 0.0:
        yaw_scale = profile.yaw_left_scale
    elif profile.yaw_right_sign * pose.yaw > 0.0:
        yaw_scale = profile.yaw_right_scale
    else:
        yaw_scale = profile.yaw_left_scale if pose.yaw < 0.0 else profile.yaw_right_scale
    if not scale_pitch or pose.pitch_proxy == 0.0:
        pitch_scale = 1.0
    elif profile.pitch_up_sign * pose.pitch_proxy > 0.0:
        pitch_scale = profile.pitch_up_scale
    elif profile.pitch_down_sign * pose.pitch_proxy > 0.0:
        pitch_scale = profile.pitch_down_scale
    else:
        pitch_scale = profile.pitch_down_scale if pose.pitch_proxy < 0.0 else profile.pitch_up_scale
    if pose.roll == 0.0:
        roll_scale = 1.0
    elif profile.roll_left_sign * pose.roll > 0.0:
        roll_scale = profile.roll_left_scale
    elif profile.roll_right_sign * pose.roll > 0.0:
        roll_scale = profile.roll_right_scale
    else:
        roll_scale = profile.roll_left_scale if pose.roll < 0.0 else profile.roll_right_scale
    return EyePose(
        yaw=pose.yaw * yaw_scale,
        pitch_proxy=pose.pitch_proxy * pitch_scale,
        roll=pose.roll * roll_scale,
        tx=pose.tx,
        ty=pose.ty,
        tz=pose.tz,
        eye_distance=pose.eye_distance,
        eye_distance_delta=pose.eye_distance_delta,
    )


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
        self.output_values: list[float] | None = None
        self.target_values: list[float] | None = None
        self.target_velocity: list[float] | None = None
        self.last_target_update_monotonic = 0.0
        self.last_smooth_monotonic = 0.0
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

    def send(self, pose: EyePose, gaze: tuple[float, float] | None = None) -> None:
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
        if pose_source.startswith("MediaPipe"):
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
            self.latest_pose_source = getattr(self.args, "pose_source", "none")

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
                send_values = self.smooth_packet(values)
                frame_id = float(self.sent)
                send_values = (*send_values[:7], frame_id, send_values[8], send_values[9], send_values[10], frame_id)
                packet = struct.pack("=" + "d" * len(send_values), *send_values)
                addr = self.addr
                extra_addrs = list(self.extra_addrs)
                pose_source = self.latest_pose_source
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
            except OSError as exc:
                with self.lock:
                    self.last_error = str(exc)

    def smooth_packet(self, values: tuple[float, ...]) -> tuple[float, ...]:
        now = time.monotonic()
        if self.output_values is None:
            self.output_values = list(values[:6])
            self.target_values = list(values[:6])
            self.target_velocity = [0.0] * 6
            self.last_target_update_monotonic = now
            self.last_smooth_monotonic = now
        else:
            still_alpha = clamp(self.args.opentrack_output_smoothing, 0.01, 1.0)
            motion_alpha = clamp(max(self.args.opentrack_motion_smoothing, still_alpha), still_alpha, 1.0)
            dt = clamp(now - self.last_smooth_monotonic, 0.001, 0.100) if self.last_smooth_monotonic else 1.0 / 60.0
            self.last_smooth_monotonic = now
            if self.target_values is None:
                self.target_values = list(values[:6])
                self.last_target_update_monotonic = now
            if self.target_velocity is None:
                self.target_velocity = [0.0] * 6

            velocity_alpha = clamp(motion_alpha * 0.65, 0.08, 0.65)
            prediction_s = clamp(self.args.opentrack_prediction_ms / 1000.0, 0.0, 0.160)
            # Step sliders are now jump guards, not normal speed governors. They only
            # prevent a bogus sample from teleporting the camera.
            step_scale = dt * 60.0
            max_angle_step = max(self.args.opentrack_max_angle_step, 0.1) * step_scale
            max_translation_step = max(self.args.opentrack_max_translation_step, 0.05) * step_scale
            sample_dt = clamp(now - self.last_target_update_monotonic, dt, 0.250) if self.last_target_update_monotonic else dt
            saw_target_update = False
            for index in range(6):
                target = values[index]
                step_limit = max_translation_step if index < 3 else max_angle_step
                target_delta = target - self.target_values[index]
                update_epsilon = 0.01 if index < 3 else 0.03
                stillness_deadband = 0.02 if index < 3 else max(self.args.opentrack_stillness_deadband_deg, update_epsilon)
                stillness_velocity = 1.0 if index < 3 else max(self.args.opentrack_stillness_velocity_dps, 0.1)
                if abs(target_delta) > update_epsilon:
                    measured_velocity = target_delta / sample_dt
                    self.target_velocity[index] += (measured_velocity - self.target_velocity[index]) * velocity_alpha
                    self.target_values[index] = target
                    saw_target_update = True
                elif sample_dt > 0.160:
                    self.target_velocity[index] *= max(0.0, 1.0 - dt * 8.0)
                extrapolate_s = clamp((now - self.last_target_update_monotonic) + prediction_s, 0.0, 0.140)
                predicted_target = self.target_values[index] + self.target_velocity[index] * extrapolate_s
                output_error = predicted_target - self.output_values[index]
                if index < 3:
                    motion_score = clamp(max(abs(output_error) / 0.35, abs(self.target_velocity[index]) / 8.0), 0.0, 1.0)
                else:
                    motion_score = clamp(max(abs(output_error) / 2.0, abs(self.target_velocity[index]) / 75.0), 0.0, 1.0)
                alpha = still_alpha + (motion_alpha - still_alpha) * (motion_score ** 0.55)
                damping = 1.0
                if abs(output_error) < stillness_deadband and abs(self.target_velocity[index]) <= stillness_velocity:
                    damping = clamp(abs(output_error) / stillness_deadband, 0.18, 1.0)
                wanted = output_error * alpha * damping
                self.output_values[index] += clamp(wanted, -step_limit, step_limit)
            if saw_target_update:
                self.last_target_update_monotonic = now
        return (*self.output_values, *values[6:])

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
        self.thread.join(timeout=0.5)
        self.sock.close()


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

    def start(self) -> None:
        if self.proc is not None:
            return
        cmd = [
            str(self.args.mediapipe_python),
            str(self.root_dir / "scripts" / "recon" / "mediapipe-face-worker.py"),
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
        assert self.proc is not None
        if self.proc.poll() is not None:
            stderr = self.proc.stderr.read() if self.proc.stderr else ""
            raise RuntimeError(f"mediapipe worker exited rc={self.proc.returncode}: {stderr.strip()[-240:]}")
        assert self.proc.stdin is not None
        assert self.proc.stdout is not None
        self.proc.stdin.write(f"{path}\t{int(elapsed_ms)}\n")
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        if not line:
            stderr = self.proc.stderr.read() if self.proc.stderr else ""
            raise RuntimeError(f"mediapipe worker produced no output: {stderr.strip()[-240:]}")
        return json.loads(line)

    def close(self) -> None:
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
        self.proc = None


class MediaPipeFaceBridge:
    def __init__(self, root_dir: Path, args: argparse.Namespace, log_dir: Path) -> None:
        self.root_dir = root_dir
        self.args = args
        self.log_dir = log_dir
        self.worker = MediaPipeWorkerClient(root_dir, args)
        self.thread: threading.Thread | None = None
        self.stop = threading.Event()
        self.frames_seen = 0
        self.frames_sent = 0
        self.frames_present = 0
        self.last_frame_monotonic = 0.0
        self.last_result_monotonic = 0.0
        self.last_error = ""
        self.latest_sample: MediaPipeFaceSample | None = None
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
        self.thread.start()

    def terminate(self) -> None:
        self.stop.set()
        self.worker.close()

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
            self.feed_frame(path)

    def feed_frame(self, path: Path) -> None:
        try:
            result = self.worker.process(path, time.monotonic() * 1000.0)
            self.frames_sent += 1
            now = time.monotonic()
            sample = self.sample_from_result(result, now)
            with self.lock:
                self.latest_sample = sample
            if sample.usable:
                self.frames_present += 1
                self.last_result_monotonic = now
                self.last_error = ""
            elif sample.error:
                self.last_error = sample.error[:160]
        except (RuntimeError, OSError, ValueError, json.JSONDecodeError) as exc:
            self.last_error = str(exc)

    def sample_from_result(self, result: dict[str, object], now: float) -> MediaPipeFaceSample:
        if not bool(result.get("present")):
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
                landmarks=0,
                latency_ms=float(result.get("latency_ms") or 0.0),
                error=str(result.get("error") or "no face"),
            )
        translation = result.get("translation") if isinstance(result.get("translation"), list) else []
        tx = float(translation[0]) if len(translation) > 0 else 0.0
        ty = float(translation[1]) if len(translation) > 1 else 0.0
        tz = float(translation[2]) if len(translation) > 2 else 0.0
        landmarks = result.get("landmarks")
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
            landmarks=len(landmarks) if isinstance(landmarks, list) else 0,
            latency_ms=float(result.get("latency_ms") or 0.0),
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


class EyePoseDashboard:
    def __init__(
        self,
        root: Tk,
        args: argparse.Namespace,
        csv_path: Path,
        proc: subprocess.Popen,
        sampler_cmd: list[str],
        root_dir: Path,
        log_path: Path,
        mediapipe_bridge: MediaPipeFaceBridge | None = None,
        hq_worker: HQFrameWorker | None = None,
    ) -> None:
        self.root = root
        self.args = args
        self.csv_path = csv_path
        self.proc = proc
        self.sampler_cmd = sampler_cmd
        self.root_dir = root_dir
        self.log_path = log_path
        self.mediapipe_bridge = mediapipe_bridge
        self.hq_worker = hq_worker
        self.tail = CsvTail(csv_path)
        self.canvas = Canvas(root, background=BG, highlightthickness=0)
        self.canvas.pack(fill=BOTH, expand=True)
        self.click_targets: list[tuple[float, float, float, float, Callable[[float, float], None]]] = []
        self.drag_target: Callable[[float, float], None] | None = None
        self.canvas.bind("<Button-1>", self.on_click)
        self.canvas.bind("<B1-Motion>", self.on_drag)
        self.canvas.bind("<ButtonRelease-1>", self.on_release)
        self.root.bind("<Escape>", self.quit)
        self.root.bind("q", self.quit)
        self.root.bind("c", self.recenter)
        self.root.bind("<space>", self.space_action)
        self.root.bind("g", self.start_gaze_calibration)
        self.root.bind("h", self.start_head_calibration)
        self.root.bind("n", self.advance_head_calibration_manually)
        self.root.bind("d", self.dump_pitch_debug)
        self.root.bind("r", self.reset_baseline)
        self.root.bind("s", self.toggle_smoothing)
        self.root.bind("<Tab>", self.toggle_harness)
        self.root.bind("y", lambda _event: self.adjust_gain("yaw", 0.25))
        self.root.bind("Y", lambda _event: self.adjust_gain("yaw", -0.25))
        self.root.bind("p", lambda _event: self.adjust_gain("pitch", 0.25))
        self.root.bind("P", lambda _event: self.adjust_gain("pitch", -0.25))
        self.root.bind("o", lambda _event: self.adjust_gain("roll", 0.25))
        self.root.bind("O", lambda _event: self.adjust_gain("roll", -0.25))
        self.root.protocol("WM_DELETE_WINDOW", self.quit)

        self.samples: deque[EyeSample] = deque(maxlen=240)
        self.recent_valid: deque[EyeSample] = deque(maxlen=max(10, args.baseline_samples))
        self.pose_trail: deque[tuple[float, float]] = deque(maxlen=45)
        self.latest: EyeSample | None = None
        self.latest_mediapipe: MediaPipeFaceSample | None = None
        self.raw_pose: EyePose | None = None
        self.pose: EyePose | None = None
        self.pose_source = "none"
        self.confidence = PoseConfidence()
        self.last_eye_pose_for_velocity: EyePose | None = None
        self.last_eye_pose_time = 0.0
        self.last_eye_velocity = 0.0
        self.profile = self.load_head_calibration()
        self.head_calibrating = False
        self.head_cal_start = 0.0
        self.head_cal_phase_index = 0
        self.head_cal_phase_start = 0.0
        self.head_cal_phase_match_count = 0
        self.head_cal_phase_reference: tuple[float, float, float, float] | None = None
        self.head_cal_phase_start_coverage: dict[str, int] = {key: 0 for key, _label in HEAD_CALIBRATION_BINS}
        self.head_cal_samples: list[tuple[EyePose, PoseConfidence, str]] = []
        self.manual_head_cal_window: Toplevel | None = None
        self.manual_head_cal_status: StringVar | None = None
        self.manual_head_cal_target: tuple[str, str] | None = None
        self.manual_head_cal_axis = ""
        self.manual_head_cal_index = 0
        self.manual_head_cal_sequence: list[tuple[str, str]] = []
        self.manual_head_cal_captures: dict[str, tuple[EyePose, PoseConfidence]] = {}
        self.coverage: dict[str, int] = {key: 0 for key, _label in HEAD_CALIBRATION_BINS}
        self.gaze_trail: deque[tuple[float, float]] = deque(maxlen=45)
        self.smoothed_gaze_ratio: tuple[float, float] | None = None
        self.gaze_affine: tuple[list[float], list[float]] | None = None
        self.gaze_offset_x = 0.0
        self.gaze_offset_y = 0.0
        self.calibration_index: int | None = None
        self.calibration_points: list[tuple[float, float, float, float]] = []
        self.harness_menu_open = False
        self.baseline: EyeBaseline | None = None
        self.smoothing_enabled = True
        self.smoother = PoseSmoother(args.smoothing)
        self.start_monotonic = time.monotonic()
        self.last_packet_monotonic = 0.0
        self.last_sample_index = -1
        self.packet_times: deque[float] = deque(maxlen=120)
        self.opentrack_bridge = OpenTrackUdpBridge(self.active_udp_target(), args) if self.active_udp_target() else None
        self.mediapipe_yaw_neutral: float | None = self.profile.face_yaw_neutral
        self.mediapipe_pitch_neutral: float | None = self.profile.face_pitch_neutral
        self.mediapipe_roll_neutral: float | None = self.profile.face_roll_neutral
        self.inband_dashboard_proc: subprocess.Popen | None = None
        self.face_handoff_yaw_bias = 0.0
        self.face_handoff_pitch_bias = 0.0
        self.face_handoff_active = False
        self.pitch_up_hold: float | None = None
        self.pitch_debug_trace: deque[dict[str, object]] = deque(maxlen=max(240, int(args.pitch_debug_seconds * 140)))
        self.pitch_debug_raw: float | None = None
        self.pitch_debug_delta: float | None = None
        self.pitch_debug_mapped: float | None = None
        self.pitch_debug_status = "waiting"
        self.pitch_debug_last_dump: Path | None = None
        self.pitch_mapper_last_raw: float | None = None
        self.pitch_mapper_last_value: float | None = None
        self.pitch_mapper_last_monotonic = 0.0
        self.pitch_mapper_hold_until = 0.0
        self.sampler_restart_count = 0
        self.last_sampler_restart_monotonic = 0.0
        self.last_pose_monotonic = 0.0
        self.status = "starting"

    def quit(self, _event=None) -> None:
        self.save_window_state()
        if self.proc.poll() is None:
            os.killpg(self.proc.pid, signal.SIGTERM)
            try:
                self.proc.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                os.killpg(self.proc.pid, signal.SIGKILL)
        if self.mediapipe_bridge is not None:
            self.mediapipe_bridge.terminate()
        if self.hq_worker is not None:
            self.hq_worker.terminate()
        self.terminate_inband_dashboard()
        if self.opentrack_bridge is not None:
            self.opentrack_bridge.close()
        self.root.destroy()

    def terminate_inband_dashboard(self) -> None:
        proc = self.inband_dashboard_proc
        if proc is None or proc.poll() is not None:
            return
        try:
            os.killpg(proc.pid, signal.SIGTERM)
            proc.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    def open_inband_mediapipe_dashboard(self, _event=None) -> None:
        if self.inband_dashboard_proc is not None and self.inband_dashboard_proc.poll() is None:
            self.status = "landmark view already open"
            return
        if not self.args.frame_face_tracking:
            self.status = "landmark view needs in-band image frames"
            return
        session_dir = self.csv_path.parent
        frames_dir = session_dir / "frames"
        if not frames_dir.exists():
            self.status = "landmark view waiting for frame directory"
            return

        log_path = session_dir / "inband-landmark-dashboard.log"
        cmd = [
            str(self.root_dir / "scripts" / "recon" / "inband-landmark-dashboard.py"),
            "--engine",
            "mediapipe",
            "--no-start-mux",
            "--session",
            str(session_dir),
            "--mediapipe-python",
            str(self.args.mediapipe_python),
            "--mediapipe-model",
            str(self.args.mediapipe_model),
            "--mediapipe-preprocess",
            self.args.mediapipe_preprocess,
            "--mediapipe-upscale",
            str(self.args.mediapipe_upscale),
            "--mediapipe-rotation-mode",
            self.args.mediapipe_rotation_mode,
            "--mediapipe-pose-source",
            self.args.mediapipe_pose_source,
            "--mediapipe-min-detection-confidence",
            str(self.args.mediapipe_min_detection_confidence),
            "--mediapipe-min-presence-confidence",
            str(self.args.mediapipe_min_presence_confidence),
            "--mediapipe-min-tracking-confidence",
            str(self.args.mediapipe_min_tracking_confidence),
        ]
        try:
            log = log_path.open("w", encoding="utf-8")
            self.inband_dashboard_proc = subprocess.Popen(
                cmd,
                cwd=self.root_dir,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            log.close()
            self.status = "opened landmark view"
        except OSError as exc:
            self.status = f"landmark view failed: {exc}"

    def active_udp_target(self) -> str:
        if self.args.harness == "trackir":
            return self.args.trackir_udp or self.args.opentrack_udp or ""
        return self.args.tobii_udp or self.args.opentrack_udp or ""

    def set_harness(self, harness: str) -> None:
        if harness not in ("tobii", "trackir"):
            return
        self.harness_menu_open = False
        if self.args.harness == harness:
            return
        self.args.harness = harness
        target = self.active_udp_target()
        if self.opentrack_bridge is None and target:
            self.opentrack_bridge = OpenTrackUdpBridge(target, self.args)
        elif self.opentrack_bridge is not None and target:
            self.opentrack_bridge.retarget(target)
        self.save_tuning()

    def toggle_harness(self, _event=None) -> None:
        self.set_harness("trackir" if self.args.harness == "tobii" else "tobii")

    def toggle_harness_menu(self, _px: float | None = None, _py: float | None = None) -> None:
        self.harness_menu_open = not self.harness_menu_open

    def space_action(self, _event=None) -> None:
        if self.calibration_index is not None:
            self.capture_calibration_point()
            return
        if self.manual_head_cal_target is not None:
            self.capture_manual_head_calibration()
            return
        self.recenter()
        self.center_gaze()

    def reset_baseline(self, _event=None) -> None:
        self.baseline = None
        self.recent_valid.clear()
        self.clear_face_calibration()
        self.smoother.reset()
        self.reset_gaze_calibration()
        self.confidence = PoseConfidence()
        self.last_eye_pose_for_velocity = None
        self.reset_pitch_mapper()
        self.status = "collecting baseline"

    def recenter(self, _event=None) -> None:
        baseline = make_baseline(list(self.recent_valid))
        if baseline is not None:
            self.baseline = baseline
            self.clear_face_calibration()
            self.capture_face_neutral()
            self.smoother.reset()
            self.last_eye_pose_for_velocity = None
            self.pitch_up_hold = None
            self.reset_pitch_mapper()
            self.status = "recentered"

    def reset_pitch_mapper(self) -> None:
        self.pitch_mapper_last_raw = None
        self.pitch_mapper_last_value = None
        self.pitch_mapper_last_monotonic = 0.0
        self.pitch_mapper_hold_until = 0.0
        self.pitch_debug_status = "reset"

    def start_head_calibration(self, _event=None) -> None:
        self.open_head_calibration_window()
        return

    def start_guided_head_calibration(self, _event=None) -> None:
        if self.head_calibrating:
            self.finish_head_calibration(force=True)
            return
        self.recenter()
        if self.latest_mediapipe is None:
            self.status = "head cal waiting for face"
        self.head_calibrating = True
        self.head_cal_start = time.monotonic()
        self.head_cal_phase_index = 0
        self.head_cal_phase_start = self.head_cal_start
        self.head_cal_phase_match_count = 0
        self.head_cal_phase_reference = None
        self.head_cal_samples = []
        self.coverage = {key: 0 for key, _label in HEAD_CALIBRATION_BINS}
        self.head_cal_phase_start_coverage = self.coverage.copy()
        self.status = "head cal: neutral"

    def finish_head_calibration(self, force: bool = False) -> None:
        good = list(self.head_cal_samples)
        if len(good) < self.args.head_cal_min_samples:
            if force:
                self.head_calibrating = False
            self.status = f"head cal needs samples {len(good)}/{self.args.head_cal_min_samples}"
            return
        if not force and self.head_cal_phase_index < len(HEAD_CALIBRATION_PHASES):
            self.status = f"head cal {self.head_cal_progress():.0f}% {len(good)} samples"
            return

        yaw_phase_values = [(eye.yaw, phase_key) for eye, confidence, phase_key in good if confidence.eye >= self.args.head_cal_min_eye_confidence]
        yaw_left_values = [value for value, phase_key in yaw_phase_values if phase_key == "yaw_left" and abs(value) > self.args.head_cal_axis_threshold_deg]
        yaw_right_values = [value for value, phase_key in yaw_phase_values if phase_key == "yaw_right" and abs(value) > self.args.head_cal_axis_threshold_deg]
        yaw_left = [abs(value) for value in yaw_left_values]
        yaw_right = [abs(value) for value in yaw_right_values]
        pitch_values = [(eye.pitch_proxy, phase_key) for eye, _confidence, phase_key in good]
        pitch_up_values = [value for value, phase_key in pitch_values if phase_key == "pitch_up" and abs(value) > self.args.head_cal_axis_threshold_deg]
        pitch_down_values = [value for value, phase_key in pitch_values if phase_key == "pitch_down" and abs(value) > self.args.head_cal_axis_threshold_deg]
        pitch_up = [abs(value) for value in pitch_up_values]
        pitch_down = [abs(value) for value in pitch_down_values]

        def side_scale(values: list[float], reference: float = 8.0) -> float:
            if not values:
                return 1.0
            med = median(values)
            if med < 1.0:
                return 1.0
            return clamp(reference / med, 0.5, 2.0)

        self.profile = HeadCalibrationProfile(
            yaw_left_scale=side_scale(yaw_left),
            yaw_right_scale=side_scale(yaw_right),
            yaw_left_sign=1.0 if not yaw_left_values or median(yaw_left_values) >= 0.0 else -1.0,
            yaw_right_sign=1.0 if not yaw_right_values or median(yaw_right_values) >= 0.0 else -1.0,
            pitch_up_scale=side_scale(pitch_up, 4.0),
            pitch_down_scale=side_scale(pitch_down, 4.0),
            pitch_up_sign=1.0 if not pitch_up_values or median(pitch_up_values) >= 0.0 else -1.0,
            pitch_down_sign=1.0 if not pitch_down_values or median(pitch_down_values) >= 0.0 else -1.0,
            face_yaw_neutral=self.mediapipe_yaw_neutral,
            face_pitch_neutral=self.mediapipe_pitch_neutral,
            face_roll_neutral=self.mediapipe_roll_neutral,
            created=time.time(),
        )
        self.save_head_calibration()
        self.head_calibrating = False
        self.status = f"head calibrated {len(good)} samples"

    def reset_head_calibration(self, _px: float | None = None, _py: float | None = None) -> None:
        self.profile = HeadCalibrationProfile()
        self.clear_face_calibration()
        self.capture_face_neutral()
        self.save_head_calibration()
        self.pitch_up_hold = None
        self.reset_pitch_mapper()
        self.manual_head_cal_captures.clear()
        self.manual_head_cal_target = None
        self.manual_head_cal_axis = ""
        self.manual_head_cal_sequence = []
        self.manual_head_cal_index = 0
        self.status = "head calibration reset"
        self.update_manual_head_cal_status("Head calibration reset. Capture neutral, then an axis.")

    def open_head_calibration_window(self, _event=None) -> None:
        if self.manual_head_cal_window is not None and self.manual_head_cal_window.winfo_exists():
            self.manual_head_cal_window.lift()
            return
        window = Toplevel(self.root)
        window.title("Tobii Head Calibration")
        window.configure(bg=PANEL)
        window.geometry("520x320")
        self.manual_head_cal_window = window
        self.manual_head_cal_status = StringVar(value="Capture neutral first, then choose yaw, pitch, or roll.")

        outer = Frame(window, bg=PANEL, padx=18, pady=18)
        outer.pack(fill=BOTH, expand=True)
        Label(outer, text="Head Calibration", bg=PANEL, fg=TEXT, font=("Sans", 18, "bold")).pack(anchor="w")
        Label(
            outer,
            textvariable=self.manual_head_cal_status,
            bg=PANEL,
            fg=YELLOW,
            font=("Sans", 11, "bold"),
            wraplength=470,
            justify="left",
        ).pack(anchor="w", pady=(8, 16))

        button_row = Frame(outer, bg=PANEL)
        button_row.pack(anchor="w", pady=(0, 12))
        Button(button_row, text="Capture Neutral", command=self.start_manual_head_cal_neutral, width=16).pack(side="left", padx=(0, 8))
        Button(button_row, text="Yaw", command=lambda: self.start_manual_head_cal_axis("yaw"), width=10).pack(side="left", padx=(0, 8))
        Button(button_row, text="Pitch", command=lambda: self.start_manual_head_cal_axis("pitch"), width=10).pack(side="left", padx=(0, 8))
        Button(button_row, text="Roll", command=lambda: self.start_manual_head_cal_axis("roll"), width=10).pack(side="left")

        action_row = Frame(outer, bg=PANEL)
        action_row.pack(anchor="w", pady=(10, 12))
        Button(action_row, text="Save Profile", command=self.save_manual_head_calibration_profile, width=14).pack(side="left", padx=(0, 8))
        Button(action_row, text="Reset Head", command=self.reset_head_calibration, width=14).pack(side="left", padx=(0, 8))
        Button(action_row, text="Close", command=window.destroy, width=10).pack(side="left")

        Label(
            outer,
            text="Workflow: choose an axis, hold the requested pose, press Space to capture, then follow the next prompt. Use Save Profile after capturing the axes you want.",
            bg=PANEL,
            fg=MUTED,
            font=("Sans", 10),
            wraplength=470,
            justify="left",
        ).pack(anchor="w", pady=(12, 0))
        window.bind("<space>", self.capture_manual_head_calibration)
        window.protocol("WM_DELETE_WINDOW", window.destroy)

    def start_manual_head_cal_neutral(self) -> None:
        self.manual_head_cal_axis = "neutral"
        self.manual_head_cal_sequence = [("neutral", "Sit naturally, face center, and press Space.")]
        self.manual_head_cal_index = 0
        self.manual_head_cal_target = self.manual_head_cal_sequence[0]
        self.update_manual_head_cal_status(self.manual_head_cal_target[1])

    def start_manual_head_cal_axis(self, axis: str) -> None:
        sequences = {
            "yaw": [
                ("yaw_left", "Turn your head left, hold that position, then press Space."),
                ("yaw_right", "Turn your head right, hold that position, then press Space."),
            ],
            "pitch": [
                ("pitch_up_mid", "Tilt your head halfway up, hold that position, then press Space."),
                ("pitch_up_max", "Tilt your head to your highest comfortable up angle, hold, then press Space."),
                ("pitch_down", "Tilt your head down, hold that position, then press Space."),
            ],
            "roll": [
                ("roll_left", "Tilt your head toward your left shoulder, hold, then press Space."),
                ("roll_right", "Tilt your head toward your right shoulder, hold, then press Space."),
            ],
        }
        self.manual_head_cal_axis = axis
        self.manual_head_cal_sequence = sequences[axis]
        self.manual_head_cal_index = 0
        self.manual_head_cal_target = self.manual_head_cal_sequence[0]
        self.update_manual_head_cal_status(self.manual_head_cal_target[1])

    def capture_manual_head_calibration(self, _event=None) -> None:
        if self.manual_head_cal_target is None:
            return
        if self.pose is None:
            self.update_manual_head_cal_status("No live pose yet. Wait for tracking, then press Space again.")
            return
        key, _instruction = self.manual_head_cal_target
        self.manual_head_cal_captures[key] = (self.pose, self.confidence)
        if key == "neutral":
            self.capture_mediapipe_neutral()
            self.update_manual_head_cal_status("Neutral captured. Choose yaw, pitch, or roll.")
            self.manual_head_cal_target = None
            return

        self.manual_head_cal_index += 1
        if self.manual_head_cal_index >= len(self.manual_head_cal_sequence):
            axis = self.manual_head_cal_axis
            self.manual_head_cal_target = None
            self.save_manual_head_calibration_profile()
            self.update_manual_head_cal_status(f"{axis.title()} captured and profile saved. Capture another axis or close.")
            return
        self.manual_head_cal_target = self.manual_head_cal_sequence[self.manual_head_cal_index]
        self.update_manual_head_cal_status(f"Captured {key}. {self.manual_head_cal_target[1]}")

    def update_manual_head_cal_status(self, message: str) -> None:
        if self.manual_head_cal_status is not None:
            captured = ", ".join(sorted(self.manual_head_cal_captures)) or "none"
            self.manual_head_cal_status.set(f"{message}\nCaptured: {captured}")

    def save_manual_head_calibration_profile(self) -> None:
        neutral = self.manual_head_cal_captures.get("neutral")
        if neutral is None:
            self.update_manual_head_cal_status("Capture neutral before saving a head profile.")
            return
        neutral_pose, _neutral_conf = neutral
        yaw_neutral = neutral_pose.yaw
        pitch_neutral = neutral_pose.pitch_proxy

        def sign(value: float, default: float) -> float:
            return default if abs(value) < 1e-6 else (1.0 if value >= 0.0 else -1.0)

        def scale(value: float, reference: float) -> float:
            if abs(value) < 0.5:
                return 1.0
            return clamp(reference / abs(value), 0.35, 3.0)

        def yaw_value(key: str) -> float | None:
            capture = self.manual_head_cal_captures.get(key)
            if capture is None:
                return None
            pose, _confidence = capture
            return pose.yaw - yaw_neutral

        def pitch_value(key: str) -> float | None:
            capture = self.manual_head_cal_captures.get(key)
            if capture is None:
                return None
            pose, _confidence = capture
            return pose.pitch_proxy - pitch_neutral

        def pitch_target_value(key: str) -> float | None:
            capture = self.manual_head_cal_captures.get(key)
            if capture is None:
                return None
            pose, _confidence = capture
            return pose.pitch_proxy - neutral_pose.pitch_proxy

        def roll_value(key: str) -> float | None:
            capture = self.manual_head_cal_captures.get(key)
            if capture is None:
                return None
            pose, _confidence = capture
            return pose.roll - neutral_pose.roll

        yaw_left = yaw_value("yaw_left")
        yaw_right = yaw_value("yaw_right")
        pitch_up_mid = pitch_value("pitch_up_mid")
        pitch_up_max = pitch_value("pitch_up_max")
        pitch_down = pitch_value("pitch_down")
        pitch_up_mid_target = pitch_target_value("pitch_up_mid")
        pitch_up_max_target = pitch_target_value("pitch_up_max")
        pitch_down_target = pitch_target_value("pitch_down")
        roll_left = roll_value("roll_left")
        roll_right = roll_value("roll_right")

        pitch_up_for_scale = pitch_up_max if pitch_up_max is not None else pitch_up_mid
        if pitch_up_mid is not None and pitch_up_mid_target is None:
            pitch_up_mid_target = pitch_up_mid
        if pitch_up_max is not None and pitch_up_max_target is None:
            pitch_up_max_target = pitch_up_max
        if pitch_down is not None and pitch_down_target is None:
            pitch_down_target = pitch_down
        if pitch_up_mid is not None and pitch_up_max is not None and pitch_up_mid_target is not None and pitch_up_max_target is not None:
            up_sign = sign(pitch_up_max if abs(pitch_up_max) >= abs(pitch_up_mid) else pitch_up_mid, self.profile.pitch_up_sign)
            if up_sign * pitch_up_max < up_sign * pitch_up_mid:
                pitch_up_mid, pitch_up_max = pitch_up_max, pitch_up_mid
                pitch_up_mid_target, pitch_up_max_target = pitch_up_max_target, pitch_up_mid_target
            if up_sign * pitch_up_max_target <= up_sign * pitch_up_mid_target:
                pitch_up_max_target = pitch_up_mid_target + up_sign * max(4.0, abs(pitch_up_mid_target) * 0.65)

        self.profile = HeadCalibrationProfile(
            yaw_left_scale=scale(yaw_left, 8.0) if yaw_left is not None else self.profile.yaw_left_scale,
            yaw_right_scale=scale(yaw_right, 8.0) if yaw_right is not None else self.profile.yaw_right_scale,
            yaw_left_sign=sign(yaw_left, self.profile.yaw_left_sign) if yaw_left is not None else self.profile.yaw_left_sign,
            yaw_right_sign=sign(yaw_right, self.profile.yaw_right_sign) if yaw_right is not None else self.profile.yaw_right_sign,
            pitch_up_scale=scale(pitch_up_for_scale, 4.0) if pitch_up_for_scale is not None else self.profile.pitch_up_scale,
            pitch_down_scale=scale(pitch_down, 4.0) if pitch_down is not None else self.profile.pitch_down_scale,
            pitch_up_sign=sign(pitch_up_for_scale, self.profile.pitch_up_sign) if pitch_up_for_scale is not None else self.profile.pitch_up_sign,
            pitch_down_sign=sign(pitch_down, self.profile.pitch_down_sign) if pitch_down is not None else self.profile.pitch_down_sign,
            roll_left_scale=scale(roll_left, 8.0) if roll_left is not None else self.profile.roll_left_scale,
            roll_right_scale=scale(roll_right, 8.0) if roll_right is not None else self.profile.roll_right_scale,
            roll_left_sign=sign(roll_left, self.profile.roll_left_sign) if roll_left is not None else self.profile.roll_left_sign,
            roll_right_sign=sign(roll_right, self.profile.roll_right_sign) if roll_right is not None else self.profile.roll_right_sign,
            face_yaw_neutral=self.mediapipe_yaw_neutral,
            face_pitch_neutral=self.mediapipe_pitch_neutral,
            face_roll_neutral=self.mediapipe_roll_neutral,
            pitch_up_mid_raw=pitch_up_mid,
            pitch_up_mid_value=pitch_up_mid_target,
            pitch_up_max_raw=pitch_up_max,
            pitch_up_max_value=pitch_up_max_target,
            pitch_down_raw=pitch_down,
            pitch_down_value=pitch_down_target,
            created=time.time(),
        )
        self.reset_pitch_mapper()
        self.save_head_calibration()
        self.status = "manual head calibration saved"

    def reset_tuning(self, _px: float | None = None, _py: float | None = None) -> None:
        for attr, value in DEFAULT_TUNING_VALUES.items():
            setattr(self.args, attr, value)
        if self.opentrack_bridge is not None:
            target = self.active_udp_target()
            if target:
                self.opentrack_bridge.retarget(target)
            with self.opentrack_bridge.lock:
                self.opentrack_bridge.output_values = None
                self.opentrack_bridge.target_values = None
                self.opentrack_bridge.target_velocity = None
                self.opentrack_bridge.last_target_update_monotonic = 0.0
        self.save_tuning()
        self.status = "tuning reset"

    def center_gaze(self) -> None:
        sample = self.latest
        if sample is None or sample.gaze_valid != 1 or not math.isfinite(sample.gaze_x) or not math.isfinite(sample.gaze_y):
            return
        self.gaze_affine = None
        self.gaze_offset_x = 0.5 - sample.gaze_x
        self.gaze_offset_y = 0.5 - sample.gaze_y
        self.smoothed_gaze_ratio = (0.5, 0.5)
        self.gaze_trail.clear()
        self.status = "gaze centered"

    def reset_gaze_calibration(self, _event=None) -> None:
        self.gaze_affine = None
        self.gaze_offset_x = 0.0
        self.gaze_offset_y = 0.0
        self.smoothed_gaze_ratio = None
        self.gaze_trail.clear()
        self.calibration_index = None
        self.calibration_points = []

    def start_gaze_calibration(self, _event=None) -> None:
        self.calibration_index = 0
        self.calibration_points = []
        self.smoothed_gaze_ratio = None
        self.gaze_trail.clear()
        self.status = "gaze calibration"

    def capture_calibration_point(self) -> None:
        if self.calibration_index is None:
            return
        valid_samples = [
            sample
            for sample in self.samples
            if sample.gaze_valid == 1 and math.isfinite(sample.gaze_x) and math.isfinite(sample.gaze_y)
        ]
        if not valid_samples:
            return
        capture_count = min(len(valid_samples), self.args.gaze_calibration_samples)
        recent = valid_samples[-capture_count:]
        raw_x = sum(sample.gaze_x for sample in recent) / capture_count
        raw_y = sum(sample.gaze_y for sample in recent) / capture_count
        _label, target_x, target_y = CALIBRATION_TARGETS[self.calibration_index]
        self.calibration_points.append((raw_x, raw_y, target_x, target_y))
        self.calibration_index += 1
        if self.calibration_index >= len(CALIBRATION_TARGETS):
            self.finish_gaze_calibration()
        else:
            self.status = f"calibrate {CALIBRATION_TARGETS[self.calibration_index][0]}"

    def finish_gaze_calibration(self) -> None:
        affine = fit_gaze_calibration(self.calibration_points)
        self.calibration_index = None
        if affine is None:
            self.status = "gaze calibration failed"
            return
        self.gaze_affine = affine
        self.gaze_offset_x = 0.0
        self.gaze_offset_y = 0.0
        self.smoothed_gaze_ratio = None
        self.gaze_trail.clear()
        self.status = "gaze calibrated"

    def gaze_ratio(self, sample: EyeSample) -> tuple[float, float]:
        if self.gaze_affine is not None:
            x_coeffs, y_coeffs = self.gaze_affine
            features = gaze_features(sample.gaze_x, sample.gaze_y)
            return (
                sum(coeff * value for coeff, value in zip(x_coeffs, features)),
                sum(coeff * value for coeff, value in zip(y_coeffs, features)),
            )
        return (sample.gaze_x + self.gaze_offset_x, sample.gaze_y + self.gaze_offset_y)

    def update_smoothed_gaze(self, sample: EyeSample) -> None:
        if sample.gaze_valid != 1 or not math.isfinite(sample.gaze_x) or not math.isfinite(sample.gaze_y):
            return
        ratio = self.gaze_ratio(sample)
        alpha = clamp(self.args.gaze_smoothing, 0.0, 1.0)
        if self.smoothed_gaze_ratio is None or alpha >= 1.0:
            self.smoothed_gaze_ratio = ratio
        else:
            old_x, old_y = self.smoothed_gaze_ratio
            self.smoothed_gaze_ratio = (old_x + alpha * (ratio[0] - old_x), old_y + alpha * (ratio[1] - old_y))
        self.gaze_trail.append(self.smoothed_gaze_ratio)

    def toggle_smoothing(self, _event=None) -> None:
        self.smoothing_enabled = not self.smoothing_enabled
        self.smoother.reset()

    def on_click(self, event) -> None:
        for x1, y1, x2, y2, callback in reversed(self.click_targets):
            if x1 <= event.x <= x2 and y1 <= event.y <= y2:
                callback(event.x, event.y)
                self.drag_target = callback
                return

    def on_drag(self, event) -> None:
        if self.drag_target is not None:
            self.drag_target(event.x, event.y)

    def on_release(self, _event) -> None:
        self.drag_target = None

    def adjust_gain(self, axis: str, delta: float) -> None:
        attr = f"opentrack_{axis}_scale"
        current = getattr(self.args, attr)
        if abs(current) < 0.001 and delta < 0:
            updated = -0.25
        else:
            updated = current + delta
        setattr(self.args, attr, clamp(updated, -30.0, 30.0))
        self.save_tuning()

    def start_loop(self) -> None:
        self.tick()

    def restart_sampler(self, reason: str, now: float) -> bool:
        if now - self.last_sampler_restart_monotonic < 2.0:
            return False
        if self.proc.poll() is None:
            try:
                os.killpg(self.proc.pid, signal.SIGTERM)
                self.proc.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                os.killpg(self.proc.pid, signal.SIGKILL)
        self.tail = CsvTail(self.csv_path)
        self.latest = None
        self.last_packet_monotonic = 0.0
        self.last_sample_index = -1
        self.packet_times.clear()
        log_file = self.log_path.open("w", encoding="utf-8")
        self.proc = subprocess.Popen(
            self.sampler_cmd,
            cwd=self.root_dir,
            stdout=subprocess.PIPE if self.args.frame_face_tracking else subprocess.DEVNULL,
            stderr=log_file,
            start_new_session=True,
        )
        log_file.close()
        if self.mediapipe_bridge is not None and self.proc.stdout is not None:
            self.mediapipe_bridge.attach_mux_stdout(self.proc.stdout)
        self.sampler_restart_count += 1
        self.last_sampler_restart_monotonic = now
        self.status = f"restarted sampler {reason}"
        return True

    def tick(self) -> None:
        now = time.monotonic()
        saw_eye_row = False
        saw_valid_eye_pose = False
        if self.mediapipe_bridge is not None:
            self.latest_mediapipe = self.mediapipe_bridge.snapshot()

        for sample in self.tail.read_new():
            saw_eye_row = True
            self.latest = sample
            self.samples.append(sample)
            self.update_smoothed_gaze(sample)
            self.last_packet_monotonic = now
            if sample.sample_index != self.last_sample_index:
                self.last_sample_index = sample.sample_index
                self.packet_times.append(now)
            if sample.valid:
                self.recent_valid.append(sample)
                if self.baseline is None and len(self.recent_valid) >= self.args.baseline_samples:
                    self.baseline = make_baseline(list(self.recent_valid))
                    self.capture_face_neutral()
                    self.smoother.reset()
                    self.status = "streaming"
                if self.baseline is not None:
                    self.raw_pose = derive_pose(sample, self.baseline, self.args.sensor_distance_mm)
                    self.confidence = self.eye_confidence(sample, self.raw_pose, now)
                    fused_pose, fused_source = self.fused_pose(self.raw_pose, self.confidence, now)
                    self.pose = self.smooth_runtime_pose(fused_pose, fused_source) if self.smoothing_enabled else fused_pose
                    self.pose_source = fused_source
                    self.collect_head_calibration(self.pose, self.confidence)
                    self.last_pose_monotonic = now
                    self.pose_trail.append((self.pose.yaw, self.pose.pitch_proxy))
                    saw_valid_eye_pose = True
            elif self.baseline is not None:
                if self.args.tobii_head_pose_source == "face" and self.apply_face_fallback(now):
                    pass
                elif self.hold_pose_if_recent(now, self.args.blink_hold_s):
                    pass
                elif not self.apply_face_fallback(now):
                    self.hold_pose_if_recent(now)

        eye_stream_stale = now - self.last_packet_monotonic > self.args.eye_fallback_after_s
        continuing_face = self.pose_source.startswith("MediaPipe")
        if (
            not saw_valid_eye_pose
            and self.baseline is not None
            and (
                continuing_face
                or eye_stream_stale
                or (self.latest is not None and not self.latest.valid)
                or (not saw_eye_row and self.latest is None)
            )
        ):
            if not self.apply_face_fallback(now):
                self.hold_pose_if_recent(now)

        if self.proc.poll() is not None:
            if not self.restart_sampler(f"rc={self.proc.returncode}", now):
                self.status = f"sampler exited rc={self.proc.returncode}"
        elif self.latest is None:
            self.status = "waiting for gaze stream"
        elif now - self.last_packet_monotonic > 0.6:
            self.status = f"stale {(now - self.last_packet_monotonic):.1f}s"
        elif self.baseline is None:
            self.status = f"collecting baseline {len(self.recent_valid)}/{self.args.baseline_samples}"

        if self.pose is not None and self.opentrack_bridge is not None:
            self.args.pose_source = self.pose_source
            target = self.active_udp_target()
            if target:
                self.opentrack_bridge.retarget(target)
            self.opentrack_bridge.send(self.pose, self.smoothed_gaze_ratio)
            self.record_pitch_debug(now)

        self.draw()
        self.root.after(self.args.interval_ms, self.tick)

    def record_pitch_debug(self, now: float) -> None:
        if self.pose is None:
            return
        packet_pitch = ""
        if self.opentrack_bridge is not None and self.opentrack_bridge.last_packet is not None:
            packet_pitch = f"{self.opentrack_bridge.last_packet[4]:.6f}"
        face_conf = self.face_confidence(now)
        eye_valid = 1 if self.latest is not None and self.latest.valid else 0
        self.pitch_debug_trace.append(
            {
                "monotonic": f"{now:.6f}",
                "timestamp": f"{time.time():.6f}",
                "raw_face_pitch": "" if self.pitch_debug_raw is None else f"{self.pitch_debug_raw:.6f}",
                "neutral_relative_face_pitch": "" if self.pitch_debug_delta is None else f"{self.pitch_debug_delta:.6f}",
                "mapped_pitch": "" if self.pitch_debug_mapped is None else f"{self.pitch_debug_mapped:.6f}",
                "smoothed_pitch": f"{self.pose.pitch_proxy:.6f}",
                "output_pitch_packet": packet_pitch,
                "pitch_status": self.pitch_debug_status,
                "confidence_eye": f"{self.confidence.eye:.6f}",
                "confidence_face": f"{self.confidence.face:.6f}",
                "confidence_blended": f"{self.confidence.blended:.6f}",
                "confidence_pitch": f"{self.confidence.pitch:.6f}",
                "source": self.pose_source,
                "eye_valid": str(eye_valid),
                "face_confidence": f"{face_conf:.6f}",
            }
        )

    def dump_pitch_debug(self, _event=None) -> None:
        out_dir = self.root_dir / ".tmp" / "sc-tobii-native-runtime"
        out_path = out_dir / "pitch-debug.csv"
        cutoff = time.monotonic() - self.args.pitch_debug_seconds
        rows = [row for row in self.pitch_debug_trace if float(row["monotonic"]) >= cutoff]
        fieldnames = [
            "timestamp",
            "monotonic",
            "raw_face_pitch",
            "neutral_relative_face_pitch",
            "mapped_pitch",
            "smoothed_pitch",
            "output_pitch_packet",
            "pitch_status",
            "confidence_eye",
            "confidence_face",
            "confidence_blended",
            "confidence_pitch",
            "source",
            "eye_valid",
            "face_confidence",
        ]
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
            with out_path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(rows)
            self.pitch_debug_last_dump = out_path
            self.status = f"pitch debug dumped {len(rows)} rows"
        except OSError as exc:
            self.status = f"pitch debug dump failed: {exc}"

    def eye_confidence(self, sample: EyeSample, pose: EyePose, now: float) -> PoseConfidence:
        if self.baseline is None or not sample.valid:
            return PoseConfidence()
        distance_delta = abs(sample.eye_distance - self.baseline.eye_distance)
        distance_score = 1.0 - clamp(distance_delta / max(self.args.eye_distance_tolerance_mm, 1.0), 0.0, 1.0)

        velocity = 0.0
        velocity_score = 1.0
        if self.last_eye_pose_for_velocity is not None and self.last_eye_pose_time > 0.0:
            dt = max(now - self.last_eye_pose_time, 0.001)
            velocity = max(
                abs(pose.yaw - self.last_eye_pose_for_velocity.yaw),
                abs(pose.pitch_proxy - self.last_eye_pose_for_velocity.pitch_proxy),
                abs(pose.roll - self.last_eye_pose_for_velocity.roll),
            ) / dt
            velocity_score = 1.0 - clamp((velocity - self.args.pose_velocity_soft_limit_dps) / max(self.args.pose_velocity_hard_limit_dps - self.args.pose_velocity_soft_limit_dps, 1.0), 0.0, 1.0)
        self.last_eye_velocity = velocity
        self.last_eye_pose_for_velocity = pose
        self.last_eye_pose_time = now

        edge_score = 1.0
        if abs(pose.yaw) >= self.args.eye_yaw_edge_deg:
            edge_score = 0.35
        if pose.pitch_proxy >= self.args.eye_pitch_up_edge_deg:
            edge_score = min(edge_score, 0.25)
        eye = clamp(distance_score * velocity_score * edge_score, 0.0, 1.0)
        pitch_conf = clamp(distance_score * velocity_score, 0.0, 1.0)
        return PoseConfidence(
            eye=eye,
            face=self.face_confidence(now),
            blended=eye,
            eye_distance_stable=distance_score,
            velocity_ok=velocity_score,
            pitch=pitch_conf,
        )

    def face_confidence(self, now: float) -> float:
        sample = self.latest_mediapipe
        if sample is None or not sample.usable:
            return 0.0
        age = now - sample.seen_monotonic
        if age > self.args.mediapipe_max_age_s:
            return 0.0
        age_score = 1.0 - clamp(age / max(self.args.mediapipe_max_age_s, 0.001), 0.0, 1.0)
        return clamp(sample.confidence * max(age_score, 0.35), 0.0, 1.0)

    def fused_pose(self, eye_pose: EyePose, confidence: PoseConfidence, now: float) -> tuple[EyePose, str]:
        eye_pose = scaled_pose(eye_pose, self.profile)
        face_primary = self.args.tobii_head_pose_source == "face"
        face_pose = self.face_fallback_pose(use_handoff=not face_primary)
        if face_pose is not None:
            face_pose = scaled_pose(face_pose, HeadCalibrationProfile(), scale_pitch=False)
        face_conf = self.face_confidence(now)
        confidence.face = face_conf
        if face_pose is None or face_conf <= 0.0 or self.args.tobii_head_pose_source == "eye":
            confidence.blended = confidence.eye
            self.face_handoff_active = False
            return eye_pose, "eye binocular"

        if face_primary:
            self.face_handoff_active = False
            confidence.blended = clamp(face_conf * max(confidence.eye, 0.35), 0.0, 1.0)
            return face_pose, self.face_primary_source_name()

        # Fade toward face as eye confidence drops, at high yaw, or during pitch-up
        # where the eye-origin vertical proxy is known to snap.
        edge_need = max(
            clamp((abs(eye_pose.yaw) - self.args.eye_yaw_edge_deg) / max(self.args.eye_yaw_full_face_deg - self.args.eye_yaw_edge_deg, 1.0), 0.0, 1.0),
            clamp((eye_pose.pitch_proxy - self.args.eye_pitch_up_edge_deg) / max(self.args.eye_pitch_up_full_face_deg - self.args.eye_pitch_up_edge_deg, 1.0), 0.0, 1.0),
        )
        confidence_need = 1.0 - confidence.eye
        face_weight = clamp(max(edge_need, confidence_need) * face_conf, 0.0, 1.0)
        if face_weight <= 0.05:
            self.face_handoff_active = False
            self.pitch_up_hold = None
            confidence.blended = confidence.eye
            return eye_pose, "eye binocular"
        fused = blend_pose(eye_pose, face_pose, face_weight)
        fused = self.guard_pitch_snapback(fused, eye_pose, confidence, face_weight)
        confidence.blended = clamp(confidence.eye * (1.0 - face_weight) + face_conf * face_weight, 0.0, 1.0)
        if face_weight >= 0.80:
            return fused, self.face_fallback_source_name()
        return fused, "blended eye+face"

    def guard_pitch_snapback(self, pose: EyePose, eye_pose: EyePose, confidence: PoseConfidence, face_weight: float) -> EyePose:
        if self.pose is None:
            return pose
        high_pitch = eye_pose.pitch_proxy >= self.args.eye_pitch_up_edge_deg or self.pose.pitch_proxy >= self.args.eye_pitch_up_edge_deg
        fallback_owns_pitch = face_weight >= 0.25 or confidence.eye < 0.65
        if not high_pitch or not fallback_owns_pitch:
            self.pitch_up_hold = None
            return pose
        if confidence.eye > 0.85 and eye_pose.pitch_proxy < self.args.eye_pitch_up_edge_deg * 0.5:
            self.pitch_up_hold = None
            return pose
        previous = self.pitch_up_hold if self.pitch_up_hold is not None else self.pose.pitch_proxy
        self.pitch_up_hold = max(previous, self.pose.pitch_proxy)
        allowed_drop = max(self.args.pitch_snapback_guard_deg, 0.01)
        guarded_pitch = max(pose.pitch_proxy, self.pitch_up_hold - allowed_drop)
        if guarded_pitch > pose.pitch_proxy:
            pose = EyePose(
                yaw=pose.yaw,
                pitch_proxy=guarded_pitch,
                roll=pose.roll,
                tx=pose.tx,
                ty=pose.ty,
                tz=pose.tz,
                eye_distance=pose.eye_distance,
                eye_distance_delta=pose.eye_distance_delta,
            )
        if pose.pitch_proxy >= self.pitch_up_hold:
            self.pitch_up_hold = pose.pitch_proxy
        return pose

    def smooth_runtime_pose(self, pose: EyePose, source: str) -> EyePose:
        base_alpha = clamp(self.args.smoothing, 0.01, 1.0)
        if source != self.pose_source or source == "hold last pose":
            alpha = min(base_alpha, self.args.transition_smoothing)
        elif self.confidence.eye < 0.55 and self.confidence.face > 0.0:
            alpha = min(base_alpha, self.args.transition_smoothing)
        else:
            alpha = base_alpha
        self.smoother.set_alpha(alpha)
        return self.smoother.update(pose)

    def face_primary_source_name(self) -> str:
        return "MediaPipe head primary"

    def face_fallback_source_name(self) -> str:
        return "MediaPipe yaw/pitch fallback"

    def face_fallback_pose(self, use_handoff: bool = True) -> EyePose | None:
        return self.mediapipe_fallback_pose(use_handoff=use_handoff)

    def apply_face_fallback(self, now: float) -> bool:
        face_primary = self.args.tobii_head_pose_source == "face"
        fallback = self.face_fallback_pose(use_handoff=not face_primary)
        if fallback is None:
            return False
        fallback = scaled_pose(fallback, HeadCalibrationProfile(), scale_pitch=False)
        if not face_primary and self.pose is not None and self.pose.pitch_proxy >= self.args.eye_pitch_up_edge_deg:
            allowed_drop = max(self.args.pitch_snapback_guard_deg, 0.01)
            guarded_pitch = max(fallback.pitch_proxy, self.pose.pitch_proxy - allowed_drop)
            if guarded_pitch > fallback.pitch_proxy:
                fallback = EyePose(
                    yaw=fallback.yaw,
                    pitch_proxy=guarded_pitch,
                    roll=fallback.roll,
                    tx=fallback.tx,
                    ty=fallback.ty,
                    tz=fallback.tz,
                    eye_distance=fallback.eye_distance,
                    eye_distance_delta=fallback.eye_distance_delta,
                )
        source = self.face_primary_source_name() if face_primary else self.face_fallback_source_name()
        self.raw_pose = fallback
        self.confidence = PoseConfidence(eye=0.0, face=self.face_confidence(now), blended=self.face_confidence(now), pitch=self.face_confidence(now))
        self.pose = self.smooth_runtime_pose(fallback, source) if self.smoothing_enabled else fallback
        self.pose_source = source
        self.last_pose_monotonic = now
        self.pose_trail.append((self.pose.yaw, self.pose.pitch_proxy))
        return True

    def hold_pose_if_recent(self, now: float, hold_s: float | None = None) -> bool:
        if hold_s is None:
            hold_s = self.args.pose_hold_s
        if self.pose is None or now - self.last_pose_monotonic > hold_s:
            return False
        self.pose_source = "hold last pose"
        self.pose_trail.append((self.pose.yaw, self.pose.pitch_proxy))
        return True

    def tobii_face_primary_pose(self, _now: float) -> EyePose | None:
        if self.args.harness != "tobii" or self.args.tobii_head_pose_source != "face":
            return None
        return self.face_fallback_pose(use_handoff=False)

    def clear_face_calibration(self) -> None:
        self.mediapipe_yaw_neutral = None
        self.mediapipe_pitch_neutral = None
        self.mediapipe_roll_neutral = None
        self.face_handoff_yaw_bias = 0.0
        self.face_handoff_pitch_bias = 0.0
        self.face_handoff_active = False

    def capture_face_neutral(self) -> None:
        self.capture_mediapipe_neutral()

    def capture_mediapipe_neutral(self) -> None:
        sample = self.latest_mediapipe
        if sample is None or not self.mediapipe_sample_usable(sample):
            return
        if time.monotonic() - sample.seen_monotonic > self.args.mediapipe_max_age_s:
            return
        self.mediapipe_yaw_neutral = sample.yaw
        self.mediapipe_pitch_neutral = sample.pitch
        self.mediapipe_roll_neutral = sample.roll

    def mediapipe_sample_usable(self, sample: MediaPipeFaceSample) -> bool:
        return sample.usable and sample.confidence >= self.args.mediapipe_min_face_confidence

    def latest_mediapipe_usable(self) -> bool:
        return (
            self.latest_mediapipe is not None
            and self.mediapipe_sample_usable(self.latest_mediapipe)
            and time.monotonic() - self.latest_mediapipe.seen_monotonic <= self.args.mediapipe_max_age_s
        )

    def corrected_mediapipe_yaw(self) -> float | None:
        if not self.latest_mediapipe_usable() or self.latest_mediapipe is None:
            return None
        if self.mediapipe_yaw_neutral is None:
            self.capture_mediapipe_neutral()
        if self.mediapipe_yaw_neutral is None:
            return None
        return angle_delta_deg(self.latest_mediapipe.yaw, self.mediapipe_yaw_neutral) * self.args.mediapipe_yaw_sign

    def corrected_mediapipe_pitch(self) -> float | None:
        if not self.latest_mediapipe_usable() or self.latest_mediapipe is None:
            return None
        if self.mediapipe_pitch_neutral is None:
            self.capture_mediapipe_neutral()
        if self.mediapipe_pitch_neutral is None:
            return None
        raw = angle_delta_deg(self.latest_mediapipe.pitch, self.mediapipe_pitch_neutral) * self.args.mediapipe_pitch_sign
        mapped = raw
        yaw = self.corrected_mediapipe_yaw()
        if yaw is not None and self.args.mediapipe_pitch_yaw_comp > 0.0:
            yaw_ratio = min(abs(yaw), 70.0) / 45.0
            comp = 1.0 + self.args.mediapipe_pitch_yaw_comp * yaw_ratio * yaw_ratio
            mapped = raw * comp
        self.pitch_debug_raw = self.latest_mediapipe.pitch
        self.pitch_debug_delta = raw
        self.pitch_debug_mapped = mapped
        self.pitch_debug_status = "mediapipe yaw-comp" if mapped != raw else "mediapipe"
        return mapped

    def corrected_mediapipe_roll(self) -> float | None:
        if not self.latest_mediapipe_usable() or self.latest_mediapipe is None:
            return None
        if self.mediapipe_roll_neutral is None:
            self.capture_mediapipe_neutral()
        if self.mediapipe_roll_neutral is None:
            return None
        return angle_delta_deg(self.latest_mediapipe.roll, self.mediapipe_roll_neutral) * self.args.mediapipe_roll_sign

    def mediapipe_fallback_pose(self, use_handoff: bool = True) -> EyePose | None:
        corrected_yaw = self.corrected_mediapipe_yaw()
        corrected_pitch = self.corrected_mediapipe_pitch()
        corrected_roll = self.corrected_mediapipe_roll()
        if corrected_yaw is None and corrected_pitch is None and corrected_roll is None:
            return None
        base = self.pose if self.pose is not None else self.raw_pose
        if base is None:
            return None
        if not use_handoff:
            self.face_handoff_active = False
        elif not self.face_handoff_active:
            self.face_handoff_yaw_bias = base.yaw - corrected_yaw if corrected_yaw is not None else 0.0
            self.face_handoff_pitch_bias = base.pitch_proxy - corrected_pitch if corrected_pitch is not None else 0.0
            self.face_handoff_active = True
        if use_handoff and corrected_yaw is not None:
            corrected_yaw += self.face_handoff_yaw_bias
        if use_handoff and corrected_pitch is not None:
            corrected_pitch += self.face_handoff_pitch_bias
        return EyePose(
            yaw=corrected_yaw if corrected_yaw is not None else base.yaw,
            pitch_proxy=corrected_pitch if corrected_pitch is not None else base.pitch_proxy,
            roll=corrected_roll if corrected_roll is not None else base.roll,
            tx=base.tx,
            ty=base.ty,
            tz=base.tz,
            eye_distance=base.eye_distance,
            eye_distance_delta=base.eye_distance_delta,
        )

    def collect_head_calibration(self, eye_pose: EyePose, confidence: PoseConfidence) -> None:
        if not self.head_calibrating:
            return
        now = time.monotonic()
        phase = self.current_head_calibration_phase()
        phase_key = phase[0] if phase is not None else "complete"
        self.head_cal_samples.append((eye_pose, confidence, phase_key))
        self.update_coverage(eye_pose)
        if phase is not None and self.head_cal_sample_matches_phase(phase_key, eye_pose):
            self.head_cal_phase_match_count += 1
        self.advance_head_calibration_phase(now)

    def current_head_calibration_phase(self) -> tuple[str, str, tuple[str, ...]] | None:
        if self.head_cal_phase_index >= len(HEAD_CALIBRATION_PHASES):
            return None
        return HEAD_CALIBRATION_PHASES[self.head_cal_phase_index]

    def advance_head_calibration_phase(self, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        phase = self.current_head_calibration_phase()
        if phase is None:
            self.finish_head_calibration()
            return
        key, _instruction, required_bins = phase
        enough_time = now - self.head_cal_phase_start >= self.args.head_cal_phase_min_duration_s
        enough_samples = self.head_cal_phase_match_count >= self.args.head_cal_phase_samples
        if not (enough_time and enough_samples):
            self.status = f"head cal {key} {self.head_cal_phase_progress():.0f}%"
            return
        self.head_cal_phase_index += 1
        self.head_cal_phase_start = now
        self.head_cal_phase_match_count = 0
        self.head_cal_phase_reference = None
        self.head_cal_phase_start_coverage = self.coverage.copy()
        if self.current_head_calibration_phase() is None:
            self.finish_head_calibration()
            return
        self.status = f"head cal: {self.current_head_calibration_phase()[0]}"

    def advance_head_calibration_manually(self, _event=None) -> None:
        if not self.head_calibrating:
            return
        self.head_cal_phase_index += 1
        self.head_cal_phase_start = time.monotonic()
        self.head_cal_phase_match_count = 0
        self.head_cal_phase_reference = None
        self.head_cal_phase_start_coverage = self.coverage.copy()
        phase = self.current_head_calibration_phase()
        if phase is None:
            self.finish_head_calibration(force=True)
            return
        self.status = f"head cal: {phase[0]}"

    def head_cal_sample_matches_phase(self, phase_key: str, pose: EyePose) -> bool:
        yaw, pitch, roll, distance = self.head_cal_phase_values(pose)
        if phase_key != "neutral" and self.head_cal_phase_reference is None:
            self.head_cal_phase_reference = (yaw, pitch, roll, distance)
            return False
        bins = set(key for key in self.coverage_bins_for_sample(pose) if key is not None)
        if phase_key == "neutral":
            return {"yaw_center", "pitch_center", "roll_center"}.issubset(bins)
        ref_yaw, ref_pitch, ref_roll, ref_distance = self.head_cal_phase_reference or (yaw, pitch, roll, distance)
        if phase_key in ("yaw_left", "yaw_right"):
            return abs(yaw - ref_yaw) >= self.args.head_cal_yaw_threshold_deg
        if phase_key in ("pitch_up", "pitch_down"):
            return abs(pitch - ref_pitch) >= self.args.head_cal_pitch_threshold_deg
        if phase_key == "roll_left":
            return abs(roll - ref_roll) >= self.args.head_cal_roll_threshold_deg
        if phase_key == "roll_right":
            return abs(roll - ref_roll) >= self.args.head_cal_roll_threshold_deg
        return phase_key in bins

    def head_cal_phase_values(self, pose: EyePose) -> tuple[float, float, float, float]:
        return pose.yaw, pose.pitch_proxy, pose.roll, pose.tz

    def coverage_bins_for_sample(self, pose: EyePose) -> tuple[str, str, str, str | None]:
        threshold = self.args.head_cal_axis_threshold_deg
        if pose.yaw < -threshold:
            yaw_bin = "yaw_left"
        elif pose.yaw > threshold:
            yaw_bin = "yaw_right"
        else:
            yaw_bin = "yaw_center"
        if pose.pitch_proxy < -threshold:
            pitch_bin = "pitch_down"
        elif pose.pitch_proxy > threshold:
            pitch_bin = "pitch_up"
        else:
            pitch_bin = "pitch_center"
        if pose.roll < -threshold:
            roll_bin = "roll_left"
        elif pose.roll > threshold:
            roll_bin = "roll_right"
        else:
            roll_bin = "roll_center"
        distance_bin = None
        if pose.tz < -self.args.head_cal_translation_threshold_mm:
            distance_bin = "near"
        elif pose.tz > self.args.head_cal_translation_threshold_mm:
            distance_bin = "far"
        return yaw_bin, pitch_bin, roll_bin, distance_bin

    def update_coverage(self, pose: EyePose) -> None:
        for key in self.coverage_bins_for_sample(pose):
            if key is not None:
                self.coverage[key] += 1

    def head_cal_progress(self) -> float:
        target = max(self.args.head_cal_phase_samples, 1)
        filled = sum(clamp(count / target, 0.0, 1.0) for count in self.coverage.values())
        return 100.0 * filled / len(self.coverage)

    def head_cal_phase_progress(self) -> float:
        phase = self.current_head_calibration_phase()
        if phase is None:
            return 100.0
        target = max(self.args.head_cal_phase_samples, 1)
        return 100.0 * clamp(self.head_cal_phase_match_count / target, 0.0, 1.0)

    def head_cal_coverage_complete(self) -> bool:
        target = max(self.args.head_cal_phase_samples, 1)
        required = {bin_key for _key, _instruction, bins in HEAD_CALIBRATION_PHASES for bin_key in bins}
        return all(self.coverage.get(key, 0) >= target for key in required)

    def head_cal_instruction(self) -> str:
        if not self.head_calibrating:
            return "Press Head Cal for guided calibration. Follow each prompt until it advances."
        phase = self.current_head_calibration_phase()
        if phase is None:
            return "Calibration complete. Saving profile."
        index = self.head_cal_phase_index + 1
        total = len(HEAD_CALIBRATION_PHASES)
        key, instruction, required_bins = phase
        target = max(self.args.head_cal_phase_samples, 1)
        counts = f"{min(self.head_cal_phase_match_count, target)}/{target}"
        delta_text = self.head_cal_delta_text()
        threshold_text = self.head_cal_threshold_text(key)
        return f"Step {index}/{total}: {instruction}  Current step {counts}{delta_text}{threshold_text}. Press N for next step."

    def head_cal_delta_text(self) -> str:
        if self.head_cal_phase_reference is None or self.pose is None:
            return ""
        yaw, pitch, roll, distance = self.head_cal_phase_values(self.pose)
        ref_yaw, ref_pitch, ref_roll, ref_distance = self.head_cal_phase_reference
        return f"  dY {yaw - ref_yaw:+.1f} dP {pitch - ref_pitch:+.1f} dR {roll - ref_roll:+.1f}"

    def head_cal_threshold_text(self, phase_key: str) -> str:
        if phase_key in ("yaw_left", "yaw_right"):
            return f" need |dY|>{self.args.head_cal_yaw_threshold_deg:.1f}"
        if phase_key in ("pitch_up", "pitch_down"):
            return f" need |dP|>{self.args.head_cal_pitch_threshold_deg:.1f}"
        if phase_key in ("roll_left", "roll_right"):
            return f" need |dR|>{self.args.head_cal_roll_threshold_deg:.1f}"
        return ""

    def sample_rate(self) -> float:
        if len(self.packet_times) < 2:
            return 0.0
        elapsed = self.packet_times[-1] - self.packet_times[0]
        return (len(self.packet_times) - 1) / elapsed if elapsed > 0 else 0.0


    def draw(self) -> None:
        self.canvas.delete("all")
        self.click_targets.clear()
        w = max(self.canvas.winfo_width(), 1)
        h = max(self.canvas.winfo_height(), 1)
        self.draw_grid(w, h)

        left_w = min(520, max(420, int(w * 0.30)))
        self.canvas.create_rectangle(0, 0, left_w, h, fill=PANEL, outline=STROKE)
        self.draw_sidebar(28, 28, left_w - 56)
        self.draw_pose_area(left_w + 28, 28, w - left_w - 56, h - 56)
        if self.calibration_index is not None:
            self.draw_calibration_overlay(w, h)

    def draw_grid(self, w: int, h: int) -> None:
        for ratio in (0.25, 0.5, 0.75):
            x = w * ratio
            y = h * ratio
            self.canvas.create_line(x, 0, x, h, fill="#101820")
            self.canvas.create_line(0, y, w, y, fill="#101820")

    def draw_sidebar(self, x: int, y: int, w: int) -> None:
        self.canvas.create_text(x, y, anchor="nw", fill=TEXT, font=("Sans", 24, "bold"), text="Tobii Eye Pose")
        self.canvas.create_text(x, y + 38, anchor="nw", fill=MUTED, font=("Sans", 12), text="blended eye + face pose for Star Citizen")
        y += 78

        sample = self.latest
        pose = self.pose
        status_color = GREEN if self.status in ("streaming", "recentered") else YELLOW if "baseline" in self.status else RED
        self.metric_box(x, y, w, "status", self.status, status_color)
        y += 68
        half = (w - 12) / 2
        self.metric_box(x, y, half, "rate", f"{self.sample_rate():.1f} Hz", GREEN)
        self.metric_box(x + half + 12, y, half, "source", self.short_source(), self.source_color())
        y += 70

        if sample is not None:
            self.metric_box(x, y, half, "eyes", f"L{sample.eye_present_l} R{sample.eye_present_r}", GREEN if sample.valid else RED)
            self.metric_box(x + half + 12, y, half, "gaze", "valid" if sample.gaze_valid == 1 else "invalid", GREEN if sample.gaze_valid == 1 else YELLOW)
            y += 68
            self.value_line(x, y, "eye distance", f"{sample.eye_distance:6.1f} mm")
            y += 24
            self.value_line(x, y, "pupil", self.pupil_text(sample))
            y += 30

        if self.hq_worker is not None:
            hq = self.hq_worker.snapshot()
            age = time.monotonic() - hq.last_frame_monotonic if hq.last_frame_monotonic else 999.0
            hq_prime_ok = self.args.hq_mode == "prime" and not hq.running and hq.private_frames > 0 and "rc=0" in hq.last_error
            hq_ok = (hq.running and age <= 0.5 and hq.private_frames > 0) or hq_prime_ok
            if hq_prime_ok:
                hq_text = f"primed {hq.private_frames} frames"
            else:
                hq_text = f"{hq.fps:.1f} Hz kind {hq.latest_kind:04x}" if hq.private_frames else "waiting"
            if not hq.running and hq.last_error and not hq_prime_ok:
                hq_text = hq.last_error[:28]
            self.metric_box(x, y, w, "HQ camera", hq_text, GREEN if hq_ok else YELLOW if hq.running else RED)
            y += 70

        if pose is not None:
            self.section(x, y, "BLENDED POSE")
            y += 26
            col = (w - 16) / 3
            self.pose_chip(x, y, col, "yaw", pose.yaw, BLUE)
            self.pose_chip(x + col + 8, y, col, "pitch", pose.pitch_proxy, YELLOW)
            self.pose_chip(x + (col + 8) * 2, y, col, "roll", pose.roll, BLUE)
            y += 76
            self.value_line(x, y, "translation", f"x {pose.tx:+.1f}  y {pose.ty:+.1f}  z {pose.tz:+.1f} mm")
            y += 24
            self.value_line(x, y, "eye delta", f"{pose.eye_distance_delta:+.1f} mm")
            y += 30

            self.section(x, y, "PITCH DEBUG")
            y += 24
            raw_text = "--" if self.pitch_debug_raw is None else f"{self.pitch_debug_raw:+.1f}"
            delta_text = "--" if self.pitch_debug_delta is None else f"{self.pitch_debug_delta:+.1f}"
            mapped_text = "--" if self.pitch_debug_mapped is None else f"{self.pitch_debug_mapped:+.1f}"
            packet_text = "--"
            if self.opentrack_bridge is not None and self.opentrack_bridge.last_packet is not None:
                packet_text = f"{self.opentrack_bridge.last_packet[4]:+.1f}"
            self.value_line(x, y, "raw/delta", f"{raw_text} / {delta_text}")
            y += 22
            self.value_line(x, y, "mapped/out", f"{mapped_text} / {packet_text}")
            y += 22
            self.value_line(x, y, "state", self.pitch_debug_status)
            y += 32

            self.section(x, y, "CONFIDENCE")
            y += 24
            y = self.confidence_grid(x, y, w)
            y += 8
            y = self.calibration_controls(x, y, w)
            y += 12

        footer_y = self.canvas.winfo_height() - 48
        self.canvas.create_text(
            x,
            footer_y,
            anchor="sw",
            fill="#6b7788",
            font=("Sans", 9),
            text="Space recenter/capture  g gaze cal  h head cal  n next phase  r reset",
        )
        self.canvas.create_text(
            x,
            footer_y + 18,
            anchor="sw",
            fill="#6b7788",
            font=("Sans", 9),
            text="Tab harness  s smoothing  d pitch dump  Esc/q quit",
        )

    def draw_pose_area(self, x: int, y: int, w: int, h: int) -> None:
        self.canvas.create_rectangle(x, y, x + w, y + h, fill="#090d12", outline=STROKE)
        pad = 28
        self.canvas.create_text(x + pad, y + pad, anchor="nw", fill=TEXT, font=("Sans", 18, "bold"), text="Blended Pose Feedback")
        self.canvas.create_text(
            x + pad,
            y + pad + 28,
            anchor="nw",
            fill=MUTED,
            font=("Sans", 11),
            text=f"{self.face_tracker_label()} primary with Tobii eye-origin anchor    source: {self.pose_source}",
        )
        header_h = 88
        if self.head_calibrating:
            banner_y = y + pad + 56
            self.canvas.create_rectangle(
                x + pad,
                banner_y - 6,
                x + w - pad,
                banner_y + 44,
                fill="#121922",
                outline=STROKE,
            )
            self.canvas.create_text(
                x + pad + 12,
                banner_y,
                anchor="nw",
                fill=YELLOW,
                font=("Sans", 10, "bold"),
                text=self.head_cal_instruction(),
                width=w - pad * 2 - 24,
            )
            header_h = 124

        plot_y = y + header_h
        plot_h = min(260, max(180, (h - header_h - 40) / 3))
        col_gap = 22
        col_w = (w - pad * 2 - col_gap) / 2
        self.draw_yaw_pitch_plot(x + pad, plot_y, col_w, plot_h)
        self.draw_roll_translation_plot(x + pad + col_w + col_gap, plot_y, col_w, plot_h)

        info_y = plot_y + plot_h + 28
        info_h = min(210, y + h - info_y - 24)
        if info_h > 90:
            split_gap = 22
            split_w = (w - pad * 2 - split_gap) / 2
            self.draw_output_panel(x + pad, info_y, split_w, info_h)
            self.draw_gaze_panel(x + pad + split_w + split_gap, info_y, split_w, info_h)
            lower_y = info_y + info_h + 28
            lower_h = y + h - lower_y - 24
            if lower_h > 150:
                tuning_w = (w - pad * 2 - split_gap) * 0.62
                diag_w = w - pad * 2 - split_gap - tuning_w
                self.draw_tuning_panel(x + pad, lower_y, tuning_w, lower_h)
                self.draw_face_fallback_panel(x + pad + tuning_w + split_gap, lower_y, diag_w, lower_h)

    def draw_yaw_pitch_plot(self, x: float, y: float, w: float, h: float) -> None:
        self.canvas.create_rectangle(x, y, x + w, y + h, fill="#0b1016", outline=STROKE)
        self.canvas.create_text(x + 14, y + 14, anchor="nw", fill=MUTED, font=("Sans", 12), text="Blended yaw / pitch")
        cx = x + w / 2
        cy = y + h / 2
        self.canvas.create_line(cx, y + 42, cx, y + h - 28, fill="#26313d")
        self.canvas.create_line(x + 26, cy, x + w - 26, cy, fill="#26313d")
        for yaw, pitch in list(self.pose_trail):
            px = cx + clamp(yaw / self.args.angle_range_deg, -1, 1) * (w / 2 - 46)
            py = cy - clamp(pitch / self.args.angle_range_deg, -1, 1) * (h / 2 - 64)
            self.canvas.create_oval(px - 2, py - 2, px + 2, py + 2, fill="#315b7b", outline="")
        if self.pose is not None:
            px = cx + clamp(self.pose.yaw / self.args.angle_range_deg, -1, 1) * (w / 2 - 46)
            py = cy - clamp(self.pose.pitch_proxy / self.args.angle_range_deg, -1, 1) * (h / 2 - 64)
            self.canvas.create_oval(px - 18, py - 18, px + 18, py + 18, outline=GREEN, width=4)
            self.canvas.create_oval(px - 5, py - 5, px + 5, py + 5, fill=GREEN, outline="")
            self.canvas.create_text(x + 16, y + h - 24, anchor="sw", fill=TEXT, font=("Sans", 12), text=f"yaw {self.pose.yaw:+.2f}  pitch {self.pose.pitch_proxy:+.2f}")

    def draw_roll_translation_plot(self, x: float, y: float, w: float, h: float) -> None:
        self.canvas.create_rectangle(x, y, x + w, y + h, fill="#0b1016", outline=STROKE)
        self.canvas.create_text(x + 14, y + 14, anchor="nw", fill=MUTED, font=("Sans", 12), text="Blended roll / translation")
        cx = x + w / 2
        cy = y + h / 2
        self.canvas.create_line(cx, y + 42, cx, y + h - 28, fill="#26313d")
        self.canvas.create_line(x + 26, cy, x + w - 26, cy, fill="#26313d")
        if self.pose is None:
            return
        tx = clamp(self.pose.tx / self.args.translation_range_mm, -1, 1)
        ty = clamp(self.pose.ty / self.args.translation_range_mm, -1, 1)
        px = cx + tx * (w / 2 - 58)
        py = cy + ty * (h / 2 - 70)
        angle = math.radians(self.pose.roll)
        radius = 72
        self.canvas.create_oval(px - radius, py - radius, px + radius, py + radius, outline=BLUE, width=3)
        self.canvas.create_line(px, py, px + math.sin(angle) * radius, py - math.cos(angle) * radius, fill=GREEN, width=5)
        self.canvas.create_text(x + 16, y + h - 24, anchor="sw", fill=TEXT, font=("Sans", 12), text=f"x {self.pose.tx:+.1f} y {self.pose.ty:+.1f} z {self.pose.tz:+.1f} mm")
        if self.pose_source == "MediaPipe yaw/pitch fallback":
            self.canvas.create_text(
                x + w - 16,
                y + h - 24,
                anchor="se",
                fill=YELLOW,
                font=("Sans", 10),
                text="roll/translation held",
            )

    def draw_output_panel(self, x: float, y: float, w: float, h: float) -> None:
        self.canvas.create_rectangle(x, y, x + w, y + h, fill="#0b1016", outline=STROKE)
        self.canvas.create_text(x + 16, y + 14, anchor="nw", fill=MUTED, font=("Sans", 12, "bold"), text=f"{self.harness_label()} UDP Output")
        if self.opentrack_bridge is None or self.opentrack_bridge.last_packet is None:
            return
        packet = self.opentrack_bridge.last_packet
        values = [
            ("yaw packet", packet[3], "deg", BLUE),
            ("pitch packet", packet[4], "deg", YELLOW),
            ("roll packet", packet[5], "deg", BLUE),
        ]
        col_w = (w - 48) / 3
        for index, (label, value, unit, color) in enumerate(values):
            cx = x + 16 + index * col_w
            self.canvas.create_text(cx, y + 48, anchor="nw", fill=MUTED, font=("Sans", 11, "bold"), text=label)
            self.canvas.create_text(cx, y + 72, anchor="nw", fill=color, font=("Sans", 24, "bold"), text=f"{value:+.2f}")
            self.canvas.create_text(cx + 114, y + 82, anchor="nw", fill=MUTED, font=("Sans", 10), text=unit)
        gaze_text = ""
        if len(packet) >= 11:
            gaze_text = f"    gaze {packet[8]:.3f},{packet[9]:.3f} valid {int(packet[10])}"
        self.canvas.create_text(
            x + 16,
            y + h - 24,
            anchor="sw",
            fill=TEXT,
            font=("Sans", 11),
            text=f"source {self.opentrack_bridge.last_pose_source}    smoothing {'on' if self.smoothing_enabled else 'off'}{gaze_text}",
        )

    def draw_gaze_panel(self, x: float, y: float, w: float, h: float) -> None:
        self.canvas.create_rectangle(x, y, x + w, y + h, fill="#0b1016", outline=STROKE)
        mode = "calibrated" if self.gaze_affine is not None else "centered" if self.gaze_offset_x or self.gaze_offset_y else "raw"
        self.canvas.create_text(x + 16, y + 14, anchor="nw", fill=MUTED, font=("Sans", 12, "bold"), text=f"Gaze Screen Point ({mode})")
        plot_x = x + 18
        plot_y = y + 46
        plot_w = w - 36
        plot_h = max(70, h - 70)
        self.canvas.create_rectangle(plot_x, plot_y, plot_x + plot_w, plot_y + plot_h, fill="#090d12", outline=STROKE)
        self.canvas.create_line(plot_x + plot_w / 2, plot_y + 8, plot_x + plot_w / 2, plot_y + plot_h - 8, fill="#1d2833")
        self.canvas.create_line(plot_x + 8, plot_y + plot_h / 2, plot_x + plot_w - 8, plot_y + plot_h / 2, fill="#1d2833")
        for gx, gy in list(self.gaze_trail):
            px = plot_x + clamp(gx, 0.0, 1.0) * plot_w
            py = plot_y + clamp(gy, 0.0, 1.0) * plot_h
            self.canvas.create_oval(px - 2, py - 2, px + 2, py + 2, fill="#315b7b", outline="")
        if self.smoothed_gaze_ratio is not None:
            gx, gy = self.smoothed_gaze_ratio
            px = plot_x + clamp(gx, 0.0, 1.0) * plot_w
            py = plot_y + clamp(gy, 0.0, 1.0) * plot_h
            self.canvas.create_oval(px - 14, py - 14, px + 14, py + 14, outline=GREEN, width=3)
            self.canvas.create_oval(px - 4, py - 4, px + 4, py + 4, fill=GREEN, outline="")
            self.canvas.create_text(plot_x + 10, plot_y + plot_h - 10, anchor="sw", fill=TEXT, font=("Sans", 10), text=f"x {gx:.3f} y {gy:.3f}")

    def draw_tuning_panel(self, x: float, y: float, w: float, h: float) -> None:
        self.canvas.create_rectangle(x, y, x + w, y + h, fill="#0b1016", outline=STROKE)
        self.canvas.create_text(x + 16, y + 14, anchor="nw", fill=MUTED, font=("Sans", 12, "bold"), text="Star Citizen Tuning")
        if self.opentrack_bridge is None:
            self.canvas.create_text(x + 16, y + 48, anchor="nw", fill=MUTED, font=("Sans", 11), text="UDP output disabled")
            return
        inner_x = x + 16
        inner_w = w - 32
        row_y = y + 44
        row_y = self.harness_selector(inner_x, row_y, inner_w)
        row_y += 8
        slider_h = max(120, h - (row_y - y) - 82)
        controls = [
            ("yaw", "opentrack_yaw_scale", -30.0, 30.0, 0.25, BLUE, "{:+.2f}"),
            ("pitch", "opentrack_pitch_scale", -30.0, 30.0, 0.25, YELLOW, "{:+.2f}"),
            ("roll", "opentrack_roll_scale", -8.0, 8.0, 0.25, BLUE, "{:+.2f}"),
            ("x", "opentrack_x_scale", -8.0, 8.0, 0.25, BLUE, "{:+.2f}"),
            ("y", "opentrack_y_scale", -8.0, 8.0, 0.25, BLUE, "{:+.2f}"),
            ("z", "opentrack_z_scale", -8.0, 8.0, 0.25, BLUE, "{:+.2f}"),
            ("yaw curve", "opentrack_yaw_curve", 1.0, 4.0, 0.02, BLUE, "{:.2f}"),
            ("pitch curve", "opentrack_pitch_curve", 1.0, 4.0, 0.02, YELLOW, "{:.2f}"),
            ("mp yaw", "mediapipe_yaw_output_scale", 0.01, 1.00, 0.01, BLUE, "{:.2f}"),
            ("mp pitch", "mediapipe_pitch_output_scale", 0.01, 1.00, 0.01, YELLOW, "{:.2f}"),
            ("mp roll", "mediapipe_roll_output_scale", 0.01, 1.00, 0.01, BLUE, "{:.2f}"),
            ("mp p/y", "mediapipe_pitch_yaw_comp", 0.00, 3.00, 0.05, YELLOW, "{:.2f}"),
            ("knee", "opentrack_curve_knee_deg", 5.0, 90.0, 1.0, BLUE, "{:.0f}"),
            ("smooth", "opentrack_output_smoothing", 0.03, 0.50, 0.01, GREEN, "{:.2f}"),
            ("motion", "opentrack_motion_smoothing", 0.03, 0.60, 0.01, GREEN, "{:.2f}"),
            ("predict", "opentrack_prediction_ms", 0.0, 120.0, 5.0, GREEN, "{:.0f}"),
            ("dampen", "opentrack_stillness_deadband_deg", 0.00, 0.80, 0.02, GREEN, "{:.2f}"),
            ("angle guard", "opentrack_max_angle_step", 5.00, 180.00, 5.00, GREEN, "{:.0f}"),
            ("out cap", "opentrack_max_output_angle", 20.00, 360.00, 5.00, GREEN, "{:.0f}"),
            ("trans step", "opentrack_max_translation_step", 0.05, 1.50, 0.05, GREEN, "{:.2f}"),
            ("blink hold", "blink_hold_s", 0.00, 0.60, 0.02, GREEN, "{:.2f}"),
        ]
        max_controls = max(6, min(len(controls), int(slider_h // 30) * 2))
        row_y = self.slider_grid(inner_x, row_y, inner_w, controls[:max_controls])
        bridge = self.opentrack_bridge
        age = time.monotonic() - bridge.last_send_monotonic if bridge.last_send_monotonic else 0.0
        status_y = y + h - 54
        self.value_line(inner_x, status_y, "udp", f"{bridge.sent} packets  age {age:.1f}s  {bridge.last_error or f'sending {self.harness_label()}'}")
        if bridge.last_packet is not None:
            self.value_line(inner_x, status_y + 24, "last sent", f"src {self.short_source_for(bridge.last_pose_source)}  yaw {bridge.last_packet[3]:+.1f}  pitch {bridge.last_packet[4]:+.1f}  roll {bridge.last_packet[5]:+.1f}")

    def draw_face_fallback_panel(self, x: float, y: float, w: float, h: float) -> None:
        self.canvas.create_rectangle(x, y, x + w, y + h, fill="#0b1016", outline=STROKE)
        self.canvas.create_text(x + 16, y + 14, anchor="nw", fill=MUTED, font=("Sans", 12, "bold"), text="Pose Source Diagnostics")
        inner_x = x + 16
        row_y = y + 48
        self.section(inner_x, row_y, "HQ LANDMARKS")
        row_y += 24
        if self.hq_worker is None:
            self.canvas.create_text(inner_x, row_y, anchor="nw", fill=MUTED, font=("Sans", 11), text="HQ frame worker disabled")
            row_y += 34
        else:
            hq = self.hq_worker.snapshot()
            age = time.monotonic() - hq.last_frame_monotonic if hq.last_frame_monotonic else 999.0
            hq_prime_ok = self.args.hq_mode == "prime" and not hq.running and hq.private_frames > 0 and "rc=0" in hq.last_error
            hq_color = GREEN if (hq.running and age <= 0.5 and hq.private_frames > 0) or hq_prime_ok else YELLOW if hq.running else RED
            status = f"primed {hq.private_frames} frames" if hq_prime_ok else f"{hq.fps:.1f} Hz  age {age:.1f}s" if hq.private_frames else "waiting"
            if not hq.running and hq.last_error and not hq_prime_ok:
                status = hq.last_error[:42]
            self.metric_box(inner_x, row_y, w - 32, "endpoint 0x82", status, hq_color)
            row_y += 70
            self.value_line(inner_x, row_y, "source", "hq-landmarks pending")
            row_y += 24
            self.value_line(inner_x, row_y, "frames", f"private {hq.private_frames}  total {hq.frames}")
            row_y += 24
            self.value_line(inner_x, row_y, "kinds", f"1:{hq.kind0001}  2:{hq.kind0002}  3:{hq.kind0003}  other:{hq.kind_other}")
            row_y += 24
            self.value_line(inner_x, row_y, "latest", f"kind 0x{hq.latest_kind:04x}  avg {hq.latest_avg:.1f}  diff {hq.latest_neighbor_diff:.1f}")
            row_y += 24
            self.value_line(inner_x, row_y, "errors", f"{hq.errors}  timeouts {hq.timeouts}")
            row_y += 24
        mode = "mapped" if profile_has_pitch_map(self.profile) else "neutral wrap"
        self.value_line(inner_x, row_y, "pitch map", f"{mode}  {self.pitch_debug_status}")
        row_y += 24
        mapped = "--" if self.pitch_debug_mapped is None else f"{self.pitch_debug_mapped:+.1f}"
        output = "--"
        if self.opentrack_bridge is not None and self.opentrack_bridge.last_packet is not None:
            output = f"{self.opentrack_bridge.last_packet[4]:+.1f}"
        self.value_line(inner_x, row_y, "pitch out", f"mapped {mapped}  packet {output}")
        row_y += 32
        button_w = min(180, max(90, (w - 40) / 2))
        self.action_button(inner_x, row_y, button_w, 28, "Landmark View", self.open_inband_mediapipe_dashboard)
        self.action_button(inner_x + button_w + 8, row_y, button_w, 28, "Dump Pitch CSV", self.dump_pitch_debug)

        if self.mediapipe_bridge is not None:
            row_y = y + h - 186 if h > 260 else row_y + 34
            self.section(inner_x, row_y, "MEDIAPIPE")
            row_y += 24
            mp = self.latest_mediapipe
            if mp is None:
                status = "waiting"
                color = YELLOW
            else:
                age = time.monotonic() - mp.seen_monotonic
                status = f"conf {mp.confidence:.2f} age {age:.1f}s latency {mp.latency_ms:.1f}ms"
                color = GREEN if self.mediapipe_sample_usable(mp) and age <= self.args.mediapipe_max_age_s else RED
            self.metric_box(inner_x, row_y, w - 32, "MediaPipe", status, color)
            row_y += 70
            bridge = self.mediapipe_bridge
            frame_age = time.monotonic() - bridge.last_frame_monotonic if bridge.last_frame_monotonic else 0.0
            self.value_line(inner_x, row_y, "frames", f"seen {bridge.frames_seen} sent {bridge.frames_sent} face {bridge.frames_present} age {frame_age:.1f}s")
            row_y += 24
            raw = "--" if mp is None else f"y {mp.yaw:+.1f} p {mp.pitch:+.1f} r {mp.roll:+.1f}"
            self.value_line(inner_x, row_y, "raw", raw)

    def draw_calibration_overlay(self, width: int, height: int) -> None:
        if self.calibration_index is None:
            return
        label, x_ratio, y_ratio = CALIBRATION_TARGETS[self.calibration_index]
        x = x_ratio * width
        y = y_ratio * height
        self.canvas.create_rectangle(0, 0, width, height, fill=BG, stipple="gray50", outline="")
        self.canvas.create_text(
            width / 2,
            52,
            fill=TEXT,
            font=("Sans", 24, "bold"),
            text=f"Gaze calibration {self.calibration_index + 1}/{len(CALIBRATION_TARGETS)}",
        )
        self.canvas.create_text(
            width / 2,
            88,
            fill=MUTED,
            font=("Sans", 14),
            text=f"Look at {label}, hold steady, then press Space.",
        )
        radius = 34
        color = YELLOW
        self.canvas.create_oval(x - radius, y - radius, x + radius, y + radius, outline=color, width=5)
        self.canvas.create_line(x - radius * 1.8, y, x - radius * 0.45, y, fill=color, width=4)
        self.canvas.create_line(x + radius * 0.45, y, x + radius * 1.8, y, fill=color, width=4)
        self.canvas.create_line(x, y - radius * 1.8, x, y - radius * 0.45, fill=color, width=4)
        self.canvas.create_line(x, y + radius * 0.45, x, y + radius * 1.8, fill=color, width=4)
        self.canvas.create_oval(x - 5, y - 5, x + 5, y + 5, fill=color, outline=color)

    def short_source(self) -> str:
        return self.short_source_for(self.pose_source)

    def short_source_for(self, source: str) -> str:
        if source == "eye binocular":
            return "eye"
        if source == "blended eye+face":
            return "blend"
        if source == "MediaPipe head primary":
            return "face"
        if source == "MediaPipe yaw/pitch fallback":
            return "face"
        if source == "HQ landmarks":
            return "hq"
        if source == "hold last pose":
            return "hold"
        return source or "none"

    def source_color(self) -> str:
        if self.pose_source == "eye binocular":
            return GREEN
        if self.pose_source == "blended eye+face":
            return "#8be8ff"
        if self.pose_source in ("MediaPipe head primary", "MediaPipe yaw/pitch fallback"):
            return BLUE
        if self.pose_source == "HQ landmarks":
            return GREEN
        if self.pose_source == "hold last pose":
            return YELLOW
        return RED

    def face_tracker_label(self) -> str:
        return "MediaPipe"

    def harness_label(self) -> str:
        return "Tobii" if self.args.harness == "tobii" else "TrackIR"

    def harness_selector(self, x: float, y: float, w: float) -> float:
        self.canvas.create_text(x, y + 8, anchor="nw", fill=MUTED, font=("Sans", 11, "bold"), text="output")
        button_w = min(210, max(150, w - 220))
        start_x = x + 84
        self.canvas.create_rectangle(start_x, y, start_x + button_w, y + 32, fill="#121922", outline=BLUE, width=2)
        self.canvas.create_text(start_x + 12, y + 16, anchor="w", fill=TEXT, font=("Sans", 11, "bold"), text=self.harness_label())
        self.canvas.create_text(start_x + button_w - 16, y + 16, anchor="center", fill=MUTED, font=("Sans", 12, "bold"), text="v")
        self.click_targets.append((start_x, y, start_x + button_w, y + 32, self.toggle_harness_menu))
        if self.harness_menu_open:
            for index, (label, harness) in enumerate((("Tobii", "tobii"), ("TrackIR", "trackir"))):
                oy = y + 34 + index * 30
                selected = self.args.harness == harness
                fill = "#16324a" if selected else "#101720"
                self.canvas.create_rectangle(start_x, oy, start_x + button_w, oy + 30, fill=fill, outline=STROKE)
                self.canvas.create_text(start_x + 12, oy + 15, anchor="w", fill=TEXT, font=("Sans", 11), text=label)
                self.click_targets.append((start_x, oy, start_x + button_w, oy + 30, lambda _px, _py, value=harness: self.set_harness(value)))
        target = self.active_udp_target() or "--"
        self.canvas.create_text(start_x + button_w + 14, y + 8, anchor="nw", fill=MUTED, font=("Sans", 10), text=target)
        return y + (102 if self.harness_menu_open else 38)

    def pose_chip(self, x: float, y: float, w: float, label: str, value: float, color: str) -> None:
        self.canvas.create_rectangle(x, y, x + w, y + 58, fill="#121922", outline=STROKE)
        self.canvas.create_text(x + 10, y + 8, anchor="nw", fill=MUTED, font=("Sans", 10, "bold"), text=label)
        self.canvas.create_text(x + 10, y + 28, anchor="nw", fill=color, font=("Sans", 16, "bold"), text=f"{value:+.2f}")

    def slider_control(
        self,
        x: float,
        y: float,
        w: float,
        label: str,
        attr: str,
        min_value: float,
        max_value: float,
        step: float,
        color: str,
        fmt: str = "{:+.2f}",
    ) -> float:
        value = float(getattr(self.args, attr))
        value = clamp(value, min_value, max_value)
        setattr(self.args, attr, value)

        compact = w < 300
        label_w = 72 if compact else 112
        value_w = 44 if compact else 58
        slider_x = x + label_w
        slider_w = max(80, w - label_w - value_w - 12)
        slider_y = y + 14
        ratio = (value - min_value) / (max_value - min_value)
        knob_x = slider_x + clamp(ratio, 0.0, 1.0) * slider_w

        self.canvas.create_text(x, y + 5, anchor="nw", fill=MUTED, font=("Sans", 9 if compact else 10, "bold"), text=label)
        self.canvas.create_rectangle(slider_x, slider_y - 4, slider_x + slider_w, slider_y + 4, fill="#101720", outline=STROKE)
        self.canvas.create_line(knob_x, slider_y - 10, knob_x, slider_y + 10, fill=color, width=2)
        self.canvas.create_oval(knob_x - 5, slider_y - 5, knob_x + 5, slider_y + 5, fill=color, outline="")
        self.canvas.create_text(x + w, y + 3, anchor="ne", fill=TEXT, font=("Sans", 10), text=fmt.format(value))

        def set_value(px: float, _py: float) -> None:
            slider_ratio = clamp((px - slider_x) / slider_w, 0.0, 1.0)
            updated = min_value + slider_ratio * (max_value - min_value)
            if step > 0:
                updated = round(updated / step) * step
            setattr(self.args, attr, clamp(updated, min_value, max_value))
            self.save_tuning()

        self.click_targets.append((slider_x - 10, y, slider_x + slider_w + 10, y + 28, set_value))
        return y + 28

    def load_head_calibration(self) -> HeadCalibrationProfile:
        profile_file = getattr(self.args, "head_calibration_file", None)
        if profile_file is None or not profile_file.exists():
            return HeadCalibrationProfile()
        try:
            data = json.loads(profile_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return HeadCalibrationProfile()
        if not isinstance(data, dict):
            return HeadCalibrationProfile()

        def number(name: str, default: float) -> float:
            try:
                return float(data.get(name, default))
            except (TypeError, ValueError):
                return default

        def optional_number(name: str) -> float | None:
            if data.get(name) is None:
                return None
            value = number(name, float("nan"))
            return value if math.isfinite(value) else None

        return HeadCalibrationProfile(
            yaw_left_scale=number("yaw_left_scale", 1.0),
            yaw_right_scale=number("yaw_right_scale", 1.0),
            yaw_left_sign=number("yaw_left_sign", -1.0),
            yaw_right_sign=number("yaw_right_sign", 1.0),
            pitch_up_scale=number("pitch_up_scale", 1.0),
            pitch_down_scale=number("pitch_down_scale", 1.0),
            pitch_up_sign=number("pitch_up_sign", 1.0),
            pitch_down_sign=number("pitch_down_sign", -1.0),
            roll_left_scale=number("roll_left_scale", 1.0),
            roll_right_scale=number("roll_right_scale", 1.0),
            roll_left_sign=number("roll_left_sign", -1.0),
            roll_right_sign=number("roll_right_sign", 1.0),
            face_yaw_neutral=optional_number("face_yaw_neutral"),
            face_pitch_neutral=optional_number("face_pitch_neutral"),
            face_roll_neutral=optional_number("face_roll_neutral"),
            pitch_up_mid_raw=optional_number("pitch_up_mid_raw"),
            pitch_up_mid_value=optional_number("pitch_up_mid_value"),
            pitch_up_max_raw=optional_number("pitch_up_max_raw"),
            pitch_up_max_value=optional_number("pitch_up_max_value"),
            pitch_down_raw=optional_number("pitch_down_raw"),
            pitch_down_value=optional_number("pitch_down_value"),
            created=number("created", 0.0),
        )

    def save_head_calibration(self) -> None:
        profile_file = getattr(self.args, "head_calibration_file", None)
        if profile_file is None:
            return
        data = {
            "profile_version": 2,
            "yaw_left_scale": self.profile.yaw_left_scale,
            "yaw_right_scale": self.profile.yaw_right_scale,
            "yaw_left_sign": self.profile.yaw_left_sign,
            "yaw_right_sign": self.profile.yaw_right_sign,
            "pitch_up_scale": self.profile.pitch_up_scale,
            "pitch_down_scale": self.profile.pitch_down_scale,
            "pitch_up_sign": self.profile.pitch_up_sign,
            "pitch_down_sign": self.profile.pitch_down_sign,
            "roll_left_scale": self.profile.roll_left_scale,
            "roll_right_scale": self.profile.roll_right_scale,
            "roll_left_sign": self.profile.roll_left_sign,
            "roll_right_sign": self.profile.roll_right_sign,
            "face_yaw_neutral": self.profile.face_yaw_neutral,
            "face_pitch_neutral": self.profile.face_pitch_neutral,
            "face_roll_neutral": self.profile.face_roll_neutral,
            "pitch_up_mid_raw": self.profile.pitch_up_mid_raw,
            "pitch_up_mid_value": self.profile.pitch_up_mid_value,
            "pitch_up_max_raw": self.profile.pitch_up_max_raw,
            "pitch_up_max_value": self.profile.pitch_up_max_value,
            "pitch_down_raw": self.profile.pitch_down_raw,
            "pitch_down_value": self.profile.pitch_down_value,
            "created": self.profile.created,
            "coverage": self.coverage,
            "samples": len(self.head_cal_samples),
        }
        try:
            profile_file.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = profile_file.with_suffix(profile_file.suffix + ".tmp")
            tmp_path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            tmp_path.replace(profile_file)
        except OSError:
            pass

    def save_tuning(self) -> None:
        tuning_file = getattr(self.args, "tuning_file", None)
        if tuning_file is None:
            return
        data: dict[str, float | str] = {}
        for attr in TUNING_ATTRS:
            if not hasattr(self.args, attr):
                continue
            value = getattr(self.args, attr)
            if attr in ("harness", "tobii_roll_source", "tobii_head_pose_source"):
                data[attr] = str(value)
            else:
                data[attr] = float(value)
        try:
            tuning_file.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = tuning_file.with_suffix(tuning_file.suffix + ".tmp")
            tmp_path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            tmp_path.replace(tuning_file)
        except OSError:
            pass

    def save_window_state(self) -> None:
        state_file = getattr(self.args, "window_state_file", None)
        if state_file is None or self.args.headless or self.args.fullscreen:
            return
        try:
            self.root.update_idletasks()
            width = int(self.root.winfo_width())
            height = int(self.root.winfo_height())
            x = int(self.root.winfo_x())
            y = int(self.root.winfo_y())
            if width < 600 or height < 400:
                return
            data = {"width": width, "height": height, "x": x, "y": y}
            state_file.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = state_file.with_suffix(state_file.suffix + ".tmp")
            tmp_path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            tmp_path.replace(state_file)
        except (OSError, RuntimeError):
            pass

    def slider_grid(
        self,
        x: float,
        y: float,
        w: float,
        controls: list[tuple[str, str, float, float, float, str, str]],
    ) -> float:
        gap = 14
        col_w = (w - gap) / 2
        row_y = y
        for index in range(0, len(controls), 2):
            left = controls[index]
            self.slider_control(x, row_y, col_w, *left)
            if index + 1 < len(controls):
                right = controls[index + 1]
                self.slider_control(x + col_w + gap, row_y, col_w, *right)
            row_y += 30
        return row_y

    def confidence_grid(self, x: float, y: float, w: float) -> float:
        gap = 14
        col_w = (w - gap) / 2
        items = [
            ("eye", self.confidence.eye, GREEN),
            ("face", self.confidence.face, BLUE),
            ("blend", self.confidence.blended, "#8be8ff"),
            ("pitch", self.confidence.pitch, YELLOW),
            ("eye dist", self.confidence.eye_distance_stable, GREEN),
            ("velocity", self.confidence.velocity_ok, GREEN if self.confidence.velocity_ok > 0.65 else YELLOW),
        ]
        row_y = y
        for index in range(0, len(items), 2):
            label, value, color = items[index]
            self.percent_bar(x, row_y, col_w, label, value, color)
            if index + 1 < len(items):
                label, value, color = items[index + 1]
                self.percent_bar(x + col_w + gap, row_y, col_w, label, value, color)
            row_y += 24
        return row_y

    def calibration_controls(self, x: float, y: float, w: float) -> float:
        self.section(x, y, "CALIBRATION")
        y += 24
        progress = self.head_cal_progress()
        state = "active" if self.head_calibrating else ("saved" if self.profile.created else "none")
        self.value_line(x, y, "head profile", f"{state} {progress:.0f}% coverage")
        y += 26
        self.canvas.create_text(x, y, anchor="nw", fill=MUTED, font=("Sans", 9), text=self.head_cal_instruction(), width=w)
        y += 40
        y = self.coverage_bars(x, y, w)
        y += 6
        button_w = (w - 16) / 3
        self.action_button(x, y, button_w, 30, "Save Cal" if self.head_calibrating else "Head Cal", self.start_head_calibration)
        self.action_button(x + button_w + 8, y, button_w, 30, "Reset Gaze", self.reset_gaze_calibration)
        self.action_button(x + (button_w + 8) * 2, y, button_w, 30, "Reset Tuning", self.reset_tuning)
        y += 38
        self.action_button(x, y, button_w, 28, "Reset Head", self.reset_head_calibration)
        return y + 34

    def coverage_bars(self, x: float, y: float, w: float) -> float:
        target = max(self.args.head_cal_phase_samples, 1)
        gap = 8
        cols = 4
        cell_w = (w - gap * (cols - 1)) / cols
        row_y = y
        for index, (key, label) in enumerate(HEAD_CALIBRATION_BINS):
            cx = x + (index % cols) * (cell_w + gap)
            if index > 0 and index % cols == 0:
                row_y += 30
            self.canvas.create_text(cx, row_y, anchor="nw", fill=MUTED, font=("Sans", 8, "bold"), text=label)
            bx = cx
            by = row_y + 14
            self.canvas.create_rectangle(bx, by, bx + cell_w, by + 8, fill="#101720", outline=STROKE)
            phase = self.current_head_calibration_phase()
            active_bins = set(phase[2]) if self.head_calibrating and phase is not None else set()
            if self.head_calibrating and phase is not None and phase[0] in ("pitch_up", "pitch_down"):
                active_bins.update(("pitch_up", "pitch_down"))
            if self.head_calibrating:
                count = self.head_cal_phase_match_count if key in active_bins else 0
                ratio = clamp(count / target, 0.0, 1.0)
            else:
                count = self.coverage.get(key, 0)
                ratio = clamp(count / target, 0.0, 1.0)
            fill = YELLOW if key in active_bins and ratio < 1.0 else GREEN if ratio >= 1.0 else BLUE if ratio > 0.35 else "#536170"
            self.canvas.create_rectangle(bx, by, bx + cell_w * ratio, by + 8, fill=fill, outline="")
        return row_y + 30

    def percent_bar(self, x: float, y: float, w: float, label: str, value: float, color: str) -> None:
        value = clamp(value, 0.0, 1.0)
        self.canvas.create_text(x, y + 1, anchor="nw", fill=MUTED, font=("Sans", 9, "bold"), text=label)
        bx = x + 70
        bw = max(40, w - 112)
        self.canvas.create_rectangle(bx, y + 5, bx + bw, y + 15, fill="#101720", outline=STROKE)
        self.canvas.create_rectangle(bx, y + 5, bx + bw * value, y + 15, fill=color, outline="")
        self.canvas.create_text(x + w, y, anchor="ne", fill=TEXT, font=("Sans", 9), text=f"{value:.2f}")

    def action_button(self, x: float, y: float, w: float, h: float, text: str, callback: Callable[[float, float], None]) -> None:
        self.canvas.create_rectangle(x, y, x + w, y + h, fill="#182333", outline=STROKE)
        self.canvas.create_text(x + w / 2, y + h / 2, anchor="center", fill=TEXT, font=("Sans", 10, "bold"), text=text)
        self.click_targets.append((x, y, x + w, y + h, lambda _px, _py: callback(None)))

    def gain_control(self, x: float, y: float, w: float, label: str, axis: str, value: float) -> float:
        self.canvas.create_text(x, y + 7, anchor="nw", fill=MUTED, font=("Sans", 11, "bold"), text=label)
        minus_x = x + w - 132
        plus_x = x + w - 42
        self.control_button(minus_x, y, 36, 30, "-", axis, -0.25)
        self.canvas.create_rectangle(minus_x + 42, y, plus_x - 6, y + 30, fill="#101720", outline=STROKE)
        self.canvas.create_text((minus_x + 42 + plus_x - 6) / 2, y + 15, anchor="center", fill=TEXT, font=("Sans", 12, "bold"), text=f"{value:+.2f}")
        self.control_button(plus_x, y, 36, 30, "+", axis, 0.25)
        return y + 40

    def control_button(self, x: float, y: float, w: float, h: float, text: str, axis: str, delta: float) -> None:
        self.canvas.create_rectangle(x, y, x + w, y + h, fill="#182333", outline=STROKE)
        self.canvas.create_text(x + w / 2, y + h / 2, anchor="center", fill=TEXT, font=("Sans", 14, "bold"), text=text)
        self.click_targets.append((x, y, x + w, y + h, lambda _px, _py: self.adjust_gain(axis, delta)))

    def metric_box(self, x: float, y: float, w: float, label: str, value: str, color: str) -> None:
        self.canvas.create_rectangle(x, y, x + w, y + 58, fill="#121922", outline=STROKE)
        self.canvas.create_text(x + 12, y + 10, anchor="nw", fill=MUTED, font=("Sans", 10, "bold"), text=label)
        self.canvas.create_text(x + 12, y + 30, anchor="nw", fill=color, font=("Sans", 14, "bold"), text=value)

    def section(self, x: float, y: float, label: str) -> None:
        self.canvas.create_text(x, y, anchor="nw", fill=MUTED, font=("Sans", 11, "bold"), text=label)

    def value_line(self, x: float, y: float, label: str, value: str) -> None:
        self.canvas.create_text(x, y, anchor="nw", fill=MUTED, font=("Sans", 11, "bold"), text=label)
        self.canvas.create_text(x + 130, y, anchor="nw", fill=TEXT, font=("Sans", 12), text=value)

    def big_number(self, x: float, y: float, w: float, label: str, value: float, unit: str, color: str) -> None:
        self.canvas.create_text(x, y, anchor="nw", fill=MUTED, font=("Sans", 11, "bold"), text=label)
        self.canvas.create_text(x + 150, y - 6, anchor="nw", fill=color, font=("Sans", 24, "bold"), text=f"{value:+.2f}")
        self.canvas.create_text(x + w - 40, y + 2, anchor="ne", fill=MUTED, font=("Sans", 11), text=unit)

    def bar(self, x: float, y: float, w: float, label: str, value: float, scale: float, color: str) -> None:
        self.canvas.create_text(x, y, anchor="nw", fill=MUTED, font=("Sans", 10, "bold"), text=label)
        bx = x + 96
        bw = w - 172
        mid = bx + bw / 2
        self.canvas.create_rectangle(bx, y + 4, bx + bw, y + 20, fill="#111923", outline=STROKE)
        self.canvas.create_line(mid, y + 2, mid, y + 22, fill="#435060")
        end = mid + clamp(value / scale, -1, 1) * (bw / 2)
        self.canvas.create_rectangle(min(mid, end), y + 7, max(mid, end), y + 17, fill=color if value >= 0 else RED, outline="")
        self.canvas.create_text(x + w - 62, y + 2, anchor="w", fill=TEXT, font=("Sans", 10), text=f"{value:+.1f}")

    def pupil_text(self, sample: EyeSample) -> str:
        left = f"{sample.pupil_l:.2f}" if sample.pupil_l is not None else "--"
        right = f"{sample.pupil_r:.2f}" if sample.pupil_r is not None else "--"
        return f"L {left} mm  R {right} mm"

    def fit_text(self, fit: tuple[float, float] | None) -> str:
        if fit is None:
            return "uncalibrated"
        return f"a {fit[0]:+.2f} b {fit[1]:+.1f}"


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
    for attr in TUNING_ATTRS:
        value = data.get(attr)
        if attr == "harness" and value in ("tobii", "trackir"):
            setattr(args, attr, value)
        elif attr == "tobii_roll_source" and value in ("zero", "pose", "eye"):
            setattr(args, attr, "pose" if value == "eye" else value)
        elif attr == "tobii_head_pose_source" and value in ("face", "blend", "eye"):
            setattr(args, attr, value)
        elif isinstance(value, (int, float)):
            setattr(args, attr, float(value))


def load_window_geometry(args: argparse.Namespace) -> str:
    state_file = getattr(args, "window_state_file", None)
    if state_file is None or not state_file.exists():
        return "1400x900"
    try:
        data = json.loads(state_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "1400x900"
    if not isinstance(data, dict):
        return "1400x900"
    try:
        width = int(data.get("width", 1400))
        height = int(data.get("height", 900))
        x = int(data.get("x", 0))
        y = int(data.get("y", 0))
    except (TypeError, ValueError):
        return "1400x900"
    width = int(clamp(width, 600, 3840))
    height = int(clamp(height, 400, 2160))
    x = int(clamp(x, -200, 8000))
    y = int(clamp(y, -200, 8000))
    return f"{width}x{height}+{x}+{y}"


def main() -> int:
    parser = argparse.ArgumentParser(description="Realtime eye-origin-only Tobii head pose dashboard.")
    parser.add_argument("--windowed", action="store_true", default=True, help="Run in a window. This is the default.")
    parser.add_argument("--fullscreen", action="store_true", help="Run fullscreen.")
    parser.add_argument("--headless", action="store_true", help="Run the bridge without showing the dashboard window.")
    parser.add_argument("--skip-build", action="store_true")
    parser.add_argument("--startup", choices=("public", "tobiifree"), default="tobiifree")
    parser.add_argument("--display-area", choices=("none", "big", "rect"), default="big")
    parser.add_argument("--sensor-distance-mm", type=float, default=25.0 * 25.4, help="Natural seated distance from sensor. Default is 25 inches.")
    parser.add_argument("--baseline-samples", type=int, default=36)
    parser.add_argument("--smoothing", type=float, default=0.42, help="EMA alpha. Higher is more responsive.")
    parser.add_argument("--pose-hold-s", type=float, default=0.35, help="Hold last good pose this long when both eyes and face tracking momentarily drop.")
    parser.add_argument("--blink-hold-s", type=float, default=0.20, help="Hold binocular pose for short blink/dropout gaps before switching to face fallback.")
    parser.add_argument("--eye-fallback-after-s", type=float, default=0.12, help="Use face fallback after this long without a fresh binocular eye pose.")
    parser.add_argument("--interval-ms", type=int, default=16)
    parser.add_argument("--angle-range-deg", type=float, default=35.0)
    parser.add_argument("--translation-range-mm", type=float, default=120.0)
    parser.add_argument("--csv", type=Path)
    parser.add_argument("--tuning-file", type=Path, help="Persist live SC/OpenTrack tuning sliders in this JSON file.")
    parser.add_argument("--window-state-file", type=Path, help="Persist dashboard window size and position in this JSON file.")
    parser.add_argument("--head-calibration-file", type=Path, help="Persist deliberate head-pose calibration separately from quick tuning.")
    parser.add_argument("--head-cal-min-duration-s", type=float, default=8.0, help="Legacy natural-motion calibration minimum duration.")
    parser.add_argument("--head-cal-min-samples", type=int, default=80, help="Minimum matched eye/face samples needed to save head calibration.")
    parser.add_argument("--head-cal-min-eye-confidence", type=float, default=0.65)
    parser.add_argument("--head-cal-bin-samples", type=int, default=12, help="Legacy alias; guided calibration uses --head-cal-phase-samples.")
    parser.add_argument("--head-cal-phase-samples", type=int, default=28, help="Fresh samples required for each guided head calibration phase.")
    parser.add_argument("--head-cal-phase-min-duration-s", type=float, default=0.8, help="Minimum time to spend on each guided calibration phase.")
    parser.add_argument("--head-cal-axis-threshold-deg", type=float, default=2.0)
    parser.add_argument("--head-cal-yaw-threshold-deg", type=float, default=4.0, help="Physical yaw movement required from the start of a guided yaw phase.")
    parser.add_argument("--head-cal-pitch-threshold-deg", type=float, default=8.0, help="Physical pitch movement required from the start of a guided pitch phase.")
    parser.add_argument("--head-cal-roll-threshold-deg", type=float, default=6.0, help="Physical roll movement required from the start of a guided roll phase.")
    parser.add_argument("--head-cal-translation-threshold-mm", type=float, default=25.0)
    parser.add_argument("--eye-distance-tolerance-mm", type=float, default=5.0, help="Eye-distance drift that fades eye-origin head-pose confidence to zero.")
    parser.add_argument("--pose-velocity-soft-limit-dps", type=float, default=160.0, help="Pose velocity where eye-origin confidence begins to decay.")
    parser.add_argument("--pose-velocity-hard-limit-dps", type=float, default=420.0, help="Pose velocity where eye-origin confidence reaches zero.")
    parser.add_argument("--eye-yaw-edge-deg", type=float, default=7.0, help="Yaw where Tobii eye-origin starts yielding to face fallback.")
    parser.add_argument("--eye-yaw-full-face-deg", type=float, default=14.0, help="Yaw where face fallback fully owns yaw if available.")
    parser.add_argument("--eye-pitch-up-edge-deg", type=float, default=1.0, help="Upward pitch where eye-origin starts yielding to face fallback.")
    parser.add_argument("--eye-pitch-up-full-face-deg", type=float, default=5.0, help="Upward pitch where face fallback fully owns pitch if available.")
    parser.add_argument("--pitch-snapback-guard-deg", type=float, default=0.20, help="Maximum raw pose degrees per tick that high upward pitch can fall while eye confidence is low.")
    parser.add_argument("--pitch-wrap-guard-deg", type=float, default=45.0, help="Hold pitch when mapped face pitch jumps more than this many degrees in one update.")
    parser.add_argument("--pitch-hold-s", type=float, default=0.28, help="Briefly hold the last stable pitch when face pitch becomes invalid.")
    parser.add_argument("--pitch-hold-min-face-confidence", type=float, default=0.55, help="Face confidence where high pitch reversal is held instead of decayed.")
    parser.add_argument("--pitch-debug-seconds", type=float, default=10.0, help="Seconds of pitch diagnostics retained for the d-key CSV dump.")
    parser.add_argument("--transition-smoothing", type=float, default=0.10, help="EMA alpha during blinks/source transitions. Lower is smoother.")
    parser.add_argument("--hq-frames", dest="hq_frames", action="store_true", default=True, help="Start the endpoint 0x82 HQ 560x560 frame worker.")
    parser.add_argument("--no-hq-frames", dest="hq_frames", action="store_false", help="Disable the endpoint 0x82 HQ frame worker.")
    parser.add_argument("--hq-mode", choices=("prime", "continuous"), default="prime", help="HQ camera mode. prime captures a short burst then releases the endpoint before gaze starts.")
    parser.add_argument("--hq-reads", type=int, default=8, help="HQ frames to read in prime mode before releasing endpoint 0x82.")
    parser.add_argument("--hq-chunk-size", type=int, default=700000, help="HQ endpoint 0x82 bulk read size. Keep above 313612.")
    parser.add_argument("--hq-timeout-ms", type=int, default=1000)
    parser.add_argument("--hq-sleep-ms", type=int, default=24, help="Delay between HQ endpoint reads for live use.")
    parser.add_argument("--hq-xu-set", default="", help="Optional UVC XU 4:1 mode a,b,c for HQ worker. Default uses no XU write.")
    parser.add_argument("--hq-start-delay-ms", type=int, default=1000, help="Delay after starting HQ frames before starting gaze.")
    parser.add_argument("--ttp-streams", default="050e,1771", help="TTP streams to subscribe for gaze, in-band image frames, and sync.")
    parser.add_argument("--face-frame-hz", type=int, default=60, help="In-band IR frame write rate for MediaPipe face tracking.")
    parser.add_argument("--mediapipe-python", type=Path, default=repo_root() / ".venv" / "mediapipe" / "bin" / "python")
    parser.add_argument("--mediapipe-model", type=Path, default=repo_root() / "assets" / "mediapipe" / "face_landmarker.task")
    parser.add_argument("--mediapipe-image-stream", default="050e")
    parser.add_argument("--mediapipe-preprocess", choices=("raw", "stretch", "clahe"), default="clahe")
    parser.add_argument("--mediapipe-upscale", type=float, default=1.0)
    parser.add_argument("--mediapipe-rotation-mode", choices=("forward", "euler"), default="forward", help="MediaPipe transform decomposition. forward preserves pitch better during combined yaw/pitch; euler is the old mode.")
    parser.add_argument("--mediapipe-pose-source", choices=("matrix", "landmark-normal", "hybrid"), default="hybrid", help="MediaPipe pose source. hybrid keeps matrix yaw/roll and uses landmark-plane pitch for combined yaw/pitch stability.")
    parser.add_argument("--mediapipe-min-detection-confidence", type=float, default=0.35)
    parser.add_argument("--mediapipe-min-presence-confidence", type=float, default=0.35)
    parser.add_argument("--mediapipe-min-tracking-confidence", type=float, default=0.35)
    parser.add_argument("--mediapipe-min-face-confidence", type=float, default=0.35)
    parser.add_argument("--mediapipe-max-age-s", type=float, default=0.35)
    parser.add_argument("--mediapipe-yaw-sign", type=float, default=1.0)
    parser.add_argument("--mediapipe-pitch-sign", type=float, default=1.0)
    parser.add_argument("--mediapipe-roll-sign", type=float, default=1.0)
    parser.add_argument("--mediapipe-yaw-output-scale", type=float, default=0.15, help="Scale MediaPipe yaw before Star Citizen gain/curve output.")
    parser.add_argument("--mediapipe-pitch-output-scale", type=float, default=0.10, help="Scale MediaPipe pitch before Star Citizen gain/curve output.")
    parser.add_argument("--mediapipe-roll-output-scale", type=float, default=0.15, help="Scale MediaPipe roll before Star Citizen gain output.")
    parser.add_argument("--mediapipe-pitch-yaw-comp", type=float, default=1.0, help="Compensate MediaPipe pitch attenuation as yaw increases. 0 disables.")
    parser.add_argument("--harness", choices=("tobii", "trackir"), default="tobii", help="Default Star Citizen output harness.")
    parser.add_argument("--tobii-head-pose-source", choices=("face", "blend", "eye"), default="face", help="Live head-pose source for native Tobii output. Face uses MediaPipe primary; blend keeps eye-origin primary with face fallback.")
    parser.add_argument(
        "--tobii-roll-source",
        choices=("zero", "pose", "eye"),
        default="pose",
        help="Roll source for native Tobii packets. Use zero to disable roll; eye is accepted as a legacy alias for pose.",
    )
    parser.add_argument("--tobii-udp", default="127.0.0.1:4243", help="UDP target for native Tobii SESP runtime mode.")
    parser.add_argument("--trackir-udp", default="127.0.0.1:4242", help="UDP target for TrackIR/OpenTrack fallback mode.")
    parser.add_argument("--stock-gaze-udp", default="", help="Optional extra UDP target for stock Tobii TCP middleware live gaze, for example 127.0.0.1:4457.")
    parser.add_argument("--gaze-smoothing", type=float, default=0.35, help="EMA alpha for gaze screen-point display.")
    parser.add_argument("--gaze-calibration-samples", type=int, default=20, help="Recent valid gaze samples averaged for each calibration point.")
    parser.add_argument("--opentrack-udp", help="Send 6DOF pose to OpenTrack UDP input, for example 127.0.0.1:4242.")
    parser.add_argument("--opentrack-yaw-sign", type=float, default=1.0)
    parser.add_argument("--opentrack-pitch-sign", type=float, default=1.0)
    parser.add_argument("--opentrack-roll-sign", type=float, default=1.0)
    parser.add_argument("--opentrack-x-sign", type=float, default=1.0)
    parser.add_argument("--opentrack-y-sign", type=float, default=1.0)
    parser.add_argument("--opentrack-z-sign", type=float, default=1.0)
    parser.add_argument("--opentrack-yaw-scale", type=float, default=1.0)
    parser.add_argument("--opentrack-pitch-scale", type=float, default=1.0)
    parser.add_argument("--opentrack-roll-scale", type=float, default=1.0)
    parser.add_argument("--opentrack-x-scale", type=float, default=1.0)
    parser.add_argument("--opentrack-y-scale", type=float, default=1.0)
    parser.add_argument("--opentrack-z-scale", type=float, default=1.0)
    parser.add_argument("--opentrack-output-smoothing", type=float, default=0.03, help="Stationary EMA alpha for values sent to TrackIR. Lower is smoother.")
    parser.add_argument("--opentrack-motion-smoothing", type=float, default=0.05, help="EMA alpha used during deliberate movement. Higher is more responsive.")
    parser.add_argument("--opentrack-prediction-ms", type=float, default=45.0, help="Output lookahead used to fill motion between sensor samples.")
    parser.add_argument("--opentrack-stillness-deadband-deg", type=float, default=0.18, help="Suppress angle jitter below this many output degrees when pose velocity is low.")
    parser.add_argument("--opentrack-stillness-velocity-dps", type=float, default=10.0, help="Velocity threshold for stationary angle jitter suppression.")
    parser.add_argument("--opentrack-send-hz", type=float, default=120.0, help="Fixed-rate UDP pose packet cadence.")
    parser.add_argument("--opentrack-max-angle-step", type=float, default=180.0, help="Emergency max degrees per 60 Hz frame sent to TrackIR.")
    parser.add_argument("--opentrack-max-output-angle", type=float, default=160.0, help="Final safety clamp for yaw/pitch/roll packets sent to the bridge.")
    parser.add_argument("--opentrack-max-translation-step", type=float, default=0.20, help="Max centimeters per UI tick sent to TrackIR.")
    parser.add_argument("--opentrack-yaw-curve", type=float, default=2.0, help="Power curve for TrackIR yaw output. 1.0 is linear.")
    parser.add_argument("--opentrack-pitch-curve", type=float, default=2.0, help="Power curve for TrackIR pitch output. 1.0 is linear.")
    parser.add_argument("--opentrack-curve-knee-deg", type=float, default=18.0, help="Output angle where the power curve roughly matches linear response.")
    args = parser.parse_args()
    load_tuning(args)
    args.frame_face_tracking = True

    root_dir = repo_root()
    if args.hq_frames:
        build_hq_frame_worker(root_dir, args.skip_build)
    if args.head_calibration_file is None:
        if args.tuning_file is not None:
            args.head_calibration_file = args.tuning_file.with_name("sc-head-calibration.json")
        else:
            args.head_calibration_file = root_dir / ".tmp" / "eye-pose-dashboard" / "head-calibration.json"
    use_ttp_mux = True
    build_mux_sampler(root_dir, args.skip_build)
    sampler = root_dir / "build" / "tobii-ttp-mux"
    if not sampler.exists():
        print(f"sampler binary missing: {sampler}")
        return 1

    if args.csv is None:
        live_dir = Path("/dev/shm/tobii-linux/eye-pose-dashboard")
        live_dir.mkdir(parents=True, exist_ok=True)
        csv_path = live_dir / "gaze.csv"
    else:
        csv_path = args.csv
        csv_path.parent.mkdir(parents=True, exist_ok=True)
    csv_path.unlink(missing_ok=True)
    log_path = csv_path.with_suffix(".log")
    log_path.unlink(missing_ok=True)
    frame_dir = csv_path.parent / "frames"
    events_path = csv_path.parent / "events.csv"
    if use_ttp_mux:
        frame_dir.mkdir(parents=True, exist_ok=True)
        for old_frame in frame_dir.glob("*.pgm"):
            old_frame.unlink(missing_ok=True)
        events_path.unlink(missing_ok=True)

    hq_worker: HQFrameWorker | None = None
    if args.hq_frames:
        hq_worker = HQFrameWorker(root_dir, args, csv_path.parent / "hq-frames")
        if hq_worker.start():
            if args.hq_mode == "prime":
                deadline = time.monotonic() + max(2.0, (args.hq_reads + 2) * (args.hq_sleep_ms / 1000.0 + args.hq_timeout_ms / 1000.0))
                while hq_worker.proc is not None and hq_worker.proc.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.05)
                if hq_worker.proc is not None and hq_worker.proc.poll() is None:
                    hq_worker.terminate()
            else:
                time.sleep(max(0.0, args.hq_start_delay_ms / 1000.0))
        else:
            print(f"HQ frame worker unavailable: {hq_worker.status.last_error}")

    mediapipe_bridge: MediaPipeFaceBridge | None = None
    if use_ttp_mux:
        mediapipe_bridge = MediaPipeFaceBridge(root_dir, args, csv_path.parent)
        if not mediapipe_bridge.start():
            print(f"MediaPipe unavailable: {mediapipe_bridge.last_error}")
            return 1
        cmd = [
            str(sampler),
            "--label",
            "eye-pose-dashboard",
            "--seconds",
            "0",
            "--startup",
            args.startup,
            "--display-area",
            args.display_area,
            "--csv",
            str(csv_path),
            "--events-csv",
            str(events_path),
            "--out",
            str(frame_dir),
            "--streams",
            args.ttp_streams,
            "--image-write-hz",
            str(args.face_frame_hz),
            "--image-ring-size",
            "16",
            "--resubscribe-after-timeouts",
            "4",
        ]
    else:
        cmd = [
            str(sampler),
            "--label",
            "eye-pose-dashboard",
            "--seconds",
            "0",
            "--startup",
            args.startup,
            "--display-area",
            args.display_area,
            "--csv",
            str(csv_path),
            "--resubscribe-after-timeouts",
            "4",
        ]
    log_file = log_path.open("w", encoding="utf-8")
    proc = subprocess.Popen(
        cmd,
        cwd=root_dir,
        stdout=subprocess.PIPE if args.frame_face_tracking else subprocess.DEVNULL,
        stderr=log_file,
        start_new_session=True,
    )
    log_file.close()
    if mediapipe_bridge is not None and proc.stdout is not None:
        mediapipe_bridge.attach_mux_stdout(proc.stdout)

    root = Tk()
    root.title("Tobii Eye Pose Dashboard")
    root.configure(background=BG)
    if args.headless:
        root.withdraw()
    elif args.fullscreen:
        root.attributes("-fullscreen", True)
    else:
        root.geometry(load_window_geometry(args))

    app = EyePoseDashboard(root, args, csv_path, proc, cmd, root_dir, log_path, mediapipe_bridge, hq_worker)
    root.after(100, app.start_loop)
    try:
        root.mainloop()
    finally:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
        if mediapipe_bridge is not None:
            mediapipe_bridge.terminate()
        if app.opentrack_bridge is not None:
            app.opentrack_bridge.close()
    print(f"csv={csv_path}")
    print(f"log={log_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
