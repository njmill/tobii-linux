#!/usr/bin/env python3
"""Live in-band 0x050e camera dashboard with Tobii eye-origin and 3DDFA landmarks."""

from __future__ import annotations

import argparse
import csv
import json
import math
import shutil
import signal
import statistics
import subprocess
import os
import time
import tkinter as tk
from collections import deque
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class FrameEvent:
    path: Path
    elapsed_ms: float
    index: int


@dataclass(frozen=True)
class GazeRow:
    elapsed_ms: float
    gaze_valid: bool
    gaze_x: float | None
    gaze_y: float | None
    left_present: bool
    right_present: bool
    lx: float
    ly: float
    lz: float
    rx: float
    ry: float
    rz: float

    @property
    def binocular(self) -> bool:
        return self.left_present and self.right_present and 20.0 <= self.ipd_mm <= 90.0

    @property
    def cx_mm(self) -> float:
        return (self.lx + self.rx) * 0.5

    @property
    def cy_mm(self) -> float:
        return (self.ly + self.ry) * 0.5

    @property
    def cz_mm(self) -> float:
        return (self.lz + self.rz) * 0.5

    @property
    def ipd_mm(self) -> float:
        return math.sqrt((self.rx - self.lx) ** 2 + (self.ry - self.ly) ** 2 + (self.rz - self.lz) ** 2)


class CsvTail:
    def __init__(self, path: Path):
        self.path = path
        self.header: list[str] | None = None
        self.pos = 0
        self.remainder = ""

    def rows(self) -> list[dict[str, str]]:
        if not self.path.exists():
            return []
        out: list[dict[str, str]] = []
        with self.path.open("r", newline="") as f:
            f.seek(self.pos)
            text = f.read()
            self.pos = f.tell()
        if not text:
            return []
        text = self.remainder + text
        if not text.endswith("\n"):
            text, self.remainder = text.rsplit("\n", 1) if "\n" in text else ("", text)
        else:
            self.remainder = ""
        for line in text.splitlines():
            if not line.strip():
                continue
            cells = next(csv.reader([line]))
            if self.header is None:
                self.header = cells
                continue
            if len(cells) < len(self.header):
                continue
            out.append(dict(zip(self.header, cells)))
        return out


def parse_float(row: dict[str, str], key: str) -> float:
    return float(row.get(key) or "nan")


def parse_gaze(row: dict[str, str]) -> GazeRow | None:
    try:
        return GazeRow(
            elapsed_ms=parse_float(row, "elapsed_ms"),
            gaze_valid=row.get("gaze_valid") == "1",
            gaze_x=parse_float(row, "gaze_x_norm") if row.get("gaze_x_norm") else None,
            gaze_y=parse_float(row, "gaze_y_norm") if row.get("gaze_y_norm") else None,
            left_present=row.get("eye_present_l") == "1",
            right_present=row.get("eye_present_r") == "1",
            lx=parse_float(row, "eye_origin_l_x_mm"),
            ly=parse_float(row, "eye_origin_l_y_mm"),
            lz=parse_float(row, "eye_origin_l_z_mm"),
            rx=parse_float(row, "eye_origin_r_x_mm"),
            ry=parse_float(row, "eye_origin_r_y_mm"),
            rz=parse_float(row, "eye_origin_r_z_mm"),
        )
    except (TypeError, ValueError):
        return None


def parse_frame(row: dict[str, str], session: Path) -> FrameEvent | None:
    if row.get("kind") != "image" or row.get("stream") != "0x050e":
        return None
    path_text = row.get("path") or ""
    if not path_text:
        return None
    path = Path(path_text)
    if not path.exists():
        path = session / "frames" / path.name
    if not path.exists():
        return None
    try:
        return FrameEvent(path=path, elapsed_ms=parse_float(row, "elapsed_ms"), index=int(row.get("index") or "0"))
    except (TypeError, ValueError):
        return None


def read_pgm(path: Path) -> tuple[int, int, list[int]]:
    with path.open("rb") as f:
        if f.readline().strip() != b"P5":
            raise ValueError(f"not P5 PGM: {path}")
        line = f.readline()
        while line.startswith(b"#"):
            line = f.readline()
        width, height = [int(part) for part in line.split()]
        max_value = int(f.readline().strip())
        data = list(f.read(width * height))
    if max_value != 255 or len(data) != width * height:
        raise ValueError(f"unsupported or short PGM: {path}")
    return width, height, data


def contrast_rgb(gray: list[int]) -> list[int]:
    sorted_gray = sorted(gray)
    lo = sorted_gray[max(0, int(len(sorted_gray) * 0.01) - 1)]
    hi = sorted_gray[min(len(sorted_gray) - 1, int(len(sorted_gray) * 0.995))]
    if hi <= lo:
        hi = lo + 1
    rgb: list[int] = []
    for value in gray:
        v = int(max(0, min(255, (value - lo) * 255 / (hi - lo))))
        rgb.extend([v, v, v])
    return rgb


def rgb_to_ppm(width: int, height: int, rgb: list[int]) -> bytes:
    return f"P6\n{width} {height}\n255\n".encode("ascii") + bytes(rgb)


def parse_roi(value: str) -> tuple[float, float, float, float]:
    parts = [float(part) for part in value.split(",")]
    if len(parts) != 4 or parts[2] <= 0 or parts[3] <= 0:
        raise ValueError("expected roi x,y,w,h")
    return parts[0], parts[1], parts[2], parts[3]


def parse_point(value: str) -> tuple[float, float]:
    parts = [float(part) for part in value.split(",")]
    if len(parts) != 2:
        raise ValueError("expected point x,y")
    return parts[0], parts[1]


def nearest_gaze(target_ms: float, rows: deque[GazeRow]) -> GazeRow | None:
    if not rows:
        return None
    return min(rows, key=lambda row: abs(row.elapsed_ms - target_ms))


def landmark_eye_center(landmarks: list[tuple[int, float, float, float]]) -> tuple[float, float] | None:
    eyes = [(x, y) for index, x, y, _z in landmarks if 36 <= index <= 47]
    if not eyes:
        return None
    return statistics.fmean(x for x, _y in eyes), statistics.fmean(y for _x, y in eyes)


def landmark_bounds(landmarks: list[tuple[int, float, float, float]]) -> tuple[float, float, float, float] | None:
    if not landmarks:
        return None
    xs = [x for _index, x, _y, _z in landmarks]
    ys = [y for _index, _x, y, _z in landmarks]
    return min(xs), min(ys), max(xs), max(ys)


def roi_from_eye_pixel(
    base_roi: tuple[float, float, float, float],
    base_eye_px: tuple[float, float],
    eye_px: tuple[float, float],
    scale: float,
    min_size: float,
    max_size: float,
    max_above_eye_px: float,
) -> tuple[str, tuple[float, float, float, float]]:
    x, y, w, h = base_roi
    base_eye_x, base_eye_y = base_eye_px
    scale = max(0.65, min(1.45, scale))
    out_w = max(min_size, min(max_size, w * scale))
    out_h = max(min_size, min(max_size, h * scale))
    roi_eye_dx = (base_eye_x - x) * scale
    roi_eye_dy = (base_eye_y - y) * scale
    out_x = eye_px[0] - roi_eye_dx
    out_y = eye_px[1] - roi_eye_dy
    out_y = max(out_y, eye_px[1] - max_above_eye_px)
    out_x = max(-out_w * 0.35, min(280.0 - out_w * 0.65, out_x))
    out_y = max(-out_h * 0.35, min(280.0 - out_h * 0.65, out_y))
    roi = (out_x, out_y, out_w, out_h)
    return f"{out_x:.1f},{out_y:.1f},{out_w:.1f},{out_h:.1f}", roi


def landmark_eye_quality(
    landmarks: list[tuple[int, float, float, float]],
    roi: tuple[float, float, float, float],
) -> tuple[bool, str]:
    eye = landmark_eye_center(landmarks)
    bounds = landmark_bounds(landmarks)
    if eye is None:
        return False, "no 3DDFA eye center"
    if bounds is None:
        return False, "no landmarks"
    min_x, min_y, max_x, max_y = bounds
    width = max_x - min_x
    height = max_y - min_y
    if width < 18.0 or height < 35.0:
        return False, f"tiny landmarks {width:.0f}x{height:.0f}"
    if width > 175.0 or height > 210.0:
        return False, f"huge landmarks {width:.0f}x{height:.0f}"
    if not (0.0 <= eye[0] <= 280.0 and 0.0 <= eye[1] <= 280.0):
        return False, "eye off frame"
    x, y, w, h = roi
    margin = 32.0
    if not (x - margin <= eye[0] <= x + w + margin and y - margin <= eye[1] <= y + h + margin):
        return False, "eye outside ROI"
    return True, f"ok {width:.0f}x{height:.0f}"


def median_gaze(rows: deque[GazeRow]) -> GazeRow | None:
    candidates = [row for row in rows if row.binocular and row.cz_mm > 1.0]
    if not candidates:
        return None
    return GazeRow(
        elapsed_ms=0.0,
        gaze_valid=False,
        gaze_x=None,
        gaze_y=None,
        left_present=True,
        right_present=True,
        lx=statistics.median([row.lx for row in candidates]),
        ly=statistics.median([row.ly for row in candidates]),
        lz=statistics.median([row.lz for row in candidates]),
        rx=statistics.median([row.rx for row in candidates]),
        ry=statistics.median([row.ry for row in candidates]),
        rz=statistics.median([row.rz for row in candidates]),
    )


def eye_anchor_point(row: GazeRow, min_z: float = 150.0, max_z: float = 1300.0) -> tuple[float, float, float, str] | None:
    """Return (x_mm, y_mm, z_mm, mode) preferring binocular center, else one present eye.

    Falls back to a single valid eye when the other is hidden (e.g. pitching up),
    so the crop stays leashed to the real eye instead of chasing 3DDFA landmarks.
    """
    if row.binocular and row.cz_mm > 1.0:
        return (row.cx_mm, row.cy_mm, row.cz_mm, "bino")
    left_ok = row.left_present and min_z <= row.lz <= max_z
    right_ok = row.right_present and min_z <= row.rz <= max_z
    if left_ok and not right_ok:
        return (row.lx, row.ly, row.lz, "mono-l")
    if right_ok and not left_ok:
        return (row.rx, row.ry, row.rz, "mono-r")
    if left_ok and right_ok and row.cz_mm > 1.0:
        return (row.cx_mm, row.cy_mm, row.cz_mm, "bino")
    return None


def neutral_anchor_point(neutral: GazeRow, mode: str) -> tuple[float, float, float]:
    if mode == "mono-l":
        return (neutral.lx, neutral.ly, neutral.lz)
    if mode == "mono-r":
        return (neutral.rx, neutral.ry, neutral.rz)
    return (neutral.cx_mm, neutral.cy_mm, neutral.cz_mm)


def roi_for_frame(
    frame: FrameEvent,
    gaze_rows: deque[GazeRow],
    neutral: GazeRow | None,
    base_roi: tuple[float, float, float, float],
    base_eye_px: tuple[float, float],
    eye_bias_px: tuple[float, float],
    focal_px: float,
    x_sign: float,
    y_sign: float,
    min_size: float,
    max_size: float,
    max_above_eye_px: float,
) -> tuple[str, tuple[float, float, float, float], GazeRow | None, str]:
    x, y, w, h = base_roi
    anchor = nearest_gaze(frame.elapsed_ms, gaze_rows)
    anchor_point = eye_anchor_point(anchor) if anchor is not None else None
    if neutral is None or anchor is None or anchor_point is None:
        return f"{x:.1f},{y:.1f},{w:.1f},{h:.1f}", base_roi, anchor, "fixed"
    ax, ay, az, mode = anchor_point
    nx, ny, nz = neutral_anchor_point(neutral, mode)
    base_eye_x, base_eye_y = base_eye_px
    roi_eye_dx = base_eye_x - x
    roi_eye_dy = base_eye_y - y
    dx = ax - nx
    dy = ay - ny
    scale = max(0.65, min(1.45, nz / az)) if az > 1.0 else 1.0
    out_w = max(min_size, min(max_size, w * scale))
    out_h = max(min_size, min(max_size, h * scale))
    eye_x = base_eye_x + x_sign * focal_px * dx / az + eye_bias_px[0]
    eye_y = base_eye_y + y_sign * focal_px * dy / az + eye_bias_px[1]
    out_x = eye_x - roi_eye_dx * scale
    out_y = eye_y - roi_eye_dy * scale
    out_y = max(out_y, eye_y - max_above_eye_px)
    out_x = max(-out_w * 0.35, min(280.0 - out_w * 0.65, out_x))
    out_y = max(-out_h * 0.35, min(280.0 - out_h * 0.65, out_y))
    roi = (out_x, out_y, out_w, out_h)
    label = "eye-origin" if mode == "bino" else "eye-mono"
    return f"{out_x:.1f},{out_y:.1f},{out_w:.1f},{out_h:.1f}", roi, anchor, label



def load_landmarks(path: Path) -> list[tuple[int, float, float, float]]:
    points: list[tuple[int, float, float, float]] = []
    with path.open(newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            points.append((int(row["index"]), float(row["x"]), float(row["y"]), float(row["z"])))
    return points


def mediapipe_eye_center(landmarks: list[tuple[int, float, float, float]]) -> tuple[float, float] | None:
    by_index = {index: (x, y) for index, x, y, _z in landmarks}
    indices = [33, 133, 159, 145, 263, 362, 386, 374, 468, 469, 470, 471, 472, 473, 474, 475, 476, 477]
    points = [by_index[index] for index in indices if index in by_index]
    if not points:
        return None
    return statistics.fmean(x for x, _y in points), statistics.fmean(y for _x, y in points)


class MediaPipeWorkerClient:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.proc: subprocess.Popen[str] | None = None

    def start(self) -> None:
        if self.proc is not None:
            return
        cmd = [
            str(self.args.mediapipe_python),
            str(ROOT / "scripts" / "recon" / "mediapipe-face-worker.py"),
            "--model",
            str(self.args.mediapipe_model),
            "--preprocess",
            self.args.mediapipe_preprocess,
            "--upscale",
            str(self.args.mediapipe_upscale),
            "--min-detection-confidence",
            str(self.args.mediapipe_min_detection_confidence),
            "--min-presence-confidence",
            str(self.args.mediapipe_min_presence_confidence),
            "--min-tracking-confidence",
            str(self.args.mediapipe_min_tracking_confidence),
        ]
        self.proc = subprocess.Popen(
            cmd,
            cwd=ROOT,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )

    def process(self, frame: FrameEvent) -> dict:
        self.start()
        assert self.proc is not None
        if self.proc.poll() is not None:
            stderr = self.proc.stderr.read() if self.proc.stderr else ""
            raise RuntimeError(f"mediapipe worker exited rc={self.proc.returncode}: {stderr.strip()[-240:]}")
        assert self.proc.stdin is not None
        assert self.proc.stdout is not None
        self.proc.stdin.write(f"{frame.path}\t{int(frame.elapsed_ms)}\n")
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
            if self.proc.stdin:
                self.proc.stdin.close()
            try:
                self.proc.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
                    self.proc.wait(timeout=1.0)
        self.proc = None


class OneEuroFilter:
    """One-Euro filter: low jitter at rest, low lag during motion.

    See Casiez et al. 2012. Cutoffs are in Hz; timestamps in seconds.
    """

    def __init__(self, min_cutoff: float = 1.0, beta: float = 0.0, d_cutoff: float = 1.0):
        self.min_cutoff = float(min_cutoff)
        self.beta = float(beta)
        self.d_cutoff = float(d_cutoff)
        self._x_prev: float | None = None
        self._dx_prev = 0.0
        self._t_prev: float | None = None

    @staticmethod
    def _alpha(cutoff: float, dt: float) -> float:
        tau = 1.0 / (2.0 * math.pi * max(1e-6, cutoff))
        return 1.0 / (1.0 + tau / dt)

    def filter(self, x: float, t: float) -> float:
        if self._x_prev is None or self._t_prev is None:
            self._x_prev = x
            self._t_prev = t
            self._dx_prev = 0.0
            return x
        dt = t - self._t_prev
        if dt <= 0.0:
            dt = 1e-3
        self._t_prev = t
        dx = (x - self._x_prev) / dt
        a_d = self._alpha(self.d_cutoff, dt)
        dx_hat = a_d * dx + (1.0 - a_d) * self._dx_prev
        cutoff = self.min_cutoff + self.beta * abs(dx_hat)
        a = self._alpha(cutoff, dt)
        x_hat = a * x + (1.0 - a) * self._x_prev
        self._x_prev = x_hat
        self._dx_prev = dx_hat
        return x_hat

    def reset(self) -> None:
        self._x_prev = None
        self._dx_prev = 0.0
        self._t_prev = None


class Dashboard:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.session = args.session or Path("/dev/shm/tobii-linux/inband-landmark-dashboard")
        self.frames_dir = self.session / "frames"
        self.landmarks_dir = self.session / "landmarks"
        self.gaze_csv = self.session / "gaze.csv"
        self.events_csv = self.session / "events.csv"
        self.mux_log = self.session / "mux.log"
        self.mux: subprocess.Popen[str] | None = None
        self.mux_log_handle = None
        self.gaze_tail = CsvTail(self.gaze_csv)
        self.events_tail = CsvTail(self.events_csv)
        self.gaze_rows: deque[GazeRow] = deque(maxlen=900)
        self.latest_frame: FrameEvent | None = None
        self.last_processed_path: Path | None = None
        self.last_landmark_time = 0.0
        self.landmarks: list[tuple[int, float, float, float]] = []
        self.pose_text = "landmarks waiting"
        self.roi_text = args.roi
        self.roi_tuple = parse_roi(args.roi)
        self.roi_mode = "fixed"
        self.anchor: GazeRow | None = None
        self.neutral: GazeRow | None = None
        self.eye_bias_px = [0.0, 0.0]
        self.eye_bias_samples = 0
        self.raw_projected_eye_px: tuple[float, float] | None = None
        self.projected_eye_px: tuple[float, float] | None = None
        self.landmark_eye_px: tuple[float, float] | None = None
        self.tracked_eye_px: tuple[float, float] | None = None
        self.tracked_eye_time = 0.0
        self.tracked_eye_quality = "none"
        self.mediapipe_worker: MediaPipeWorkerClient | None = None
        self.mediapipe_result: dict | None = None
        self.mediapipe_status = "not started"
        self.roi_scale = 1.0
        self.yaw_filter = OneEuroFilter(args.pose_min_cutoff, args.pose_beta, args.pose_d_cutoff)
        self.pitch_filter = OneEuroFilter(args.pose_min_cutoff, args.pose_beta, args.pose_d_cutoff)
        self.roll_filter = OneEuroFilter(args.pose_min_cutoff, args.pose_beta, args.pose_d_cutoff)
        self.roi_cx_filter = OneEuroFilter(args.roi_smooth_min_cutoff, args.roi_smooth_beta)
        self.roi_cy_filter = OneEuroFilter(args.roi_smooth_min_cutoff, args.roi_smooth_beta)
        self.roi_w_filter = OneEuroFilter(args.roi_smooth_min_cutoff, args.roi_smooth_beta)
        self.roi_h_filter = OneEuroFilter(args.roi_smooth_min_cutoff, args.roi_smooth_beta)
        self.landmark_filters: dict[int, tuple[OneEuroFilter, OneEuroFilter]] = {}
        self.pose = (0.0, 0.0, 0.0)
        self.pose_raw = (0.0, 0.0, 0.0)
        self.pose_raw_history: deque[tuple[float, float, float]] = deque(maxlen=30)
        self.last_anchored_roi: tuple[float, float, float, float] | None = None
        self.last_anchored_time = 0.0
        self.pose_accepted_raw: tuple[float, float, float] | None = None
        self.pose_accepted_time = 0.0
        self.pose_at_hold_start: tuple[float, float, float] | None = None
        self.pose_held_reason = ""
        self.frame_count = 0
        self.gaze_count = 0
        self.last_frame_wall = 0.0
        self.photo: tk.PhotoImage | None = None

        self.root = tk.Tk()
        self.root.title("Tobii In-Band Landmark Dashboard")
        self.root.configure(bg="#090d12")
        self.root.protocol("WM_DELETE_WINDOW", self.close)

        self.canvas_scale = max(1, args.scale)
        canvas_size = int(280 * self.canvas_scale)
        self.canvas = tk.Canvas(self.root, width=canvas_size, height=canvas_size, bg="#070a0f", highlightthickness=0)
        self.canvas.grid(row=0, column=0, padx=18, pady=18, sticky="nsew")
        side = tk.Frame(self.root, bg="#0d131a")
        side.grid(row=0, column=1, padx=(0, 18), pady=18, sticky="nsew")
        self.status = tk.Label(side, text="", justify="left", anchor="nw", bg="#0d131a", fg="#d7e0ea", font=("DejaVu Sans Mono", 11))
        self.status.pack(fill="both", expand=True, padx=16, pady=16)
        self.root.grid_columnconfigure(0, weight=1)
        self.root.grid_rowconfigure(0, weight=1)

    def start_mux(self) -> None:
        if self.args.no_start_mux:
            return
        self.cleanup_stale_mux()
        if self.session.exists():
            shutil.rmtree(self.session)
        self.frames_dir.mkdir(parents=True, exist_ok=True)
        self.landmarks_dir.mkdir(parents=True, exist_ok=True)
        cmd = [
            str(ROOT / "build" / "tobii-ttp-mux"),
            "--startup",
            self.args.startup,
            "--display-area",
            self.args.display_area,
            "--label",
            "inband-landmark-dashboard",
            "--seconds",
            str(self.args.seconds),
            "--csv",
            str(self.gaze_csv),
            "--events-csv",
            str(self.events_csv),
            "--out",
            str(self.frames_dir),
            "--streams",
            "050e,1771",
            "--image-write-hz",
            str(self.args.image_write_hz),
            "--image-ring-size",
            str(self.args.image_ring_size),
        ]
        log = self.mux_log.open("w")
        self.mux_log_handle = log
        self.mux = subprocess.Popen(cmd, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, text=True)

    def cleanup_stale_mux(self) -> None:
        current_pid = os.getpid()
        try:
            proc = subprocess.run(["pgrep", "-af", "tobii-ttp-mux"], text=True, capture_output=True, check=False)
        except FileNotFoundError:
            return
        for line in proc.stdout.splitlines():
            parts = line.split(maxsplit=1)
            if len(parts) != 2:
                continue
            try:
                pid = int(parts[0])
            except ValueError:
                continue
            cmdline = parts[1]
            if pid == current_pid:
                continue
            if "inband-landmark-dashboard" not in cmdline:
                continue
            try:
                os.kill(pid, signal.SIGINT)
            except ProcessLookupError:
                continue
        time.sleep(0.15)

    def update_csv(self) -> None:
        for row in self.gaze_tail.rows():
            gaze = parse_gaze(row)
            if gaze is None:
                continue
            self.gaze_rows.append(gaze)
            self.gaze_count += 1
        if self.neutral is None and len([row for row in self.gaze_rows if row.binocular]) >= 30:
            self.neutral = median_gaze(self.gaze_rows)
        for row in self.events_tail.rows():
            frame = parse_frame(row, self.session)
            if frame is None:
                continue
            self.latest_frame = frame
            self.frame_count += 1
            self.last_frame_wall = time.monotonic()

    def run_landmarks(self) -> None:
        frame = self.latest_frame
        if frame is None or frame.path == self.last_processed_path:
            return
        now = time.monotonic()
        if now - self.last_landmark_time < 1.0 / max(0.1, self.args.landmark_hz):
            return
        self.last_landmark_time = now
        self.last_processed_path = frame.path
        if self.args.engine == "mediapipe":
            self.run_mediapipe(frame, now)
            return
        roi_text, roi_tuple, anchor, roi_mode = roi_for_frame(
            frame,
            self.gaze_rows,
            self.neutral,
            parse_roi(self.args.roi),
            parse_point(self.args.eye_anchor_px),
            (self.eye_bias_px[0], self.eye_bias_px[1]),
            self.args.eye_origin_focal_px,
            self.args.eye_origin_x_sign,
            self.args.eye_origin_y_sign,
            self.args.roi_min_size,
            self.args.roi_max_size,
            self.args.roi_max_above_eye_px,
        )
        if self.args.roi_source == "fixed":
            roi_tuple = parse_roi(self.args.roi)
            roi_text = self.args.roi
            roi_mode = "fixed"
        self.roi_text = roi_text
        self.roi_tuple = roi_tuple
        self.anchor = anchor
        self.roi_mode = roi_mode
        anchored = roi_mode in ("eye-origin", "eye-mono")
        if anchored:
            base_w = parse_roi(self.args.roi)[2]
            if base_w > 0:
                self.roi_scale = max(0.65, min(1.45, roi_tuple[2] / base_w))
            self.last_anchored_roi = roi_tuple
            self.last_anchored_time = now
        elif self.args.roi_source == "landmark-primary":
            # Eye-origin anchor lost (e.g. both eyes hidden). Prefer freezing the
            # last good eye-anchored crop over chasing 3DDFA landmarks, which
            # drift into the background/curtains when the face is not found.
            frozen = (
                self.last_anchored_roi is not None
                and (now - self.last_anchored_time) <= self.args.roi_freeze_hold_s
            )
            if frozen:
                roi_tuple = self.last_anchored_roi
                roi_text = f"{roi_tuple[0]:.1f},{roi_tuple[1]:.1f},{roi_tuple[2]:.1f},{roi_tuple[3]:.1f}"
                self.roi_text = roi_text
                self.roi_tuple = roi_tuple
                self.roi_mode = "anchor-hold"
            elif self.tracked_eye_px is not None and (now - self.tracked_eye_time) <= self.args.landmark_eye_hold_s:
                roi_text, roi_tuple = roi_from_eye_pixel(
                    parse_roi(self.args.roi),
                    parse_point(self.args.eye_anchor_px),
                    self.tracked_eye_px,
                    self.roi_scale,
                    self.args.roi_min_size,
                    self.args.roi_max_size,
                    self.args.roi_max_above_eye_px,
                )
                self.roi_text = roi_text
                self.roi_tuple = roi_tuple
                self.roi_mode = "landmark"
        roi_tuple = self.stabilize_roi(roi_tuple, now)
        roi_text = f"{roi_tuple[0]:.1f},{roi_tuple[1]:.1f},{roi_tuple[2]:.1f},{roi_tuple[3]:.1f}"
        self.roi_text = roi_text
        self.roi_tuple = roi_tuple
        landmarks_path = self.landmarks_dir / f"landmarks-{frame.index:06d}.csv"
        cmd = [
            str(self.args.native_bin),
            "--model",
            str(self.args.model),
            "--stats",
            str(self.args.stats),
            "--frame",
            str(frame.path),
            "--roi",
            roi_text,
            "--bfm-sparse",
            str(self.args.bfm_sparse),
            "--landmarks-out",
            str(landmarks_path),
            "--preprocess",
            self.args.preprocess,
            "--threads",
            str(self.args.threads),
        ]
        proc = subprocess.run(cmd, cwd=ROOT, text=True, capture_output=True, check=False)
        if proc.returncode != 0:
            self.pose_text = f"3DDFA rc={proc.returncode} {proc.stderr.strip()[:120]}"
            self.landmarks = []
            return
        try:
            self.landmarks = self.smooth_landmarks(load_landmarks(landmarks_path), now)
            self.update_eye_bias()
            self.update_landmark_track(now)
            # Avoid importing json in the hot path unless the native tool succeeded.
            import json

            result = json.loads(proc.stdout)
            raw = (
                float(result.get("yaw", 0.0)),
                float(result.get("pitch", 0.0)),
                float(result.get("roll", 0.0)),
            )
            self.pose_raw = raw
            self.pose_raw_history.append(raw)
            accept, reason = self.evaluate_pose(raw, now)
            if accept:
                if self.pose_at_hold_start is not None:
                    # Re-acquiring after a hold: clear stale filter state for a clean lock.
                    self.yaw_filter.reset()
                    self.pitch_filter.reset()
                    self.roll_filter.reset()
                    self.pose_at_hold_start = None
                self.pose = (
                    self.yaw_filter.filter(raw[0], now),
                    self.pitch_filter.filter(raw[1], now),
                    self.roll_filter.filter(raw[2], now),
                )
                self.pose_accepted_raw = raw
                self.pose_accepted_time = now
                self.pose_held_reason = ""
                self.pose_text = f"yaw {self.pose[0]:+.1f} pitch {self.pose[1]:+.1f} roll {self.pose[2]:+.1f}"
            else:
                self.apply_pose_hold(now)
                self.pose_held_reason = reason
                self.pose_text = (
                    f"HELD ({reason}) yaw {self.pose[0]:+.1f} pitch {self.pose[1]:+.1f} roll {self.pose[2]:+.1f}"
                )
        except Exception as exc:  # noqa: BLE001 - live diagnostics.
            self.pose_text = f"parse error {exc}"
            self.landmarks = []

    def run_mediapipe(self, frame: FrameEvent, now: float) -> None:
        self.anchor = nearest_gaze(frame.elapsed_ms, self.gaze_rows)
        self.roi_tuple = (0.0, 0.0, 280.0, 280.0)
        self.roi_text = "0.0,0.0,280.0,280.0"
        self.roi_mode = "mediapipe"
        if self.mediapipe_worker is None:
            self.mediapipe_worker = MediaPipeWorkerClient(self.args)
        try:
            result = self.mediapipe_worker.process(frame)
        except Exception as exc:  # noqa: BLE001 - live diagnostics.
            self.mediapipe_status = f"worker error {exc}"
            self.pose_text = self.mediapipe_status[:140]
            self.landmarks = []
            self.apply_pose_hold(now)
            return
        self.mediapipe_result = result
        if not result.get("present"):
            self.mediapipe_status = result.get("error") or "no face"
            self.landmarks = []
            self.landmark_eye_px = None
            self.apply_pose_hold(now)
            self.pose_text = f"HELD ({self.mediapipe_status}) yaw {self.pose[0]:+.1f} pitch {self.pose[1]:+.1f} roll {self.pose[2]:+.1f}"
            return
        self.mediapipe_status = f"present latency={float(result.get('latency_ms') or 0.0):.1f}ms"
        landmarks = [
            (int(item["index"]), float(item["x"]), float(item["y"]), float(item["z"]))
            for item in (result.get("landmarks") or [])
        ]
        self.landmarks = self.smooth_landmarks(landmarks, now)
        self.landmark_eye_px = mediapipe_eye_center(self.landmarks)
        bounds = landmark_bounds(self.landmarks)
        if bounds is not None:
            min_x, min_y, max_x, max_y = bounds
            margin = 18.0
            x = max(0.0, min_x - margin)
            y = max(0.0, min_y - margin)
            w = min(280.0 - x, max_x - min_x + margin * 2.0)
            h = min(280.0 - y, max_y - min_y + margin * 2.0)
            self.roi_tuple = (x, y, w, h)
            self.roi_text = f"{x:.1f},{y:.1f},{w:.1f},{h:.1f}"
        raw = (
            float(result.get("yaw") or 0.0),
            float(result.get("pitch") or 0.0),
            float(result.get("roll") or 0.0),
        )
        self.pose_raw = raw
        self.pose_raw_history.append(raw)
        if self.pose_at_hold_start is not None:
            self.yaw_filter.reset()
            self.pitch_filter.reset()
            self.roll_filter.reset()
            self.pose_at_hold_start = None
        self.pose = (
            self.yaw_filter.filter(raw[0], now),
            self.pitch_filter.filter(raw[1], now),
            self.roll_filter.filter(raw[2], now),
        )
        self.pose_accepted_raw = raw
        self.pose_accepted_time = now
        self.pose_held_reason = ""
        self.pose_text = f"mp yaw {self.pose[0]:+.1f} pitch {self.pose[1]:+.1f} roll {self.pose[2]:+.1f}"

    def update_landmark_track(self, now: float) -> None:
        detected_eye = landmark_eye_center(self.landmarks)
        self.landmark_eye_px = detected_eye
        ok, reason = landmark_eye_quality(self.landmarks, self.roi_tuple)
        if not ok or detected_eye is None:
            self.tracked_eye_quality = reason
            return
        raw_eye = self.raw_projected_eye()
        if raw_eye is not None:
            anchor_distance = math.hypot(detected_eye[0] - raw_eye[0], detected_eye[1] - raw_eye[1])
            if anchor_distance > self.args.landmark_eye_anchor_max_error_px:
                self.tracked_eye_px = raw_eye
                self.tracked_eye_time = now
                self.tracked_eye_quality = f"reject anchor {anchor_distance:.0f}px"
                return
            # While Tobii eye-origin is healthy, keep the tracker leashed to the
            # Tobii projection.  3DDFA can refine diagnostics, but it should not
            # steer the next crop away from the device-provided eye anchor.
            alpha = min(self.args.landmark_eye_alpha, self.args.eye_anchor_alpha)
            target_x = raw_eye[0] + max(
                -self.args.landmark_eye_anchor_refine_px,
                min(self.args.landmark_eye_anchor_refine_px, detected_eye[0] - raw_eye[0]),
            )
            target_y = raw_eye[1] + max(
                -self.args.landmark_eye_anchor_refine_px,
                min(self.args.landmark_eye_anchor_refine_px, detected_eye[1] - raw_eye[1]),
            )
            if self.tracked_eye_px is None:
                self.tracked_eye_px = (target_x, target_y)
            else:
                self.tracked_eye_px = (
                    (1.0 - alpha) * self.tracked_eye_px[0] + alpha * target_x,
                    (1.0 - alpha) * self.tracked_eye_px[1] + alpha * target_y,
                )
            self.tracked_eye_time = now
            self.tracked_eye_quality = f"eye-origin leash {anchor_distance:.0f}px"
            return
        if self.tracked_eye_px is None:
            self.tracked_eye_px = detected_eye
            self.tracked_eye_time = now
            self.tracked_eye_quality = reason
            return
        distance = math.hypot(detected_eye[0] - self.tracked_eye_px[0], detected_eye[1] - self.tracked_eye_px[1])
        if distance > self.args.landmark_eye_max_step_px:
            self.tracked_eye_quality = f"reject jump {distance:.0f}px"
            return
        alpha = self.args.landmark_eye_alpha
        self.tracked_eye_px = (
            (1.0 - alpha) * self.tracked_eye_px[0] + alpha * detected_eye[0],
            (1.0 - alpha) * self.tracked_eye_px[1] + alpha * detected_eye[1],
        )
        self.tracked_eye_time = now
        self.tracked_eye_quality = reason

    def raw_projected_eye(self) -> tuple[float, float] | None:
        if not self.anchor or not self.neutral:
            return None
        ap = eye_anchor_point(self.anchor)
        if ap is None:
            return None
        ax, ay, az, mode = ap
        if not math.isfinite(az) or az <= 1.0:
            return None
        nx, ny, _nz = neutral_anchor_point(self.neutral, mode)
        base_eye_x, base_eye_y = parse_point(self.args.eye_anchor_px)
        dx = ax - nx
        dy = ay - ny
        return (
            base_eye_x + self.args.eye_origin_x_sign * self.args.eye_origin_focal_px * dx / az,
            base_eye_y + self.args.eye_origin_y_sign * self.args.eye_origin_focal_px * dy / az,
        )

    def update_eye_bias(self) -> None:
        raw_eye = self.raw_projected_eye()
        detected_eye = landmark_eye_center(self.landmarks)
        self.raw_projected_eye_px = raw_eye
        self.landmark_eye_px = detected_eye
        if raw_eye is None or detected_eye is None:
            self.projected_eye_px = None
            return
        error_x = detected_eye[0] - raw_eye[0]
        error_y = detected_eye[1] - raw_eye[1]
        if self.args.eye_bias_alpha <= 0.0:
            self.projected_eye_px = raw_eye
            return
        if abs(error_x) > self.args.eye_bias_max_error_px or abs(error_y) > self.args.eye_bias_max_error_px:
            self.projected_eye_px = (raw_eye[0] + self.eye_bias_px[0], raw_eye[1] + self.eye_bias_px[1])
            return
        alpha = self.args.eye_bias_alpha
        self.eye_bias_px[0] = (1.0 - alpha) * self.eye_bias_px[0] + alpha * error_x
        self.eye_bias_px[1] = (1.0 - alpha) * self.eye_bias_px[1] + alpha * error_y
        self.eye_bias_samples += 1
        self.projected_eye_px = (raw_eye[0] + self.eye_bias_px[0], raw_eye[1] + self.eye_bias_px[1])

    def smooth_landmarks(
        self, landmarks: list[tuple[int, float, float, float]], now: float
    ) -> list[tuple[int, float, float, float]]:
        if self.args.landmark_smooth_min_cutoff <= 0.0 or not landmarks:
            return landmarks
        out: list[tuple[int, float, float, float]] = []
        for index, x, y, z in landmarks:
            filters = self.landmark_filters.get(index)
            if filters is None:
                filters = (
                    OneEuroFilter(self.args.landmark_smooth_min_cutoff, self.args.landmark_smooth_beta),
                    OneEuroFilter(self.args.landmark_smooth_min_cutoff, self.args.landmark_smooth_beta),
                )
                self.landmark_filters[index] = filters
            out.append((index, filters[0].filter(x, now), filters[1].filter(y, now), z))
        return out

    def stabilize_roi(
        self, roi: tuple[float, float, float, float], now: float
    ) -> tuple[float, float, float, float]:
        if self.args.roi_smooth_min_cutoff <= 0.0:
            return roi
        x, y, w, h = roi
        scx = self.roi_cx_filter.filter(x + w * 0.5, now)
        scy = self.roi_cy_filter.filter(y + h * 0.5, now)
        sw = self.roi_w_filter.filter(w, now)
        sh = self.roi_h_filter.filter(h, now)
        out_x = scx - sw * 0.5
        out_y = scy - sh * 0.5
        out_x = max(-sw * 0.35, min(280.0 - sw * 0.65, out_x))
        out_y = max(-sh * 0.35, min(280.0 - sh * 0.65, out_y))
        return (out_x, out_y, sw, sh)

    def evaluate_pose(self, raw: tuple[float, float, float], now: float) -> tuple[bool, str]:
        """Confidence gate: reject implausible / unanchored 3DDFA pose.

        3DDFA has no face detector and will confidently fit background (curtains)
        when the face is not in the crop. We trust the estimate only when the
        landmarks are plausible and either a live Tobii eye-origin anchors the
        face, or we are briefly coasting continuously from a recent anchored pose.
        """
        if self.args.no_pose_gate:
            return True, ""
        ok, lm_reason = landmark_eye_quality(self.landmarks, self.roi_tuple)
        if not ok:
            return False, lm_reason
        raw_eye = self.raw_projected_eye()
        det_eye = landmark_eye_center(self.landmarks)
        if raw_eye is not None and det_eye is not None:
            if math.hypot(det_eye[0] - raw_eye[0], det_eye[1] - raw_eye[1]) > self.args.pose_eye_disagree_px:
                return False, "eye disagree"
        eye_anchor_live = self.anchor is not None and eye_anchor_point(self.anchor) is not None
        if eye_anchor_live:
            return True, ""
        # No live eye-origin: only coast briefly with continuous motion, then hold.
        if self.pose_accepted_raw is None:
            return False, "no anchor"
        if (now - self.pose_accepted_time) > self.args.pose_hold_timeout_s:
            return False, "no anchor (envelope)"
        jump = max(abs(raw[i] - self.pose_accepted_raw[i]) for i in range(3))
        if jump > self.args.pose_reject_jump_deg:
            return False, f"jump {jump:.0f}deg"
        return True, ""

    def apply_pose_hold(self, now: float) -> None:
        """Hold last accepted pose briefly, then decay toward neutral (graceful)."""
        if self.pose_at_hold_start is None:
            self.pose_at_hold_start = self.pose
        held_for = (now - self.pose_accepted_time) if self.pose_accepted_time else 1e9
        if held_for <= self.args.pose_hold_timeout_s:
            self.pose = self.pose_at_hold_start
            return
        t = held_for - self.args.pose_hold_timeout_s
        factor = math.exp(-t / max(1e-3, self.args.pose_decay_tau_s))
        self.pose = tuple(v * factor for v in self.pose_at_hold_start)

    def draw(self) -> None:
        self.canvas.delete("all")
        if self.latest_frame is not None:
            try:
                width, height, gray = read_pgm(self.latest_frame.path)
                rgb = contrast_rgb(gray)
                photo = tk.PhotoImage(data=rgb_to_ppm(width, height, rgb), format="PPM")
                self.photo = photo.zoom(self.canvas_scale)
                self.canvas.create_image(0, 0, image=self.photo, anchor="nw")
            except Exception as exc:  # noqa: BLE001
                self.canvas.create_text(12, 12, text=str(exc), anchor="nw", fill="#ff7777")
        s = self.canvas_scale
        x, y, w, h = self.roi_tuple
        self.canvas.create_rectangle(x * s, y * s, (x + w) * s, (y + h) * s, outline="#35ff8a", width=2)
        raw_eye = self.raw_projected_eye()
        if raw_eye is not None:
            raw_x = raw_eye[0] * s
            raw_y = raw_eye[1] * s
            self.canvas.create_oval(raw_x - 4, raw_y - 4, raw_x + 4, raw_y + 4, outline="#1f8f54", width=1)
        if self.projected_eye_px is not None:
            ax = self.projected_eye_px[0] * s
            ay = self.projected_eye_px[1] * s
            self.canvas.create_oval(ax - 7, ay - 7, ax + 7, ay + 7, outline="#35ff8a", width=2)
        if self.landmark_eye_px is not None:
            ex = self.landmark_eye_px[0] * s
            ey = self.landmark_eye_px[1] * s
            self.canvas.create_line(ex - 7, ey, ex + 7, ey, fill="#ffffff", width=1)
            self.canvas.create_line(ex, ey - 7, ex, ey + 7, fill="#ffffff", width=1)
        if self.tracked_eye_px is not None:
            tx = self.tracked_eye_px[0] * s
            ty = self.tracked_eye_px[1] * s
            self.canvas.create_oval(tx - 9, ty - 9, tx + 9, ty + 9, outline="#ffae42", width=2)
        latest_gaze = self.gaze_rows[-1] if self.gaze_rows else None
        if latest_gaze and latest_gaze.gaze_valid and latest_gaze.gaze_x is not None and latest_gaze.gaze_y is not None:
            gx = latest_gaze.gaze_x * 280 * s
            gy = latest_gaze.gaze_y * 280 * s
            self.canvas.create_oval(gx - 5, gy - 5, gx + 5, gy + 5, outline="#ffe66b", width=2)
        for index, lx, ly, _lz in self.landmarks:
            if self.args.engine == "mediapipe" and index in {
                33, 133, 159, 145, 263, 362, 386, 374, 468, 469, 470, 471, 472, 473, 474, 475, 476, 477
            }:
                color = "#ffe66b"
            elif self.args.engine == "mediapipe" and index in {1, 2, 4, 5, 6, 45, 275}:
                color = "#ff5edb"
            elif self.args.engine == "mediapipe" and index in {0, 13, 14, 17, 61, 291}:
                color = "#58dcff"
            elif 36 <= index <= 47:
                color = "#ffe66b"
            elif 27 <= index <= 35:
                color = "#ff5edb"
            elif 48 <= index <= 67:
                color = "#58dcff"
            else:
                color = "#4bb7ff"
            r = 2.4
            self.canvas.create_oval(lx * s - r, ly * s - r, lx * s + r, ly * s + r, fill=color, outline="")

    def update_status(self) -> None:
        latest_gaze = self.gaze_rows[-1] if self.gaze_rows else None
        frame_age = time.monotonic() - self.last_frame_wall if self.last_frame_wall else 0.0
        jitter_text = "n/a"
        if len(self.pose_raw_history) >= 3:
            ys = [p[0] for p in self.pose_raw_history]
            ps = [p[1] for p in self.pose_raw_history]
            rs = [p[2] for p in self.pose_raw_history]
            jitter_text = (
                f"y{statistics.pstdev(ys):.1f} p{statistics.pstdev(ps):.1f} r{statistics.pstdev(rs):.1f} deg"
            )
        mux_status = "external" if self.args.no_start_mux else ("running" if self.mux and self.mux.poll() is None else f"exited {self.mux.poll() if self.mux else 'n/a'}")
        mux_tail = ""
        if self.mux and self.mux.poll() is not None and self.mux_log.exists():
            lines = self.mux_log.read_text(errors="replace").splitlines()[-4:]
            mux_tail = "\n".join(f"  {line[:68]}" for line in lines)
        gaze_text = "none"
        if latest_gaze:
            gaze_text = (
                f"valid={int(latest_gaze.gaze_valid)} eyes=L{int(latest_gaze.left_present)} R{int(latest_gaze.right_present)} "
                f"ipd={latest_gaze.ipd_mm:.1f}mm"
            )
        anchor_text = "none"
        if self.anchor and self.neutral:
            anchor_text = (
                f"x={self.anchor.cx_mm:+.1f} y={self.anchor.cy_mm:+.1f} z={self.anchor.cz_mm:.1f} "
                f"dx={self.anchor.cx_mm - self.neutral.cx_mm:+.1f} dy={self.anchor.cy_mm - self.neutral.cy_mm:+.1f}"
            )
        projected_text = "none"
        if self.raw_projected_eye_px is not None:
            mode = "locked" if self.args.eye_bias_alpha <= 0.0 else "learn"
            projected_text = (
                f"raw={self.raw_projected_eye_px[0]:.1f},{self.raw_projected_eye_px[1]:.1f} "
                f"bias={self.eye_bias_px[0]:+.1f},{self.eye_bias_px[1]:+.1f} "
                f"samples={self.eye_bias_samples} {mode}"
            )
        tracked_text = "none"
        if self.tracked_eye_px is not None:
            tracked_text = (
                f"{self.tracked_eye_px[0]:.1f},{self.tracked_eye_px[1]:.1f} "
                f"age={time.monotonic() - self.tracked_eye_time:.2f}s {self.tracked_eye_quality}"
            )
        self.status.configure(
            text=(
                "Tobii In-Band Landmarks\n\n"
                f"mux         {mux_status}\n"
                f"session     {self.session}\n\n"
                f"{mux_tail + chr(10) if mux_tail else ''}"
                f"frames      {self.frame_count} age={frame_age:.2f}s\n"
                f"engine      {self.args.engine} {self.mediapipe_status if self.args.engine == 'mediapipe' else ''}\n"
                f"gaze rows   {self.gaze_count}\n"
                f"gaze        {gaze_text}\n\n"
                f"roi mode    {self.roi_mode}\n"
                f"roi         {self.roi_text}\n"
                f"eye anchor  {anchor_text}\n\n"
                f"eye px      {projected_text}\n\n"
                f"track eye   {tracked_text}\n\n"
                f"landmarks   {len(self.landmarks)} pts\n"
                f"pose        {self.pose_text}\n"
                f"pose raw    {self.pose_raw[0]:+.1f}/{self.pose_raw[1]:+.1f}/{self.pose_raw[2]:+.1f}\n"
                f"jitter raw  {jitter_text}\n"
                f"preprocess  {self.args.preprocess}\n\n"
                "overlay\n"
                "  green box: active face bounds / 3DDFA ROI\n"
                "  green ring: corrected eye-origin center\n"
                "  dim green ring: raw eye-origin projection\n"
                "  orange ring: landmark-driven ROI center\n"
                "  white cross: 3DDFA eye landmark center\n"
                "  yellow ring: raw gaze point\n"
                "  yellow pts: eyes, magenta: nose, cyan: mouth\n\n"
                "n recaptures neutral, r resets projection, Esc/q closes"
            )
        )

    def tick(self) -> None:
        self.update_csv()
        self.run_landmarks()
        self.draw()
        self.update_status()
        self.root.after(max(10, int(1000 / self.args.ui_hz)), self.tick)

    def close(self) -> None:
        if self.mux and self.mux.poll() is None:
            self.mux.send_signal(signal.SIGINT)
            try:
                self.mux.wait(timeout=1.5)
            except subprocess.TimeoutExpired:
                self.mux.terminate()
                try:
                    self.mux.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    self.mux.kill()
                    self.mux.wait(timeout=1.0)
        if self.mux_log_handle:
            self.mux_log_handle.close()
            self.mux_log_handle = None
        if self.mediapipe_worker:
            self.mediapipe_worker.close()
            self.mediapipe_worker = None
        self.root.destroy()

    def reset_projection(self) -> None:
        self.eye_bias_px = [0.0, 0.0]
        self.eye_bias_samples = 0
        self.raw_projected_eye_px = None
        self.projected_eye_px = None
        self.landmark_eye_px = None
        self.tracked_eye_px = None
        self.tracked_eye_time = 0.0
        self.tracked_eye_quality = "none"
        self.yaw_filter.reset()
        self.pitch_filter.reset()
        self.roll_filter.reset()
        self.roi_cx_filter.reset()
        self.roi_cy_filter.reset()
        self.roi_w_filter.reset()
        self.roi_h_filter.reset()
        self.landmark_filters.clear()
        self.pose_raw_history.clear()
        self.last_anchored_roi = None
        self.last_anchored_time = 0.0
        self.pose_accepted_raw = None
        self.pose_accepted_time = 0.0
        self.pose_at_hold_start = None
        self.pose_held_reason = ""

    def recapture_neutral(self) -> None:
        neutral = median_gaze(self.gaze_rows)
        if neutral is not None:
            self.neutral = neutral
        self.reset_projection()

    def run(self) -> int:
        self.root.bind("<Escape>", lambda _event: self.close())
        self.root.bind("q", lambda _event: self.close())
        self.root.bind("r", lambda _event: self.reset_projection())
        self.root.bind("n", lambda _event: self.recapture_neutral())
        self.start_mux()
        self.root.after(100, self.tick)
        self.root.mainloop()
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session", type=Path, help="Session directory. Defaults to /dev/shm/tobii-linux/inband-landmark-dashboard.")
    parser.add_argument("--no-start-mux", action="store_true", help="Display an already-running session instead of launching tobii-ttp-mux.")
    parser.add_argument("--seconds", type=float, default=3600.0)
    parser.add_argument("--startup", choices=("public", "tobiifree"), default="tobiifree")
    parser.add_argument("--display-area", choices=("none", "big", "rect"), default="big")
    parser.add_argument("--image-write-hz", type=float, default=12.0)
    parser.add_argument("--image-ring-size", type=int, default=96)
    parser.add_argument("--landmark-hz", type=float, default=6.0)
    parser.add_argument("--ui-hz", type=float, default=20.0)
    parser.add_argument("--scale", type=int, default=2)
    parser.add_argument("--engine", choices=("3ddfa", "mediapipe"), default="3ddfa")
    parser.add_argument("--mediapipe-python", type=Path, default=ROOT / ".venv" / "mediapipe" / "bin" / "python")
    parser.add_argument("--mediapipe-model", type=Path, default=ROOT / "assets" / "mediapipe" / "face_landmarker.task")
    parser.add_argument("--mediapipe-preprocess", choices=("raw", "stretch", "clahe"), default="clahe")
    parser.add_argument("--mediapipe-upscale", type=float, default=1.0)
    parser.add_argument("--mediapipe-min-detection-confidence", type=float, default=0.35)
    parser.add_argument("--mediapipe-min-presence-confidence", type=float, default=0.35)
    parser.add_argument("--mediapipe-min-tracking-confidence", type=float, default=0.35)
    parser.add_argument("--roi", default="62,0,165,190")
    parser.add_argument(
        "--roi-source",
        choices=("landmark-primary", "eye-origin", "fixed"),
        default="landmark-primary",
        help="ROI steering source. landmark-primary follows stable 3DDFA eye landmarks, with Tobii eye-origin as bootstrap.",
    )
    parser.add_argument("--eye-anchor-px", default="140,108", help="Neutral image-space eye-origin anchor as x,y, separate from ROI center.")
    parser.add_argument("--eye-bias-alpha", type=float, default=0.0, help="EWMA alpha for aligning projected eye-origin to detected 3DDFA eye landmarks. Default 0 disables learning.")
    parser.add_argument("--eye-bias-max-error-px", type=float, default=70.0, help="Ignore landmark/projection corrections larger than this many pixels on either axis.")
    parser.add_argument("--eye-origin-focal-px", type=float, default=240.0)
    parser.add_argument("--eye-origin-x-sign", type=float, default=-1.0, help="Image-space sign for Tobii eye-origin X translation.")
    parser.add_argument("--eye-origin-y-sign", type=float, default=-1.0, help="Image-space sign for Tobii eye-origin Y translation.")
    parser.add_argument("--landmark-eye-alpha", type=float, default=0.35, help="EWMA alpha for landmark-primary ROI eye tracking.")
    parser.add_argument("--eye-anchor-alpha", type=float, default=0.20, help="EWMA alpha while Tobii eye-origin is actively leashing landmark tracking.")
    parser.add_argument("--landmark-eye-anchor-refine-px", type=float, default=12.0, help="Maximum per-axis 3DDFA refinement allowed around a valid Tobii eye-origin anchor.")
    parser.add_argument("--landmark-eye-hold-s", type=float, default=0.9, help="How long to keep steering ROI from the last good 3DDFA eye center.")
    parser.add_argument("--landmark-eye-max-step-px", type=float, default=42.0, help="Reject 3DDFA eye-center jumps larger than this many pixels per landmark sample.")
    parser.add_argument(
        "--landmark-eye-anchor-max-error-px",
        type=float,
        default=38.0,
        help="When binocular Tobii eye-origin is valid, reject 3DDFA eye centers farther than this from the projected anchor.",
    )
    parser.add_argument("--roi-min-size", type=float, default=150.0)
    parser.add_argument("--roi-max-size", type=float, default=230.0)
    parser.add_argument("--roi-max-above-eye-px", type=float, default=85.0, help="Cap how far the crop may extend above the eye anchor, keeping background/curtains above the head out of the 3DDFA crop.")
    parser.add_argument("--roi-freeze-hold-s", type=float, default=1.5, help="When the eye-origin anchor is lost, hold the last good eye-anchored crop for this long instead of chasing 3DDFA landmarks.")
    parser.add_argument("--preprocess", choices=("raw", "roi-normalize"), default="raw")
    parser.add_argument("--threads", type=int, default=2)
    parser.add_argument("--model", type=Path, default=ROOT / "assets" / "3ddfa" / "mb05_120x120.onnx")
    parser.add_argument("--stats", type=Path, default=ROOT / "assets" / "3ddfa" / "param_mean_std_62d_120x120.tsv")
    parser.add_argument("--bfm-sparse", type=Path, default=ROOT / "assets" / "3ddfa" / "bfm_sparse_68.bin")
    parser.add_argument("--native-bin", type=Path, default=ROOT / "build" / "tobii-3ddfa-onnx")
    parser.add_argument("--pose-min-cutoff", type=float, default=1.0, help="One-Euro min cutoff (Hz) for yaw/pitch/roll. Lower = steadier at rest, more lag.")
    parser.add_argument("--pose-beta", type=float, default=0.015, help="One-Euro speed coefficient for pose. Higher = snappier during motion.")
    parser.add_argument("--pose-d-cutoff", type=float, default=1.0, help="One-Euro derivative cutoff (Hz) for pose.")
    parser.add_argument("--roi-smooth-min-cutoff", type=float, default=1.2, help="One-Euro min cutoff (Hz) for the crop box center/size. 0 disables ROI smoothing.")
    parser.add_argument("--roi-smooth-beta", type=float, default=0.02, help="One-Euro speed coefficient for the crop box.")
    parser.add_argument("--landmark-smooth-min-cutoff", type=float, default=1.5, help="One-Euro min cutoff (Hz) for landmark points. 0 disables landmark smoothing.")
    parser.add_argument("--landmark-smooth-beta", type=float, default=0.02, help="One-Euro speed coefficient for landmark points.")
    parser.add_argument("--no-pose-gate", action="store_true", help="Disable the pose confidence gate (accept every 3DDFA frame, including background fits).")
    parser.add_argument("--pose-reject-jump-deg", type=float, default=28.0, help="When no live eye anchor, reject pose whose per-update change exceeds this on any axis.")
    parser.add_argument("--pose-eye-disagree-px", type=float, default=55.0, help="Reject pose when the 3DDFA eye center disagrees with the Tobii eye-origin projection by more than this.")
    parser.add_argument("--pose-hold-timeout-s", type=float, default=0.5, help="How long to hold the last accepted pose steady before decaying toward neutral.")
    parser.add_argument("--pose-decay-tau-s", type=float, default=0.6, help="Exponential time constant for decaying held pose back toward neutral once outside the visible envelope.")
    args = parser.parse_args()
    return Dashboard(args).run()


if __name__ == "__main__":
    raise SystemExit(main())
