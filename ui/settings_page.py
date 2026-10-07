"""Settings: paths, network, hardware, access tokens, external services, custom tools, import/export."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import (QComboBox, QDoubleSpinBox, QFileDialog, QGridLayout, QHBoxLayout, QLabel, QLineEdit,
                               QMessageBox, QScrollArea, QSpinBox, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from core.config import PATH_LABELS, Config
from core.secrets import get_secret, set_secret
from .context import Ctx
from .theme import BAD, OK
from .widgets import PathRow, button, card, page_header, section


class SettingsPage(QWidget):
    def __init__(self, ctx: Ctx, on_changed):
        super().__init__()
        self.setObjectName("Page")
        self.ctx, self.on_changed = ctx, on_changed
        self.rows: dict[str, PathRow] = {}
        self.ports: dict[str, QSpinBox] = {}

        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 14, 18, 10)
        outer.addWidget(page_header("Settings", f"Root (auto-detected): {ctx.cfg.root}   -   paths below the root are saved "
                                                 "as {ROOT}/... and survive a drive-letter change"))
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        body = QWidget()
        body.setObjectName("Page")
        lay = QVBoxLayout(body)

        # ---- paths
        lay.addWidget(section("Paths"))
        f, fl = card()
        grid = QGridLayout()
        for i, (key, label) in enumerate(PATH_LABELS.items()):
            row = PathRow()
            row.changed.connect(lambda text, k=key: self._typed(k, text))
            self.rows[key] = row
            grid.addWidget(QLabel(label), i, 0)
            grid.addWidget(row, i, 1)
        grid.setColumnStretch(1, 1)
        fl.addLayout(grid)
        lay.addWidget(f)

        # ---- network + hardware
        lay.addWidget(section("Network and hardware"))
        f, fl = card()
        g = QGridLayout()
        self.host = QComboBox()
        self.host.addItem("127.0.0.1  (this PC only)", "127.0.0.1")
        self.host.addItem("0.0.0.0  (reachable in LAN)", "0.0.0.0")
        g.addWidget(QLabel("Bind address"), 0, 0)
        g.addWidget(self.host, 0, 1)
        for j, key in enumerate(ctx.cfg.data["ports"]):
            s = QSpinBox()
            s.setRange(1, 65535)
            self.ports[key] = s
            g.addWidget(QLabel(f"Port {key}"), 1 + j, 0)
            g.addWidget(s, 1 + j, 1)
        n = 1 + len(self.ports)
        self.vram = QDoubleSpinBox()
        self.vram.setRange(1, 256)
        self.vram.setSuffix(" GB")
        g.addWidget(QLabel("GPU VRAM (for 'fits' hints)"), n, 0)
        g.addWidget(self.vram, n, 1)
        self.interval = QDoubleSpinBox()
        self.interval.setRange(0.5, 10)
        self.interval.setSingleStep(0.5)
        self.interval.setSuffix(" s")
        g.addWidget(QLabel("Monitor refresh"), n + 1, 0)
        g.addWidget(self.interval, n + 1, 1)
        g.setColumnStretch(2, 1)
        fl.addLayout(g)
        lay.addWidget(f)

        # ---- tokens
        lay.addWidget(section("Access tokens (encrypted with your Windows account)"))
        f, fl = card()
        g = QGridLayout()
        self.hf = QLineEdit()
        self.hf.setEchoMode(QLineEdit.Password)
        self.hf.setPlaceholderText("hf_...  (needed for gated models, faster downloads)")
        self.civ = QLineEdit()
        self.civ.setEchoMode(QLineEdit.Password)
        self.civ.setPlaceholderText("Civitai API key (needed for most downloads)")
        self.owui = QLineEdit()
        self.owui.setEchoMode(QLineEdit.Password)
        self.owui.setPlaceholderText("OpenWebUI API key (reserved for later integration)")
        for i, (lab, w) in enumerate((("Hugging Face token", self.hf), ("Civitai API key", self.civ),
                                      ("OpenWebUI API key", self.owui))):
            g.addWidget(QLabel(lab), i, 0)
            g.addWidget(w, i, 1)
        g.setColumnStretch(1, 1)
        fl.addLayout(g)
        self.tok_note = QLabel("Stored in secrets.json (DPAPI-encrypted, only readable by this Windows user on this PC). "
                               "After moving the stick to another PC, re-enter the tokens.")
        self.tok_note.setObjectName("Muted")
        self.tok_note.setWordWrap(True)
        fl.addWidget(self.tok_note)
        lay.addWidget(f)

        # ---- external services + custom tools
        lay.addWidget(section("External services (monitored + opened, never started here)"))
        self.ext = self._table(["Id", "Name", "URL"])
        lay.addWidget(self._with_buttons(self.ext, ("Add", lambda: self.ext.insertRow(self.ext.rowCount())),
                                         ("Remove", lambda: self._remove(self.ext))))
        lay.addWidget(section("Custom tools (started from the Dashboard)"))
        self.cust = self._table(["Id", "Name", "Command", "Working dir", "Port", "URL"])
        lay.addWidget(self._with_buttons(self.cust, ("Add", lambda: self.cust.insertRow(self.cust.rowCount())),
                                         ("Remove", lambda: self._remove(self.cust))))
        lay.addStretch(1)
        scroll.setWidget(body)
        outer.addWidget(scroll, 1)

        bar = QHBoxLayout()
        for text, kind, fn in (("Save", "Primary", self.save), ("Reload", "", self.load),
                               ("Reset to defaults", "Danger", self.reset), ("Import...", "", self.import_cfg),
                               ("Export...", "", self.export_cfg)):
            bar.addWidget(button(text, kind, fn))
        bar.addStretch(1)
        self.msg = QLabel("")
        bar.addWidget(self.msg)
        outer.addLayout(bar)
        self.load()

    # ---------------------------------------------------------------- tables
    def _table(self, headers: list[str]) -> QTableWidget:
        t = QTableWidget(0, len(headers))
        t.setHorizontalHeaderLabels(headers)
        t.setMaximumHeight(150)
        t.setMinimumHeight(110)
        t.horizontalHeader().setStretchLastSection(True)
        t.verticalHeader().setVisible(False)
        return t

    def _with_buttons(self, table: QTableWidget, *btns) -> QWidget:
        w = QWidget()
        w.setObjectName("Page")
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        v.addWidget(table)
        h = QHBoxLayout()
        for text, fn in btns:
            h.addWidget(button(text, "", fn))
        h.addStretch(1)
        v.addLayout(h)
        return w

    def _remove(self, t: QTableWidget) -> None:
        for r in sorted({i.row() for i in t.selectedIndexes()}, reverse=True):
            t.removeRow(r)

    def _cell(self, t: QTableWidget, r: int, c: int) -> str:
        it = t.item(r, c)
        return it.text().strip() if it else ""

    # ---------------------------------------------------------------- load / save
    def load(self) -> None:
        cfg = self.ctx.cfg
        for key, row in self.rows.items():
            row.edit.setText(str(cfg.path(key)))
        for key, s in self.ports.items():
            s.setValue(cfg.port(key))
        self.host.setCurrentIndex(max(0, self.host.findData(cfg.host())))
        self.vram.setValue(float(cfg.data.get("vram_gb", 12)))
        self.interval.setValue(float(cfg.data.get("monitor_interval", 1.0)))
        self.hf.setText(get_secret("hf_token"))
        self.civ.setText(get_secret("civitai_key"))
        self.owui.setText(get_secret("openwebui_key"))
        self.ext.setRowCount(0)
        for e in cfg.data.get("external_services", []):
            r = self.ext.rowCount()
            self.ext.insertRow(r)
            for c, k in enumerate(("id", "name", "url")):
                self.ext.setItem(r, c, QTableWidgetItem(str(e.get(k, ""))))
        self.cust.setRowCount(0)
        for e in cfg.data.get("custom_tools", []):
            r = self.cust.rowCount()
            self.cust.insertRow(r)
            vals = [e.get("id", ""), e.get("name", ""), " ".join(e.get("command", [])), e.get("cwd", ""),
                    str(e.get("port", "") or ""), e.get("url", "")]
            for c, v in enumerate(vals):
                self.cust.setItem(r, c, QTableWidgetItem(v))
        self._marks()

    def _marks(self) -> None:
        for key, row in self.rows.items():
            exists = self.ctx.cfg.path(key).exists()
            portable = self.ctx.cfg.is_portable(key)
            row.set_mark(("OK" if exists else "missing") + (" - portable" if portable else " - absolute"),
                         OK if exists else BAD)

    def _typed(self, key: str, text: str) -> None:
        if text:
            self.ctx.cfg.set_absolute(key, text)
        self.rows[key].edit.setText(str(self.ctx.cfg.path(key)))
        self._marks()

    def save(self) -> None:
        cfg = self.ctx.cfg
        for key, s in self.ports.items():
            cfg.data["ports"][key] = s.value()
        cfg.data["host"] = self.host.currentData()
        cfg.data["vram_gb"] = self.vram.value()
        cfg.data["monitor_interval"] = self.interval.value()
        self.ctx.monitor.interval = self.interval.value()
        cfg.data["external_services"] = [
            {"id": self._cell(self.ext, r, 0) or f"svc{r}", "name": self._cell(self.ext, r, 1) or self._cell(self.ext, r, 0),
             "url": self._cell(self.ext, r, 2)} for r in range(self.ext.rowCount()) if self._cell(self.ext, r, 2)]
        tools = []
        for r in range(self.cust.rowCount()):
            if not self._cell(self.cust, r, 2):
                continue
            e = {"id": self._cell(self.cust, r, 0) or f"tool{r}", "name": self._cell(self.cust, r, 1),
                 "command": self._cell(self.cust, r, 2).split()}
            if self._cell(self.cust, r, 3):
                e["cwd"] = self._cell(self.cust, r, 3)
            if self._cell(self.cust, r, 4).isdigit():
                e["port"] = int(self._cell(self.cust, r, 4))
            if self._cell(self.cust, r, 5):
                e["url"] = self._cell(self.cust, r, 5)
            tools.append(e)
        cfg.data["custom_tools"] = tools
        try:
            set_secret("hf_token", self.hf.text().strip())
            set_secret("civitai_key", self.civ.text().strip())
            set_secret("openwebui_key", self.owui.text().strip())
            cfg.save()
        except OSError as exc:
            QMessageBox.warning(self, "Save failed", str(exc))
            return
        self.msg.setText("Saved")
        self.msg.setStyleSheet(f"color:{OK}; font-weight:700;")
        self.ctx.log(f"Settings saved ({sum(cfg.is_portable(k) for k in self.rows)}/{len(self.rows)} paths portable)")
        self.on_changed()

    def reset(self) -> None:
        if QMessageBox.question(self, "Reset", "Reset paths, ports and tools to defaults?") == QMessageBox.Yes:
            self.ctx.cfg.reset()
            self.load()
            self.msg.setText("Defaults loaded - press Save")

    def export_cfg(self) -> None:
        f, _ = QFileDialog.getSaveFileName(self, "Export config", "ai_launcher_config.json", "JSON (*.json)")
        if f:
            self.ctx.cfg.save(Path(f))
            self.ctx.log(f"Config exported: {f}")

    def import_cfg(self) -> None:
        f, _ = QFileDialog.getOpenFileName(self, "Import config", "", "JSON (*.json)")
        if f:
            self.ctx.cfg.data = Config.load(self.ctx.cfg.root, Path(f)).data
            self.load()
            self.msg.setText("Imported - press Save")
