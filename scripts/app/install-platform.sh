#!/usr/bin/env bash
# Shared dependency definitions for source installation and removal.
detect_package_manager() {
  local os_id='' os_like=''
  if [[ -f /etc/os-release ]]; then
    os_id="$(. /etc/os-release; printf '%s' "${ID:-}")"
    os_like="$(. /etc/os-release; printf '%s' "${ID_LIKE:-}")"
  fi
  case " $os_id $os_like " in
    *debian*|*ubuntu*) echo apt ;;
    *fedora*|*rhel*) echo dnf ;;
    *arch*) echo pacman ;;
    *)
      if command -v apt-get >/dev/null; then echo apt
      elif command -v dnf >/dev/null; then echo dnf
      elif command -v pacman >/dev/null; then echo pacman
      else echo none; fi ;;
  esac
}

system_packages_for_pm() {
  case "$1" in
    apt) echo build-essential make pkg-config libusb-1.0-0-dev libssl-dev mingw-w64 wine python3 python3-tk python3-venv curl tar ca-certificates usbutils util-linux libxcb-cursor0 libxkbcommon-x11-0 libegl1 libgl1 libdbus-1-3 libxcb-icccm4 libxcb-image0 libxcb-keysyms1 libxcb-render-util0 libxcb-xinerama0 libx11-xcb1 xwayland ;;
    dnf) echo gcc gcc-c++ make pkgconf-pkg-config libusb1-devel openssl-devel mingw64-gcc wine python3 python3-tkinter python3-pip curl tar ca-certificates systemd-udev usbutils util-linux xcb-util-cursor libxkbcommon-x11 mesa-libEGL mesa-libGL dbus-libs xcb-util-wm xcb-util-image xcb-util-keysyms xcb-util-renderutil tk xorg-x11-server-Xwayland ;;
    pacman) echo base-devel pkgconf libusb openssl mingw-w64-gcc wine python tk curl tar ca-certificates usbutils util-linux xcb-util-cursor libxkbcommon-x11 libglvnd dbus xcb-util-wm xcb-util-image xcb-util-keysyms xcb-util-renderutil xorg-xwayland ;;
    *) return 1 ;;
  esac
}

run_privileged() {
  local command="${SUDO:-sudo}"
  if ! command -v "$command" >/dev/null 2>&1; then
    if command -v doas >/dev/null 2>&1; then command=doas
    else echo 'sudo/doas is needed for missing dependencies or USB permissions.' >&2; return 1; fi
  fi
  "$command" "$@"
}
