# Client-driven capture

The background service starts idle and keeps compatibility endpoints available.
Opening the dashboard only connects a UI client; it does not open the tracker or
launch MediaPipe inference.
Capture starts when a client requests a supported tracking stream. No executable
names or game-process allowlist are used.

The dashboard-owned prototype passed live Star Citizen tests: normal exit and
force-close both released capture. The logic now belongs to the background
[tracking service](tracking-service.md).

## Service live test

1. Start the service with `make headless`, or use its login autostart. Leave
   capture in **Auto** mode; the dashboard is optional.
2. Confirm the status says `idle: waiting for client` and the tracker is inactive.
3. Launch Star Citizen normally. A head-pose start or native stream subscription
   should start capture, collect a fresh neutral baseline, and deliver tracking
   using the saved tuning and personal profile.
4. Open and close the dashboard during tracking; capture should continue. Then
   close the game. Capture should stop after all supported subscriptions are
   released or their command connections close.
5. Check both the dashboard's capture state and the physical tracker lights.
   Software lifecycle tests do not establish that the device illumination has
   switched off correctly on this hardware.

**Start Capture** forces capture on for calibration or troubleshooting.
**Stop Capture** forces it off, even with subscribed clients. **Auto** restores
client-driven behavior. Overrides belong to the service until Auto is selected or the service restarts;
they are not saved in tuning. Closing the dashboard does not stop capture.

A normal stop has a one-second grace period to avoid repeated start/stop during
brief reconnects. If a compatibility process disappears without reporting zero
clients, its demand expires after two seconds, followed by the grace period.
The UI counts requesting connections across transports, not distinct games.

## Protocol triggers

| Transport | Activate | Release |
| --- | --- | --- |
| SESP named-pipe commands | Head-pose start, wrapper type `4` | Head-pose stop, wrapper type `6`, or command connection closes |
| TCP middleware | Request `0x04c4` subscribing to `0x0500` gaze or `0x0504` presence | Request `0x04ce` for that stream, disconnect, or idle connection cleanup |

SESP initialize, discovery, display queries, and feature updates do not request
capture. This first test handles SESP head pose and the existing TCP gaze/presence
path; other SESP stream families are not used as activation triggers.
Repeated starts are idempotent per connection. One client's stop cannot cancel
another client's request, and unsubscribing one TCP stream leaves its other
subscriptions active. TCP requests are reassembled across reads before dispatch.

The SESP start/stop type mapping was checked against local decompiled serializers
and exercised through an isolated Wine named-pipe fixture. The implementation
reuses the public compatibility protocol; proprietary binaries and raw decompiler
output remain outside the repository.

## Implementation

- `scripts/runtime/capture_control.py`: loopback demand protocol, source leases,
  sequence checks, manual override policy, and receiver.
- `scripts/runtime/tracking_engine.py`: capture process lifecycle and tracking.
- `scripts/runtime/tracking-service.py`: engine owner and local control API.
- `scripts/runtime/eye-pose-dashboard.py`: remote controls and presentation.
- `scripts/app/tobii-middleware-spy.py`: per-connection TCP subscriptions.
- `tools/app/tobii-middleware-pipe-spy.c`: per-command-connection SESP head-pose
  requests and disconnect cleanup. Registration state is local to each thread.
- `scripts/app/run-sc-tobii-native-runtime.sh`: connects both producers to the
  engine and restarts the service stack after a Wine reset.

Both compatibility processes publish their current requesting-connection count
every 200 ms, including zero. Messages use IPv4 loopback UDP port `4244` by
default. `SC_TOBII_CAPTURE_DEMAND_PORT` changes the port for all supervised
components; the standalone tracking service accepts `--capture-demand-port`.

Each producer has a unique instance identifier and increasing sequence number.
The receiver rejects malformed and reordered snapshots and expires stale demand.
Late-starting engines discover already-active requests from periodic snapshots.
Only one tracking engine can own the demand listener; a duplicate listener fails rather
than dividing requests between processes.

The Tk-free service owns the engine; dashboards and the landmark viewer consume
its snapshots. See [service architecture](tracking-service.md) for startup and
shutdown behavior.

## Verification and limits

Local checks cover idle startup, manual controls, subscription-driven start/stop,
multiple requesting transports/clients, repeated requests, fragmented/coalesced
TCP messages, explicit stop/unsubscribe, abrupt command disconnect, producer
expiry, and sampler restart. A GUI smoke test uses a fake USB sampler plus real
MediaPipe inference to check process cleanup without opening the device. A Wine
fixture exercises actual named pipes and SESP replies. These fixtures and tests
remain in ignored `.tmp/development/`.

The final check is the live Star Citizen launch/exit sequence and hardware
shutdown. In particular, a client that retains a presence subscription still
requests capture even if it releases gaze or head pose.
