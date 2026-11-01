"""GUI-independent tracking engine owned exclusively by the service."""
from tracking_core import *


class TrackingEngine:
    def __init__(self, args, csv_path, sampler_cmd, root_dir, log_path):
        self.args = args
        self.csv_path = csv_path
        self.proc = None
        self.capture_demand = DemandReceiver(args.capture_demand_port)
        self.capture_retry_after = 0.0
        self.capture_error = ""
        self.sampler_cmd = sampler_cmd
        self.root_dir = root_dir
        self.log_path = log_path
        self.mediapipe_bridge = None
        self.hq_worker = None
        self.tail = CsvTail(csv_path)
        self.samples: deque[EyeSample] = deque(maxlen=240)
        self.recent_valid: deque[EyeSample] = deque(maxlen=max(10, args.baseline_samples))
        self.pose_trail: deque[tuple[float, float]] = deque(maxlen=45)
        self.latest: EyeSample | None = None
        self.latest_mediapipe: MediaPipeFaceSample | None = None
        self.raw_pose: EyePose | None = None
        self.latest_eye_pose: EyePose | None = None
        self.last_eye_translation = (0.0, 0.0, 0.0)
        self.last_eye_translation_time: float | None = None
        self.translation_source = "neutral"
        self.translation_observation_status = "no measurement"
        self.pose: EyePose | None = None
        self.pose_source = "none"
        self.confidence = PoseConfidence()
        self.last_eye_pose_for_velocity: EyePose | None = None
        self.last_eye_pose_time = 0.0
        self.last_eye_velocity = 0.0
        self.gaze_trail: deque[tuple[float, float]] = deque(maxlen=45)
        self.smoothed_gaze_ratio: tuple[float, float] | None = None
        self.gaze_sample_time: float | None = None
        self.gaze_calibration_file: Path | None = getattr(args, "gaze_calibration_file", None)
        self.gaze_affine: tuple[list[float], list[float]] | None = load_gaze_calibration(self.gaze_calibration_file)
        self.gaze_offset_x = 0.0
        self.gaze_offset_y = 0.0
        self.calibration_index: int | None = None
        self.calibration_points: list[tuple[float, float, float, float]] = []
        self.screen_profile = load_screen_calibration(getattr(args, "screen_calibration_file", None))
        self.baseline: EyeBaseline | None = None
        self.smoothing_enabled = True
        self.smoother = PoseSmoother(args.smoothing)
        self.start_monotonic = time.monotonic()
        self.last_packet_monotonic = 0.0
        self.last_sample_index = -1
        self.packet_times: deque[float] = deque(maxlen=120)
        self.opentrack_bridge = OpenTrackUdpBridge(self.active_udp_target(), args) if self.active_udp_target() else None
        self.mediapipe_yaw_neutral: float | None = None
        self.mediapipe_pitch_neutral: float | None = None
        self.mediapipe_roll_neutral: float | None = None
        self.native_head_position = NativeHeadEstimator(args.native_head_calibration) if args.native_head_calibration is not None else None
        self.head_position = None
        self.head_position_mode = args.head_position_mode
        if args.head_position_calibration is not None:
            self.head_position = HeadPositionEstimator(args.head_position_calibration)
            print(f"head_position=calibrated mode={self.head_position_mode} profile={args.head_position_profile}")
        elif self.native_head_position is not None:
            print(f"head_position=native mode={self.head_position_mode} profile={args.native_head_position_profile}")
        else:
            print("head_position=eye_origins (no calibrated profile selected)")
        self.face_handoff_yaw_bias = 0.0
        self.face_handoff_pitch_bias = 0.0
        self.face_handoff_active = False
        self.last_mediapipe_pose: EyePose | None = None
        self.last_mediapipe_pose_monotonic = 0.0
        self.last_mediapipe_pose_confidence = 0.0
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
        self.last_filter_sample_time: float | None = None
        self.last_filter_source = "none"
        self.status = "starting"


    def active_udp_target(self) -> str:
        if self.args.harness == "trackir":
            return self.args.trackir_udp or self.args.opentrack_udp or ""
        return self.args.tobii_udp or self.args.opentrack_udp or ""


    def set_harness(self, harness: str) -> None:
        if harness not in ("tobii", "trackir"):
            return
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


    def space_action(self, _event=None) -> None:
        if self.calibration_index is not None:
            self.capture_calibration_point()
            return
        self.recenter()
        self.center_gaze()


    def reset_baseline(self, _event=None) -> None:
        self.baseline = None
        self.recent_valid.clear()
        self.clear_face_calibration()
        self.smoother.reset()
        self.clear_gaze_runtime_state()
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


    def reset_tuning(self, _px: float | None = None, _py: float | None = None) -> None:
        for attr, value in DEFAULT_TUNING_VALUES.items():
            setattr(self.args, attr, value)
        if self.opentrack_bridge is not None:
            target = self.active_udp_target()
            if target:
                self.opentrack_bridge.retarget(target)
            self.opentrack_bridge.reset()
        self.save_tuning()
        self.status = "tuning reset"


    def center_gaze(self) -> None:
        sample = self.latest
        if sample is None or sample.gaze_valid != 1 or not math.isfinite(sample.gaze_x) or not math.isfinite(sample.gaze_y):
            return
        if self.gaze_affine is not None:
            self.smoothed_gaze_ratio = None
            self.gaze_sample_time = None
            self.gaze_trail.clear()
            return
        self.gaze_affine = None
        self.gaze_offset_x = 0.5 - sample.gaze_x
        self.gaze_offset_y = 0.5 - sample.gaze_y
        self.smoothed_gaze_ratio = (0.5, 0.5)
        self.gaze_trail.clear()
        self.status = "gaze centered"


    def reset_gaze_calibration(self, _event=None) -> None:
        self.clear_gaze_calibration(delete_saved=True)


    def clear_gaze_runtime_state(self) -> None:
        self.smoothed_gaze_ratio = None
        self.gaze_sample_time = None
        self.gaze_trail.clear()
        self.calibration_index = None
        self.calibration_points = []


    def clear_gaze_calibration(self, delete_saved: bool) -> None:
        self.gaze_affine = None
        self.gaze_offset_x = 0.0
        self.gaze_offset_y = 0.0
        self.clear_gaze_runtime_state()
        if delete_saved:
            delete_file_quietly(self.gaze_calibration_file)


    def start_gaze_calibration(self, _event=None) -> None:
        self.calibration_index = 0
        self.calibration_points = []
        self.smoothed_gaze_ratio = None
        self.gaze_sample_time = None
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
        self.gaze_sample_time = None
        self.gaze_trail.clear()
        save_gaze_calibration(self.gaze_calibration_file, affine)
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
            self.smoothed_gaze_ratio = None
            self.gaze_sample_time = None
            return
        self.gaze_sample_time = sample.monotonic_ns / 1e9
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


    def adjust_gain(self, axis: str, delta: float) -> None:
        attr = f"opentrack_{axis}_scale"
        current = tuning_multiplier(attr, getattr(self.args, attr))
        if abs(current) < 0.001 and delta < 0:
            updated = -0.25
        else:
            updated = current + delta
        setattr(self.args, attr, tuning_physical_value(attr, clamp(updated, -3.0, 3.0)))
        self.save_tuning()


    def set_capture_manual(self, value) -> None:
        self.capture_demand.policy.manual = value
        self.capture_retry_after = 0.0
        self.update_capture(time.monotonic())


    def start_capture(self) -> None:
        if self.proc is not None:
            return
        self.csv_path.unlink(missing_ok=True)
        (self.csv_path.parent / "events.csv").unlink(missing_ok=True)
        self.tail = CsvTail(self.csv_path)
        self.reset_baseline()
        self.latest = self.latest_mediapipe = None
        self.pose = self.raw_pose = None
        self.pose_source = "none"
        self.last_packet_monotonic = 0.0
        self.last_sample_index = -1
        self.packet_times.clear()
        self.samples.clear()
        self.mediapipe_bridge = MediaPipeFaceBridge(self.root_dir, self.args, self.csv_path.parent)
        if not self.mediapipe_bridge.start():
            error = self.mediapipe_bridge.last_error
            self.mediapipe_bridge = None
            raise RuntimeError(error)
        if self.args.hq_frames:
            self.hq_worker = HQFrameWorker(self.root_dir, self.args, self.csv_path.parent / "hq-frames")
            self.hq_worker.start()
        try:
            with self.log_path.open("a", encoding="utf-8") as log_file:
                self.proc = subprocess.Popen(self.sampler_cmd, cwd=self.root_dir,
                    stdout=subprocess.PIPE, stderr=log_file, start_new_session=True)
            write_runtime_pidfile(self.root_dir, "mux", self.proc.pid)
            self.mediapipe_bridge.attach_mux_stdout(self.proc.stdout)
        except BaseException:
            self.stop_capture()
            raise
        self.status = "starting capture"
        print("capture=started", flush=True)


    def stop_capture(self) -> None:
        proc, self.proc = self.proc, None
        if proc is not None:
            if proc.poll() is None:
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                    proc.wait(timeout=2.0)
                except ProcessLookupError:
                    pass
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait(timeout=1.0)
            if proc.stdout is not None:
                proc.stdout.close()
            pid_dir = Path(os.environ.get("SC_TOBII_NATIVE_RUNTIME_DIR", self.root_dir / ".tmp" / "sc-tobii-native-runtime")) / "pids"
            (pid_dir / "mux.pid").unlink(missing_ok=True)
            print("capture=stopped", flush=True)
        if self.mediapipe_bridge is not None:
            self.mediapipe_bridge.terminate()
            self.mediapipe_bridge = None
        if self.hq_worker is not None:
            self.hq_worker.terminate()
            self.hq_worker = None
        self.latest = self.latest_mediapipe = None
        self.pose = self.raw_pose = None
        self.pose_source = "none"
        self.reset_baseline()
        self.status = "capture idle"


    def update_capture(self, now: float) -> None:
        wanted = self.capture_demand.poll(now)
        if not wanted:
            if self.proc is not None:
                self.stop_capture()
            self.capture_error = ""
            self.status = "capture stopped manually" if self.capture_demand.policy.manual is False else "idle: waiting for client"
        elif self.proc is None and now >= self.capture_retry_after:
            try:
                self.start_capture()
                self.capture_error = ""
            except (OSError, RuntimeError) as exc:
                self.capture_error = str(exc)
                self.capture_retry_after = now + 3.0
                self.status = f"capture failed: {exc}"
                print(self.status, flush=True)


    def restart_sampler(self, reason: str, now: float) -> bool:
        if now - self.last_sampler_restart_monotonic < 2.0:
            return False
        self.stop_capture()
        self.start_capture()
        self.sampler_restart_count += 1
        self.last_sampler_restart_monotonic = now
        self.status = f"restarted sampler {reason}"
        return True


    def tick(self) -> None:
        now = time.monotonic()
        self.update_capture(now)
        if self.proc is None:
            return
        saw_eye_row = False
        saw_valid_eye_pose = False
        if self.mediapipe_bridge is not None:
            self.latest_mediapipe = self.mediapipe_bridge.snapshot()

        eye_rows = self.tail.read_new()
        for row_index, sample in enumerate(eye_rows):
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
                if self.baseline is not None and row_index == len(eye_rows) - 1:
                    self.raw_pose = derive_pose(sample, self.baseline, self.args.sensor_distance_mm)
                    self.latest_eye_pose = self.raw_pose
                    self.confidence = self.eye_confidence(sample, self.raw_pose, now)
                    self.observe_eye_translation(sample, self.raw_pose, now)
                    fused_pose, fused_source = self.fused_pose(self.raw_pose, self.confidence, now)
                    self.pose = self.smooth_runtime_pose(fused_pose, fused_source) if self.smoothing_enabled else fused_pose
                    self.pose_source = fused_source
                    self.last_pose_monotonic = self.pose_sample_time(fused_source)
                    self.pose_trail.append((self.pose.yaw, self.pose.pitch_proxy))
                    saw_valid_eye_pose = True
            elif self.baseline is not None and row_index == len(eye_rows) - 1:
                self.translation_observation_status = "invalid eyes"
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
            try:
                if not self.restart_sampler(f"rc={self.proc.returncode}", now):
                    self.status = f"sampler exited rc={self.proc.returncode}"
            except (OSError, RuntimeError) as exc:
                self.stop_capture()
                self.capture_retry_after = now + 3.0
                self.status = f"capture restart failed: {exc}"
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
            self.opentrack_bridge.send(
                self.pose, self.smoothed_gaze_ratio,
                sample_time=self.last_pose_monotonic, gaze_time=self.gaze_sample_time,
                context=self.tracking_context() if self.opentrack_bridge.trace is not None else None,
            )
            self.record_pitch_debug(now)



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
        if sample is not None and sample.usable:
            age = now - sample.seen_monotonic
            if age <= self.args.mediapipe_max_age_s:
                age_score = 1.0 - clamp(age / max(self.args.mediapipe_max_age_s, 0.001), 0.0, 1.0)
                return clamp(sample.confidence * max(age_score, 0.35), 0.0, 1.0)

        held_age = now - self.last_mediapipe_pose_monotonic
        if self.last_mediapipe_pose is not None and held_age <= self.args.mediapipe_pose_hold_s:
            hold_score = 1.0 - clamp(held_age / max(self.args.mediapipe_pose_hold_s, 0.001), 0.0, 1.0)
            return clamp(self.last_mediapipe_pose_confidence * max(hold_score, 0.35), 0.18, 0.75)
        return 0.0


    def fused_pose(self, eye_pose: EyePose, confidence: PoseConfidence, now: float) -> tuple[EyePose, str]:
        face_primary = self.args.tobii_head_pose_source == "face"
        face_pose = self.face_fallback_pose(use_handoff=not face_primary)
        face_conf = self.face_confidence(now)
        confidence.face = face_conf

        if self.args.tobii_head_pose_source == "eye":
            confidence.blended = confidence.eye
            self.face_handoff_active = False
            return eye_pose, "eye binocular"

        if face_primary:
            self.face_handoff_active = False
            if face_pose is not None and face_conf > 0.0:
                confidence.blended = clamp(max(face_conf, 0.35) * max(confidence.eye, 0.35), 0.0, 1.0)
                return face_pose, self.face_primary_source_name()
            if self.pose is not None:
                confidence.blended = 0.18
                return self.pose, self.face_primary_source_name()
            confidence.blended = confidence.eye
            return eye_pose, "eye binocular"

        if face_pose is None or face_conf <= 0.0:
            confidence.blended = confidence.eye
            self.face_handoff_active = False
            return eye_pose, "eye binocular"

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


    def pose_sample_time(self, source: str) -> float:
        if source.startswith("MediaPipe") and self.latest_mediapipe is not None:
            if self.latest_mediapipe.usable:
                native = getattr(self, "native_head_position", None)
                if native is not None and self.head_position_mode == "native" and native.last_eye_time is not None:
                    return max(self.latest_mediapipe.seen_monotonic, native.last_eye_time)
                return self.latest_mediapipe.seen_monotonic
            return self.last_mediapipe_pose_monotonic
        if self.latest is not None:
            return self.latest.monotonic_ns / 1e9
        return self.last_pose_monotonic


    def smooth_runtime_pose(self, pose: EyePose, source: str) -> EyePose:
        sample_time = self.pose_sample_time(source)
        if self.last_filter_sample_time is not None and sample_time <= self.last_filter_sample_time and source == self.last_filter_source:
            return self.pose if self.pose is not None else pose
        dt = 1.0 / 60.0 if self.last_filter_sample_time is None else max(0.0, sample_time - self.last_filter_sample_time)
        base_alpha = clamp(self.args.smoothing, 0.01, 1.0)
        alpha = min(base_alpha, self.args.transition_smoothing) if source != self.pose_source else base_alpha
        self.smoother.set_alpha(alpha)
        self.last_filter_sample_time = sample_time
        self.last_filter_source = source
        return self.smoother.update(pose, dt)


    def tracking_context(self) -> dict:
        face = self.latest_mediapipe
        return {
            "source": self.pose_source,
            "face": asdict(face) if face is not None else None,
            "neutral_ypr": [self.mediapipe_yaw_neutral, self.mediapipe_pitch_neutral, self.mediapipe_roll_neutral],
            "eye_pose": asdict(self.latest_eye_pose) if self.latest_eye_pose is not None else None,
            "translation": {
                "source": self.translation_source,
                "eye_sample_time": self.last_eye_translation_time,
                "trusted_position_mm": self.last_eye_translation,
                "observation_status": self.translation_observation_status,
                "latest_eye_sample_time": self.latest.monotonic_ns / 1e9 if self.latest is not None else None,
                "latest_eyes_valid": self.latest.valid if self.latest is not None else False,
                "eye_separation_mm": self.latest.eye_distance if self.latest is not None and self.latest.valid else None,
            },
            "native_head_position": self.native_head_position.snapshot() if self.native_head_position is not None else None,
            "head_position": self.head_position.snapshot() if self.head_position is not None else None,
            "neutral_relative_face_ypr": [
                angle_delta_deg(value, neutral) if neutral is not None else None
                for value, neutral in zip(
                    (face.yaw, face.pitch, face.roll) if face else (0.0, 0.0, 0.0),
                    (self.mediapipe_yaw_neutral, self.mediapipe_pitch_neutral, self.mediapipe_roll_neutral),
                )
            ],
            "filtered_pose": asdict(self.pose) if self.pose is not None else None,
            "pitch_mapping": {
                "yaw_compensation": self.args.mediapipe_pitch_yaw_comp,
                "curve": self.args.opentrack_pitch_curve,
                "gain": self.args.mediapipe_pitch_output_scale * self.args.opentrack_pitch_scale,
            },
        }


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
            if face_primary and self.pose is not None:
                self.pose_source = self.face_primary_source_name()
                self.confidence = PoseConfidence(eye=0.0, face=0.0, blended=0.18, pitch=0.0)
                self.pose_trail.append((self.pose.yaw, self.pose.pitch_proxy))
                return True
            return False
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
        self.last_pose_monotonic = self.pose_sample_time(source)
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
        self.latest_eye_pose = None
        self.last_eye_translation = (0.0, 0.0, 0.0)
        self.last_eye_translation_time = None
        self.translation_source = "neutral"
        self.translation_observation_status = "no measurement"
        if getattr(self, "native_head_position", None) is not None:
            self.native_head_position.recenter()
        if getattr(self, "head_position", None) is not None:
            self.head_position.recenter()
        self.last_filter_sample_time = None
        self.last_mediapipe_pose = None
        self.last_mediapipe_pose_monotonic = 0.0
        if self.opentrack_bridge is not None:
            self.opentrack_bridge.reset()
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
        if self.head_position is not None:
            self.head_position.update(sample.matrix, sample.seen_monotonic, time.monotonic())


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


    def observe_eye_translation(
        self, sample: EyeSample, pose: EyePose, now: float,
    ) -> None:
        # Keep the metric observation separate from raw_pose, which face fallback
        # overwrites. Never recycle a fallback pose as new eye-origin data.
        sample_time = sample.monotonic_ns / 1e9
        max_age = max(self.args.blink_hold_s, self.args.eye_fallback_after_s, self.args.mediapipe_max_age_s)
        if not sample.valid:
            self.translation_observation_status = "invalid eyes"
            return
        # Eye-distance stability is an orientation-confidence heuristic, not a
        # position-validity flag. In the lean capture it rejected valid moving
        # midpoints for over a second. Keep a broad geometry sanity check, but
        # do not require separation to stay within a few mm of startup.
        if not 20.0 < sample.eye_distance <= 100.0:
            self.translation_observation_status = "implausible eye separation"
            return
        if not 0.0 <= now - sample_time <= max_age:
            self.translation_observation_status = "stale eye sample"
            return
        if not all(math.isfinite(v) for v in (pose.tx, pose.ty, pose.tz)):
            self.translation_observation_status = "non-finite eye position"
            return
        if self.last_eye_translation_time is not None and sample_time <= self.last_eye_translation_time:
            self.translation_observation_status = "repeated eye sample"
            return
        self.last_eye_translation = (pose.tx, pose.ty, pose.tz)
        self.last_eye_translation_time = sample_time
        self.translation_observation_status = "accepted"


    def eye_origin_translation(self) -> tuple[float, float, float]:
        # A face bounding box changes with rotation and cannot supply metric
        # depth. Hold the last trusted binocular position during eye loss.
        if self.last_eye_translation_time is None:
            self.translation_source = "neutral"
        else:
            max_age = max(self.args.blink_hold_s, self.args.eye_fallback_after_s, self.args.mediapipe_max_age_s)
            fresh = 0.0 <= time.monotonic() - self.last_eye_translation_time <= max_age
            accepted = self.translation_observation_status == "accepted"
            self.translation_source = "eye origins" if fresh and accepted and self.latest is not None and self.latest.valid else "held eye origins"
        return self.last_eye_translation


    def head_translation(self) -> tuple[float, float, float]:
        native = self.eye_origin_translation()
        corrected = getattr(self, "native_head_position", None)
        if corrected is not None:
            now = time.monotonic()
            face = self.latest_mediapipe
            if face is not None and self.latest_mediapipe_usable():
                corrected.observe_orientation(face.matrix, face.seen_monotonic, now)
            eye = self.latest
            if eye is not None:
                corrected.update_eye(eye.left, eye.right, eye.monotonic_ns / 1e9, now, eye.valid)
            if self.head_position_mode == "native":
                self.translation_source = corrected.status
                return corrected.position
        estimator = getattr(self, "head_position", None)
        if estimator is None:
            return native
        sample = self.latest_mediapipe
        eye = self.latest
        eye_depth = None
        now = time.monotonic()
        if (eye is not None and eye.valid and 20 < eye.eye_distance <= 100
                and 0 <= now - eye.monotonic_ns / 1e9 < .1):
            eye_depth = eye.center[2]
        neutral = all(
            reference is not None and abs(angle_delta_deg(value, reference)) < 5.0
            for value, reference in zip((sample.yaw, sample.pitch, sample.roll),
                                       (self.mediapipe_yaw_neutral, self.mediapipe_pitch_neutral, self.mediapipe_roll_neutral))
        )
        candidate = estimator.update(sample.matrix, sample.seen_monotonic, now,
                                     eye_depth_mm=eye_depth, neutral_angles=neutral)
        if self.head_position_mode == "shadow":
            return native
        self.translation_source = "held face reference" if estimator.status.startswith("held:") else "MediaPipe head reference"
        return candidate


    def mediapipe_fallback_pose(self, use_handoff: bool = True) -> EyePose | None:
        now = time.monotonic()
        corrected_yaw = self.corrected_mediapipe_yaw()
        corrected_pitch = self.corrected_mediapipe_pitch()
        corrected_roll = self.corrected_mediapipe_roll()
        if corrected_yaw is None and corrected_pitch is None and corrected_roll is None:
            if (
                self.args.tobii_head_pose_source == "face"
                and self.last_mediapipe_pose is not None
                and now - self.last_mediapipe_pose_monotonic <= self.args.mediapipe_pose_hold_s
            ):
                self.pitch_debug_status = "mediapipe source hold"
                return self.last_mediapipe_pose
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
        tx, ty, tz = self.head_translation()
        pose = EyePose(
            yaw=corrected_yaw if corrected_yaw is not None else base.yaw,
            pitch_proxy=corrected_pitch if corrected_pitch is not None else base.pitch_proxy,
            roll=corrected_roll if corrected_roll is not None else base.roll,
            tx=tx,
            ty=ty,
            tz=tz,
            eye_distance=base.eye_distance,
            eye_distance_delta=base.eye_distance_delta,
        )
        self.last_mediapipe_pose = pose
        self.last_mediapipe_pose_monotonic = self.latest_mediapipe.seen_monotonic
        sample = self.latest_mediapipe
        self.last_mediapipe_pose_confidence = sample.confidence if sample is not None else 0.75
        return pose


    def sample_rate(self) -> float:
        if len(self.packet_times) < 2:
            return 0.0
        elapsed = self.packet_times[-1] - self.packet_times[0]
        return (len(self.packet_times) - 1) / elapsed if elapsed > 0 else 0.0


    def save_tuning(self) -> None:
        tuning_file = getattr(self.args, "tuning_file", None)
        if tuning_file is None:
            return
        data: dict[str, float | str] = {"tuning_version": 2}
        for attr in TUNING_ATTRS:
            if not hasattr(self.args, attr):
                continue
            value = getattr(self.args, attr)
            if attr in ("harness", "tobii_roll_source", "tobii_head_pose_source"):
                data[attr] = str(value)
            else:
                data[attr] = tuning_multiplier(attr, float(value))
        try:
            tuning_file.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = tuning_file.with_suffix(tuning_file.suffix + ".tmp")
            tmp_path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            tmp_path.replace(tuning_file)
        except OSError:
            pass


