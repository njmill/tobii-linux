#!/usr/bin/env python3
"""Optional desktop tray client for the Tobii tracking service."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys

try:
    from PySide6.QtCore import QLockFile, QProcess, QTimer, Qt
    from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap
    from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon
except ImportError as error:
    raise SystemExit('The tray requires system Python PySide6 QtWidgets. '
        'Run ./install.sh to install its desktop dependencies.') from error

from service_protocol import ServiceClient, default_socket

ROOT = Path(__file__).resolve().parents[2]
MANAGER = ROOT / 'scripts/app/manage-tracking-service.py'


def eye_icon(state):
    color = QColor({'tracking': '#46d97d', 'idle': '#91a0b2', 'stopped': '#dc7c72'}[state])
    pixmap = QPixmap(64, 64)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(QPen(color, 5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
    path = QPainterPath()
    path.moveTo(5, 32)
    path.cubicTo(19, 10, 45, 10, 59, 32)
    path.cubicTo(45, 54, 19, 54, 5, 32)
    painter.drawPath(path)
    if state == 'tracking':
        painter.setBrush(color)
        painter.drawEllipse(24, 24, 16, 16)
    else:
        painter.drawLine(23, 32, 41, 32)
    painter.end()
    return QIcon(pixmap)


class TrackingTray:
    def __init__(self, app, path=None):
        self.app = app
        self.client = ServiceClient(path or default_socket())
        self.startup_enabled = False
        self.busy = False
        self.icon_state = None
        self.operation = QProcess(app)
        self.operation.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        self.operation.finished.connect(self.command_finished)
        self.operation.errorOccurred.connect(self.command_error)
        self.completion = None
        self.command_name = ''
        self.dashboard = None
        log_dir = Path(os.environ.get('XDG_STATE_HOME', Path.home() / '.local/state')) / 'tobii-linux'
        log_dir.mkdir(parents=True, exist_ok=True)
        self.dashboard_log = log_dir / 'dashboard.log'
        self.tray = QSystemTrayIcon(app)
        self.menu = QMenu()
        self.status_action = self.menu.addAction('Service connecting…')
        self.status_action.setEnabled(False)
        self.menu.addSeparator()
        self.menu.addAction('Open dashboard', self.open_dashboard)
        self.start_action = self.menu.addAction('Start service', lambda: self.run_command('start'))
        self.stop_action = self.menu.addAction('Stop service', lambda: self.run_command('stop'))
        self.menu.addSeparator()
        self.startup_action = self.menu.addAction('Start at boot')
        self.startup_action.setCheckable(True)
        self.startup_action.setToolTip('Start the service and tray when you log in')
        self.startup_action.triggered.connect(self.change_startup)
        self.menu.addSeparator()
        self.quit_action = self.menu.addAction('Quit tray', app.quit)
        self.quit_action.setToolTip('Close this icon and leave the tracking service running')
        self.menu.aboutToShow.connect(self.refresh_startup)
        self.tray.setContextMenu(self.menu)
        self.tray.activated.connect(self.activated)
        self.timer = QTimer(app)
        self.timer.timeout.connect(self.update_status)
        self.timer.start(750)
        self.update_status()
        self.tray.show()
        # Starting a desktop client ensures service availability, never capture.
        self.run_command('start', self.refresh_startup)

    def update_status(self):
        if self.dashboard is not None:
            self.dashboard.poll()  # Reap a dashboard that was closed independently.
        snapshot, connected, _ = self.client.read()
        running = connected and snapshot is not None
        state = 'tracking' if running and snapshot['capture_running'] else 'idle' if running else 'stopped'
        label = {'tracking': 'Tracking active', 'idle': 'Service idle', 'stopped': 'Service stopped or unavailable'}[state]
        self.status_action.setText(label)
        self.tray.setToolTip('Tobii Linux — ' + label)
        if state != self.icon_state:
            self.tray.setIcon(eye_icon(state))
            self.icon_state = state
        self.start_action.setEnabled(not self.busy and not running)
        self.stop_action.setEnabled(not self.busy and running)
        self.startup_action.setEnabled(not self.busy)
        self.quit_action.setEnabled(not self.busy)

    def activated(self, reason):
        if reason in (QSystemTrayIcon.ActivationReason.Trigger, QSystemTrayIcon.ActivationReason.DoubleClick):
            self.open_dashboard()

    def open_dashboard(self):
        if self.dashboard is not None and self.dashboard.poll() is None:
            return
        try:
            with self.dashboard_log.open('a') as output:
                self.dashboard = subprocess.Popen([sys.executable, str(MANAGER), 'dashboard'],
                    cwd=ROOT, stdin=subprocess.DEVNULL, stdout=output,
                    stderr=subprocess.STDOUT, start_new_session=True)
        except OSError as error:
            self.show_error(str(error))

    def run_command(self, action, completion=None):
        if self.busy:
            return
        self.busy = True
        self.completion = completion
        self.command_name = action
        self.update_status()
        self.operation.start(sys.executable, [str(MANAGER), action])

    def command_error(self, error):
        if error == QProcess.ProcessError.FailedToStart:
            self.busy = False
            self.startup_action.setChecked(self.startup_enabled)
            self.show_error(self.operation.errorString())
            self.update_status()

    def command_finished(self, code, exit_status):
        output = bytes(self.operation.readAllStandardOutput()).decode(errors='replace')
        action, completion = self.command_name, self.completion
        self.busy = False
        self.completion = None
        if code or exit_status != QProcess.ExitStatus.NormalExit:
            self.startup_action.setChecked(self.startup_enabled)
            self.show_error(output.strip() or f'{action} failed')
        elif action == 'autostart-status':
            try:
                self.startup_enabled = bool(json.loads(output)['enabled'])
                self.startup_action.setChecked(self.startup_enabled)
            except (ValueError, KeyError, TypeError):
                self.show_error('Could not read the startup preference.')
        self.update_status()
        if completion is not None:
            completion()

    def refresh_startup(self):
        self.run_command('autostart-status')

    def change_startup(self, checked):
        self.run_command('enable-autostart' if checked else 'disable-autostart', self.refresh_startup)

    def show_error(self, message):
        print(message, file=sys.stderr, flush=True)
        self.tray.showMessage('Tobii Linux', message[-1500:], QSystemTrayIcon.MessageIcon.Warning)

    def close(self):
        self.tray.hide()
        self.client.close()
        # Dashboard and service lifetimes are independent of the tray.


def main():
    app = QApplication(sys.argv)
    app.setApplicationName('tobii-linux-tray')
    app.setQuitOnLastWindowClosed(False)
    work = Path(os.environ.get('SC_TOBII_NATIVE_RUNTIME_DIR', ROOT / '.tmp/sc-tobii-native-runtime'))
    if not work.is_absolute():
        work = ROOT / work
    work.mkdir(parents=True, exist_ok=True)
    work = work.resolve()
    lock = QLockFile(str(work / 'tray.lock'))
    lock.setStaleLockTime(0)
    if not lock.tryLock(0):
        return 0
    pidfile = work / 'pids/tray.pid'
    pidfile.parent.mkdir(parents=True, exist_ok=True)
    pidfile.write_text(str(os.getpid()) + '\n')
    tray = None
    try:
        tray = TrackingTray(app)
        signal.signal(signal.SIGTERM, lambda *_: app.quit())
        signal.signal(signal.SIGINT, lambda *_: app.quit())
        return app.exec()
    finally:
        if tray is not None:
            tray.close()
        pidfile.unlink(missing_ok=True)
        lock.unlock()


if __name__ == '__main__':
    raise SystemExit(main())
