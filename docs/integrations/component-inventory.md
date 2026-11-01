# Tobii Linux Component Inventory

This document lists the discrete components that make the current runtime work.
It is intended as a troubleshooting and onboarding reference: what each part is,
where it lives, what starts it, what it communicates with, and how it usually
fails.

For the full flow diagram, see
[Stock runtime diagram](../diagrams/star-citizen-stock-tobii-runtime.puml). For
the narrative architecture, see
[Star Citizen stock Tobii runtime architecture](star-citizen-stock-tobii-runtime-architecture.md).

## Physical Device

### Tobii Eye Tracker 5 / EyeChip

- **USB ID:** `2104:0313`
- **Role:** Physical Tobii camera/IR/gaze device.
- **Used by:** `tobii-ttp-mux`
- **Access path:** Native Linux `libusb`, not the Windows Tobii service.
- **Setup dependency:** udev rule at
  `/etc/udev/rules.d/99-tobii-eyetracker5.rules`

Common failure signs:

- `make diag` reports Tobii USB not visible.
- `make diag` reports libusb cannot open the device.
- Dashboard never gets gaze rows or IR frames.

## Native Linux Data Path

### `tools/app/tobii-ttp-mux.c`

- **Built artifact:** `build/tobii-ttp-mux`
- **Started by:** `tracking_engine.py`
- **Role:** Owns the native Tobii USB/TTP session and emits gaze/image data.
- **Inputs:** USB interface 0 from the Tobii ET5.
- **Outputs:**
  - Gaze and eye-origin rows.
  - In-band IR frames from stream `0x050e`.
  - Events/log rows used by diagnostics.

Common failure signs:

- `device_not_found vid=0x2104 pid=0x0313`
- Dashboard says waiting for gaze stream.
- `make diag` libusb check fails.

### `scripts/runtime/eye-pose-dashboard.py`

- **Started by:** `make runtime`, `make dashboard`, app-menu launcher.
- **Role:** Optional UI client of the background service.
- **Responsibilities:** Renders status, tuning controls, and calibration windows;
  sends validated commands over the private Unix socket. It does not own capture
  or inference and does not send game packets.
- **Lifetime:** Closing the window leaves the service and active tracking running.

### `scripts/runtime/tracking-service.py` and `tracking_engine.py`

- **Started by:** The user-session service through the runtime supervisor.
- **Role:** Own native USB capture, inference, pose solving, calibration, settings,
  filtering, and game-facing output. `tracking_core.py` holds shared helpers.
- **Input demand:** SESP head-pose start/stop and TCP stream subscriptions through
  `capture_control.py`; hardware remains idle when no client requests it.
- **Control API:** User-private Unix socket; see [service architecture](tracking-service.md).
- **UDP outputs:** `127.0.0.1:4243` for SESP head pose and `127.0.0.1:4457` for gaze.

Runtime UDP packet format is `double[12]`:

```text
0  x translation
1  y translation
2  z translation
3  yaw
4  pitch
5  roll
6  source code
7  pose packet counter
8  gaze x
9  gaze y
10 gaze valid
11 gaze packet counter
```

Common failure signs:

- Dashboard opens but pose/gaze values stay stale.
- `make status` shows `linux gaze producer=0`.
- `make status` shows `linux pose producer=0`.

### `scripts/runtime/mediapipe-face-worker.py`

- **Started by:** `tracking_engine.py`
- **Role:** Runs MediaPipe Face Landmarker as a service-owned worker.
- **Input:** Preprocessed in-band IR frames from the tracking engine.
- **Output:** Face landmarks, pose estimate, confidence, latency.
- **Environment:** `.venv/mediapipe` Python 3.11/3.12 virtual environment.
- **Model:** `assets/mediapipe/face_landmarker.task`

Common failure signs:

- Dashboard falls back away from face pose.
- MediaPipe status is missing or stale.
- `make diag` reports venv/model missing.

## Star Citizen Compatibility Services

### `scripts/app/run-sc-tobii-native-runtime.sh`

- **Started by:** `make runtime`, `make services`, `make headless`.
- **Role:** Runtime supervisor for the dashboard and Wine-visible services.
- **Responsibilities:**
  - Checks the stock Star Citizen Tobii DLL.
  - Starts TCP middleware emulator.
  - Starts SESP pipe emulator under the Star Citizen Wine prefix.
  - Starts discovery marker pipes under the Star Citizen Wine prefix.
  - Cleans stale helper processes and occupied ports.
- **Important ports:**
  - TCP `4455` for Tobii middleware protocol.
  - UDP `4457` for live gaze input to middleware.
  - UDP `4243` for live pose input to the per-prefix bridge relay.

Common failure signs:

- `make runtime` exits before dashboard opens.
- Port conflicts on `4243`, `4455`, or `4457`.
- `make status` shows TCP connected but SESP pipe attach is zero.

### `scripts/app/tobii-middleware-spy.py`

- **Started by:** `run-sc-tobii-native-runtime.sh`
- **Role:** Emulates the TCP Tobii middleware endpoint expected by the stock DLL.
- **Listens on:** `127.0.0.1:4455`
- **Receives live gaze on:** UDP `127.0.0.1:4457`
- **Talks to:** `tobii_gameintegration_x64.dll`
- **Handles:**
  - hello/query/device discovery
  - device info
  - capabilities
  - display area
  - runtime metadata
  - presence subscription `0x0504`
  - gaze subscription `0x0500`
  - live gaze events

Common failure signs:

- `make status` shows no TCP probe.
- TCP reaches discovery but not runtime metadata.
- Live dashboard gaze exists, but stock DLL live gaze delivery is zero.

### `tools/app/tobii-middleware-pipe-spy.c`

- **Built artifact:** `.tmp/sc-tobii-stock-recon/tobii-middleware-pipe-spy.exe`
- **Started by:** `wine_bridges.py` under each Wine prefix.
- **Role:** Emulates the Wine named-pipe SESP provider used for head pose.
- **Receives live pose on:** A dedicated loopback UDP port assigned by the bridge
  manager, which relays engine packets from `127.0.0.1:4243`.
- **Talks to:** `tobii_gameintegration_x64.dll`
- **Handles:**
  - SESP initialize
  - display info
  - provider status
  - feature updates
  - live head-pose events

Common failure signs:

- `make status` shows `sesp pipe attach=0`.
- `make status` shows live pose producer but no stock-DLL headpose delivery.
- `middleware-pipe-spy.log` shows UDP bind failure.

### Discovery Pipe Helpers

These are separate instances of `tobii-middleware-pipe-spy.exe` started under
Wine.

- `\\.\pipe\ETDefaultPIPE`
- `\\.\pipe\TOBII-127.0.0.1`
- `\\.\pipe\TOBIIPRP-IS5FF-100203612152`

Role:

- Advertise a local Tobii provider to the stock DLL's discovery path.
- They are not the main gaze or head-pose stream.

Common failure signs:

- `make status` shows discovery markers listening but zero accepts.
- Stock DLL performs early TCP probing but never fully adopts the provider.

## Star Citizen / Wine Side

### `tobii_gameintegration_x64.dll`

- **Location:** `StarCitizen/LIVE/Bin64/tobii_gameintegration_x64.dll`
- **Role:** Stock Tobii Game Integration DLL bundled with Star Citizen.
- **Owned by:** Star Citizen.
- **Important rule:** The public runtime should not replace this DLL.

It consumes:

- TCP middleware metadata/gaze from `tobii-middleware-spy.py`.
- SESP provider head pose from `tobii-middleware-pipe-spy.exe`.

Common failure signs:

- `make status` reports stock DLL load not found in game logs.
- TCP probe happens but no stream subscriptions occur.
- Gaze/head pose are produced by Linux, but no stock-DLL delivery is seen.

### `StarCitizen.exe`

- **Role:** Game process.
- **Expected setting:** Head tracking source set to `Tobii`.
- **Consumes:** Head pose and gaze through the stock Tobii DLL.

Common failure signs:

- `make status` reports `SC headtracking source` missing or not `Tobii`.
- Head/gaze works in dashboard but not in game.

### Desktop Tray

- **Script:** `scripts/runtime/tracking-tray.py` (system Python PySide6 QtWidgets).
- **Role:** Optional status icon and service controls; no hardware or inference ownership.
- **Menu:** Open dashboard, Start/Stop service, Start at boot, Quit tray.
- **Startup:** The checkmark controls service and tray startup at desktop login.
  Disabled preferences survive installer updates. Quitting the icon leaves the
  dashboard and service running. A per-runtime lock prevents duplicate icons.
- **Launch:** `make tray` or the **Tobii Tracking** application-menu entry.

### Wine Bridge Manager

- **Script:** `scripts/runtime/wine_bridges.py`
- **Role:** Prepares the configured prefix, discovers active host Wine/Proton
  prefixes/runners, and independently recovers SESP/discovery providers.
- **Activation:** Preparing pipes never activates capture; client subscriptions do.
- **Pose delivery:** Relays engine UDP packets to a separate receiver per prefix.
- **Diagnostics:** `wine-bridges.json`, `wine-bridges.stdout`, and per-prefix logs.
- **Legacy migration:** `make remove-launch-hook` removes only the marked legacy
  block with a backup. No hook installation or launcher modification is required
  for normal operation. See [bridge lifecycle](wine-bridges.md).

## Setup And Diagnostics

### `install.sh`

- **Role:** End-user setup path.
- **Responsibilities:**
  - Installs packages on supported distros.
  - Installs udev rule.
  - Creates MediaPipe environment.
  - Fetches MediaPipe model.
  - Builds helper binaries.
  - Detects Star Citizen paths.
  - Checks stock DLL.
  - Removes legacy managed launch hooks with a backup.
  - Runs `make diag` at the end.

### `uninstall.sh`

- **Role:** Removes project-installed user-facing artifacts.
- **Default behavior:** Does not remove distro packages.
- **Optional behavior:** `--remove-packages` removes installer-known packages.

### `scripts/app/tobii-diag.sh`

- **Make target:** `make diag`
- **Role:** Setup-focused diagnostic.
- **Checks:**
  - Tobii visible in `lsusb`.
  - udev rule installed and no `plugdev` dependency remains.
  - libusb can open the Tobii.
  - MediaPipe venv/model exist.
  - Star Citizen `Bin64` detected.
  - Stock Tobii DLL present.
  - Wine prefix and runner detected.
  - Launch hook installed or missing.

Use this when installation or first launch fails.

### `scripts/app/analyze-sc-tobii-runtime-logs.py`

- **Make target:** `make status`
- **Role:** Runtime-focused diagnostic after Star Citizen starts.
- **Reads:**
  - middleware logs
  - pipe logs
  - discovery logs
  - Star Citizen profile/log files
  - Wine bridge status and recovery logs
- **Reports:**
  - TCP discovery and subscriptions.
  - SESP pipe attach and provider state.
  - Live dashboard pose/gaze production.
  - Stock-DLL pose/gaze delivery.
  - Star Citizen Tobii settings.
  - Launch hook presence and execution.

Use this when the dashboard works but in-game tracking does not.

## Persistent Files

### User Config

- `~/.config/tobii-linux/runtime.env`

Stores detected Star Citizen paths and optional Wine overrides.

### Runtime State

Default directory:

```text
.tmp/sc-tobii-native-runtime/
```

Important files:

- `sc-tuning.json` - dashboard tuning sliders.
- `screen-calibration.json` - physical screen/Tobii marker calibration.
- `gaze-calibration.json` - gaze calibration.
- `sc-window.json` - dashboard window position/size.
- `middleware-spy.log` - TCP middleware log.
- `middleware-pipe-spy.log` - SESP pipe log.
- `wine-bridges.json` - current Wine prefixes, runner paths, provider PIDs and readiness.
- `wine-bridges.stdout` - bridge starts, exits and retries.

### Build Artifacts

- `build/tobii-ttp-mux`
- `.tmp/sc-tobii-stock-recon/tobii-middleware-pipe-spy.exe`
- `.tmp/sc-tobii-stock-recon/wine-window-probe.exe`

## Normal End-To-End Flow

```text
Tobii ET5
  -> tobii-ttp-mux
  -> tracking service (optional dashboard client)
      -> MediaPipe worker
      -> UDP pose 127.0.0.1:4243 -> per-prefix bridge relay
      -> UDP gaze 127.0.0.1:4457
  -> Wine-visible compatibility services
      -> TCP middleware 127.0.0.1:4455
      -> SESP named pipe
      -> discovery marker pipes
  -> stock tobii_gameintegration_x64.dll
  -> Star Citizen Tobii mode
```

## Diagnostic Split

Use `make diag` for setup problems:

- USB permissions.
- Missing dependencies.
- Missing MediaPipe files.
- Star Citizen path detection.
- Launch hook installation.

Use `make status` for runtime problems:

- SC did not load the stock DLL.
- SC did not select Tobii.
- TCP discovery stopped early.
- SESP pipe did not attach.
- Dashboard is not sending live pose/gaze.
- Stock DLL is not receiving live pose/gaze.
