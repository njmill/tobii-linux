"""Deterministic output filtering and numeric tracking traces (no UI or hardware)."""

from __future__ import annotations

import json
import math
from pathlib import Path

FILTER_SETTINGS = (
    "opentrack_output_smoothing", "opentrack_motion_smoothing",
    "opentrack_prediction_ms", "opentrack_stillness_deadband_deg",
    "opentrack_stillness_velocity_dps", "opentrack_max_translation_step",
    "opentrack_max_angle_step", "opentrack_max_output_angle",
)


def filter_settings(args):
    return {key: getattr(args, key) for key in FILTER_SETTINGS}


def clamp(value, low, high):
    return min(high, max(low, value))


def timed_alpha(alpha: float, dt: float, reference_hz: float) -> float:
    """Preserve a filter's response when its update frequency changes."""
    return 1.0 - (1.0 - clamp(alpha, 0.0, 1.0)) ** (max(dt, 0.0) * reference_hz)


class PacketFilter:
    def __init__(self, args, version=2):
        if version not in (1, 2):
            raise ValueError(f"unsupported output filter version: {version}")
        self.args = args
        self.version = version
        self.reset()

    def reset(self):
        self.output = None
        self.target = None
        self.velocity = [0.0] * 6
        self.sample_time = None
        self.output_time = None

    def update(self, values, sample_time: float, now: float, gaze_time=None):
        if not all(math.isfinite(v) for v in values[:6]):
            raise ValueError("non-finite pose")
        if self.output is None:
            self.output = list(values[:6])
            self.target = list(values[:6])
            self.sample_time = sample_time
            self.output_time = now
        else:
            dt = max(0.0, now - self.output_time)
            self.output_time = now
            still = clamp(self.args.opentrack_output_smoothing, 0.01, 1.0)
            motion = clamp(self.args.opentrack_motion_smoothing, still, 1.0)
            if sample_time > self.sample_time:
                sample_dt = sample_time - self.sample_time
                for i in range(6):
                    # An unchanged axis is a zero-velocity observation, even if
                    # another axis moved. Never infer velocity at the send rate.
                    measured = (values[i] - self.target[i]) / sample_dt
                    alpha = timed_alpha(clamp(motion * 0.65, 0.08, 0.65), sample_dt, 60.0)
                    self.velocity[i] += (measured - self.velocity[i]) * alpha
                    self.target[i] = values[i]
                self.sample_time = sample_time
            age = max(0.0, now - self.sample_time)
            prediction = clamp(self.args.opentrack_prediction_ms / 1000.0, 0.0, 0.160)
            # Prediction expires with the measurement, rather than drifting on
            # indefinitely while the sender repeats the last sample.
            freshness = max(0.0, 1.0 - age / 0.160)
            for i in range(6):
                if i < 3 and self.version >= 2:
                    # Position uses a short time constant, not a travel-speed
                    # governor. 0.2 cm per 60 Hz step used to cap it at 12 cm/s,
                    # adding seconds of delay after the user's depth gain.
                    # Do not extrapolate position across an eye-tracking hold.
                    alpha = -math.expm1(-dt / 0.080)
                    self.output[i] += (self.target[i] - self.output[i]) * alpha
                    continue
                predicted = self.target[i] + self.velocity[i] * min(age + prediction, 0.140) * freshness
                error = predicted - self.output[i]
                score = clamp(max(abs(error) / (0.35 if i < 3 else 2.0), abs(self.velocity[i]) / (8.0 if i < 3 else 75.0)), 0.0, 1.0)
                alpha = still + (motion - still) * score ** 0.55
                deadband = 0.02 if i < 3 else max(self.args.opentrack_stillness_deadband_deg, 0.03)
                velocity_limit = 1.0 if i < 3 else max(self.args.opentrack_stillness_velocity_dps, 0.1)
                damping = 1.0
                if abs(error) < deadband and abs(self.velocity[i]) <= velocity_limit:
                    damping = clamp(abs(error) / deadband, 0.18, 1.0)
                step = max(self.args.opentrack_max_translation_step, 0.05) if i < 3 else max(self.args.opentrack_max_angle_step, 0.1)
                self.output[i] += clamp(error * timed_alpha(alpha * damping, dt, 120.0), -step * dt * 60.0, step * dt * 60.0)
        limit = abs(self.args.opentrack_max_output_angle)
        if limit > 0:
            for i in range(3, 6):
                self.output[i] = clamp(self.output[i], -limit, limit)
        tail = list(values[6:])
        if gaze_time is None or not 0.0 <= now - gaze_time <= 0.250:
            tail[2:5] = [0.5, 0.5, 0.0]
        return (*self.output, *tail)


class TrackingTrace:
    """Opt-in JSONL: numeric estimates, mapping inputs, and emitted packets."""

    def __init__(self, path: Path, args):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.file = path.open("w", encoding="utf-8")
        self.last_flush = 0.0
        settings = {key: value for key, value in vars(args).items()
                    if key.startswith(("opentrack_", "mediapipe_")) and isinstance(value, (str, float, int, bool))}
        self.write({"kind": "header", "version": 1, "filter_version": 2, "settings": settings})

    def write(self, row):
        self.file.write(json.dumps(row, allow_nan=False, separators=(",", ":")) + "\n")
        now = row.get("time", 0.0)
        if now - self.last_flush >= 1.0:
            self.file.flush()
            self.last_flush = now

    def close(self):
        self.file.close()
