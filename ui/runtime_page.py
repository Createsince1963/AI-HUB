"""Shared runtime (@Runtime): Python/pip status, package manager, Qt tool launchers."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from PySide6.QtWidgets import (QGridLayout, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QTableWidgetItem, QVBoxLayout,
                               QWidget)

from .context import Ctx
from .models_page import open_folder
from .widgets import NumItem, button, card, make_table, page_header, run_async, section

NOWIN = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
QT_TOOLS = ["designer", "linguist", "assistant", "qmllint", "deploy", "uic", "rcc"]


class RuntimePage(QWidget):
    def __init__(self, ctx: Ctx):
        super().__init__()
        self.setObjectName("Page")
        self.ctx = ctx
        self.pkgs: list[dict] = []
        self.outdated: dict[str, str] = {}
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 14, 18, 10)
        lay.addWidget(page_header("Runtime", "Shared portable Python platform for all own tools (@Runtime)"))

        f, fl = card(12)
        g = QGridLayout()
        self.info = {}
        for i, k in enumerate(("Python", "pip", "PySide6 / Qt", "Location", "Wheel cache")):
            g.addWidget(QLabel(k), i, 0)
            lab = QLabel("-")
            lab.setTextInteractionFlags(lab.textInteractionFlags().TextSelectableByMouse)
            g.addWidget(lab, i, 1)
            self.info[k] = lab
        g.setColumnStretch(1, 1)
        fl.addLayout(g)
        r = QHBoxLayout()
        r.addWidget(button("Run runtime setup", "Primary", self.run_setup))
        r.addWidget(button("Open runtime folder", "", lambda: open_folder(self.ctx.cfg.path("runtime"))))
        r.addWidget(QLabel("Qt tools:"))
        for t in ("designer", "linguist", "assistant"):
            r.addWidget(button(t, "", lambda t=t: self.launch_tool(t)))
        r.addStretch(1)
        fl.addLayout(r)
        lay.addWidget(f)

        lay.addWidget(section("Installed packages"))
        top = QHBoxLayout()
        self.filter = QLineEdit()
        self.filter.setPlaceholderText("Filter packages...")
        self.filter.textChanged.connect(self.fill)
        top.addWidget(self.filter, 1)
        top.addWidget(button("Refresh", "", self.refresh))
        top.addWidget(button("Check for updates", "", self.check_outdated))
        lay.addLayout(top)
        self.table = make_table(["Package", "Version", "Latest"], 0)
        lay.addWidget(self.table, 1)
        bot = QHBoxLayout()
        self.new = QLineEdit()
        self.new.setPlaceholderText("Install package, e.g. requests  or  numpy==2.1.0")
        self.new.returnPressed.connect(self.install)
        bot.addWidget(self.new, 1)
        bot.addWidget(button("Install", "Primary", self.install))
        bot.addWidget(button("Upgrade selected", "", self.upgrade))
        bot.addWidget(button("Uninstall selected", "Danger", self.uninstall))
        lay.addLayout(bot)
        self.status = QLabel("")
        self.status.setObjectName("Muted")
        lay.addWidget(self.status)
        self._loaded = False

    def py(self) -> str:
        p = self.ctx.cfg.path("runtime") / "python" / "python.exe"
        return str(p) if p.exists() else sys.executable

    def on_show(self) -> None:
        if not self._loaded:
            self._loaded = True
            self.refresh()

    def _run(self, args: list[str], timeout: int = 600) -> str:
        r = subprocess.run([self.py(), *args], capture_output=True, text=True, timeout=timeout,
                           creationflags=NOWIN, encoding="utf-8", errors="replace")
        return (r.stdout or "") + (r.stderr or "")

    def refresh(self) -> None:
        self.status.setText("Reading packages ...")

        def work():
            info = self._run(["-c", "import sys,pip;print(sys.version.split()[0]);print(pip.__version__)"]).split()
            qt = self._run(["-c", "import PySide6, PySide6.QtCore;print(PySide6.__version__, PySide6.QtCore.qVersion())"]).strip().splitlines()
            pk = json.loads(self._run(["-m", "pip", "list", "--format=json", "--disable-pip-version-check"]) or "[]")
            return info, qt[-1] if qt else "not installed", pk

        def done(res):
            info, qt, pk = res
            self.info["Python"].setText(f"{info[0]}   ({self.py()})" if info else "not found")
            self.info["pip"].setText(info[1] if len(info) > 1 else "-")
            self.info["PySide6 / Qt"].setText(qt)
            rt = self.ctx.cfg.path("runtime")
            self.info["Location"].setText(str(rt))
            wheels = rt / "wheels"
            n = len(list(wheels.glob("*.whl"))) if wheels.is_dir() else 0
            self.info["Wheel cache"].setText(f"{n} wheels (offline re-install possible)" if n else "empty")
            self.pkgs = pk
            self.fill()
            self.status.setText(f"{len(pk)} packages")
        run_async(work, on_result=done, on_error=lambda e: self.status.setText(f"Error: {e}"))

    def fill(self) -> None:
        text = self.filter.text().lower()
        rows = [p for p in self.pkgs if text in p["name"].lower()]
        t = self.table
        t.setSortingEnabled(False)
        t.setRowCount(len(rows))
        for i, p in enumerate(rows):
            t.setItem(i, 0, QTableWidgetItem(p["name"]))
            t.setItem(i, 1, QTableWidgetItem(p["version"]))
            latest = self.outdated.get(p["name"].lower(), "")
            it = QTableWidgetItem(latest)
            if latest:
                from PySide6.QtGui import QColor
                it.setForeground(QColor("#c58a00"))
            t.setItem(i, 2, it)
        t.setSortingEnabled(True)

    def check_outdated(self) -> None:
        self.status.setText("Checking PyPI for updates (internet needed) ...")

        def work():
            return json.loads(self._run(["-m", "pip", "list", "--outdated", "--format=json",
                                         "--disable-pip-version-check"]) or "[]")

        def done(res):
            self.outdated = {p["name"].lower(): p["latest_version"] for p in res}
            self.fill()
            self.status.setText(f"{len(res)} updates available")
        run_async(work, on_result=done, on_error=lambda e: self.status.setText(f"Error: {e}"))

    def _pip(self, args: list[str], label: str) -> None:
        self.status.setText(f"{label} ...")
        self.ctx.log(f"pip {' '.join(args)}")

        def done(out: str):
            for line in out.strip().splitlines()[-8:]:
                self.ctx.log(f"  {line}")
            self.refresh()
        run_async(lambda: self._run(["-m", "pip", *args, "--no-warn-script-location"]), on_result=done,
                  on_error=lambda e: self.status.setText(f"Error: {e}"))

    def _sel(self) -> str | None:
        rows = self.table.selectionModel().selectedRows()
        return self.table.item(rows[0].row(), 0).text() if rows else None

    def install(self) -> None:
        spec = self.new.text().strip()
        if spec:
            self._pip(["install", *spec.split()], f"Installing {spec}")
            self.new.clear()

    def upgrade(self) -> None:
        n = self._sel()
        if n:
            self._pip(["install", "--upgrade", n], f"Upgrading {n}")

    def uninstall(self) -> None:
        n = self._sel()
        if n and QMessageBox.question(self, "Uninstall", f"Uninstall {n}?") == QMessageBox.Yes:
            self._pip(["uninstall", "-y", n], f"Uninstalling {n}")

    def run_setup(self) -> None:
        script = self.ctx.cfg.root / "AI_Launcher" / "Setup_Runtime.bat"
        self.ctx.procs.start("install:runtime", [str(script)], str(script.parent), None)
        self.ctx.log("Runtime setup started in its own console")

    def launch_tool(self, tool: str) -> None:
        wrapper = self.ctx.cfg.path("runtime") / "bin" / f"pyside6-{tool}.cmd"
        if wrapper.exists():
            self.ctx.procs.start(f"tool:{tool}", [str(wrapper)], str(self.ctx.cfg.root), None)
            self.ctx.log(f"Started {wrapper.name}")
        else:
            self.ctx.log(f"Wrapper not found: {wrapper} (run runtime setup)")
