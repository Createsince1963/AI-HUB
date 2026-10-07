"""Installation tab: ordered checks with "next step" highlighting (idea from the old PS1 setup GUI)."""
from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Callable

from PySide6.QtWidgets import QGridLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from core.config import Config
from core.modules import ComfyUIModule, OllamaModule
from core.procs import ProcessManager

OK, NEXT, LATER = "#90ee90", "#f0e68c", "#e6f2ff"


@dataclass
class Step:
    label: str
    check: Callable[[], bool]
    action_label: str | None = None
    action: Callable[[], None] | None = None


class InstallTab(QWidget):
    def __init__(self, cfg: Config, procs: ProcessManager, log, on_changed):
        super().__init__()
        self.cfg, self.procs, self.log, self.on_changed = cfg, procs, log, on_changed
        self.steps = self._steps()
        self.buttons: list[QPushButton] = []
        self.actions: list[QPushButton] = []
        outer = QVBoxLayout(self)
        self.next_label = QLabel()
        self.next_label.setStyleSheet("font-weight: bold;")
        outer.addWidget(self.next_label)
        grid = QGridLayout()
        for i, st in enumerate(self.steps):
            b = QPushButton(f"{i + 1}) {st.label}")
            b.setEnabled(False)
            grid.addWidget(b, i, 0)
            self.buttons.append(b)
            a = QPushButton(st.action_label or "")
            a.setVisible(bool(st.action))
            if st.action:
                a.clicked.connect(st.action)
            grid.addWidget(a, i, 1)
            self.actions.append(a)
        grid.setColumnStretch(0, 1)
        outer.addLayout(grid)
        refresh = QPushButton("Check status")
        refresh.clicked.connect(self.refresh)
        outer.addWidget(refresh)
        outer.addStretch(1)
        self.refresh()

    def _steps(self) -> list[Step]:
        c = self.cfg
        return [
            Step("Shared runtime (@Runtime\\python)", lambda: (c.path("runtime") / "python" / "python.exe").exists(),
                 "Run runtime setup", self._run_runtime_setup),
            Step("Runtime tools (ExifTool, FFmpeg/ffprobe in @Runtime\\Tools)",
                 lambda: all(__import__("core.runtime_tools", fromlist=["status"]).status(c).values())),
            Step("Folder structure (root, models, ComfyUI_Portable)",
                 lambda: c.root.is_dir() and c.path("models").is_dir() and c.path("comfyui").is_dir(),
                 "Create folders", self._create_dirs),
            Step("ComfyUI installation moved into ComfyUI_Portable",
                 lambda: ComfyUIModule(c).check()[0],
                 "Run move script", self._run_move),
            Step("Model library filled (AI_Modells has files)", self._models_filled),
            Step("Ollama installed", lambda: OllamaModule(c).check()[0]),
            Step("CLI launchers found (AI_CLI\\*.bat)", lambda: any(c.path("cli").glob("*.bat"))
                 if c.path("cli").is_dir() else False),
        ]

    def _models_filled(self) -> bool:
        m = self.cfg.path("models")
        return m.is_dir() and any(p.is_file() and p.suffix in (".safetensors", ".gguf", ".ckpt", ".pt")
                                  for p in m.rglob("*"))

    def _create_dirs(self) -> None:
        for key in ("models", "comfyui"):
            self.cfg.path(key).mkdir(parents=True, exist_ok=True)
        self.log("Folders created")
        self.refresh()
        self.on_changed()

    def _run_runtime_setup(self) -> None:
        script = self.cfg.root / "AI_Launcher" / "Setup_Runtime.bat"
        self.procs.start("install:runtime", [str(script)], str(script.parent), None)
        self.log("Runtime setup started in its own console")

    def _run_move(self) -> None:
        script = self.cfg.root / "move-comfyui-install.ps1"
        if not script.exists():
            self.log(f"Move script not found: {script}")
            return
        cmd = ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)]
        self.procs.start("install:move", cmd, str(self.cfg.root), None)
        self.log("Move script started in its own console")

    def refresh(self) -> None:
        first_open = None
        for i, (st, b) in enumerate(zip(self.steps, self.buttons)):
            done = st.check()
            if not done and first_open is None:
                first_open = i
            colour = OK if done else (NEXT if first_open == i else LATER)
            b.setStyleSheet(f"background:{colour}; text-align:left; padding:6px; color:black;")
            self.actions[i].setEnabled(not done)
        self.next_label.setText("All steps done" if first_open is None
                                else f"Next step: {first_open + 1}) {self.steps[first_open].label}")
