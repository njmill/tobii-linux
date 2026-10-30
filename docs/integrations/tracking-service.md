# Background tracking service and dashboard client

The service owns capture, inference, head/eye geometry, filtering, game output,
calibration calculations, tuning, and persistence. The dashboard is an optional
client. Closing or force-closing it does not stop capture requested by a game.
No executable-name matching is used for device activation.

## Processes and ownership

```text
user-session autostart
  -> run-sc-tobii-native-runtime.sh service
       -> tracking-service.py
            -> TrackingEngine
                 -> tobii-ttp-mux, while capture is requested
                 -> MediaPipe worker, while processing camera frames
       -> TCP middleware compatibility process
       -> Wine SESP and discovery pipe processes

optional dashboard / landmark viewer
  <-> private Unix control socket <-> tracking-service.py
```

`tracking_engine.py` owns the live state and capture lifecycle.
`tracking_core.py` contains shared types, native/inference bridges, mapping,
filtering, calibration math, and persistence helpers. Neither imports Tk.
`runtime_config.py` loads service settings and validates profiles before capture.

`eye-pose-dashboard.py` renders service snapshots and submits commands.
`inband-landmark-dashboard.py` displays the service's cached camera frame and
landmarks; it never starts a USB reader or a second inference worker.

The supervisor owns the engine and compatibility processes. Compatibility
listeners run without a session-length timeout by default. A failed engine or
Wine provider restarts the stack. The user service owns its process group, so
stopping it also cleans up children. Capture still follows the tested
[subscription lifecycle](client-driven-capture.md).

## Install, start, and stop

`./install.sh` registers autostart, updates the existing Star Citizen launch
hook, and installs the dashboard shortcut. Use `--no-service` to skip autostart
registration. An on-demand dashboard launch can create/start the service without
enabling login autostart. Explicit registration enables future login startup; the next dashboard or
game launch also starts the service on demand.

With a working systemd user manager, the generated unit is:

```text
~/.config/systemd/user/tobii-linux.service
```

It is attached to the graphical session because Wine discovery/display binding
uses that session, although the tracking engine itself runs without a display.
On systems without a user manager, an XDG desktop autostart entry and detached
supervisor provide the same service/client ownership model.

```bash
make install-service  # register/enable login autostart
make headless         # ensure the service is running, without a dashboard
make dashboard        # start/attach the dashboard; closing it leaves the service
make services         # restart the service stack, including compatibility pipes
make stop             # stop the service and release the tracker
make status           # service state plus integration diagnostics
```

On systemd:

```bash
systemctl --user status tobii-linux.service
journalctl --user -u tobii-linux.service
```

Stopping the service does not disable future login autostart. Use
`systemctl --user disable --now tobii-linux.service` for that, or unregister it
with `scripts/app/manage-tracking-service.py uninstall`. `./uninstall.sh` stops
and unregisters the service before removing runtime files.

The existing Star Citizen hook runs after its `wineserver -k` and restarts the
service stack before waiting for readiness. This restores Wine's prefix-local
pipes after the launcher reset. It does not decide whether to activate hardware;
client subscriptions still make that decision. The configured Wine prefix and
runner must match the game's Wine environment.

## Settings and controls

Saved tuning, head-position profiles, gaze calibration, screen calibration, and
window geometry keep their existing paths under `.tmp/sc-tobii-native-runtime/`.
No migration of personal calibration is needed.

Gain changes are validated and persisted by the service. The dashboard only
converts slider positions into requests. All connected dashboards see the same
state. **Reset Tuning** restores the existing unity baseline.

**Start Capture** and **Stop Capture** set a service-wide manual override.
**Auto** returns to subscription-driven capture. An override survives dashboard
closure, but not a service restart. Disconnecting a dashboard does not itself
remove a game's request or a manual override.

Screen calibration sends monitor/marker input to the service, which computes and
saves the profile. Gaze calibration samples and fitting also run in the service.
Only one dashboard may own a gaze-calibration session; disconnecting that client
cancels the unfinished calibration without stopping game-requested capture.

Runtime environment overrides apply when the service starts. To apply an override
to an already-running service, restart it explicitly, for example:

```bash
SC_HEAD_POSITION_MODE=eye make services
```

Opening another dashboard alone does not restart the engine or apply a different
backend configuration. A systemd user manager retains imported runtime environment
values for its session; remove a temporary override with
`systemctl --user unset-environment SC_HEAD_POSITION_MODE` before restarting.

## Control protocol

The default socket is `.tmp/sc-tobii-native-runtime/control.sock`; override it with
`SC_TOBII_CONTROL_SOCKET`. Its permissions are `0600`. An exclusive instance lock
prevents a second service from replacing an active endpoint.

Requests and replies are bounded newline-delimited JSON with protocol version 1.
The API supports snapshots, health queries, and a whitelist of commands. The
service validates tuning ranges and screen input; clients cannot supply arbitrary
file paths or execute arbitrary methods. Non-finite values are not emitted as
invalid JSON. Camera previews are included only when a viewer requests them.

The UI transport runs on a worker thread, so capture startup/shutdown cannot block
Tk. It reconnects after service restarts. Commands with an uncertain result are
not replayed after a broken connection. Device/inference lifecycle does not depend
on the UI connection or its event loop.

## Verification

The previous dashboard-owned prototype passed the user's live Star Citizen test:
capture remained idle until launch and stopped after both normal exit and force
close. The service version preserves those subscription handlers.

Local service checks cover no-display engine operation, native packet mapping,
manual/automatic capture, tuning persistence, service-owned gaze/screen
calibration, dashboard close/crash, reconnect across service restart, producer
expiry, and process cleanup. An isolated supervisor test uses real Wine/TCP
compatibility processes with a fake USB reader, including provider failure and
shutdown. Tests and fixtures remain under ignored `.tmp/development/`.

The next hardware check is to launch Star Citizen without opening the dashboard,
open/close the dashboard during tracking, and confirm that game exit still releases
the device. The headless fixtures do not replace that live validation.
