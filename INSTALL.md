# Install or update Tobii Linux

The current runtime targets Tobii Eye Tracker 5 on x86-64 Linux with Star Citizen
under Wine. The background service starts capture when the game's Tobii client
requests it and releases capture when those requests end. The dashboard is
optional during play.

## Install a release bundle

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
the dashboard shortcut, and the Star Citizen launch hook where detected. For a
manual setup or installer options, see [README](README.md).

If Star Citizen is not detected, specify its path, including quotes around spaces:

```bash
SC_BIN64="/path/to/StarCitizen/LIVE/Bin64" ./install.sh
```

For a custom runner/prefix, set `STAR_CITIZEN_PREFIX` and `STAR_CITIZEN_WINE` when
installing. The detected configuration is saved in
`~/.config/tobii-linux/runtime.env`. Wine compatibility pipes must use the same
prefix and runner as the game.

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

The helper stops the service, creates a private settings backup under
`.tmp/backups/runtime-state-*.tar.gz`, and reruns the installer. Saved tuning,
screen/gaze calibration, and head-position profiles remain in place. It does not
fetch source code or select a release for you. Installer options can be passed
through, for example `./update.sh --no-system-packages --no-udev`.

Open the dashboard or run `make headless` after updating to start the refreshed
service. Keep the previous release archive if you want to restore the old source.

Each backup contains `runtime/` settings, `config/runtime.env` when present, and
`backup.json` with their original paths and checksums. To restore settings, stop
the service, extract a backup into a temporary directory, and copy the selected
files back to the paths recorded in `backup.json`. Restart the service afterward.

## Status and troubleshooting

```bash
make diag       # setup, USB permissions, prefix and launch-hook checks
make status     # service state and game integration diagnostics
make headless   # start the service without a dashboard
make dashboard  # open the optional dashboard
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
