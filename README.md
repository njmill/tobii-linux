# Tobii Linux

Linux user-space runtime for Tobii Eye Tracker 5 with Star Citizen support.

This public tree contains the current working runtime path:

- Native Linux USB/TTP reader for Tobii ET5 gaze and in-band IR frames.
- MediaPipe-based head pose in the live dashboard.
- Stock Star Citizen Tobii DLL compatibility services.
- Live head pose and gaze delivery into Star Citizen's native Tobii mode.

## Current Scope

This is an early public POC. It is focused on one working path:

```text
Tobii ET5 USB
  -> tobii-ttp-mux
  -> eye-pose-dashboard.py + MediaPipe
  -> Tobii middleware compatibility services
  -> stock Star Citizen Tobii DLL
  -> Star Citizen Tobii head tracking and gaze targeting
```

## Install With Installer

For the normal setup path, run:

```bash
./install.sh
```

The installer handles the setup work for you:

- Checks and installs distro packages where supported.
- Installs the Tobii udev rule for USB permissions.
- Prepares a MediaPipe-compatible Python environment.
- Downloads the MediaPipe face model.
- Builds the runtime helpers.
- Creates an application-menu entry named `Tobii Dashboard`.
- Searches common Star Citizen Wine/Lutris/Bottles/Heroic/Steam locations,
  saves the detected runtime path, and checks that its Tobii DLL is stock when
  possible.

Installer options:

```bash
./install.sh --preflight            # dry-run report; make no changes
./install.sh --no-system-packages   # manage distro packages yourself
./install.sh --no-udev              # skip USB permission rule install
./install.sh --no-desktop           # skip menu entry creation
./install.sh --no-sc-check          # skip Star Citizen stock DLL safety check
```

After installation, replug the Tobii device or reboot if device permissions do
not update immediately. Then open `Tobii Dashboard` from your application menu.

On first launch, the dashboard guides you through screen calibration and gaze
calibration.

Then launch Star Citizen normally. In-game, set head tracking source to `Tobii`
and enable head tracking.

If Star Citizen is installed in a custom location and the installer does not
detect it, set the `Bin64` path once and rerun the installer:

```bash
SC_BIN64="/path/to/StarCitizen/LIVE/Bin64" ./install.sh
```

The installer writes the detected path to:

```text
~/.config/tobii-linux/runtime.env
```

The menu launcher and `make runtime` load that file automatically.

To remove generated files and the application-menu entry:

```bash
./uninstall.sh
```

The uninstaller keeps distro packages installed by default. To remove the
installer-known packages too:

```bash
./uninstall.sh --remove-packages
```

To preview removal without changing anything:

```bash
./uninstall.sh --preflight
```

Advanced installer/runtime environment:

- `MEDIAPIPE_UV_VERSION` controls the pinned `uv` release used only when the
  installer needs to bootstrap a local Python for MediaPipe.
- `SC_BIN64=/path/to/StarCitizen/LIVE/Bin64` overrides Star Citizen detection.
- `STAR_CITIZEN_PREFIX=/path/to/wine-prefix` overrides the Wine prefix used for
  Wine-visible Tobii compatibility services.
- `SC_TOBII_PIPE_SUFFIX_SCAN=1` enables a diagnostic Wine named-pipe fallback
  used during protocol debugging. It is off by default for normal runtime use.

## Manual Install

Use this path if you prefer to manage dependencies yourself or if your distro is
not handled by `install.sh`.

Install system packages:

```bash
sudo apt install build-essential make pkg-config libusb-1.0-0-dev libssl-dev \
  mingw-w64 wine python3-tk curl
```

MediaPipe requires Python 3.11 or 3.12. If your distro Python is newer, use the
project-local Python setup:

```bash
make mediapipe-python
make mediapipe-venv
```

If you already have Python 3.11/3.12:

```bash
MEDIAPIPE_PYTHON=/path/to/python3.12 make mediapipe-venv
```

Install the udev rule:

```bash
make install-udev-rule
```

Then replug the Tobii device or reboot if needed.

Fetch the MediaPipe face model and build the runtime helpers:

```bash
make mediapipe-fetch-model
make all
```

Start the full runtime:

```bash
make runtime
```

Or start just the dashboard without the Star Citizen stock-DLL preflight:

```bash
make dashboard
```

## Runtime Status

After Star Citizen has initialized, check:

```bash
make status
```

A healthy run should show the stock Tobii DLL loaded, runtime metadata reached,
presence/gaze subscriptions, live gaze packets, SESP pipe attach, and live head
pose delivery.

## Useful Targets

- `make runtime` starts services plus dashboard.
- `make runtime-clean` clears saved tuning/window/calibration state, then starts the runtime for first-run testing.
- `make services` starts only the Wine-visible Tobii compatibility services.
- `make dashboard` starts only the Linux dashboard/data producer.
- `make status` analyzes runtime logs.
- `make clear-logs` clears runtime logs.
- `make preflight` verifies Star Citizen is using the stock Tobii DLL.
- `make install-launch-hook` installs the helper launch hook.
- `make disable-launch-hook` removes the helper launch hook.

## Documentation

- [Runtime architecture](docs/integrations/star-citizen-stock-tobii-runtime-architecture.md)
- [Stock runtime diagram](docs/diagrams/star-citizen-stock-tobii-runtime.puml)
- [Stock DLL discovery notes](docs/recon/star-citizen-stock-dll-discovery.md)

## Acknowledgements

Several open source projects and community experiments around Tobii Eye Tracker 5 protocol decoding were very helpful in getting this up and running. Those include:
- [tobiifree](https://github.com/Aetherall/tobiifree)
- [tobii_eye_tracker_linux_installer](https://github.com/megagtrwrath/tobii_eye_tracker_linux_installer)
- [opentrack](https://github.com/megagtrwrath/opentrack)
- [MediaPipe](https://github.com/google-ai-edge/mediapipe)
