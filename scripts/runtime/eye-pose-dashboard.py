#!/usr/bin/env python3
"""Optional dashboard client. Capture, inference, calibration and settings live in the service."""
import argparse
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from types import SimpleNamespace
from collections import deque
from dataclasses import asdict
from tkinter import BOTH, Canvas, Tk, Toplevel
from typing import Callable

from tracking_core import (BG, PANEL, STROKE, TEXT, MUTED, BLUE, GREEN, YELLOW, RED,
    TOBII_ET5_MARKER_SPACING_MM, DEFAULT_WINDOW_GEOMETRY, CALIBRATION_TARGETS,
    TUNING_ATTRS, EyeSample, EyePose, PoseConfidence, MediaPipeFaceSample,
    MonitorInfo, ScreenCalibrationProfile, clamp, ellipsize, tuning_multiplier,
    tuning_physical_value)
from runtime_config import create_parser
from service_protocol import ServiceClient, RpcConnection, default_socket

def detect_monitors(root: Tk) -> list[MonitorInfo]:
    monitors: list[MonitorInfo] = []
    try:
        proc = subprocess.run(
            ["xrandr", "--query"],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        proc = None
    if proc is not None and proc.returncode == 0:
        for line in proc.stdout.splitlines():
            parts = line.split()
            if " connected" not in line:
                continue
            name = parts[0]
            primary = " primary " in f" {line} "
            for part in parts[1:]:
                if "x" not in part or "+" not in part:
                    continue
                try:
                    size, x_text, y_text = part.split("+", 2)
                    width_text, height_text = size.split("x", 1)
                    monitors.append(
                        MonitorInfo(
                            name=name,
                            x=int(x_text),
                            y=int(y_text),
                            width=int(width_text),
                            height=int(height_text),
                            primary=primary,
                        )
                    )
                    break
                except ValueError:
                    continue
    if monitors:
        return monitors
    return [
        MonitorInfo(
            name="primary",
            x=0,
            y=0,
            width=max(root.winfo_screenwidth(), 1),
            height=max(root.winfo_screenheight(), 1),
            primary=True,
        )
    ]


def tk_geometry(width: int, height: int, x: int = 0, y: int = 0) -> str:
    return f"{width}x{height}{x:+d}{y:+d}"


class ScreenCalibrationWindow:
    def __init__(
        self,
        parent: Tk,
        profile_file: Path | None,
        initial_profile: ScreenCalibrationProfile | None,
        on_saved: Callable[[dict], None],
    ) -> None:
        self.parent = parent
        self.profile_file = profile_file
        self.initial_profile = initial_profile
        self.on_saved = on_saved
        self.monitors = detect_monitors(parent)
        self.monitor: MonitorInfo | None = None
        self.window: Toplevel | None = None
        self.canvas: Canvas | None = None
        self.left_x = 0.0
        self.right_x = 0.0
        self.drag_mode: str | None = None
        self.save_buttons: list[tuple[float, float, float, float]] = []
        self.open_picker()

    def open_picker(self) -> None:
        win = Toplevel(self.parent)
        win.title("Tobii Screen Calibration - Select Display")
        win.configure(background=BG)
        win.geometry("560x360")
        win.protocol("WM_DELETE_WINDOW", self.close)
        canvas = Canvas(win, background=BG, highlightthickness=0)
        canvas.pack(fill=BOTH, expand=True)
        self.window = win
        self.canvas = canvas
        canvas.bind("<Button-1>", self.on_picker_click)
        win.bind("<Escape>", lambda _event: self.close())
        self.draw_picker()

    def draw_picker(self) -> None:
        if self.canvas is None:
            return
        canvas = self.canvas
        canvas.delete("all")
        w = max(canvas.winfo_width(), 560)
        canvas.create_text(28, 28, anchor="nw", fill=TEXT, font=("Sans", 20, "bold"), text="Choose calibration display")
        canvas.create_text(
            28,
            64,
            anchor="nw",
            fill=MUTED,
            font=("Sans", 12),
            text="Pick the monitor that Star Citizen uses. The next screen shows alignment guides.",
        )
        y = 112
        for index, monitor in enumerate(self.monitors):
            label = f"{monitor.name}  {monitor.width}x{monitor.height}+{monitor.x}+{monitor.y}"
            if monitor.primary:
                label += "  primary"
            canvas.create_rectangle(28, y, w - 28, y + 54, fill="#121922", outline=STROKE)
            canvas.create_text(44, y + 17, anchor="nw", fill=TEXT, font=("Sans", 13, "bold"), text=label)
            canvas.create_text(44, y + 36, anchor="nw", fill=MUTED, font=("Sans", 9), text=f"display {index + 1}")
            y += 66
        canvas.create_text(28, y + 18, anchor="nw", fill="#6b7788", font=("Sans", 10), text="Esc cancels")

    def on_picker_click(self, event) -> None:
        y = 112
        for monitor in self.monitors:
            if 28 <= event.x and y <= event.y <= y + 54:
                self.open_calibration(monitor)
                return
            y += 66

    def open_calibration(self, monitor: MonitorInfo) -> None:
        if self.window is not None:
            self.window.destroy()
        self.monitor = monitor
        win = Toplevel(self.parent)
        win.title(f"Tobii Screen Calibration - {monitor.name}")
        win.configure(background=BG)
        win.geometry(tk_geometry(monitor.width, monitor.height, monitor.x, monitor.y))
        win.update_idletasks()
        win.attributes("-fullscreen", True)
        win.attributes("-topmost", True)
        win.protocol("WM_DELETE_WINDOW", self.close)
        canvas = Canvas(win, background=BG, highlightthickness=0)
        canvas.pack(fill=BOTH, expand=True)
        self.window = win
        self.canvas = canvas

        if self.initial_profile and self.initial_profile.monitor_name == monitor.name:
            self.left_x = self.initial_profile.device_center_x_px - self.initial_profile.marker_spacing_px * 0.5
            self.right_x = self.initial_profile.device_center_x_px + self.initial_profile.marker_spacing_px * 0.5
        else:
            spacing = min(monitor.width * 0.46, max(260.0, monitor.width * 0.24))
            self.left_x = monitor.width * 0.5 - spacing * 0.5
            self.right_x = monitor.width * 0.5 + spacing * 0.5
        self.clamp_guides()

        canvas.bind("<Button-1>", self.on_calibration_press)
        canvas.bind("<B1-Motion>", self.on_calibration_drag)
        canvas.bind("<ButtonRelease-1>", self.on_calibration_release)
        win.bind("<Escape>", lambda _event: self.close())
        win.bind("q", lambda _event: self.close())
        win.bind("s", lambda _event: self.save())
        win.bind("<Return>", lambda _event: self.save())
        win.bind("<Left>", lambda event: self.nudge(-1 if not event.state & 0x1 else -10, 0, 0))
        win.bind("<Right>", lambda event: self.nudge(1 if not event.state & 0x1 else 10, 0, 0))
        win.bind("+", lambda _event: self.nudge(0, 0, 2))
        win.bind("=", lambda _event: self.nudge(0, 0, 2))
        win.bind("-", lambda _event: self.nudge(0, 0, -2))
        self.draw_calibration()

    def close(self) -> None:
        if self.window is not None:
            self.window.destroy()
        self.window = None
        self.canvas = None

    def clamp_guides(self) -> None:
        if self.monitor is None:
            return
        min_spacing = 80.0
        max_spacing = max(min_spacing, self.monitor.width - 40.0)
        spacing = clamp(self.right_x - self.left_x, min_spacing, max_spacing)
        center = clamp((self.left_x + self.right_x) * 0.5, spacing * 0.5 + 20.0, self.monitor.width - spacing * 0.5 - 20.0)
        self.left_x = center - spacing * 0.5
        self.right_x = center + spacing * 0.5

    def draw_calibration(self) -> None:
        if self.canvas is None or self.monitor is None:
            return
        canvas = self.canvas
        canvas.delete("all")
        w = max(canvas.winfo_width(), self.monitor.width)
        h = max(canvas.winfo_height(), self.monitor.height)
        spacing = max(self.right_x - self.left_x, 1.0)
        px_per_mm = spacing / TOBII_ET5_MARKER_SPACING_MM
        mm_per_px = TOBII_ET5_MARKER_SPACING_MM / spacing
        center_x = (self.left_x + self.right_x) * 0.5

        for ratio in (0.25, 0.5, 0.75):
            canvas.create_line(w * ratio, 0, w * ratio, h, fill="#101820")
            canvas.create_line(0, h * ratio, w, h * ratio, fill="#101820")
        canvas.create_line(self.left_x, 0, self.left_x, h, fill=GREEN, width=4)
        canvas.create_line(self.right_x, 0, self.right_x, h, fill=GREEN, width=4)
        canvas.create_line(center_x, 0, center_x, h, fill=YELLOW, width=2, dash=(8, 8))
        canvas.create_oval(center_x - 11, h - 28, center_x + 11, h - 6, outline=YELLOW, width=3)

        panel_w = min(w - 56, 860)
        panel_h = 184
        panel_x = 28
        panel_y = 28
        canvas.create_rectangle(panel_x, panel_y, panel_x + panel_w, panel_y + panel_h, fill="#0d1218", outline=STROKE)
        canvas.create_text(48, 48, anchor="nw", fill=TEXT, font=("Sans", 20, "bold"), text="Align Tobii ET5 physical markers")
        canvas.create_text(
            48,
            84,
            anchor="nw",
            fill=MUTED,
            font=("Sans", 12),
            width=max(320, panel_w - 40),
            text="Drag the green vertical lines onto the two white ET5 device marks. The ET5 is assumed to be mounted below the selected display.",
        )
        canvas.create_text(
            48,
            126,
            anchor="nw",
            fill=TEXT,
            font=("Sans", 12),
            width=max(320, panel_w - 40),
            text=f"{self.monitor.name}: {self.monitor.width}x{self.monitor.height}+{self.monitor.x}+{self.monitor.y}    spacing {spacing:.1f}px = {TOBII_ET5_MARKER_SPACING_MM:.0f}mm",
        )
        canvas.create_text(
            48,
            154,
            anchor="nw",
            fill=TEXT,
            font=("Sans", 12),
            width=max(320, panel_w - 40),
            text=f"device center x {center_x:.1f}px at display bottom    {px_per_mm:.3f}px/mm  {mm_per_px:.4f}mm/px",
        )

        button_w = 142
        button_h = 42
        self.save_buttons = []
        panel_button_x = panel_x + panel_w - button_w - 18
        panel_button_y = panel_y + panel_h - button_h - 18
        self.draw_save_button(panel_button_x, panel_button_y, button_w, button_h)
        bottom_button_x = w - button_w - 28
        bottom_button_y = h - button_h - 24
        self.draw_save_button(bottom_button_x, bottom_button_y, button_w, button_h)
        canvas.create_text(
            48,
            h - 42,
            anchor="sw",
            fill="#6b7788",
            font=("Sans", 10),
            width=max(320, w - button_w - 112),
            text="Drag green lines to align with the ET5 marks. Left/right arrows move center, +/- changes spacing, Shift+arrow moves faster. Enter/s saves, Esc/q cancels.",
        )

    def draw_save_button(self, x: float, y: float, w: float, h: float) -> None:
        if self.canvas is None:
            return
        self.save_buttons.append((x, y, x + w, y + h))
        self.canvas.create_rectangle(x, y, x + w, y + h, fill="#1e3652", outline=BLUE, width=2)
        self.canvas.create_text(x + w / 2, y + h / 2, anchor="center", fill=TEXT, font=("Sans", 13, "bold"), text="Save")

    def on_calibration_press(self, event) -> None:
        for x1, y1, x2, y2 in self.save_buttons:
            if x1 <= event.x <= x2 and y1 <= event.y <= y2:
                self.drag_mode = None
                self.save()
                return
        center_x = (self.left_x + self.right_x) * 0.5
        if abs(event.x - self.left_x) < 24:
            self.drag_mode = "left"
        elif abs(event.x - self.right_x) < 24:
            self.drag_mode = "right"
        elif abs(event.x - center_x) < 36:
            self.drag_mode = "pair"
        else:
            self.drag_mode = "pair"

    def on_calibration_drag(self, event) -> None:
        if self.monitor is None:
            return
        if self.drag_mode == "left":
            self.left_x = float(event.x)
        elif self.drag_mode == "right":
            self.right_x = float(event.x)
        elif self.drag_mode == "pair":
            spacing = self.right_x - self.left_x
            center = float(event.x)
            self.left_x = center - spacing * 0.5
            self.right_x = center + spacing * 0.5
        self.clamp_guides()
        self.draw_calibration()

    def on_calibration_release(self, _event) -> None:
        self.drag_mode = None

    def nudge(self, dx: float, _dy: float, dspacing: float) -> None:
        if self.monitor is None:
            return
        if dspacing:
            self.left_x -= dspacing * 0.5
            self.right_x += dspacing * 0.5
        if dx:
            self.left_x += dx
            self.right_x += dx
        self.clamp_guides()
        self.draw_calibration()

    def save(self) -> None:
        if self.monitor is not None:
            self.on_saved(dict(monitor=asdict(self.monitor), left=self.left_x, right=self.right_x))
            self.close()



class EyePoseDashboard:
    def __init__(self, root, socket_path, fullscreen=False):
        self.root = root
        self.socket_path = socket_path
        self.client = ServiceClient(socket_path)
        self.fullscreen = fullscreen
        self.args = create_parser().parse_args([])
        self.args.headless = False
        self.args.fullscreen = fullscreen
        self.canvas = Canvas(root, background=BG, highlightthickness=0)
        self.canvas.pack(fill=BOTH, expand=True)
        self.click_targets = []
        self.drag_target = None
        self.canvas.bind('<Button-1>', self.on_click)
        self.canvas.bind('<B1-Motion>', self.on_drag)
        self.canvas.bind('<ButtonRelease-1>', self.on_release)
        self.root.bind('<Escape>', self.quit)
        self.root.bind('q', self.quit)
        for key, callback in {'c': self.recenter, '<space>': self.space_action,
                'g': self.start_gaze_calibration, 'm': self.open_screen_calibration,
                'd': self.dump_pitch_debug, 'r': self.reset_baseline,
                's': self.toggle_smoothing, '<Tab>': self.toggle_harness}.items():
            self.root.bind(key, callback)
        for key, axis, delta in (('y','yaw',.25),('Y','yaw',-.25),('p','pitch',.25),
                                ('P','pitch',-.25),('o','roll',.25),('O','roll',-.25)):
            self.root.bind(key, lambda _, a=axis, d=delta: self.adjust_gain(a,d))
        self.root.protocol('WM_DELETE_WINDOW', self.quit)
        self.root_dir = Path(__file__).resolve().parents[2]
        self.csv_path = self.root_dir/'.tmp/sc-tobii-native-runtime/gaze.csv'
        self.status = 'connecting to tracking service'
        self.latest = self.latest_mediapipe = self.pose = None
        self.pose_source = 'none'
        self.translation_source = 'none'
        self.confidence = self.display_confidence = PoseConfidence()
        self.display_confidence_initialized = False
        self.pose_trail = deque()
        self.gaze_trail = deque()
        self.smoothed_gaze_ratio = self.gaze_affine = None
        self.gaze_offset_x = self.gaze_offset_y = 0.
        self.calibration_index = None
        self.calibration_valid_samples = 0
        self.screen_profile = None
        self.screen_calibration_window = None
        self.gaze_calibration_fullscreen_active = False
        self.gaze_calibration_previous_geometry = ''
        self.gaze_calibration_previous_topmost = False
        self.smoothing_enabled = True
        self.harness_menu_open = False
        self.opentrack_bridge = self.mediapipe_bridge = self.hq_worker = None
        self.pitch_debug_raw = self.pitch_debug_delta = self.pitch_debug_mapped = None
        self.pitch_debug_status = 'waiting for service'
        self.capture_manual = None
        self.capture_clients = 0
        self.inband_dashboard_proc = None
        self.remote_tuning = {}
        self.rate = 0.
        self.face_usable = False
        self.initial_geometry = False
        self.first_run_prompted = False
        self.first_run_waiting_for_screen = False
        self.root.after(50, self.tick)

    def command(self, name, **values):
        try:
            _, connected, _ = self.client.read()
            if not connected:
                raise ConnectionError('tracking service is disconnected')
            self.client.command(name, **values)
        except (ConnectionError, OSError, ValueError) as exc:
            self.status = str(exc)

    def tick(self):
        snapshot, connected, error = self.client.read()
        if connected and snapshot is not None:
            self.apply_snapshot(snapshot)
            if error:
                self.status = error
        else:
            self.status = 'service disconnected: ' + error
            self.pose = self.latest = self.latest_mediapipe = None
            self.pose_source = 'none'
            self.restore_gaze_calibration_window()
        self.draw()
        self.root.after(50, self.tick)

    def apply_snapshot(self, snapshot):
        for key in ('status','pose_source','translation_source','smoothed_gaze_ratio',
                'gaze_affine','gaze_offset_x','gaze_offset_y','calibration_index',
                'smoothing_enabled','pitch_debug_delta','pitch_debug_mapped',
                'pitch_debug_raw','pitch_debug_status','capture_manual','capture_clients',
                'calibration_valid_samples','face_usable'):
            setattr(self,key,snapshot[key])
        for key, value in snapshot['args'].items():
            setattr(self.args,key,value)
        self.args.headless = False
        self.args.fullscreen = self.fullscreen
        self.remote_tuning = {key:getattr(self.args,key) for key in TUNING_ATTRS}
        self.root_dir = Path(snapshot['root_dir'])
        self.csv_path = Path(snapshot['csv_path'])
        self.rate = snapshot['sample_rate']
        self.pose = EyePose(**snapshot['pose']) if snapshot['pose'] is not None else None
        self.latest_mediapipe = MediaPipeFaceSample(**snapshot['latest_mediapipe']) if snapshot['latest_mediapipe'] is not None else None
        raw = snapshot['latest']
        if raw is not None:
            raw = dict(raw)
            for key in ('left','right'):
                raw[key] = tuple(float('nan') if v is None else v for v in raw[key])
            for key in ('gaze_x','gaze_y'):
                if raw[key] is None:raw[key]=float('nan')
        self.latest = EyeSample(**raw) if raw is not None else None
        self.confidence = PoseConfidence(**snapshot['confidence'])
        self.screen_profile = ScreenCalibrationProfile.from_json(snapshot['screen_profile']) if snapshot['screen_profile'] is not None else None
        self.pose_trail = deque(snapshot['pose_trail'])
        self.gaze_trail = deque(snapshot['gaze_trail'])
        self.opentrack_bridge = SimpleNamespace(**snapshot['output']) if snapshot['output'] is not None else None
        self.mediapipe_bridge = SimpleNamespace(**snapshot['mediapipe']) if snapshot['mediapipe'] is not None else None
        self.hq_worker = SimpleNamespace(**snapshot['hq']) if snapshot['hq'] is not None else None
        if not self.initial_geometry:
            if not self.fullscreen:self.root.geometry(snapshot.get('window_geometry',DEFAULT_WINDOW_GEOMETRY))
            self.initial_geometry = True
        if snapshot['capture_running'] and self.args.first_run_calibration and not self.first_run_prompted:
            self.first_run_prompted=True
            if self.screen_profile is None:
                self.first_run_waiting_for_screen=True
                self.open_screen_calibration()
            elif self.gaze_affine is None:self.start_gaze_calibration()
        if self.first_run_waiting_for_screen and self.screen_profile is not None:
            self.first_run_waiting_for_screen=False
            if self.gaze_affine is None:self.start_gaze_calibration()
        if self.calibration_index is not None:self.enter_gaze_calibration_window()
        else:self.restore_gaze_calibration_window()

    def quit(self, _event=None):
        self.restore_gaze_calibration_window()
        try:
            if not self.fullscreen:
                rpc = RpcConnection(self.socket_path, timeout=.5)
                try:
                    rpc.request('command',name='save_window',values=dict(width=self.root.winfo_width(),
                        height=self.root.winfo_height(),x=self.root.winfo_x(),y=self.root.winfo_y()))
                finally:rpc.close()
        except (OSError,ValueError,ConnectionError):
            pass
        self.client.close()
        self.terminate_inband_dashboard()
        if self.screen_calibration_window is not None:self.screen_calibration_window.close()
        self.root.destroy()

    def active_udp_target(self):
        return (self.args.trackir_udp if self.args.harness=='trackir' else self.args.tobii_udp) or self.args.opentrack_udp or ''

    def sample_rate(self):return self.rate
    def mediapipe_sample_usable(self, _sample):return self.face_usable
    def set_capture_manual(self, value):self.command('set_capture_manual',value=value)
    def set_harness(self, harness):
        self.harness_menu_open=False
        self.command('set_harness',harness=harness)
    def toggle_harness(self, _event=None):self.set_harness('trackir' if self.args.harness=='tobii' else 'tobii')
    def adjust_gain(self, axis, delta):self.command('adjust_gain',axis=axis,delta=delta)
    def recenter(self, _event=None):self.command('recenter')
    def reset_baseline(self, _event=None):self.command('reset_baseline')
    def reset_tuning(self, _event=None):self.command('reset_tuning')
    def reset_gaze_calibration(self, _event=None):self.command('reset_gaze_calibration')
    def toggle_smoothing(self, _event=None):self.command('toggle_smoothing')
    def dump_pitch_debug(self, _event=None):self.command('dump_pitch_debug')
    def start_gaze_calibration(self, _event=None):self.command('start_gaze_calibration')
    def space_action(self, _event=None):
        if self.calibration_index is not None:self.command('capture_calibration_point')
        else:
            self.command('recenter');self.command('center_gaze')

    def save_tuning(self):
        updates={key:getattr(self.args,key) for key in TUNING_ATTRS
            if isinstance(getattr(self.args,key),(int,float)) and getattr(self.args,key)!=self.remote_tuning.get(key)}
        if updates:self.command('set_tuning',updates=updates)

    def open_screen_calibration(self, _event=None):
        if self.screen_calibration_window is not None and self.screen_calibration_window.window is not None:return
        self.screen_calibration_window=ScreenCalibrationWindow(self.root,None,self.screen_profile,self.on_screen_calibration_saved)

    def on_screen_calibration_saved(self, raw):
        self.command('save_screen',**raw)

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


    def open_inband_mediapipe_dashboard(self, _event=None):
        if self.inband_dashboard_proc is not None and self.inband_dashboard_proc.poll() is None:
            return
        self.inband_dashboard_proc = subprocess.Popen(
            [sys.executable, str(self.root_dir / "scripts/runtime/inband-landmark-dashboard.py"),
             "--socket", str(self.socket_path)], start_new_session=True)


    def toggle_harness_menu(self, _px: float | None = None, _py: float | None = None) -> None:
        self.harness_menu_open = not self.harness_menu_open


    def enter_gaze_calibration_window(self) -> None:
        if self.args.headless or self.args.fullscreen or self.gaze_calibration_fullscreen_active:
            return
        try:
            self.root.update_idletasks()
            self.gaze_calibration_previous_geometry = self.root.geometry()
            self.gaze_calibration_previous_topmost = bool(self.root.attributes("-topmost"))
            self.root.attributes("-fullscreen", True)
            self.root.attributes("-topmost", True)
            self.gaze_calibration_fullscreen_active = True
        except Exception:
            self.gaze_calibration_fullscreen_active = False


    def restore_gaze_calibration_window(self) -> None:
        if not self.gaze_calibration_fullscreen_active:
            return
        try:
            self.root.attributes("-fullscreen", False)
            self.root.attributes("-topmost", self.gaze_calibration_previous_topmost)
            if self.gaze_calibration_previous_geometry:
                self.root.geometry(self.gaze_calibration_previous_geometry)
        except Exception:
            pass
        self.gaze_calibration_fullscreen_active = False


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


    def draw(self) -> None:
        self.canvas.delete("all")
        self.click_targets.clear()
        w = max(self.canvas.winfo_width(), 1)
        h = max(self.canvas.winfo_height(), 1)
        if self.calibration_index is not None:
            self.draw_calibration_overlay(w, h)
            return
        self.draw_grid(w, h)

        left_w = min(520, max(420, int(w * 0.30)))
        self.canvas.create_rectangle(0, 0, left_w, h, fill=PANEL, outline=STROKE)
        self.draw_sidebar(28, 28, left_w - 56)
        self.draw_pose_area(left_w + 28, 28, w - left_w - 56, h - 56)


    def draw_grid(self, w: int, h: int) -> None:
        for ratio in (0.25, 0.5, 0.75):
            x = w * ratio
            y = h * ratio
            self.canvas.create_line(x, 0, x, h, fill="#101820")
            self.canvas.create_line(0, y, w, y, fill="#101820")


    def draw_sidebar(self, x: int, y: int, w: int) -> None:
        original_y = y
        self.canvas.create_text(x, y, anchor="nw", fill=TEXT, font=("Sans", 24, "bold"), text="Tobii Eye Pose")
        self.canvas.create_text(x, y + 38, anchor="nw", fill=MUTED, font=("Sans", 12), text="blended eye + face pose for Star Citizen")
        y += 78
        button_w = (w - 16) / 3
        self.action_button(x, y, button_w, 30, "Start Capture", lambda _: self.set_capture_manual(True))
        self.action_button(x + button_w + 8, y, button_w, 30, "Stop Capture", lambda _: self.set_capture_manual(False))
        self.action_button(x + 2 * (button_w + 8), y, button_w, 30, "Auto", lambda _: self.set_capture_manual(None))
        y += 36
        mode = {None: "Auto", True: "Manual start", False: "Manual stop"}[self.capture_manual]
        self.value_line(x, y, "capture", f"{mode} / {self.capture_clients} client connections")
        y += 28

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
            hq = self.hq_worker
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
        if y <= footer_y - 34:
            self.canvas.create_text(
                x,
                footer_y,
                anchor="sw",
                fill="#6b7788",
                font=("Sans", 9),
                text="Space recenter  g gaze cal  m screen cal  r reset",
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
            number = f"{value:+.2f}"
            number_font = ("Sans", 21 if col_w < 150 else 24, "bold")
            self.canvas.create_text(cx, y + 72, anchor="nw", fill=color, font=number_font, text=number)
            self.canvas.create_text(cx + col_w - 14, y + 82, anchor="ne", fill=MUTED, font=("Sans", 10), text=unit)
        gaze_text = ""
        if len(packet) >= 11:
            gaze_text = f"    gaze {packet[8]:.3f},{packet[9]:.3f} valid {int(packet[10])}"
        self.canvas.create_text(
            x + 16,
            y + h - 24,
            anchor="sw",
            fill=TEXT,
            font=("Sans", 11),
            text=ellipsize(
                f"source {self.opentrack_bridge.last_pose_source}    smoothing {'on' if self.smoothing_enabled else 'off'}{gaze_text}",
                max(36, int((w - 32) / 7.0)),
            ),
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
            ("yaw", "opentrack_yaw_scale", -3.0, 3.0, 0.01, BLUE, "{:.2f}"),
            ("pitch", "opentrack_pitch_scale", -3.0, 3.0, 0.01, YELLOW, "{:.2f}"),
            ("roll", "opentrack_roll_scale", -3.0, 3.0, 0.01, BLUE, "{:.2f}"),
            ("x", "opentrack_x_scale", -3.0, 3.0, 0.01, BLUE, "{:.2f}"),
            ("y", "opentrack_y_scale", -3.0, 3.0, 0.01, BLUE, "{:.2f}"),
            ("z", "opentrack_z_scale", -3.0, 3.0, 0.01, BLUE, "{:.2f}"),
            ("yaw curve", "opentrack_yaw_curve", 1.0, 4.0, 0.02, BLUE, "{:.2f}"),
            ("pitch curve", "opentrack_pitch_curve", 1.0, 4.0, 0.02, YELLOW, "{:.2f}"),
            ("mp yaw", "mediapipe_yaw_output_scale", -3.0, 3.0, 0.01, BLUE, "{:.2f}"),
            ("mp pitch", "mediapipe_pitch_output_scale", -3.0, 3.0, 0.01, YELLOW, "{:.2f}"),
            ("mp roll", "mediapipe_roll_output_scale", -3.0, 3.0, 0.01, BLUE, "{:.2f}"),
            ("mp p/y", "mediapipe_pitch_yaw_comp", 0.00, 3.00, 0.05, YELLOW, "{:.2f}"),
            ("knee", "opentrack_curve_knee_deg", 5.0, 90.0, 1.0, BLUE, "{:.0f}"),
            ("smooth", "opentrack_output_smoothing", 0.03, 0.50, 0.01, GREEN, "{:.2f}"),
            ("motion", "opentrack_motion_smoothing", 0.03, 0.60, 0.01, GREEN, "{:.2f}"),
            ("predict", "opentrack_prediction_ms", 0.0, 120.0, 5.0, GREEN, "{:.0f}"),
            ("dampen", "opentrack_stillness_deadband_deg", 0.00, 0.80, 0.02, GREEN, "{:.2f}"),
            ("angle guard", "opentrack_max_angle_step", 5.00, 180.00, 5.00, GREEN, "{:.0f}"),
            ("out cap", "opentrack_max_output_angle", 20.00, 360.00, 5.00, GREEN, "{:.0f}"),
            ("blink hold", "blink_hold_s", 0.00, 0.60, 0.02, GREEN, "{:.2f}"),
        ]
        max_controls = max(6, min(len(controls), int(slider_h // 30) * 2))
        row_y = self.slider_grid(inner_x, row_y, inner_w, controls[:max_controls])
        bridge = self.opentrack_bridge
        age = time.monotonic() - bridge.last_send_monotonic if bridge.last_send_monotonic else 0.0
        status_y = y + h - 54
        udp_status = f"{bridge.sent} packets  age {age:.1f}s  {bridge.last_error or f'sending {self.harness_label()}'}"
        self.value_line(inner_x, status_y, "udp", ellipsize(udp_status, max(34, int(inner_w / 7.5))))
        if bridge.last_packet is not None:
            sent_status = f"src {self.short_source_for(bridge.last_pose_source)}  yaw {bridge.last_packet[3]:+.1f}  pitch {bridge.last_packet[4]:+.1f}  roll {bridge.last_packet[5]:+.1f}"
            self.value_line(inner_x, status_y + 24, "last sent", ellipsize(sent_status, max(34, int(inner_w / 7.5))))


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
            hq = self.hq_worker
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
        self.value_line(inner_x, row_y, "pitch debug", self.pitch_debug_status)
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
            compact = h < 300
            row_y = max(row_y + 44, y + h - (126 if compact else 186))
            if row_y + (74 if compact else 142) > y + h - 12:
                return
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
            if compact:
                return
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
        self.canvas.create_rectangle(0, 0, width, height, fill=BG, outline="")
        total = len(CALIBRATION_TARGETS)
        step = self.calibration_index + 1
        valid_samples = self.calibration_valid_samples
        gaze_ready = valid_samples >= max(3, min(self.args.gaze_calibration_samples, 8))
        status_text = "gaze ready" if gaze_ready else "waiting for valid gaze"
        status_color = GREEN if gaze_ready else YELLOW
        panel_w = min(width - 48, 780)
        panel_h = 168
        panel_x = (width - panel_w) / 2
        panel_y = 26
        self.canvas.create_rectangle(panel_x, panel_y, panel_x + panel_w, panel_y + panel_h, fill="#0d1218", outline=STROKE, width=2)
        self.canvas.create_text(
            width / 2,
            panel_y + 20,
            anchor="n",
            fill=TEXT,
            font=("Sans", 24, "bold"),
            text="Gaze calibration",
        )
        self.canvas.create_text(
            width / 2,
            panel_y + 58,
            anchor="n",
            fill=YELLOW,
            font=("Sans", 15, "bold"),
            text=f"Step {step}/{total}: look at the {label} target",
        )
        self.canvas.create_text(
            width / 2,
            panel_y + 88,
            anchor="n",
            fill=MUTED,
            font=("Sans", 13),
            width=panel_w - 52,
            text="Keep your head still, look directly at the yellow target, then press Space to capture. Repeat for each target.",
        )
        self.canvas.create_text(
            panel_x + 24,
            panel_y + panel_h - 30,
            anchor="w",
            fill=status_color,
            font=("Sans", 12, "bold"),
            text=f"{status_text}  samples {valid_samples}",
        )
        self.canvas.create_text(
            panel_x + panel_w - 24,
            panel_y + panel_h - 30,
            anchor="e",
            fill="#6b7788",
            font=("Sans", 11),
            text="Esc/q exits dashboard",
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
        value = tuning_multiplier(attr, float(getattr(self.args, attr)))
        # Drawing must not modify a loaded setting, including custom values
        # outside the visible slider range. Only a deliberate edit changes it.

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
            setattr(self.args, attr, tuning_physical_value(attr, clamp(updated, min_value, max_value)))
            self.save_tuning()

        self.click_targets.append((slider_x - 10, y, slider_x + slider_w + 10, y + 28, set_value))
        return y + 28



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
        confidence = self.smoothed_display_confidence()
        items = [
            ("eye", confidence.eye, GREEN),
            ("face", confidence.face, BLUE),
            ("blend", confidence.blended, "#8be8ff"),
            ("pitch", confidence.pitch, YELLOW),
            ("eye dist", confidence.eye_distance_stable, GREEN),
            ("velocity", confidence.velocity_ok, GREEN if confidence.velocity_ok > 0.65 else YELLOW),
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


    def smoothed_display_confidence(self) -> PoseConfidence:
        current = self.confidence
        if not self.display_confidence_initialized:
            self.display_confidence = PoseConfidence(
                eye=current.eye,
                face=current.face,
                blended=current.blended,
                eye_distance_stable=current.eye_distance_stable,
                velocity_ok=current.velocity_ok,
                pitch=current.pitch,
            )
            self.display_confidence_initialized = True
            return self.display_confidence

        for attr in ("eye", "face", "blended", "eye_distance_stable", "velocity_ok", "pitch"):
            shown = getattr(self.display_confidence, attr)
            target = getattr(current, attr)
            alpha = 0.30 if target >= shown else 0.08
            setattr(self.display_confidence, attr, shown + (target - shown) * alpha)
        return self.display_confidence


    def calibration_controls(self, x: float, y: float, w: float) -> float:
        self.section(x, y, "CALIBRATION")
        y += 24
        self.value_line(x, y, "head neutral", "Space/c recenters")
        y += 26
        self.value_line(x, y, "position", self.translation_source)
        y += 26
        if self.screen_profile is None:
            self.value_line(x, y, "screen", "not calibrated")
        else:
            profile = self.screen_profile
            self.value_line(
                x,
                y,
                "screen",
                f"{profile.monitor_name} {profile.width_px}x{profile.height_px}  {profile.marker_spacing_px:.0f}px/184mm",
            )
        y += 28
        self.value_line(x, y, "gaze", "calibrated" if self.gaze_affine is not None else "not calibrated")
        y += 28
        button_w = (w - 16) / 3
        self.action_button(x, y, button_w, 30, "Recenter", self.recenter)
        self.action_button(x + button_w + 8, y, button_w, 30, "Gaze Cal", self.start_gaze_calibration)
        self.action_button(x + (button_w + 8) * 2, y, button_w, 30, "Screen Cal", self.open_screen_calibration)
        y += 38
        self.action_button(x, y, button_w, 30, "Reset Gaze", self.reset_gaze_calibration)
        self.action_button(x + button_w + 8, y, button_w, 30, "Reset Tuning", self.reset_tuning)
        return y + 40


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



def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--socket','--control-socket',dest='socket',type=Path,default=default_socket())
    parser.add_argument('--fullscreen',action='store_true')
    parser.add_argument('--windowed',action='store_true')
    args=parser.parse_args()
    root=Tk();root.title('Tobii Dashboard');root.configure(background=BG)
    root.geometry(DEFAULT_WINDOW_GEOMETRY)
    if args.fullscreen:root.attributes('-fullscreen',True)
    app=EyePoseDashboard(root,args.socket,args.fullscreen)
    signal.signal(signal.SIGTERM,lambda *_:root.after(0,app.quit))
    signal.signal(signal.SIGINT,lambda *_:root.after(0,app.quit))
    try:root.mainloop()
    finally:app.client.close()
    return 0


if __name__=='__main__':raise SystemExit(main())
