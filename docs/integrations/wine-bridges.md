# Wine bridge discovery and recovery

The background service prepares Wine-visible Tobii SESP and discovery pipes
without editing game launch scripts. Client subscriptions still control capture;
discovering a Wine process never starts the USB reader or MediaPipe.

## Ownership and discovery

`run-sc-tobii-native-runtime.sh` supervises the tracking engine, TCP middleware,
and `scripts/runtime/wine_bridges.py`. The bridge manager owns the Wine provider
processes. A provider exit restarts that provider rather than the tracking engine.
Dashboard connections, manual controls, and calibration state survive recovery.

The configured `STAR_CITIZEN_PREFIX` is prepared in advance whenever it contains
an initialized Wine prefix (`system.reg`). `STAR_CITIZEN_WINE` selects its initial
runner; the existing launcher's `wine_path` remains a read-only fallback.

Every 250 ms, the manager checks this user's host Wine application processes in `/proc` for
their prefix and Wine installation. It also understands Proton's
`STEAM_COMPAT_DATA_PATH/pfx`. It finds the installation's public `bin/wine` or
`bin/wine64`, rather than invoking an internal Wine ELF loader directly. Runner
environment settings, including esync/fsync and library paths, follow the detected
process. A runner/environment change replaces that prefix's bridge group.

There is no game executable-name allowlist, catalogue, or recursive game-directory
scan. Passive compatibility pipes are prepared for active host Wine prefixes,
whether or not their application ultimately uses Tobii. Only a real Tobii client
request activates the hardware. Processes launched by the bridge manager are
marked and excluded from discovery, so its own Wine processes do not keep a
discovered prefix alive. Discovered prefixes are released after five seconds
without an external Wine application process. Wine servers and generic Wine background
services are excluded, so the bridge cannot keep itself alive through them.
The explicitly configured prefix stays prepared.

Set `SC_TOBII_WINE_DISCOVERY=0` before restarting the service to restrict it to the
configured prefix. This is also useful for isolated integration tests.

## Recovery and pose delivery

Provider exits are checked at roughly 50 ms intervals. Healthy providers restart
after a 100 ms delay; repeated immediate failures back off up to five seconds.
A provider that fails to become ready within 30 seconds is also restarted.
`wineserver -k` can close every provider in a prefix, and the manager recreates
them without restarting the tracking engine or touching other prefixes.

The manager receives the engine's existing loopback pose packets (default port
4243) and forwards them unchanged to a dedicated UDP receiver for each prefix.
This avoids multiple SESP providers competing for the same UDP socket. Gaze
continues through the shared TCP middleware. Per-provider subscription leases
continue to expire if a provider dies. Each managed provider has a unique demand
identity, even when Wine assigns the same Windows PID in different prefixes.

The manager terminates only its own provider processes, never a game's
`wineserver`. The service retains ownership of its children for normal shutdown.

## Upgrade and diagnostics

`install.sh` removes the old marked native-Tobii hook with a timestamped backup.
To migrate an existing checkout directly:

```bash
make remove-launch-hook
make services
```

The removal preserves launcher bytes outside the marked block and file permissions,
does nothing if the block is absent, and refuses malformed/unclosed markers.
It does not stop tracking. `make disable-launch-hook` remains a compatibility alias.

`make status` reports bridge readiness, prefixes, and runner paths. Current state
is written atomically to `.tmp/sc-tobii-native-runtime/wine-bridges.json`, with
provider PIDs, logs, UDP ports, and a freshness timestamp. Recovery events appear
in `wine-bridges.stdout`. The configured prefix retains the previous top-level
provider log names; discovered prefixes use `wine-prefixes/<prefix-hash>/`.
`scripts/app/wait-sc-tobii-runtime-ready.sh` checks live processes, fresh bridge
state, and the TCP listener rather than historical log messages.

## Limits and live validation

Discovery is limited to Wine processes visible to the host service, owned by the
same user and using the same filesystem root. Flatpak/container Wine processes
are skipped: host Wine cannot safely join their isolated server environment.
An initialized prefix and executable runner are required. There is no Steam
library inventory or automatic provisioning of prefixes that have never run.

Polling and Wine startup take time. Preparing the configured prefix minimizes
the initial discovery race, but a game that probes exactly once before a newly
discovered bridge is ready may still require a restart. Recovery is not a launch
barrier, and host integration must be tested with the actual game/launcher.

For Star Citizen, test a cold launch, a second launch, normal exit, and force-close
using the ordinary launcher after removing the hook. Confirm capture stays idle
before subscription, starts in-game, and stops afterward. Verify the correct
runner and ready providers in `make status`. In an isolated test prefix, a real
`wineserver -k` should change provider PIDs while preserving the engine instance
and dashboard connection. Never use a test reset against an unrelated live game.
