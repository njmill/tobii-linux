# Tobii Linux

Linux user-space runtime for Tobii Eye Tracker 5 with Star Citizen support.

This public tree contains the current working runtime path:

- Native Linux USB/TTP reader for Tobii ET5 gaze and in-band IR frames.
- MediaPipe-based head pose in the live dashboard.
- Stock Star Citizen Tobii DLL compatibility services.
- Live head pose and gaze delivery into Star Citizen's native Tobii mode.

The important bit: the normal runtime does not replace
`tobii_gameintegration_x64.dll` in Star Citizen and does not inject into the
game process.

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

The research repository contains many exploratory probes and reverse
engineering artifacts. This repository intentionally keeps only the runtime and
the docs needed to run, debug, and polish the current system.

## Requirements

System packages:

```bash
sudo apt install build-essential make pkg-config libusb-1.0-0-dev libssl-dev \
  mingw-w64 wine python3-tk curl
```

MediaPipe requires Python 3.11 or 3.12. If your distro Python is newer, use:

```bash
make mediapipe-python
make mediapipe-venv
```

If you already have Python 3.11/3.12:

```bash
MEDIAPIPE_PYTHON=/path/to/python3.12 make mediapipe-venv
```

## Device Permissions

Install the udev rule:

```bash
make install-udev-rule
```

Then replug the Tobii device or reboot if needed.

## First Run

Fetch the MediaPipe face model and build the runtime helpers:

```bash
make mediapipe-fetch-model
make all
```

Start the full runtime:

```bash
make runtime
```

Then launch Star Citizen normally. In-game, set head tracking source to
`Tobii` and enable head tracking.

Check status after the game has initialized:

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
