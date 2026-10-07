"""Main window: sidebar navigation, stacked pages, collapsible output log, live status bar."""
from __future__ import annotations

import datetime as dt
from pathlib import Path

from PySide6.QtCore import QByteArray, Qt
from PySide6.QtGui import QGuiApplication, QKeySequence, QShortcut
from PySide6.QtWidgets import (QFileDialog, QFrame, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QMainWindow,
                               QMessageBox, QPlainTextEdit, QPushButton, QScrollArea, QSplitter, QStackedWidget, QVBoxLayout,
                               QWidget)

from core.config import Config
from core.downloads import DownloadManager
from core.monitor import Monitor
from core.procs import ProcessManager
from core.system import fmt_size
from .context import Ctx
from .dashboard import Dashboard
from .hub_terminal_page import HubTerminalPage
from .pty_terminal import drain_reader_threads, pty_available
from .models_page import ModelsPage
from .processes_page import ProcessesPage
from .runtime_page import RuntimePage
from .settings_page import SettingsPage
from .setup_page import SetupPage
from .terminal_page import TerminalPage
from . import theme
from .widgets import button

# Sidebar order. "HUB Terminal" hosts the Shell, the Processes view and the CLI terminals (Claude, Codex,
# Copilot, Antigravity) as sub-tabs; "HUB Modell" (the former Model Hub) sits directly below it.
PAGES = [("dashboard", "Dashboard"), ("terminal", "HUB Terminal"), ("hub", "HUB Modell"), ("models", "AI models"),
         ("tuning", "Tuning"), ("runtime", "Runtime"), ("setup", "Setup"), ("settings", "Settings")]

# CLI tools that get an embedded sub-tab in HUB Terminal (created when the tool is started) - only for tools
# whose <cli dir>\<Name>.bat actually exists on this system (cli dir comes from the config, never hard-coded).
CLI_TOOLS = ["Claude", "Codex", "Copilot", "Antigravity"]


def _scrolled(page: QWidget) -> QScrollArea:
    """Page inside a resizable scroll area: the page still fills the window, but its minimum size no longer
    becomes the window's minimum size (that caused 'QWindowsWindow::setGeometry: Unable to set geometry')."""
    sa = QScrollArea()
    sa.setWidgetResizable(True)
    sa.setFrameShape(QFrame.NoFrame)
    sa.setWidget(page)
    return sa


class MainWindow(QMainWindow):
    def __init__(self, cfg: Config):
        super().__init__()
        self.setWindowTitle("AI Workspace Control Center")
        self.setMinimumSize(640, 420)            # small on purpose - pages scroll instead of forcing a big window
        self.resize(1200, 800)                   # Qt places and clamps the window itself (no screen queries)
        self.cfg = cfg
        theme.set_mode(str(cfg.data.get("theme", "light")))
        theme.apply()
        self.log_box = QPlainTextEdit(readOnly=True)
        self.log_box.setMaximumBlockCount(5000)

        self.monitor = Monitor(float(cfg.data.get("monitor_interval", 1.0)), cfg.root)
        self.downloads = DownloadManager()
        self.ctx = Ctx(cfg, ProcessManager(), self.downloads, self.monitor, self.log, self.goto, self._refresh_services)

        # ---- sidebar
        side = QFrame()
        side.setObjectName("Sidebar")
        side.setMinimumWidth(150)
        side.setMaximumWidth(200)
        sl = QVBoxLayout(side)
        sl.setContentsMargins(0, 14, 0, 10)
        brand = QLabel("AI Workspace")
        brand.setObjectName("Brand")
        sub = QLabel("portable  |  RTX 5070")
        sub.setObjectName("Muted")
        sub.setStyleSheet("padding-left:16px; padding-bottom:10px;")
        sl.addWidget(brand)
        sl.addWidget(sub)
        cli_dir = cfg.path("cli")
        self.cli_tools = [t for t in CLI_TOOLS if (cli_dir / f"{t}.bat").exists()]
        nav_entries = list(PAGES)

        self.nav = QListWidget()
        self.nav.setObjectName("Nav")
        for key, title in nav_entries:
            it = QListWidgetItem(title)
            it.setData(Qt.UserRole, key)
            self.nav.addItem(it)
        sl.addWidget(self.nav, 1)
        self.root_label = QLabel(f"Root: {cfg.root}")
        self.root_label.setObjectName("Muted")
        self.root_label.setWordWrap(True)
        self.root_label.setStyleSheet("padding:0 12px; font-size:8pt;")
        sl.addWidget(self.root_label)
        self.theme_btn = QPushButton()
        self.theme_btn.setToolTip("Zwischen hellem und dunklem Design umschalten (wird gespeichert)")
        self.theme_btn.clicked.connect(self.toggle_theme)
        sl.addWidget(self.theme_btn)
        self._theme_btn_text()

        # ---- pages
        self.pages: dict[str, QWidget] = {
            "dashboard": Dashboard(self.ctx), "models": ModelsPage(self.ctx),
            "runtime": RuntimePage(self.ctx), "setup": SetupPage(self.ctx),
        }
        self.pages["terminal"] = HubTerminalPage(self.ctx, TerminalPage(self.ctx, header=False),
                                                 ProcessesPage(self.ctx, header=False), self.cli_tools)
        self.pages["settings"] = SettingsPage(self.ctx, self._settings_changed)
        from .model_hub_tab import ModelHubTab
        hub = ModelHubTab(self.ctx)
        hub.before_show = self.pages["models"].mgr.on_show      # index (models.db) is filled by the Manager scan
        self.pages["hub"] = hub
        from .tuning_page import TuningPage
        self.pages["tuning"] = TuningPage(self.ctx)
        self.nav_entries = nav_entries
        self.stack = QStackedWidget()
        for key, _t in nav_entries:
            self.stack.addWidget(_scrolled(self.pages[key]))
        self.nav.currentRowChanged.connect(self._nav)

        # ---- log dock
        logw = QWidget()
        ll = QVBoxLayout(logw)
        ll.setContentsMargins(10, 4, 10, 6)
        bar = QHBoxLayout()
        title = QLabel("OUTPUT / LOG")
        title.setObjectName("Section")
        bar.addWidget(title)
        bar.addStretch(1)
        bar.addWidget(button("Copy log", "", self.copy_log))
        bar.addWidget(button("Clear log", "", self.log_box.clear))
        bar.addWidget(button("Save log...", "", self.save_log))
        self.log_collapse_btn = QPushButton("▾")
        self.log_collapse_btn.setMaximumWidth(32)
        self.log_collapse_btn.setToolTip("Log einklappen, um Platz für Start/Admin zu schaffen (Strg+L)")
        self.log_collapse_btn.clicked.connect(self.toggle_log)
        bar.addWidget(self.log_collapse_btn)
        ll.addLayout(bar)
        ll.addWidget(self.log_box, 1)
        self.log_widget = logw

        right = QSplitter(Qt.Vertical)
        right.addWidget(self.stack)
        right.addWidget(logw)
        right.setStretchFactor(0, 4)
        right.setStretchFactor(1, 1)
        right.setSizes([720, 110])
        self.split = right

        central = QWidget()
        cl = QHBoxLayout(central)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.setSpacing(0)
        cl.addWidget(side)
        cl.addWidget(right, 1)
        self.setCentralWidget(central)

        # ---- status bar
        sb = self.statusBar()
        self.sb_cli = QLabel("")      # global CLI capacity indicator (green/orange)
        self.sb_cli.setStyleSheet("padding:0 10px; font-weight:600;")
        sb.addPermanentWidget(self.sb_cli)
        self.sb_gpu, self.sb_ram, self.sb_disk, self.sb_dl = QLabel(""), QLabel(""), QLabel(""), QLabel("")
        for w in (self.sb_gpu, self.sb_ram, self.sb_disk, self.sb_dl):
            w.setStyleSheet("padding:0 10px;")
            sb.addPermanentWidget(w)
        self._cli_status_timer = None
        if self.cli_tools:
            from PySide6.QtCore import QTimer
            self._cli_status_timer = QTimer(self)
            self._cli_status_timer.timeout.connect(self._update_cli_status)
            self._cli_status_timer.start(500)
            self._update_cli_status()
        self.log_btn = QPushButton("Hide log")
        self.log_btn.clicked.connect(self.toggle_log)
        sb.addPermanentWidget(self.log_btn)
        self.monitor.sample.connect(self._status)
        self.downloads.changed.connect(lambda _j: self.sb_dl.setText(
            f"Downloads: {self.downloads.active()}" if self.downloads.active() else ""))

        for i in range(min(9, len(nav_entries))):
            QShortcut(QKeySequence(f"Ctrl+{i + 1}"), self, activated=lambda i=i: self.nav.setCurrentRow(i))
        QShortcut(QKeySequence("Ctrl+L"), self, activated=self.toggle_log)
        QShortcut(QKeySequence("F11"), self, activated=self.toggle_fullscreen)
        QShortcut(QKeySequence("Esc"), self, activated=self._leave_fullscreen)

        self.log(f"Root detected: {cfg.root}")
        self.nav.setCurrentRow(0)
        self.monitor.start()
        self._disk_tick = 0

    # ---------------------------------------------------------------- window size / fullscreen
    def show_initial(self) -> None:
        """Open windowed (85% of the screen, centred) instead of maximized - on request, to rule out
        maximized-window/monitor-edge geometry handling as a factor. F11 / the fullscreen shortcut
        still work as before."""
        geo = self.cfg.data.get("ui", {}).get("window_geometry") if hasattr(self, "cfg") else None
        if geo:                                   # Qt's own state blob - Qt validates it against the current monitors
            try:
                self.restoreGeometry(QByteArray.fromBase64(geo.encode("ascii")))
            except Exception:        # noqa: BLE001
                pass
        self.show()

    def toggle_fullscreen(self) -> None:
        """F11: true fullscreen (no title bar / taskbar) <-> maximized."""
        if self.isFullScreen():
            self.showMaximized()
        else:
            self.showFullScreen()

    def _leave_fullscreen(self) -> None:
        if self.isFullScreen():
            self.showMaximized()

    # ---------------------------------------------------------------- navigation
    def _nav(self, row: int) -> None:
        if row < 0:
            return
        self.stack.setCurrentIndex(row)
        w = self.stack.currentWidget()
        w = w.widget() if isinstance(w, QScrollArea) else w
        if hasattr(w, "on_show"):
            w.on_show()

    def goto(self, key: str) -> None:
        if key == "downloads":
            self.nav.setCurrentRow([k for k, _ in self.nav_entries].index("models"))
            self.pages["models"].show_downloads()
            return
        if key.startswith("tuning:"):                      # "tuning:ollama" / "tuning:llamacpp" -> page + backend + measurements tab
            self.nav.setCurrentRow([k for k, _ in self.nav_entries].index("tuning"))
            self.pages["tuning"].select_backend(key.split(":", 1)[1], measurements=True)
            return
        if key == "processes":                             # live log / progress of started services + updaters
            self.nav.setCurrentRow([k for k, _ in self.nav_entries].index("terminal"))
            self.pages["terminal"].open_processes()
            return
        if key.startswith("cli:"):
            self.nav.setCurrentRow([k for k, _ in self.nav_entries].index("terminal"))
            self.pages["terminal"].open_cli(key.split(":", 1)[1])
            return
        self.nav.setCurrentRow([k for k, _ in self.nav_entries].index(key))

    def start_cli_embedded(self, tool: str) -> bool:
        """Dashboard 'Start' of a CLI: open (and start) its sub-tab in HUB Terminal. False -> caller falls back
        to the external console (tool not available here, or pywinpty/pyte missing)."""
        if tool not in self.cli_tools or not pty_available():
            return False
        self.nav.setCurrentRow([k for k, _ in self.nav_entries].index("terminal"))
        self.pages["terminal"].open_cli(tool, start=True)
        return True

    def _refresh_services(self) -> None:
        self.pages["dashboard"].rebuild()
        self.pages["setup"].refresh()

    def _settings_changed(self) -> None:
        self._refresh_services()
        self.root_label.setText(f"Root: {self.cfg.root}")

    # ---------------------------------------------------------------- log
    def log(self, text: str) -> None:
        self.log_box.appendPlainText(f"{dt.datetime.now():%H:%M:%S}  {text}")

    def copy_log(self) -> None:
        QGuiApplication.clipboard().setText(self.log_box.toPlainText())
        self.statusBar().showMessage("Log copied to clipboard", 2000)

    def save_log(self) -> None:
        f, _ = QFileDialog.getSaveFileName(self, "Save log", "ai_workspace_log.txt", "Text (*.txt)")
        if f:
            Path(f).write_text(self.log_box.toPlainText(), encoding="utf-8")

    def toggle_log(self) -> None:
        vis = not self.log_widget.isVisible()
        self.log_widget.setVisible(vis)
        self.log_btn.setText("Hide log" if vis else "Show log")
        self.log_collapse_btn.setText("▾" if vis else "▸")

    # ---------------------------------------------------------------- theme
    def _theme_btn_text(self) -> None:
        self.theme_btn.setText("☀  Helles Design" if theme.mode() == "dark" else "☾  Dunkles Design")

    def apply_theme(self, mode: str) -> None:
        theme.set_mode(mode)
        theme.apply()
        self._theme_btn_text()
        for w in self.findChildren(QWidget):          # custom-painted widgets read tokens at paint time
            w.update()
        for pg in self.pages.values():
            if hasattr(pg, "apply_theme"):
                pg.apply_theme()

    def toggle_theme(self) -> None:
        mode = "light" if theme.mode() == "dark" else "dark"
        self.cfg.data["theme"] = mode
        try:
            self.cfg.save()
        except OSError as exc:
            self.log(f"Design konnte nicht gespeichert werden: {exc}")
        self.apply_theme(mode)

    # ---------------------------------------------------------------- status bar
    def _update_cli_status(self) -> None:
        """Global CLI capacity indicator (running / max_concurrent_clis): green while there is room,
        orange once the group is full and further CLIs are OnHold."""
        procs = self.ctx.procs
        active = procs.running_cli_keys()
        cap = procs.max_concurrent_clis
        if not active:
            self.sb_cli.setText(f"✓ Bereit (0/{cap})")
            self.sb_cli.setStyleSheet("padding:0 10px; font-weight:600; color:#2e9e4f;")
            return
        names = ", ".join(k.split(":", 1)[-1] for k in active)
        if len(active) >= cap:
            self.sb_cli.setText(f"⏸ {names} aktiv ({len(active)}/{cap}) - weitere CLIs OnHold")
            self.sb_cli.setStyleSheet("padding:0 10px; font-weight:600; color:#f9ab00;")
        else:
            self.sb_cli.setText(f"● {names} aktiv ({len(active)}/{cap})")
            self.sb_cli.setStyleSheet("padding:0 10px; font-weight:600; color:#2e9e4f;")

    def _status(self, d: dict) -> None:
        if "error" in d:
            return
        g = d.get("gpu")
        self.sb_gpu.setText(f"GPU {g['util']}%  VRAM {g['used_mb'] / 1024:.1f}/{g['total_mb'] / 1024:.0f} GB  {g['temp']} °C" if g else "GPU n/a")
        self.sb_ram.setText(f"CPU {d['cpu']:.0f}%   RAM {d['ram_pct']:.0f}%")
        self._disk_tick += 1
        free = d.get("disk_free")          # measured in the monitor thread (never touch the SSD from the UI thread)
        if free is not None:
            self.sb_disk.setText(f"Drive {self.cfg.root.drive or self.cfg.root}: {fmt_size(free)} free")

    def closeEvent(self, event) -> None:            # noqa: N802
        try:
            self.cfg.data.setdefault("ui", {})["window_geometry"] = bytes(self.saveGeometry().toBase64()).decode("ascii")
            self.cfg.save()
        except Exception:        # noqa: BLE001
            pass
        if self.downloads.active():
            if QMessageBox.question(self, "Downloads running", "Downloads are still running. Quit anyway?\n"
                                    "(partial files stay and can be resumed)") != QMessageBox.Yes:
                event.ignore()
                return
            for j in list(self.downloads.jobs):
                self.downloads.cancel(j)
        running = self.ctx.procs.running_keys()
        if running and QMessageBox.question(self, "Services running",
                                            "Started services will be stopped when the launcher closes:\n  "
                                            + "\n  ".join(running) + "\n\nQuit anyway?") != QMessageBox.Yes:
            event.ignore()
            return
        self.ctx.procs.stop_all()
        self.pages["dashboard"].shutdown()
        self.pages["terminal"].shutdown()
        self.pages["tuning"].shutdown()
        # Each tab's stop() joins its reader thread only briefly (see pty_terminal.STOP_JOIN_MS),
        # so closing four live terminal tabs does not cost four full timeouts. Whatever did not
        # finish is joined here ONCE, against a single shared budget, before the process exits -
        # this is what replaces the old per-tab wait(2000) and the QThread.terminate() fallback.
        left = drain_reader_threads()
        if left:
            # Deliberately NOT called "safe": a QThread still running at interpreter teardown
            # would abort the process from its destructor. The entry point therefore ends the
            # process via pty_terminal.finalize_process_exit() instead of normal teardown.
            self.log(f"WARNUNG: {left} Terminal-Leseprozess(e) hängen in winpty read() und konnten nicht "
                      "beendet werden - der Launcher beendet sich deshalb hart (os._exit), ohne regulären "
                      "Python/Qt-Abbau. Bitte im Task-Manager prüfen, ob CLI-Prozesse übrig sind.")
        self.monitor.stop()
        super().closeEvent(event)      
