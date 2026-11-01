#!/usr/bin/env python3
"""Install/start the per-user runtime and launch its optional dashboard client."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'scripts/runtime'))
from service_protocol import RpcConnection, default_socket

UNIT='tobii-linux.service'
CONFIG=Path(os.environ.get('XDG_CONFIG_HOME',Path.home()/'.config'))
UNIT_PATH=CONFIG/'systemd/user'/UNIT
AUTOSTART=CONFIG/'autostart/tobii-linux-service.desktop'
TRAY_AUTOSTART=CONFIG/'autostart/tobii-linux-tray.desktop'
WORK=Path(os.environ.get('SC_TOBII_NATIVE_RUNTIME_DIR',ROOT/'.tmp/sc-tobii-native-runtime'))
if not WORK.is_absolute():WORK=ROOT/WORK


def systemd_available():
    try:
        return subprocess.run(['systemctl','--user','show-environment'],stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL).returncode==0
    except FileNotFoundError:return False


def systemctl(*args, check=True):
    return subprocess.run(['systemctl','--user',*args],check=check)


def ping():
    try:
        connection=RpcConnection(default_socket(),timeout=.3)
        try:return connection.request('ping')
        finally:connection.close()
    except (OSError,ValueError,ConnectionError):return None


def unit_quote(value):
    return '"'+str(value).replace('\\','\\\\').replace('"','\\"').replace('%','%%')+'"'


def unit_content():
    template=(ROOT/'configs/systemd/tobii-linux.service.in').read_text()
    return template.replace('@ROOT@',str(ROOT).replace('%','%%')).replace('@EXEC@',unit_quote(ROOT/'scripts/app/run-sc-tobii-native-runtime.sh'))


def desktop_quote(value):
    value=str(value).replace('\\','\\\\\\\\').replace('"','\\\\"').replace('`','\\\\`').replace('$','\\\\$').replace('%','%%')
    return '"'+value+'"'


def desktop_command(action):
    return ' '.join(desktop_quote(value) for value in
        (sys.executable,ROOT/'scripts/app/manage-tracking-service.py',action))


def tray_desktop_content():
    return ('[Desktop Entry]\nType=Application\nName=Tobii Tracking\n'
        'Comment=Tracking status, dashboard, and automatic startup\n'
        f'Exec={desktop_command("tray")}\nTerminal=false\nCategories=Utility;Game;\nIcon=input-gaming\n')


def write_autostart(path, name, action, enabled=True):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text('[Desktop Entry]\nType=Application\n'
        f'Name={name}\nExec={desktop_command(action)}\nTerminal=false\nNoDisplay=true\n'
        f'Hidden={str(not enabled).lower()}\n')


def desktop_enabled(path):
    if not path.is_file():return False
    import configparser
    config=configparser.ConfigParser(interpolation=None)
    try:
        config.read(path)
        return not config.getboolean('Desktop Entry','Hidden',fallback=False) and config.getboolean(
            'Desktop Entry','X-GNOME-Autostart-enabled',fallback=True)
    except (ValueError,configparser.Error):return False


def autostart_status():
    if systemd_available():
        service=systemctl('is-enabled','--quiet',UNIT,check=False).returncode==0
    else:service=desktop_enabled(AUTOSTART)
    tray=desktop_enabled(TRAY_AUTOSTART)
    return dict(enabled=service and tray,service=service,tray=tray)


def install(enable=True):
    if systemd_available():
        UNIT_PATH.parent.mkdir(parents=True,exist_ok=True)
        content=unit_content()
        if not UNIT_PATH.exists() or UNIT_PATH.read_text()!=content:
            UNIT_PATH.write_text(content)
            systemctl('daemon-reload')
        if enable:
            systemctl('enable',UNIT)
            AUTOSTART.unlink(missing_ok=True)
        print(f'Installed user service: {UNIT_PATH}')
    elif enable:
        write_autostart(AUTOSTART,'Tobii Tracking Service','start')
        print(f'Installed desktop-session autostart: {AUTOSTART}')
    if enable:
        write_autostart(TRAY_AUTOSTART,'Tobii Tracking','tray')


def set_autostart(enabled):
    if enabled:
        install(enable=True)
    else:
        if systemd_available() and UNIT_PATH.exists():systemctl('disable',UNIT)
        if not systemd_available() or AUTOSTART.exists():
            write_autostart(AUTOSTART,'Tobii Tracking Service','start',enabled=False)
        # A disabled XDG entry also remembers the choice on non-systemd desktops.
        write_autostart(TRAY_AUTOSTART,'Tobii Tracking','tray',enabled=False)


def install_preserving_autostart():
    registered=UNIT_PATH.exists() or AUTOSTART.exists() or TRAY_AUTOSTART.exists()
    enabled=autostart_status()['service'] if registered else True
    install(enable=enabled)
    if not enabled:set_autostart(False)


def stop_tray():
    pidfile=WORK/'pids/tray.pid'
    try:
        pid=int(pidfile.read_text())
        command=Path(f'/proc/{pid}/cmdline').read_bytes().split(b'\0')
        if os.fsencode(ROOT/'scripts/runtime/tracking-tray.py') in command:
            os.kill(pid,signal.SIGTERM)
    except (OSError,ValueError):pass


def import_session_environment():
    names=[key for key in os.environ if key in ('DISPLAY','WAYLAND_DISPLAY','XAUTHORITY','XDG_CURRENT_DESKTOP','SC_BIN64')
        or key.startswith(('SC_TOBII_','SC_TRACKIR_','SC_HEAD_','SC_NATIVE_HEAD_','SC_TUNING_','SC_WINDOW_','SC_SCREEN_','SC_GAZE_','STAR_CITIZEN_','MEDIAPIPE_'))]
    names=[key for key in names if key!='SC_TOBII_SERVICE_LOCK_HELD']
    if names:systemctl('import-environment',*names)


def stop():
    if systemd_available() and UNIT_PATH.exists():
        systemctl('stop',UNIT,check=False)
    pidfile=WORK/'pids/runtime-services.pid'
    if pidfile.exists():
        try:
            pid=int(pidfile.read_text())
            command=Path(f'/proc/{pid}/cmdline').read_bytes()
            if b'run-sc-tobii-native-runtime.sh' in command and b'service' in command:
                os.kill(pid,signal.SIGTERM)
                deadline=time.monotonic()+10
                while Path(f'/proc/{pid}').exists() and time.monotonic()<deadline:time.sleep(.05)
        except (OSError,ValueError):pass


def ensure_started(restart=False):
    WORK.mkdir(parents=True,exist_ok=True)
    with (WORK/'service-start.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        if not restart and ping():return
        if systemd_available():
            if not UNIT_PATH.exists() or UNIT_PATH.read_text()!=unit_content():
                install(enable=False)
                restart=True
            import_session_environment()
            systemctl('restart' if restart else 'start',UNIT)
        else:
            if restart:stop()
            if not ping():
                with (WORK/'tracking-service.stdout').open('a') as output:
                    subprocess.Popen([str(ROOT/'scripts/app/run-sc-tobii-native-runtime.sh'),'service'],
                        cwd=ROOT,stdin=subprocess.DEVNULL,stdout=output,stderr=subprocess.STDOUT,start_new_session=True)
        deadline=time.monotonic()+float(os.environ.get('SC_TOBII_READY_TIMEOUT','20'))
        while time.monotonic()<deadline:
            if ping():return
            time.sleep(.1)
        raise RuntimeError('tracking service did not become ready; check journalctl --user -u tobii-linux.service or tracking-service.stdout')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('install','uninstall','start','restart','stop','dashboard','status',
        'tray','enable-autostart','disable-autostart','autostart-status'))
    parser.add_argument('--preserve-autostart',action='store_true',
        help='Keep the existing startup preference when installing/updating')
    args=parser.parse_args()
    if args.action=='install':
        if args.preserve_autostart:install_preserving_autostart()
        else:install()
    elif args.action=='uninstall':
        stop_tray()
        stop()
        if systemd_available() and UNIT_PATH.exists():systemctl('disable',UNIT,check=False)
        UNIT_PATH.unlink(missing_ok=True);AUTOSTART.unlink(missing_ok=True);TRAY_AUTOSTART.unlink(missing_ok=True)
        if systemd_available():systemctl('daemon-reload')
    elif args.action=='enable-autostart':set_autostart(True)
    elif args.action=='disable-autostart':set_autostart(False)
    elif args.action=='autostart-status':print(json.dumps(autostart_status()))
    elif args.action=='tray':
        os.execv(sys.executable,[sys.executable,str(ROOT/'scripts/runtime/tracking-tray.py')])
    elif args.action=='stop':stop()
    elif args.action=='status':
        state=ping()
        if state:
            connection=RpcConnection(default_socket(),timeout=.5)
            try:snapshot=connection.request('snapshot')
            finally:connection.close()
            capture='active' if snapshot['capture_running'] else 'idle'
            print(f"tracking service: running pid={state['pid']} capture={capture} clients={snapshot['capture_clients']}")
        else:print('tracking service: not running')
        return 0 if state else 1
    else:
        ensure_started(restart=args.action=='restart')
        if args.action=='dashboard':
            os.execv(sys.executable,[sys.executable,str(ROOT/'scripts/runtime/eye-pose-dashboard.py'),'--socket',str(default_socket())])
        print('tracking service ready')
    return 0


if __name__=='__main__':
    try:raise SystemExit(main())
    except (RuntimeError,OSError,subprocess.CalledProcessError,ValueError) as exc:raise SystemExit(str(exc))
