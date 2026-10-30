# Star Citizen Stock Tobii Runtime Architecture

## Purpose

This document describes the current working Star Citizen Tobii integration path.
The goal is to let Star Citizen use its bundled, stock
`tobii_gameintegration_x64.dll` while all hardware access and pose solving stays
native on Linux.

The important property of this architecture is that Star Citizen game files are
left untouched. The Linux side emulates the Tobii middleware and provider
services that the stock DLL expects to discover.

See also:

- [Stock runtime diagram](../diagrams/star-citizen-stock-tobii-runtime.puml)
- [Background service and dashboard](tracking-service.md)
- [Client-driven capture lifecycle](client-driven-capture.md)
- [Component inventory](component-inventory.md)
- [Native eye position and rotation compensation](native-head-position.md)
- [Stock DLL discovery notes](../recon/star-citizen-stock-dll-discovery.md)

## High-Level Data Flow

```text
Tobii ET5 USB
  -> tobii-ttp-mux
  -> tracking-service.py + MediaPipe
  -> UDP pose/gaze packets
      -> Wine SESP pipe emulator for head pose
      -> TCP middleware emulator for gaze/presence/runtime discovery
  -> stock Star Citizen tobii_gameintegration_x64.dll
  -> Star Citizen native Tobii mode
```

## Components

### Tobii ET5 Hardware

The physical device appears as USB vendor/product `2104:0313`. The current
runtime uses the native Tobii TTP path on interface 0 rather than the Windows
Tobii service.

The useful streams for the current runtime are:

- Gaze and eye-origin data from the native gaze/TTP stream.
- In-band IR image frames, currently stream `0x050e`, used by MediaPipe.
- Timing/sync data such as stream `0x1771` where needed by the mux path.

The higher-resolution endpoint `0x82` work remains useful research, but the
current working runtime uses the gaze-safe in-band camera stream.

### `tobii-ttp-mux`

`tobii-ttp-mux` is the native Linux USB/TTP reader. It owns the device-side
protocol during runtime and provides a combined stream of gaze rows and in-band
IR frames to the tracking engine.

The service starts it only while capture is requested. Users normally do not run the mux
by hand for Star Citizen testing.

### `tracking-service.py` and `tracking_engine.py`

The background service owns capture, MediaPipe, pose solving, calibration,
filtering, settings, and both game-facing output paths. It starts idle and is
independent of the dashboard. `tracking_core.py` provides shared runtime helpers
without a Tk dependency.

The service loads the existing personal profiles and tuning under
`.tmp/sc-tobii-native-runtime/`, and sends live UDP pose/gaze packets to the
compatibility services. It publishes status and accepts validated commands over
its private Unix socket.

### `eye-pose-dashboard.py`

The optional dashboard renders snapshots and sends capture, tuning, recenter,
and calibration commands. Calibration sampling/calculation and persistence stay
in the service. Closing or force-closing this client does not stop tracking for
an active game. The landmark viewer is also a passive service client.

With a validated personal profile, `native_head_position.py` combines binocular
Tobii eye origins with time-matched MediaPipe rotation to compensate for the eye
midpoint's rotation arc. It holds the whole corrected position when a valid pair
is unavailable. Without a profile, translation follows native eye origins.
MediaPipe supplies head rotation; its estimated translation is not used by the
native estimator. See [native head position](native-head-position.md) for the
calculation, validation results, and current limitations.

`pose_runtime.py` supplies time-based filtering and trace recording. Gain sliders
use the tested movement baseline at `1.00`; legacy saved physical gains are
supported through versioned tuning conversion. Research tools, tests, and local
captures live under ignored `.tmp/development/` or other `.tmp/` directories.

Capture starts idle and is controlled by supported SESP head-pose requests and
TCP gaze/presence subscriptions. Compatibility endpoints stay available while
capture is stopped. A loopback demand listener aggregates requests; the engine
starts/stops its USB reader and inference workers. Manual Start/Stop override this
policy until Auto is selected. See [capture lifecycle](client-driven-capture.md).

The runtime packet is a little-endian `double[12]`:

```text
0  x translation
1  y translation
2  z translation
3  yaw
4  pitch
5  roll
6  source code
7  packet counter
8  gaze x
9  gaze y
10 gaze valid
11 packet counter
```

The same packet shape is sent to both runtime UDP targets:

- `127.0.0.1:4243` for SESP head pose.
- `127.0.0.1:4457` for TCP middleware live gaze.

Head pose and gaze are intentionally handled separately. Head pose can be
smoothed, curved, and scaled for comfortable cockpit motion. Gaze should remain
responsive so Star Citizen target-under-gaze behavior works.

### MediaPipe Face Landmarker

MediaPipe is the current face landmark backend. It is run from a separate Python
3.11/3.12 virtual environment because MediaPipe wheels are not available for the
system Python 3.13 environment used on this machine.

The engine sends preprocessed 280x280 IR frames to the MediaPipe worker and
receives:

- 478 face landmarks.
- Face pose estimates.
- Confidence and latency information.

MediaPipe is the face tracking runtime because it is stable on the Tobii
in-band IR frames. Eye-origin data remains useful for gaze, metric anchoring,
and fallback diagnostics.

### Runtime Supervisor

`scripts/app/run-sc-tobii-native-runtime.sh` starts the stock-DLL runtime stack.

Common Make targets:

- `make runtime` or `make sc-tobii-stock-runtime`
  Ensures the background service is running and opens the dashboard client.
- `make services` or `make sc-tobii-stock-runtime-services`
  Restarts the tracking and compatibility service stack.
- `make dashboard` or `make sc-tobii-stock-runtime-dashboard`
  Connects the optional dashboard to the service.
- `make status` or `make sc-tobii-stock-runtime-status`
  Runs the log analyzer.

The supervisor also checks that Star Citizen's stock DLL is present. The stock
path should not install a replacement DLL into `Bin64`.

### TCP Middleware Emulator

`scripts/app/tobii-middleware-spy.py` listens on `127.0.0.1:4455` and emulates
the Tobii middleware protocol used by the stock game DLL.

It handles:

- Initial hello/query/discovery objects.
- Device info and model metadata.
- Capabilities and stream catalog.
- Display area and session metadata.
- Runtime metadata objects `0x0672` and `0x0c62`.
- Stream subscriptions, especially:
  - `0x0504` user presence.
  - `0x0500` gaze.

It receives live service gaze packets on UDP `127.0.0.1:4457`. Once the stock
DLL subscribes to gaze, the middleware emits async `0x0500` gaze events using
the latest service gaze data.

Synthetic presence is still used as an availability signal. Synthetic gaze is
not the desired runtime mode for testing; live service gaze should be present
and valid.

### SESP Pipe Emulator

`tools/app/tobii-middleware-pipe-spy.c` builds
`.tmp/sc-tobii-stock-recon/tobii-middleware-pipe-spy.exe`, which runs under the
Star Citizen Wine prefix.

It emulates the SESP provider pipe used by the stock DLL for head pose and
provider/display status. It handles:

- Pipe attach.
- Initialize.
- Display info.
- Status/session metadata and feature updates.
- Live head pose events.

It receives service pose packets on UDP `127.0.0.1:4243` and converts them
into the SESP stream expected by the stock DLL.

### Discovery Pipes

The stock DLL probes Tobii runtime discovery surfaces. The runtime starts marker
or discovery helpers for:

- `\\.\pipe\ETDefaultPIPE`
- `\\.\pipe\TOBII-127.0.0.1`
- `\\.\pipe\TOBIIPRP-IS5FF-100203612152`

These are not the main data path. They help the embedded Tobii runtime inside
the stock Star Citizen DLL decide that a local Tobii provider exists.

### Stock Star Citizen Tobii DLL

Star Citizen loads its bundled `tobii_gameintegration_x64.dll`. The game calls
the DLL's API, including `GetApi`, `TrackWindow`, `Update`, and pose/gaze
getters.

The current working observation is:

- The stock DLL can naturally discover the emulated runtime.
- The SC-owned session can reach runtime metadata.
- The SC-owned session can subscribe to presence and gaze streams.
- The stock DLL receives SESP head-pose events and TCP gaze events.

This is the key milestone that replaces the earlier hacked-DLL path.

### Star Citizen

Star Citizen should be configured with:

- Head tracking source: `Tobii`.
- Head tracking enabled.

With the stock runtime active, Star Citizen receives:

- Head pose through the stock DLL's SESP/provider path.
- Gaze point through the stock DLL's TCP middleware gaze stream.

This enables cockpit view movement and gaze target selection without modifying
the game DLL.

## Launch Order

The runtime services should exist before Star Citizen initializes its Tobii
path. The recommended path is:

```bash
make runtime
```

Then launch Star Citizen normally through the configured Wine prefix/launcher.

For split testing:

```bash
make services
make dashboard
```

The analyzer should be run after the game has initialized:

```bash
make status
```

## Healthy Runtime Status

A good status run should show:

- SC stock Tobii DLL loaded.
- SC headtracking source value `3` / `Tobii`.
- SC-owned TCP session reaches `0x0672` and `0x0c62`.
- SC-owned stream subscriptions include:
  - `0x0504/presence`
  - `0x0500/gaze`
- SESP pipe attach succeeds.
- SESP display-info and provider latch are seen.
- Live pose UDP packets are present.
- Live gaze UDP packets are present and valid.
- Stock-DLL live gaze events are valid.

If runtime/subscriptions are green but live pose or live gaze UDP is zero, the
stock DLL path is working and the issue is the tracking-engine producer path.

## Current Runtime Boundaries

This architecture deliberately avoids:

- Replacing `Bin64/tobii_gameintegration_x64.dll`.
- Injecting into the Star Citizen process.
- Emulating a kernel USB device.
- Depending on Windows Tobii Experience or GameHub during gameplay.

Diagnostic harnesses still exist for reverse engineering, but the normal
testing path is stock DLL plus middleware/SESP emulation.

## Important Files

- `tools/app/tobii-ttp-mux.c`
  Native USB/TTP gaze, eye-origin, and in-band frame reader.
- `scripts/runtime/tracking-service.py`
  Local control API and engine owner.
- `scripts/runtime/tracking_engine.py`
  Capture, pose solving, calibration, and output lifecycle.
- `scripts/runtime/native_head_position.py`
  Native eye-position normalization, rotation compensation, and dropout holds.
- `scripts/runtime/pose_runtime.py`
  Time-based packet filtering and optional trace recording.
- `scripts/runtime/eye-pose-dashboard.py`
  Optional dashboard client and calibration presentation.
- `scripts/app/run-sc-tobii-native-runtime.sh`
  Background supervisor for the tracking engine and stock-DLL compatibility processes.
- `scripts/app/tobii-middleware-spy.py`
  TCP middleware emulator and live gaze stream provider.
- `tools/app/tobii-middleware-pipe-spy.c`
  Wine SESP pipe emulator and live head-pose provider.
- `scripts/app/analyze-sc-tobii-runtime-logs.py`
  Runtime status and adoption analyzer.
- `docs/recon/star-citizen-stock-dll-discovery.md`
  Detailed reverse-engineering notes for stock DLL discovery/adoption.
