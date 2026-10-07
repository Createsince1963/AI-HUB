from __future__ import annotations

import webbrowser

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (QGridLayout, QHBoxLayout, QLabel, QPushButton, QScrollArea, QVBoxLayout,
                               QWidget)

from core.config import Config
from core.modules import Module, discover_modules
from core.procs import ProcessManager, port_open

GREEN, GREY, RED = "#2e9e4f", "#9aa0a6", "#d93025"


class Row:
    def __init__(self, mod: Module):
        self.mod = mod
        self.dot = QLabel("●")
        self.name = QLabel(mod.name)
        self.info = QLabel("")
        self.start = QPushButton("Start")
        self.stop = QPushButton("Stop")
        self.open = QPushButton("Open")
        self.open.setVisible(bool(mod.url()))
        self.start.setVisible(not mod.external)
        self.stop.setVisible(not mod.external)


class StartTab(QWidget):
    def __init__(self, cfg: Config, procs: ProcessManager, log):
        super().__init__()
        self.cfg, self.procs, self.log = cfg, procs, log
        self.rows: list[Row] = []
        self.outer = QVBoxLayout(self)
        self.root_label = QLabel()
        self.outer.addWidget(self.root_label)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.outer.addWidget(self.scroll, 1)
        self.hint = QLabel("Note: ComfyUI and Ollama share the GPU (12 GB VRAM). "
                           "Running both with big models at once can run out of memory.")
        self.hint.setWordWrap(True)
        self.outer.addWidget(self.hint)
        self.rebuild()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(2000)

    def rebuild(self) -> None:
        """(Re)create rows, e.g. after paths were changed."""
        self.root_label.setText(f"Root: {self.cfg.root}")
        body = QWidget()
        grid = QGridLayout(body)
        self.rows = []
        for i, mod in enumerate(discover_modules(self.cfg)):
            r = Row(mod)
            r.start.clicked.connect(lambda _=False, r=r: self.start_module(r))
            r.stop.clicked.connect(lambda _=False, r=r: self.stop_module(r))
            r.open.clicked.connect(lambda _=False, r=r: webbrowser.open(r.mod.url()))
            grid.addWidget(r.dot, i, 0)
            grid.addWidget(r.name, i, 1)
            grid.addWidget(r.info, i, 2)
            box = QHBoxLayout()
            for b in (r.start, r.stop, r.open):
                box.addWidget(b)
            holder = QWidget()
            holder.setLayout(box)
            grid.addWidget(holder, i, 3)
            self.rows.append(r)
        grid.setColumnStretch(2, 1)
        grid.setRowStretch(len(self.rows), 1)
        self.scroll.setWidget(body)
        self.refresh()

    def _state(self, r: Row) -> str:
        port = r.mod.port()
        if r.mod.external:
            return "online" if port_open(port, r.mod.host, 0.4) else "offline"
        if port and port_open(port):
            return "running"
        if self.procs.is_running(r.mod.id):
            return "starting" if port else "running"
        return "stopped"

    def refresh(self) -> None:
        for r in self.rows:
            ok, msg = r.mod.check()
            state = self._state(r)
            colour = {"running": GREEN, "online": GREEN, "starting": "#f9ab00", "stopped": GREY, "offline": RED}[state]
            if not ok:
                colour = RED
            r.dot.setStyleSheet(f"color: {colour}; font-size: 18px;")
            port = r.mod.port()
            r.info.setText(f"{state}" + (f"  |  {r.mod.url()}" if r.mod.external else (f"  |  port {port}" if port else "")) + ("" if ok else f"  |  {msg}"))
            r.start.setStyleSheet("background:#90ee90;" if ok else "")
            r.start.setEnabled(ok and state == "stopped")
            r.stop.setEnabled(self.procs.is_running(r.mod.id))

    def start_module(self, r: Row) -> None:
        try:
            for line in r.mod.prepare():
                self.log(f"[{r.mod.name}] {line}")
            spec = r.mod.build()
            self.procs.start(r.mod.id, spec.cmd, spec.cwd, spec.env)
            self.log(f"[{r.mod.name}] started: {' '.join(spec.cmd)}")
        except Exception as exc:      # noqa: BLE001 - show any start problem in the log
            self.log(f"[{r.mod.name}] ERROR: {exc}")
        self.refresh()

    def stop_module(self, r: Row) -> None:
        self.procs.stop(r.mod.id)
        self.log(f"[{r.mod.name}] stopped")
        self.refresh()
