"""Setup: ordered installation checklist with 'next step' guidance."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from PySide6.QtWidgets import QHBoxLayout, QLabel, QVBoxLayout, QWidget

from core.modules import ComfyUIModule, OllamaModule, ScraplingMcpModule
from .context import Ctx
from .theme import BAD, MUTED, OK, WARN
from .widgets import button, card, page_header


@dataclass
class Step:
    label: str
    hint: str
    check: Callable[[], bool]
    action_label: str = ""
    action: Callable[[], None] | None = None


class SetupPage(QWidget):
    def __init__(self, ctx: Ctx):
        super().__init__()
        self.setObjectName("Page")
        self.ctx = ctx
        self.steps = self._steps()
        self.dots, self.acts, self.hints = [], [], []
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 14, 18, 10)
        lay.addWidget(page_header("Setup", "Installation checklist for the portable workspace"))
        self.next = QLabel("")
        self.next.setStyleSheet("font-weight:700; font-size:11pt;")
        lay.addWidget(self.next)
        for i, st in enumerate(self.steps):
            f, fl = card(10)
            row = QHBoxLayout()
            dot = QLabel("●")
            dot.setMaximumWidth(24)
            txt = QVBoxLayout()
            t = QLabel(f"{i + 1}.  {st.label}")
            t.setStyleSheet("font-weight:700;")
            h = QLabel(st.hint)
            h.setObjectName("Muted")
            txt.addWidget(t)
            txt.addWidget(h)
            row.addWidget(dot)
            row.addLayout(txt, 1)
            act = button(st.action_label, "Primary", st.action) if st.action else None
            if act:
                row.addWidget(act)
            fl.addLayout(row)
            lay.addWidget(f)
            self.dots.append(dot)
            self.hints.append(h)
            self.acts.append(act)
        bar = QHBoxLayout()
        bar.addStretch(1)
        bar.addWidget(button("Check status", "", self.refresh))
        lay.addLayout(bar)
        lay.addStretch(1)
        self.refresh()

    def _steps(self) -> list[Step]:
        c = self.ctx.cfg
        rt = c.path("runtime")
        return [
            Step("Shared runtime", "@Runtime\\python (CPython 3.13) with PySide6, psutil, PyInstaller",
                 lambda: (rt / "python" / "python.exe").exists(), "Run runtime setup", self._runtime),
            Step("Runtime packages complete", "PySide6 and psutil present in the runtime",
                 lambda: (rt / "python" / "Lib" / "site-packages" / "PySide6").exists()
                 and (rt / "python" / "Lib" / "site-packages" / "psutil").exists(), "Repair packages", self._runtime),
            Step("Folder structure", "Root, model library and ComfyUI folder",
                 lambda: c.root.is_dir() and c.path("models").is_dir() and c.path("comfyui").is_dir(),
                 "Create folders", self._dirs),
            Step("NVIDIA RTX GPU + driver", "nvidia-smi must answer; ComfyUI CUDA 13.0 build needs a current driver (R580 or newer)",
                 self._gpu_ok),
            Step("ComfyUI installed (NVIDIA, CUDA 13.0, Python 3.13)",
                 "Official Comfy-Org portable: ComfyUI_windows_portable_nvidia -> python_embeded + ComfyUI",
                 lambda: ComfyUIModule(c).check()[0], "Download + extract", self._install_comfy),
            Step("ComfyUI-Manager enabled", "Manager package in python_embeded (launcher then adds --enable-manager)",
                 lambda: ComfyUIModule._manager_installed(c.path("comfyui")), "Install Manager", self._comfy_manager),
            Step("ComfyUI workflow templates complete", "All comfyui-workflow-templates-* sub packages installed (empty Templates browser otherwise)",
                 self._templates_ok, "Repair templates",
                 lambda: self._script(self.ctx.cfg.root / "AI_Launcher" / "Repair_ComfyUI_Templates.ps1", "ComfyUI templates repair")),
            Step("Model library filled", "AI_Modells contains model files",
                 self._models_filled),
            Step("Ollama installed", "ollama.exe inside AI_Ollama_portable", lambda: OllamaModule(c).check()[0],
                 "Install Ollama", lambda: self._script(c.root / "AI_Ollama_portable" / "install_ollama_portable.bat", "Ollama install")),
            Step("llama.cpp installed", "AI_Ollama_CCP\\bin\\llama-server.exe (CUDA build)",
                 lambda: (c.path("llamacpp") / "bin" / "llama-server.exe").is_file(),
                 "Install llama.cpp", lambda: self._script(c.path("llamacpp") / "install_llamacpp.ps1", "llama.cpp install")),
            Step("Scrapling installed", "AI_Scrapling\\site-packages\\scrapling (web scraping: MCP server + shell)",
                 lambda: ScraplingMcpModule(c).check()[0],
                 "Install Scrapling", lambda: self._script(c.path("scrapling") / "Install_Scrapling.ps1", "Scrapling install")),
            Step("Scrapling browsers", "AI_Scrapling\\browsers (Chromium for stealth / dynamic fetchers)",
                 lambda: (c.path("scrapling") / "browsers").is_dir() and any((c.path("scrapling") / "browsers").iterdir()),
                 "Install browsers", lambda: self._script(c.path("scrapling") / "Install_Scrapling.ps1", "Scrapling browsers")),
            Step("OpenWebUI reachable", "External server (Home Assistant) answers on its port", self._openwebui_up),
            Step("CLI launchers", "AI_CLI\\*.bat (Claude, Codex, Copilot, Happy, Antigravity)",
                 lambda: c.path("cli").is_dir() and any(c.path("cli").glob("*.bat"))),
        ]

    def _templates_ok(self) -> bool:
        """True if every pinned sub package of comfyui-workflow-templates is installed in python_embeded."""
        site = self.ctx.cfg.path("comfyui") / "python_embeded" / "Lib" / "site-packages"
        if not site.is_dir():
            return False
        import re
        metas = list(site.glob("comfyui_workflow_templates-*.dist-info"))
        if not metas:
            return False
        try:
            text = (metas[0] / "METADATA").read_text(encoding="utf-8", errors="replace")
        except OSError:
            return False
        for line in text.splitlines():
            m = re.match(r"Requires-Dist:\s*([A-Za-z0-9_.-]+)==([^\s;]+)\s*$", line)
            if m and "extra ==" not in line:
                norm = re.sub(r"[-.]", "_", m.group(1)).lower()
                if not any(d.name.lower() == f"{norm}-{m.group(2)}.dist-info" for d in site.glob(f"{norm}-*.dist-info")):
                    return False
        return True

    def _script(self, script, title: str) -> None:
        """Run an installer (.ps1 / .bat) captured; output goes to the Processes tab."""
        import os
        from pathlib import Path
        script = str(script)
        if not os.path.exists(script):
            self.ctx.log(f"Installer not found: {script}")
            return
        cmd = (["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script]
               if script.lower().endswith(".ps1") else ["cmd.exe", "/c", script])
        self.ctx.procs.start("install:" + title.lower().replace(" ", "-"), cmd, str(Path(script).parent), None, title=title)
        self.ctx.log(f"{title} started (see Processes)")
        self.ctx.goto("processes")

    def _openwebui_up(self) -> bool:
        from core.modules import ExternalModule
        from core.procs import port_open
        for e in self.ctx.cfg.data.get("external_services", []):
            m = ExternalModule(self.ctx.cfg, e)
            if port_open(m.port(), m.host, 0.4):
                return True
        return False

    def _models_filled(self) -> bool:
        from core.models import MODEL_EXT
        m = self.ctx.cfg.path("models")
        if not m.is_dir():
            return False
        import os
        for _d, _s, files in os.walk(m):
            if any(os.path.splitext(f)[1].lower() in MODEL_EXT for f in files):
                return True
        return False

    def _dirs(self) -> None:
        for k in ("models", "comfyui"):
            self.ctx.cfg.path(k).mkdir(parents=True, exist_ok=True)
        self.ctx.log("Folders created")
        self.refresh()
        self.ctx.refresh_services()

    def _runtime(self) -> None:
        s = self.ctx.cfg.root / "AI_Launcher" / "Setup_Runtime.bat"
        self.ctx.procs.start("install:runtime", [str(s)], str(s.parent), None, title="Runtime setup")
        self.ctx.log("Runtime setup started (see Processes)")
        self.ctx.goto("processes")

    def _gpu_info(self) -> tuple[str, str] | None:
        """(GPU name, driver version) from nvidia-smi, or None."""
        import subprocess
        import sys
        try:
            r = subprocess.run(["nvidia-smi", "--query-gpu=name,driver_version", "--format=csv,noheader"],
                               capture_output=True, text=True, timeout=6,
                               creationflags=0x08000000 if sys.platform == "win32" else 0)
            line = r.stdout.strip().splitlines()[0] if r.returncode == 0 and r.stdout.strip() else ""
            name, _, drv = line.partition(",")
            return (name.strip(), drv.strip()) if name.strip() else None
        except (OSError, subprocess.SubprocessError, IndexError):
            return None

    def _gpu_ok(self) -> bool:
        info = self._gpu_info()
        self._gpu_text = ""
        if not info:
            return False
        name, drv = info
        try:
            major = int(drv.split(".")[0])
        except ValueError:
            major = 0
        self._gpu_text = f"{name}  |  driver {drv}" + ("" if major >= 580 else "  |  older than R580: update the NVIDIA driver for CUDA 13.0")
        return "RTX" in name.upper() and major >= 580

    def _comfy_manager(self) -> None:
        root = self.ctx.cfg.path("comfyui")
        py, req = root / "python_embeded" / "python.exe", root / "ComfyUI" / "manager_requirements.txt"
        if not py.is_file() or not req.is_file():
            self.ctx.log(f"Manager requirements not found: {req}")
            return
        self.ctx.procs.start("install:comfy-manager", [str(py), "-s", "-m", "pip", "install", "-r", str(req), "--no-warn-script-location"],
                             str(root), None, title="ComfyUI-Manager")
        self.ctx.goto("processes")

    def _install_comfy(self) -> None:
        self._script(self.ctx.cfg.root / "AI_Launcher" / "Install_ComfyUI.ps1", "ComfyUI install")

    def _move(self) -> None:
        s = self.ctx.cfg.root / "move-comfyui-install.ps1"
        if not s.exists():
            self.ctx.log(f"Move script not found: {s}")
            return
        self.ctx.procs.start("install:move", ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(s)],
                             str(self.ctx.cfg.root), None, title="ComfyUI move")
        self.ctx.log("Move script started (see Processes)")
        self.ctx.goto("processes")

    def refresh(self) -> None:
        first = None
        for i, st in enumerate(self.steps):
            done = st.check()
            if not done and first is None:
                first = i
            col = OK if done else (WARN if first == i else MUTED)
            self.dots[i].setStyleSheet(f"color:{col}; font-size:18px;")
            if st.label.startswith("NVIDIA") and getattr(self, "_gpu_text", ""):
                self.hints[i].setText(self._gpu_text)
            if self.acts[i]:
                self.acts[i].setEnabled(not done)
        self.next.setText("All steps done" if first is None else f"Next step: {first + 1}. {self.steps[first].label}")
        self.next.setStyleSheet(f"font-weight:700; font-size:11pt; color:{OK if first is None else '#102A43'};")
