# Star Citizen Stock DLL Discovery

This note tracks the current stock-DLL adoption path. The goal is to make the
unmodified `tobii_gameintegration_x64.dll` naturally adopt our Linux Tobii
runtime without seeding the DLL's internal provider table, forcing a provider
connect, modifying game files, or injecting into the Star Citizen process.

## Current Model

The direct proof path works:

- `TrackTracker("tet-tcp://127.0.0.1")` reaches runtime metadata.
- The TCP middleware emulator can deliver live gaze.
- The SESP pipe emulator can deliver live head pose.

The natural path is still the blocker:

- Star Citizen and the public harness `TrackWindow(hwnd)` complete TCP discovery.
- They stop after the basic device/display/session metadata sequence.
- They do not naturally advance to `0x0672`, `0x0c62`, or stream subscriptions.

The current hypothesis is that the TCP-discovered provider is not becoming a
monitor-bound, selectable provider entry. The next milestone is to inspect the
stock DLL's own unseeded provider table and align display-binding metadata until
`TrackWindow(hwnd)` selects a real provider index.

## Natural Table Dump

Start the runtime services/dashboard first, then run:

```bash
make sc-tobii-stock-natural-table-dump-kick
make sc-tobii-stock-runtime-status
```

This target uses the stock harness with:

- no provider-table seed,
- no direct `TrackTracker`,
- no forced provider select/connect,
- real `TrackWindow(hwnd)`,
- table dumps at `after_GetApi`, `after_discovery_wait`,
  `before_TrackWindow`, and `after_TrackWindow`.

The analyzer reports `natural discovery table` with one of the useful failure
classes:

- `no connector table dump`
- `no entries dumped`
- `inactive`
- `zero_rect`
- `non_overlapping_rect`
- `wrong_type`
- `missing_caps`
- `valid candidate`

The desired harness result is a valid candidate whose rectangle overlaps the
window rectangle, followed by runtime metadata and subscriptions without using
seeded or forced provider paths.

## Star Citizen Window Comparison

Once the natural harness path works, the remaining question is whether Star
Citizen passes a different Wine window/surface than the harness. Capture Wine's
view of the active SC windows while the game is running:

```bash
make sc-tobii-stock-window-probe
make sc-tobii-stock-runtime-status
```

The probe writes `.tmp/sc-tobii-native-runtime/wine-window-probe.log`; the
analyzer summarizes likely SC candidates as `Wine SC window candidates`.

To replay one candidate in the stock harness, convert the candidate rectangle
from `left,top,right,bottom` to `x,y,width,height` and pass it to the natural
table dump:

```bash
SC_TOBII_STOCK_TRACKWINDOW_RECT=0,0,3440,1440 \
  make sc-tobii-stock-natural-table-dump-kick
make sc-tobii-stock-runtime-status
```

If the harness fails with the SC-shaped rectangle, the selector/display binding
is still wrong. If the harness succeeds with the SC-shaped rectangle, focus on
the actual HWND/class/window hierarchy or SC's API-call ordering rather than the
provider protocol.

## Display Binding

The TCP middleware and SESP pipe now share display-binding controls:

```bash
SC_TOBII_DISPLAY_NAME='\\.\DISPLAY1'
SC_TOBII_DISPLAY_ID='DISPLAY\DEFAULT_MONITOR\0000&0000'
SC_TOBII_DISPLAY_X=0
SC_TOBII_DISPLAY_Y=0
SC_TOBII_DISPLAY_WIDTH=6000
SC_TOBII_DISPLAY_HEIGHT=1440
SC_TOBII_DISPLAY_AREA_MODE=dynamic
```

`SC_TOBII_DISPLAY_RECT=x,y,width,height` can override the split fields.

`SC_TOBII_DISPLAY_AREA_MODE=canned` restores the older canned `0x0596` payload
for regression comparisons.

## Windows Oracle Workflow

On a Windows VM or Windows host with the real Tobii runtime working, capture only
the metadata surfaces needed for display binding:

- TCP middleware traffic on `127.0.0.1:4455`,
- `\\.\pipe\streamengineservices`,
- `ETDefaultPIPE`,
- `TOBII-*` / `TOBIIPRP-*` discovery pipes if present.

Do not save raw camera/image payloads for this workflow.

Copy the capture/log files into an ignored local path, then run:

```bash
make sc-tobii-stock-oracle-parse ORACLE_INPUTS='.tmp/oracle/windows-stock-tobii.pcapng .tmp/oracle/*.log'
```

The parser writes:

- `captures/sc-tobii-oracle/<timestamp>/summary.json`
- `captures/sc-tobii-oracle/<timestamp>/summary.csv`

Compare Windows oracle fields against Linux emulator fields for:

- TCP `0x0596/display_area`,
- TCP `0x083e/session_metadata`,
- TCP `0x06a4/model_name`,
- TCP `0x0672` and `0x0c62`,
- SESP display-info/list-devices records,
- discovery pipe names and payloads.

If Windows and Linux differ, adjust the display-binding metadata first before
changing provider or subscription logic.

## Verification Target

With services already running:

```bash
make sc-tobii-stock-natural-adoption-check
```

This clears runtime logs, verifies the service sockets/pipes are present, runs
the natural table dump, and prints analyzer status.

Success criteria:

- natural table has an active provider entry,
- provider rect overlaps the harness window,
- selector returns a real index,
- runtime reaches `0x0672/0x0c62`,
- subscriptions reach `0x0504/0x0500`,
- no seed or forced provider connect was used.
