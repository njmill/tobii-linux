# Install or update Tobii Linux

The current runtime targets Tobii Eye Tracker 5 on x86-64 Linux with Star Citizen
under Wine. The background service starts capture when the game's Tobii client
requests it and releases capture when those requests end. The dashboard is
optional during play.

## Managed installation (recommended)

```bash
curl -fsSL https://raw.githubusercontent.com/njmill/tobii-linux-pub/main/bootstrap.sh | bash
```

Run in a normal user terminal. Sudo/doas is requested only for missing system
packages and changed USB permissions. The source is built locally; there is no
project .deb/.rpm package and no need to clone the repository.

The installer selects the latest release when one exists. A published
`tobii-linux-VERSION.tar.gz` with a matching `.sha256` is verified, including its
internal manifest. Releases without that source asset use the tag's resolved Git
commit. Before the first release, the default is the resolved `main` commit.
GitHub source snapshots are pinned to a full commit and fetched over HTTPS; they
have no separately published archive checksum/signature. Computed archive hashes
are recorded in the installation receipt. Optional `GITHUB_TOKEN` is used only for
GitHub API calls, for example when an API rate limit is reached.

The bootstrap needs Bash and curl (or wget for a downloaded bootstrap). It can
install missing Python 3 and CA certificates on supported distro families. Source
setup supplies a compatible private Python, MediaPipe environment, desktop
PySide6 environment, model, native builds, USB permission rule, menu entries, and
service/tray registration. It does not replace the system Python. A first
installation starts the service idle and opens the tray/dashboard if a graphical
session is available. Calibration itself is unchanged in this installer milestone.

### Locations and commands

```text
~/.local/share/tobii-linux/
  current -> versions/<installed-version>/
  previous -> versions/<previous-version>/
  versions/                  isolated source/build/Python environments
  state/runtime/             calibration, tuning, logs, control socket
  backups/                   private pre-activation settings/integration backups
  install.json               installed source/version receipt
~/.local/bin/tobii           command launcher
```

`XDG_DATA_HOME`, `XDG_CONFIG_HOME`, and `XDG_STATE_HOME` are respected. Use
`--install-dir PATH` to choose a different managed folder. Each version links its
runtime-state directory to the shared state folder, so settings survive switching
versions. Do not delete an active version: its virtual environments and service
registration refer to its physical path. Older and failed preparation directories
are retained for diagnosis; automatic cache pruning is not implemented yet.

```bash
~/.local/bin/tobii                 # open the dashboard
~/.local/bin/tobii tray
~/.local/bin/tobii status
~/.local/bin/tobii update           # same effect as rerunning the one-line installer
~/.local/bin/tobii rollback
~/.local/bin/tobii version
~/.local/bin/tobii uninstall
```

You can use `tobii` without the full path if `~/.local/bin` is on your PATH.
The installer reports the full command path and does not modify shell profiles.
The application-menu entries work immediately regardless of shell PATH.

### Updates, recovery, and migration

Updates download, validate, and prepare a separate version before stopping the
current service. Active capture causes the installer to ask you to close the game
or stop capture and rerun. At activation it closes this installation's Tobii
windows, backs up known tuning/calibration and integration files, changes the
`current` link, registers the prepared version, and checks service startup when
resuming a running service or starting a first installation. A stopped service
remains stopped on update. **Start at boot** is preserved.

If activation fails, the installer restores the prior code link, known settings,
service/menu files, and startup links, and attempts to resume the old service if
it was running. Installed OS dependencies and USB-rule changes are not rolled
back. This is process-level recovery, not a power-loss transaction. Keep manual
settings backups if changing formats across experimental versions.

`rollback` switches back to the retained previous prepared version while keeping
current personal settings. Only managed versions are rollback targets; the first
migration from a developer checkout retains that checkout untouched but does not
make it a managed rollback target.

For an existing checkout/source installation:

```bash
curl -fsSL https://raw.githubusercontent.com/njmill/tobii-linux-pub/main/bootstrap.sh | bash -s -- --migrate-from /path/to/checkout
```

This copies known tuning, window, screen/gaze, and head-position profile JSON
files into shared state and replaces service/menu registration. It leaves the old
checkout, captures, and research files intact. Existing custom profile environment
overrides should be reviewed before migration; arbitrary capture files are not
copied. Without an explicit migration path, an existing registered source install
is detected and installation stops rather than silently resetting its settings.

### Installer options

Append options using `| bash -s -- OPTIONS`:

```bash
# Inspect the plan (temporary bootstrap download only; no persistent changes).
curl -fsSL https://raw.githubusercontent.com/njmill/tobii-linux-pub/main/bootstrap.sh | bash -s -- --preflight

# Pick a published release or deliberately follow a specific Git reference.
# Replace TAG/REF with an actual repository tag/branch/commit.
bash bootstrap.sh --version TAG
bash bootstrap.sh --ref REF

# Prepare/register without starting processes, or supply dependencies yourself.
bash bootstrap.sh --no-start
bash bootstrap.sh --no-system-packages --no-udev
```

`--force` rebuilds an already current version. `--archive /path/to/source.tar.gz`
uses a local source bundle with its `.sha256` sidecar or an explicit `--sha256 HEX`.
Python packages and the face model still require network access unless cached.
`--rollback` needs no source download. `--repo OWNER/NAME` supports explicit forks;
`--help` lists all options. Concurrent installers for the same managed folder are
locked out.

### Compatibility scope

- Runtime: Tobii Eye Tracker 5, **x86-64 Linux with glibc 2.28 or newer**. ARM, musl/Alpine, macOS, and
  Windows are not supported by this runtime/installer.
- Automatic system dependencies: apt (Debian/Ubuntu family), dnf (Fedora family),
  and pacman (Arch family), using the distribution's existing repositories.
  Repo availability, Wine restrictions, and older distro libraries can still
  require manual provisioning. An Arch update does not silently perform a full
  OS upgrade; if package metadata is stale, update the OS normally and rerun.
- Immutable/OSTree systems: automatic package layering and reboot handling are
  not implemented. Provision host dependencies first and use
  `--no-system-packages`; USB permissions must still be addressed on the host.
- Desktops: XDG application/autostart integration, with systemd user services
  when available. KDE-specific scripting is not required. GNOME can use the
  dashboard and service without a tray extension; an icon needs a tray host.
  Tk uses X11/XWayland; dependency lists include XWayland for Wayland desktops.
- These are supported installation paths, not a claim of full live hardware
  validation on every distro/desktop combination. Current automated checks cover
  installer transactions and desktop fallback behavior; full distro tests remain
  important before calling any combination verified.

## Source checkout / manually extracted release

Keep the installed folder at a stable location: the service and shortcuts refer
to it. The example below uses `~/Applications/tobii-linux`. Replace `VERSION`
with the version in the downloaded archive's name.

```bash
cd ~/Downloads
sha256sum -c tobii-linux-VERSION.tar.gz.sha256
mkdir -p "$HOME/Applications/tobii-linux"
tar -xzf tobii-linux-VERSION.tar.gz --strip-components=1 \
  -C "$HOME/Applications/tobii-linux"
cd "$HOME/Applications/tobii-linux"
sha256sum -c MANIFEST.sha256
./install.sh --preflight
./install.sh
```

Run the installer as your normal user. It requests sudo for system packages and
USB permissions. Internet access is needed for Python dependencies and the
MediaPipe model; the source archive does not include them.

The installer supports apt, dnf, and pacman. It sets up build dependencies,
MediaPipe Python, the model, USB permissions, native helpers, service autostart,
the dashboard/tray shortcuts, a private PySide6 QtWidgets environment, and automatic Wine
bridge recovery. It removes any legacy
managed launch hook with a backup. For a
manual setup or installer options, see [README](README.md).

If Star Citizen is not detected, specify its path, including quotes around spaces:

```bash
SC_BIN64="/path/to/StarCitizen/LIVE/Bin64" ./install.sh
```

For a custom runner/prefix, set `STAR_CITIZEN_PREFIX` and `STAR_CITIZEN_WINE` when
installing. The detected configuration is saved in
`~/.config/tobii-linux/runtime.env`. Wine compatibility pipes must use the same
prefix and runner as the game. The configured prefix is prepared in advance;
active host Wine/Proton prefixes and their runners are also discovered without
game executable-name matching. See [bridge lifecycle](docs/integrations/wine-bridges.md).

## First setup and test

1. Replug the tracker after installing USB permissions if needed.
2. Open **Tobii Dashboard** from the application menu. It connects to the service
   and should show capture idle in **Auto** mode.
3. Use **Start Capture** to complete screen/gaze calibration and recenter while
   facing forward. Select **Auto** afterward to restore client-driven activation.
4. Launch Star Citizen, select **Tobii** as the head-tracking source, and enable
   head tracking. This project's tested camera baseline uses Input Mix at 1.00
   toward head contribution.
5. Confirm tracking starts. Close the dashboard while playing: tracking should
   continue. Exit the game and confirm capture stops.

You can subsequently launch the game without opening the dashboard. On systemd
systems the user service is `tobii-linux.service`; otherwise the installer uses a
normal desktop-session autostart entry.

Open **Tobii Tracking** from your application menu to show the tray. Its right-click
menu provides dashboard access, service Start/Stop, and **Start at boot**, which
controls service and tray startup at desktop login. Turning it off leaves the
current service running. **Quit tray** closes only the icon. The tray is optional;
capture still follows game subscriptions.

The package includes the tested default gains at 1.00. Personal screen/gaze and
native head-position profiles are not included. The dashboard provides screen
and gaze calibration. Rotation-compensated native translation additionally
requires a validated personal `native-head-position.json`; without that profile,
the runtime uses native eye-origin translation. See
[native head position](docs/integrations/native-head-position.md).

## Update an existing installation

Exit the game first. Extract the new release into the **same installation folder**
and run the update helper:

```bash
cd ~/Downloads
sha256sum -c tobii-linux-VERSION.tar.gz.sha256
tar -xzf tobii-linux-VERSION.tar.gz --strip-components=1 \
  -C "$HOME/Applications/tobii-linux"
cd "$HOME/Applications/tobii-linux"
sha256sum -c MANIFEST.sha256
./update.sh --preflight
./update.sh
```

For an existing Git checkout, update the source using your normal Git workflow,
then run `./update.sh` in that checkout.

The installer preserves your existing **Start at boot** choice on updates.

The helper stops the service, creates a private settings backup under
`.tmp/backups/runtime-state-*.tar.gz`, and reruns the installer. Saved tuning,
screen/gaze calibration, and head-position profiles remain in place. It does not
fetch source code or select a release for you. Installer options can be passed
through, for example `./update.sh --no-system-packages --no-udev`.

If updating the source without running the installer, run `make remove-launch-hook`
and `make services` once. The removal helper only removes the marked legacy block,
creates a launcher backup, and leaves the service running.

Open the dashboard or run `make headless` after updating to start the refreshed
service. Keep the previous release archive if you want to restore the old source.

Each backup contains `runtime/` settings, `config/runtime.env` when present, and
`backup.json` with their original paths and checksums. To restore settings, stop
the service, extract a backup into a temporary directory, and copy the selected
files back to the paths recorded in `backup.json`. Restart the service afterward.

## Status and troubleshooting

```bash
make diag       # setup, USB permissions, prefix and legacy-hook migration checks
make status     # service state and game integration diagnostics
make headless   # start the service without a dashboard
make dashboard  # open the optional dashboard
make tray       # open the tray and its Start at boot checkmark
make stop       # stop the service and release the tracker
```

Idle capture is normal when no client requests tracking. Check game integration
status after the game has initialized its Tobii client.

On systemd:

```bash
systemctl --user status tobii-linux.service
journalctl --user -u tobii-linux.service
```

Additional logs are under `.tmp/sc-tobii-native-runtime/` in the installed folder.
Dashboard startup logs are normally at `~/.local/state/tobii-linux/dashboard.log`.
See [service documentation](docs/integrations/tracking-service.md) for environment
overrides and non-systemd behavior.

## Uninstall

Preview the changes:

```bash
./uninstall.sh --preflight
```

Remove the service, shortcuts, and generated runtime while preserving personal
settings and the downloaded model for a reinstall:

```bash
./uninstall.sh --keep-user-data --keep-model
```

`./uninstall.sh` without those flags also removes the current runtime settings,
calibrations, and downloaded model. System packages are retained by default.

## Build a distributable archive

From the source tree:

```bash
make package
# Or choose a release label:
make package VERSION=0.1.0
```

Output goes under ignored `.tmp/releases/`. Each archive has a SHA-256 sidecar,
contains `MANIFEST.sha256` for its files, and records provenance in `RELEASE.json`.
The packager includes the current public working-tree source, including pending
changes, plus developer documentation. It excludes `.git`, personal settings,
research/test scratch files, captures, downloaded models, virtualenvs, and build
outputs. Native helpers are compiled by the installer on the destination machine.

`SOURCE_DATE_EPOCH` fixes archive timestamps for reproducible builds of the same
source, version label, and recorded provenance. Packaging itself does not install
anything or contact a server.
