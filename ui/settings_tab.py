from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import (QComboBox, QFileDialog, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QMessageBox,
                               QPushButton, QSpinBox, QVBoxLayout, QWidget)

from core.config import PATH_LABELS, Config


class SettingsTab(QWidget):
    def __init__(self, cfg: Config, on_changed, log):
        super().__init__()
        self.cfg, self.on_changed, self.log = cfg, on_changed, log
        self.edits: dict[str, QLineEdit] = {}
        self.marks: dict[str, QLabel] = {}
        self.ports: dict[str, QSpinBox] = {}

        outer = QVBoxLayout(self)
        outer.addWidget(QLabel(f"Root (auto-detected): {cfg.root}\n"
                               "Paths inside the root are saved as {ROOT}/... and survive a drive-letter change."))
        grid = QGridLayout()
        for i, (key, label) in enumerate(PATH_LABELS.items()):
            edit = QLineEdit()
            edit.editingFinished.connect(lambda k=key: self._typed(k))
            btn = QPushButton("Browse...")
            btn.clicked.connect(lambda _=False, k=key: self._browse(k))
            mark = QLabel()
            self.edits[key], self.marks[key] = edit, mark
            grid.addWidget(QLabel(label), i, 0)
            grid.addWidget(edit, i, 1)
            grid.addWidget(btn, i, 2)
            grid.addWidget(mark, i, 3)
        row = len(PATH_LABELS)
        for j, key in enumerate(cfg.data["ports"]):
            spin = QSpinBox()
            spin.setRange(1, 65535)
            self.ports[key] = spin
            grid.addWidget(QLabel(f"Port {key}"), row + j, 0)
            grid.addWidget(spin, row + j, 1)
        hrow = row + len(cfg.data["ports"])
        self.host = QComboBox()
        self.host.addItem("127.0.0.1  (this PC only)", "127.0.0.1")
        self.host.addItem("0.0.0.0  (reachable in LAN)", "0.0.0.0")
        grid.addWidget(QLabel("Host"), hrow, 0)
        grid.addWidget(self.host, hrow, 1)
        grid.setColumnStretch(1, 1)
        outer.addLayout(grid)

        buttons = QHBoxLayout()
        for text, fn in (("Save", self.save), ("Reload", self.load), ("Reset to defaults", self.reset),
                         ("Import config...", self.import_cfg), ("Export config...", self.export_cfg)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            buttons.addWidget(b)
        buttons.addStretch(1)
        outer.addLayout(buttons)
        outer.addStretch(1)
        self.load()

    def load(self) -> None:
        for key, edit in self.edits.items():
            edit.setText(str(self.cfg.path(key)))
        for key, spin in self.ports.items():
            spin.setValue(self.cfg.port(key))
        self.host.setCurrentIndex(max(0, self.host.findData(self.cfg.host())))
        self._marks()

    def _marks(self) -> None:
        for key, mark in self.marks.items():
            exists = self.cfg.path(key).exists()
            portable = self.cfg.is_portable(key)
            mark.setText(("OK" if exists else "missing") + ("  (portable)" if portable else "  (absolute!)"))
            mark.setStyleSheet(f"color: {'#2e9e4f' if exists else '#d93025'};")

    def _typed(self, key: str) -> None:
        text = self.edits[key].text().strip()
        if text:
            self.cfg.set_absolute(key, text)
        self.edits[key].setText(str(self.cfg.path(key)))
        self._marks()

    def _browse(self, key: str) -> None:
        start = str(self.cfg.path(key)) if self.cfg.path(key).exists() else str(self.cfg.root)
        chosen = QFileDialog.getExistingDirectory(self, PATH_LABELS[key], start)
        if chosen:
            self.cfg.set_absolute(key, chosen)
            self.edits[key].setText(str(self.cfg.path(key)))
            self._marks()

    def save(self) -> None:
        for key, spin in self.ports.items():
            self.cfg.data["ports"][key] = spin.value()
        self.cfg.data["host"] = self.host.currentData()
        self.cfg.save()
        self.log(f"Config saved ({sum(self.cfg.is_portable(k) for k in self.edits)}/{len(self.edits)} paths portable)")
        self.on_changed()

    def reset(self) -> None:
        if QMessageBox.question(self, "Reset", "Reset all paths and ports to defaults?") == QMessageBox.Yes:
            self.cfg.reset()
            self.load()
            self.log("Config reset to defaults (not saved yet)")

    def export_cfg(self) -> None:
        file, _ = QFileDialog.getSaveFileName(self, "Export config", "ai_launcher_config.json", "JSON (*.json)")
        if file:
            self.cfg.save(Path(file))
            self.log(f"Config exported: {file}")

    def import_cfg(self) -> None:
        file, _ = QFileDialog.getOpenFileName(self, "Import config", "", "JSON (*.json)")
        if file:
            self.cfg.data = Config.load(self.cfg.root, Path(file)).data
            self.load()
            self.log(f"Config imported: {file} (not saved yet)")
