"""Service configuration and capture command preparation."""
from tracking_core import *
from tracking_engine import TrackingEngine


def create_parser():
    parser = argparse.ArgumentParser(description="Background Tobii tracking engine and service configuration.")
    parser.add_argument("--control-socket", type=Path, default=Path(os.environ.get("SC_TOBII_CONTROL_SOCKET", repo_root() / ".tmp" / "sc-tobii-native-runtime" / "control.sock")))
    parser.add_argument("--windowed", action="store_true", default=True, help="Run in a window. This is the default.")
    parser.add_argument("--fullscreen", action="store_true", help="Run fullscreen.")
    parser.add_argument("--capture-demand-port", type=int, default=4244, help="Local UDP capture-demand listener; starts idle until a subscription or manual start.")
    parser.add_argument("--headless", action="store_true", help="Run the bridge without showing the dashboard window.")
    parser.add_argument("--head-position-mode", choices=("auto", "eye", "shadow", "face", "native", "native-shadow"), default="auto", help="Prefer validated native compensation; eye disables compensation; shadow records candidates without applying them.")
    parser.add_argument("--native-head-position-profile", type=Path, default=repo_root() / ".tmp" / "sc-tobii-native-runtime" / "native-head-position.json", help="Native eye/rotation reference profile.")
    parser.add_argument("--head-position-profile", type=Path, default=repo_root() / ".tmp" / "sc-tobii-native-runtime" / "head-position.json", help="Validated head-reference calibration profile.")
    parser.add_argument("--tracking-trace", type=Path, default=Path(os.environ["SC_TOBII_TRACKING_TRACE"]) if os.environ.get("SC_TOBII_TRACKING_TRACE") else None, help="Record numeric pose/gaze inputs and output packets to JSONL for offline replay.")
    parser.add_argument("--skip-build", action="store_true")
    parser.add_argument("--startup", choices=("native",), default="native")
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
    parser.add_argument("--screen-calibration-file", type=Path, help="Persist monitor/device physical screen calibration in this JSON file.")
    parser.add_argument("--gaze-calibration-file", type=Path, help="Persist multi-point gaze calibration in this JSON file.")
    parser.add_argument("--no-first-run-calibration", dest="first_run_calibration", action="store_false", help="Do not automatically prompt for missing screen/gaze calibration on startup.")
    parser.set_defaults(first_run_calibration=True)
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
    parser.add_argument("--mediapipe-pose-source", choices=("matrix", "landmark-normal", "hybrid"), default="hybrid", help="MediaPipe pose source. hybrid keeps matrix yaw/roll and continuously averages matrix and landmark-plane pitch.")
    parser.add_argument("--mediapipe-min-detection-confidence", type=float, default=0.35)
    parser.add_argument("--mediapipe-min-presence-confidence", type=float, default=0.35)
    parser.add_argument("--mediapipe-min-tracking-confidence", type=float, default=0.35)
    parser.add_argument("--mediapipe-min-face-confidence", type=float, default=0.35)
    parser.add_argument("--mediapipe-max-age-s", type=float, default=0.35)
    parser.add_argument("--mediapipe-pose-hold-s", type=float, default=0.55, help="Keep the last good MediaPipe pose through brief face-tracking gaps before falling back.")
    parser.add_argument("--mediapipe-yaw-sign", type=float, default=1.0)
    parser.add_argument("--mediapipe-pitch-sign", type=float, default=1.0)
    parser.add_argument("--mediapipe-roll-sign", type=float, default=1.0)
    parser.add_argument("--mediapipe-yaw-output-scale", type=float, default=DEFAULT_TUNING_VALUES["mediapipe_yaw_output_scale"], help="Scale MediaPipe yaw before Star Citizen gain/curve output.")
    parser.add_argument("--mediapipe-pitch-output-scale", type=float, default=DEFAULT_TUNING_VALUES["mediapipe_pitch_output_scale"], help="Scale MediaPipe pitch before Star Citizen gain/curve output.")
    parser.add_argument("--mediapipe-roll-output-scale", type=float, default=DEFAULT_TUNING_VALUES["mediapipe_roll_output_scale"], help="Scale MediaPipe roll before Star Citizen gain output.")
    parser.add_argument("--mediapipe-translation-output-scale", type=float, default=10.0, help="Deprecated compatibility option; face translation is no longer used.")
    parser.add_argument("--mediapipe-depth-output-scale", type=float, default=250.0, help="Deprecated compatibility option; face-size depth is no longer used.")
    parser.add_argument("--eye-origin-z-deadband-mm", type=float, default=1.0, help="Deprecated compatibility option; valid zero depth is preserved.")
    parser.add_argument("--mediapipe-pitch-yaw-comp", type=float, default=0.0, help="Optional experimental pitch amplification as yaw increases. Disabled by default.")
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
    parser.add_argument("--opentrack-yaw-scale", type=float, default=DEFAULT_TUNING_VALUES["opentrack_yaw_scale"])
    parser.add_argument("--opentrack-pitch-scale", type=float, default=DEFAULT_TUNING_VALUES["opentrack_pitch_scale"])
    parser.add_argument("--opentrack-roll-scale", type=float, default=DEFAULT_TUNING_VALUES["opentrack_roll_scale"])
    parser.add_argument("--opentrack-x-scale", type=float, default=DEFAULT_TUNING_VALUES["opentrack_x_scale"])
    parser.add_argument("--opentrack-y-scale", type=float, default=DEFAULT_TUNING_VALUES["opentrack_y_scale"])
    parser.add_argument("--opentrack-z-scale", type=float, default=DEFAULT_TUNING_VALUES["opentrack_z_scale"])
    parser.add_argument("--opentrack-output-smoothing", type=float, default=0.03, help="Stationary EMA alpha for values sent to TrackIR. Lower is smoother.")
    parser.add_argument("--opentrack-motion-smoothing", type=float, default=0.05, help="EMA alpha used during deliberate movement. Higher is more responsive.")
    parser.add_argument("--opentrack-prediction-ms", type=float, default=45.0, help="Output lookahead used to fill motion between sensor samples.")
    parser.add_argument("--opentrack-stillness-deadband-deg", type=float, default=0.18, help="Suppress angle jitter below this many output degrees when pose velocity is low.")
    parser.add_argument("--opentrack-stillness-velocity-dps", type=float, default=10.0, help="Velocity threshold for stationary angle jitter suppression.")
    parser.add_argument("--opentrack-send-hz", type=float, default=120.0, help="Fixed-rate UDP pose packet cadence.")
    parser.add_argument("--opentrack-max-angle-step", type=float, default=180.0, help="Emergency max degrees per 60 Hz frame sent to TrackIR.")
    parser.add_argument("--opentrack-max-output-angle", type=float, default=160.0, help="Final safety clamp for yaw/pitch/roll packets sent to the bridge.")
    parser.add_argument("--opentrack-max-translation-step", type=float, default=0.20, help="Deprecated compatibility option; translation now uses a time-based filter.")
    parser.add_argument("--opentrack-yaw-curve", type=float, default=DEFAULT_TUNING_VALUES["opentrack_yaw_curve"], help="Power curve for TrackIR yaw output. 1.0 is linear.")
    parser.add_argument("--opentrack-pitch-curve", type=float, default=DEFAULT_TUNING_VALUES["opentrack_pitch_curve"], help="Power curve for TrackIR pitch output. 1.0 is linear (default).")
    parser.add_argument("--opentrack-curve-knee-deg", type=float, default=18.0, help="Output angle where the power curve roughly matches linear response.")
    return parser


def prepare_engine(argv=None):
    parser = create_parser()
    args = parser.parse_args(argv)
    load_tuning(args)
    # Validate before acquiring USB or starting worker/UI processes.
    args.native_head_calibration = None
    if args.head_position_mode in ("auto", "native", "native-shadow", "shadow") and args.native_head_position_profile.exists():
        try:
            args.native_head_calibration = NativeHeadProfile.load(args.native_head_position_profile)
            if args.mediapipe_image_stream.lower().removeprefix("0x") != args.native_head_calibration.image_stream:
                raise ValueError("native profile uses a different image stream")
            if args.head_position_mode == "native":
                args.native_head_calibration.require_live()
            elif args.head_position_mode == "auto":
                args.head_position_mode = "native" if args.native_head_calibration.validation.get("live_approved") is True else "native-shadow"
        except (ValueError, TypeError, OSError) as exc:
            parser.error(f"invalid native head-position profile: {exc}")
    elif args.head_position_mode in ("native", "native-shadow"):
        parser.error(f"native compensation requires {args.native_head_position_profile}")
    args.head_position_calibration = None
    if args.head_position_mode in ("auto", "face", "shadow") and args.head_position_profile.exists():
        try:
            args.head_position_calibration = HeadPositionProfile.load(args.head_position_profile)
            if args.mediapipe_image_stream.lower().removeprefix("0x") != args.head_position_calibration.image_stream:
                raise ValueError("profile uses a different image stream")
            if args.head_position_mode == "face":
                args.head_position_calibration.require_live()
            elif args.head_position_mode == "auto" and args.head_position_calibration.validation.get("live_approved") is not True:
                args.head_position_mode = "shadow"
                print("head_position=shadow_only (profile lacks passing bidirectional validation)")
        except (ValueError, TypeError, OSError) as exc:
            parser.error(f"invalid head-position profile: {exc}")
    elif args.head_position_mode == "face":
        parser.error(f"calibrated face position requires {args.head_position_profile}")
    args.frame_face_tracking = True

    root_dir = repo_root()
    if args.hq_frames:
        build_hq_frame_worker(root_dir, args.skip_build)
    use_ttp_mux = True
    build_mux_sampler(root_dir, args.skip_build)
    sampler = root_dir / "build" / "tobii-ttp-mux"
    if not sampler.exists():
        print(f"sampler binary missing: {sampler}")
        raise RuntimeError(f"sampler binary missing: {sampler}")

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

    if use_ttp_mux:
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

    return TrackingEngine(args, csv_path, cmd, root_dir, log_path)
