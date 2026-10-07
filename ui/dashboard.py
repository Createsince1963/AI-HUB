"""Dashboard: live tiles + service control."""
from __future__ import annotations

import os
import threading
import time
import urllib.request
import webbrowser

from PySide6.QtCore import QThread, Qt, QTimer, Signal
from PySide6.QtWidgets import (QCheckBox, QInputDialog, QMessageBox, QComboBox, QGridLayout, QHBoxLayout, QLabel, QMenu, QProgressBar, QPushButton, QScrollArea, QTabBar,
                               QSizePolicy, QSplitter, QVBoxLayout, QWidget)

from core import mcp, models, ollama_ops, profiles, provider
from core.library import list_gguf, list_ollama
from core.modules import Module, discover_modules
from core.net import OllamaClient, http_json
from core.procs import port_open
from core.system import fmt_size
from .context import Ctx
from . import theme
from .theme import ACCENT, BAD, MUTED, OK, WARN
from .top_processes import ProcessTable, TopProcessesWindow, snapshot_lines
from .widgets import CoreBars, Meter, SparkChart, button, card, page_header, section

STATE_COL = {"running": OK, "online": OK, "starting": WARN, "stopped": BAD, "offline": BAD}

# Backend-kind badge (core.modules.Module.backend_kind()): local model/GPU on this PC vs a
# cloud-API CLI tool vs an always-on network service elsewhere. For CLI tools (BatModule) this
# is LIVE and clickable - see ServiceRow.backend_toggleable / Dashboard.provider_menu(): switching
# a profile to "local" routes that CLI to whichever local server actually answers right now.
BACKEND_BADGE = {
    "local":     ("Local AI", OK),      # ComfyUI, Ollama, llama.cpp, Scrapling - or a CLI tool routed to one of them
    "api":       ("API", WARN),         # cloud-API CLI tool (default for Claude/Codex/Copilot/Antigravity/Happy)
    "local-off": ("Local (offline)", BAD),   # CLI tool set to "local" but no local server answers right now
    "cloud":     ("Cloud", "#8e5ce6"),  # always-on service reachable over the network, not on this PC
}


def _backend_label(kind: str) -> tuple[str, str]:
    return BACKEND_BADGE.get(kind, ("", ""))


def _set_text(w, text: str) -> None:
    """QLabel/QPushButton.setText(same text) still repolishes/repaints on Windows every call - guard it."""
    if w.text() != text:
        w.setText(text)


def _set_enabled(w, enabled: bool) -> None:
    """QWidget.setEnabled(same value) still re-triggers a native style/accessibility repaint on
    Windows every call. Dashboard.refresh() runs every second on every row's widgets regardless of
    whether anything actually changed, and on some monitors/DPI scalings (e.g. "LG SDQHD") that
    constant churn is what produced the QWindowsWindow::setGeometry spam and the flickering
    "GoWindow"/"DangerWindow" native helper windows - guarding every setter below so it only fires on
    an actual change removes the churn instead of just tweaking widget sizes."""
    if w.isEnabled() != enabled:
        w.setEnabled(enabled)


def _set_tooltip(w, tip: str) -> None:
    if w.toolTip() != tip:
        w.setToolTip(tip)


def _set_style(w, qss: str) -> None:
    if w.styleSheet() != qss:
        w.setStyleSheet(qss)


def _style_backend(widget, kind: str, clickable: bool) -> None:
    """Apply the Local/Cloud/API/offline badge text + colour to a QLabel or QPushButton and
    show/hide it. Shared by ServiceRow's initial build and Dashboard's live refresh(), so a CLI
    tool's badge stays correct as the local server is started/stopped while the GUI is open."""
    label, colour = _backend_label(kind)
    if widget.isVisible() != bool(label):
        widget.setVisible(bool(label))
    if not label:
        return
    _set_text(widget, label)
    _set_style(widget, f"font-size:8pt; font-weight:700; color:{colour}; border:1px solid {colour}; "
                        "border-radius:6px; padding:0 5px; background:transparent;")
    if clickable:
        _set_tooltip(widget, "Klicken: Cloud-API oder lokales Modell (llama.cpp/Ollama) für dieses Profil wählen")


def http_ready(host: str, port: int, path: str, timeout: float = 1.5) -> bool:
    """True when GET http://host:port/path answers 2xx (llama-server: /health is 503 while the model loads)."""
    try:
        with urllib.request.urlopen(f"http://{host}:{port}{path}", timeout=timeout) as r:
            return 200 <= r.status < 300
    except Exception:        # noqa: BLE001 - 503 / refused / timeout all mean "not ready yet"
        return False


class StatusPoller(QThread):
    """Does all blocking checks (file access on the external SSD, port probes, version files) off the UI thread.

    While a big model loads the SSD is saturated; doing these calls in the UI thread made the window
    show 'not responding'."""
    result = Signal(dict)        # module id -> (ok, message, port_open, version, ready)

    def __init__(self, get_mods, cfg=None, interval: float = 2.0):
        super().__init__()
        self.get_mods, self.cfg, self.interval, self._stop = get_mods, cfg, interval, False

    def stop(self) -> None:
        self._stop = True
        self.wait(3000)

    def run(self) -> None:
        while not self._stop:
            out = {}
            if self.cfg is not None:
                # warms provider._cache (provider.cached_local_source()) so the UI thread's
                # refresh() - BatModule.backend_kind() / Dashboard._update_local_ai_banner() -
                # never has to open the TCP socket itself. See core/provider.py for why.
                try:
                    provider.local_source(self.cfg)
                except Exception:        # noqa: BLE001 - stale cache beats a dead poller thread
                    pass
            for m in list(self.get_mods()):
                if self._stop:
                    return
                try:
                    ok, msg = m.check()
                    port = m.port()
                    po = port_open(port, m.host, 0.4 if m.external else 0.15) if port else False
                    ver = "" if m.external else m.version()
                    hp = getattr(m, "health_path", None)
                    ready = po and (not hp or m.external or http_ready(m.host, port, hp))
                    out[m.id] = (ok, msg, po, ver, ready)
                except Exception as exc:        # noqa: BLE001
                    out[m.id] = (False, f"check failed: {exc}", False, "", False)
            self.result.emit(out)
            self.msleep(int(self.interval * 1000))


class ModelScanner(QThread):
    """Reads the GGUF library and the installed Ollama models off the UI thread (external SSD)."""
    result = Signal(list, list)         # gguf [(label, value)], ollama [name]

    def __init__(self, cfg):
        super().__init__()
        self.cfg = cfg

    def run(self) -> None:
        try:
            self.result.emit(list_gguf(self.cfg), list_ollama(self.cfg))
        except Exception:        # noqa: BLE001
            self.result.emit([], [])


PICKER_MODULES = ("llamacpp", "ollama")       # services that get a model selector

# Service row grid geometry (identical in every row, so the buttons line up in columns)
BTN_W, BTN_H = 88, 30          # every action button has the same size
# Grid of every service card (identical in all cards, so the selector fields line up under each other):
#   0 name | 1 profile | 2 AI model | 3 MCP | 4 info (stretch) | 5.. badge + action buttons
# A model picker (Ollama / llama.cpp) spans the three selector columns. The selector columns have a minimum width
# (nothing is cut off at normal window sizes) and share extra space by stretch factor.
NAME_W = 255                    # fits "OpenWebUI (Home Assistant server)"
COL_NAME, COL_PROFILE, COL_AIMODEL, COL_MCP, COL_INFO, COL_ACT = 0, 1, 2, 3, 4, 5
SEL_COLS = {COL_PROFILE: (175, 4), COL_AIMODEL: (240, 6), COL_MCP: (130, 3)}      # column -> (minimum width, stretch)
PICK_W = sum(v[0] for v in SEL_COLS.values())
PICK_H = 32


class ServiceRow:
    def __init__(self, mod: Module):
        # Hidden parent for every widget of the row until rebuild() puts them into the card grid.
        # Without it, setVisible(True) on a still parentless button turned it into its own top-level
        # window ("QWidgetWindow/GoWindow" = Start, ".../DangerWindow" = Stop) -> mini windows + the
        # endless "QWindowsWindow::setGeometry: Unable to set geometry 88x30..." warnings.
        self._park = QWidget()
        self.mod = mod
        self.dot = QLabel("●", self._park)
        self.name = QLabel(mod.name, self._park)
        self.name.setStyleSheet("font-weight:700;")
        # "Local AI" / "API" / "Local (offline)" / "Cloud" badge, see _backend_label()/_style_backend().
        # For a CLI tool with its own profile (Claude/Codex/Copilot/Antigravity), the badge is a real
        # button: clicking it opens Dashboard.provider_menu() to switch that profile between the
        # cloud API and a local server. Other rows (ComfyUI/Ollama/llama.cpp/Scrapling/External) get a
        # plain label - their backend kind is fixed, there is nothing to toggle.
        self.backend_toggleable = mod.id.startswith("cli:") and profiles.supported(getattr(mod, "bat").stem)
        if self.backend_toggleable:
            self.backend = QPushButton("", self._park)
            self.backend.setFlat(True)
            self.backend.setCursor(Qt.PointingHandCursor)
            self.backend.setAutoDefault(False)
            self.backend.setDefault(False)
        else:
            self.backend = QLabel("", self._park)
            self.backend.setAlignment(Qt.AlignCenter)
        _style_backend(self.backend, mod.backend_kind(), self.backend_toggleable)
        self.info = QLabel("", self._park)
        self.info.setObjectName("Muted")
        self.start = QPushButton("Start", self._park)
        self.start.setObjectName("Go")
        self.stop = QPushButton("Stop", self._park)
        self.stop.setObjectName("Danger")
        self.restart = QPushButton("Restart", self._park)
        self.open = QPushButton("Open", self._park)
        self.update = QPushButton("Update", self._park)
        self.update.setVisible(not mod.external and bool(mod.update_options()))
        self.progress: QProgressBar | None = None     # Ollama: server start / model load into VRAM
        if mod.id == "ollama":
            self.progress = QProgressBar(self._park)
            self.progress.setMaximumHeight(18)
            self.progress.setTextVisible(True)
            self.progress.setAlignment(Qt.AlignCenter)
            self.progress.setVisible(False)
        # Ollama / llama.cpp: current tuning values (from config.json) + shortcut to the Tuning page
        self.tune_label: QLabel | None = None
        self.tune_btn: QPushButton | None = None
        if mod.id in ("ollama", "llamacpp"):
            self.tune_label = QLabel("", self._park)
            self.tune_label.setObjectName("Muted")
            self.tune_btn = QPushButton("Tuning / Messwerte", self._park)
            self.tune_btn.setAutoDefault(False)
            self.tune_btn.setToolTip("Öffnet die Tuning-Seite: Parameter, Live-Test, Maximum messen und alle Live-Messwerte")
        self.model: QComboBox | None = None
        if mod.id in PICKER_MODULES:
            self.model = QComboBox(self._park)
            self.model.setMinimumHeight(PICK_H)
            self.model.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
            self.model.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
            self.model.setMinimumContentsLength(20)
            self.model.setToolTip("Model to use - saved as start option; applied at the next start")
        self.profile: QPushButton | None = None      # profile of the AI_CLI launcher (own login / sessions per profile)
        if mod.id.startswith("cli:") and profiles.supported(getattr(mod, "bat").stem):
            self.profile = QPushButton("Profile: default", self._park)
            self.profile.setMinimumHeight(PICK_H)
            self.profile.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
            self.profile.setToolTip("Profile used at the next start (the last one you choose is remembered)")
        self.mcp: QPushButton | None = None          # MCP server selection (CLI tools with an adapter in core/mcp.py)
        if mod.id.startswith("cli:") and mcp.supported(getattr(mod, "bat").stem):
            self.mcp = QPushButton("MCP: none", self._park)
            self.mcp.setMinimumHeight(PICK_H)
            self.mcp.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
            self.mcp.setToolTip("MCP servers passed to this tool at the next start (Scrapling is started automatically)")
        self.aimodel: QPushButton | None = None      # AI model + reasoning-effort (core/models.py, mirrors the Node launcher's model-select.mjs)
        if mod.id.startswith("cli:") and profiles.supported(getattr(mod, "bat").stem):
            self.aimodel = QPushButton("Modell: Standard", self._park)
            self.aimodel.setMinimumHeight(PICK_H)
            self.aimodel.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
            self.aimodel.setToolTip("AI model + reasoning-effort (Stärke) for this profile - applied at the next start")
        self.open.setVisible(bool(mod.url()))
        for b in (self.start, self.stop, self.restart):
            b.setVisible(not mod.external)
        # same minimum size for all buttons; a hidden button keeps its grid cell so the columns stay
        # aligned. A hard setFixedSize (equal min/max) used to be set here, but on some monitors/DPI
        # scalings (e.g. "LG SDQHD") that exact pixel lock fought with Windows' own DPI resizing and
        # produced a stream of QWindowsWindow::setGeometry warnings plus flickering helper windows
        # ("QWidgetWindow/GoWindow", ".../DangerWindow") - setMinimumSize alone still keeps the grid
        # columns aligned without forcing that exact size.
        for b in self.buttons:
            b.setMinimumSize(BTN_W, BTN_H)
            sp = b.sizePolicy()
            sp.setRetainSizeWhenHidden(True)
            b.setSizePolicy(sp)
            # autoDefault (Qt default: on) makes the WindowsVista style draw a pulsing "default button"
            # glow via its own native helper window. refresh() toggles setEnabled() on these buttons every
            # second, which keeps re-creating/re-positioning that helper window. These buttons are never
            # meant to be triggered by Enter anyway, so just turn autoDefault off.
            b.setAutoDefault(False)
            b.setDefault(False)
        for b in (self.profile, self.mcp, self.aimodel):     # same fix for the other push buttons in the row
            if b is not None:
                b.setAutoDefault(False)
                b.setDefault(False)
        # the backend badge sits right before Start, in the button row - same minimum size as the
        # action buttons so it lines up in its own grid column instead of floating next to the name
        self.backend.setMinimumSize(BTN_W, BTN_H)
        sp = self.backend.sizePolicy()
        sp.setRetainSizeWhenHidden(True)
        self.backend.setSizePolicy(sp)

    @property
    def buttons(self) -> tuple:
        """Grid order of the action buttons (one column each)."""
        return (self.start, self.stop, self.restart, self.update, self.open)

    @property
    def action_cells(self) -> tuple:
        """Grid order of the whole action zone - backend badge first (same size as a button), then the actions."""
        return (self.backend,) + self.buttons


class Dashboard(QWidget):
    logline = Signal(str)          # log from worker threads
    load_done = Signal(str, bool, str)      # Ollama model load finished: model, ok, error text

    def __init__(self, ctx: Ctx):
        super().__init__()
        self.setObjectName("Page")
        self.ctx = ctx
        self.rows: list[ServiceRow] = []
        self._cache: dict[str, tuple] = {}
        self._launched: set[str] = set()      # started from this launcher, "ready" message still pending
        self._toast: QMessageBox | None = None
        self._gguf: list[tuple[str, str]] = []
        self._ollama: list[str] = []
        self._scanner: ModelScanner | None = None
        self.logline.connect(self.ctx.log)
        self.load_done.connect(self._on_load_done)
        self._load: dict | None = None        # running Ollama model load (progress bar)
        self._vram_mb = 0.0                   # latest used VRAM from the monitor
        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 10, 18, 8)
        outer.setSpacing(6)
        head = QHBoxLayout()
        head.addWidget(page_header("Dashboard", "Live system load, top processes and control of all portable AI services"), 1)
        head.addWidget(QLabel("Refresh"))
        self.interval = QComboBox()
        for label, v in (("0.5 s", 0.5), ("1 s", 1.0), ("2 s", 2.0), ("5 s", 5.0)):
            self.interval.addItem(label, v)
        self.interval.setCurrentIndex(1)
        self.interval.currentIndexChanged.connect(lambda: setattr(ctx.monitor, "interval", self.interval.currentData()))
        head.addWidget(self.interval)
        outer.addLayout(head)

        # ---- live tiles (2 x 4)
        tiles = QGridLayout()
        tiles.setSpacing(10)
        # CPU-Komponente (Orange/Warm)
        self.t_cpu = SparkChart("CPU", WARN)
        # RAM/Memory-Komponente (Grün)
        self.t_ram = SparkChart("RAM", OK)
        # GPU-Komponente (Blau - ACCENT)
        self.t_gpu = SparkChart("GPU load", ACCENT)
        self.t_vram = SparkChart("VRAM", ACCENT)
        self.t_temp = SparkChart("GPU temp", WARN, maximum=100, unit=" °C")
        self.t_power = SparkChart("GPU power", ACCENT, unit=" W", auto_scale=True)
        # Storage & Network (Teal)
        self.t_disk = SparkChart("Disk I/O", theme.col("chunk"), unit=" MB/s", auto_scale=True)
        self.t_net = SparkChart("Network", theme.col("chunk"), unit=" MB/s", auto_scale=True)
        for i, t in enumerate((self.t_cpu, self.t_ram, self.t_gpu, self.t_vram,
                               self.t_temp, self.t_power, self.t_disk, self.t_net)):
            t.setMinimumHeight(74)
            t.setMaximumHeight(84)
            tiles.addWidget(t, i // 4, i % 4)
            tiles.setColumnStretch(i % 4, 1)
        outer.addLayout(tiles)

        # ---- CPU cores + memory meters (carried over from the former System monitor page)
        mid = QHBoxLayout()
        mid.setSpacing(10)
        sz_h = 152      # compact row: everything above the services/processes split stays small, the rest is left to them
        cf, cl = card(8)
        cl.setSpacing(2)
        hl = QHBoxLayout()
        hl.addWidget(section("CPU cores (logical)"))
        hl.addStretch(1)
        self.freq = QLabel("")
        self.freq.setObjectName("Muted")
        hl.addWidget(self.freq)
        cl.addLayout(hl)
        self.cores = CoreBars()
        self.cores.setMinimumHeight(40)
        cl.addWidget(self.cores)
        cf.setMaximumHeight(sz_h)
        self._mid_cards = [cf]
        mid.addWidget(cf, 3)
        mf, ml = card(8)
        ml.setSpacing(3)
        ml.addWidget(section("Memory"))
        self.m_ram, self.m_swap, self.m_vram = Meter("RAM"), Meter("Page file"), Meter("VRAM")
        for m in (self.m_ram, self.m_swap, self.m_vram):
            ml.addWidget(m)
        mf.setMaximumHeight(sz_h)
        self._mid_cards.append(mf)
        mid.addWidget(mf, 2)
        gf, gl = card(8)
        gl.setSpacing(2)
        gl.addWidget(section("Graphics cards"))
        self.gpu_list = QLabel("reading ...")
        self.gpu_list.setTextFormat(Qt.RichText)
        self.gpu_list.setWordWrap(True)
        self.gpu_list.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.gpu_tabs = QTabBar()
        self.gpu_tabs.setExpanding(False)
        self.gpu_tabs.setDrawBase(True)
        for _key, title in self.GPU_TABS:
            self.gpu_tabs.addTab(title)
        self.gpu_tabs.currentChanged.connect(lambda _i: self._render_gpu())
        self._gpus: list[dict] = []
        gl.addWidget(self.gpu_tabs)
        gsc = QScrollArea()
        gsc.setWidgetResizable(True)
        gsc.setFrameShape(QScrollArea.NoFrame)
        gsc.setStyleSheet("background: transparent;")
        gsc.setWidget(self.gpu_list)
        gl.addWidget(gsc, 1)
        gf.setMaximumHeight(sz_h)
        self._mid_cards.append(gf)
        mid.addWidget(gf, 3)
        outer.addLayout(mid)

        # ---- services | top processes (resizable split)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QScrollArea.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        svc = QWidget()
        svc.setObjectName("Page")
        sl = QVBoxLayout(svc)
        sl.setContentsMargins(0, 0, 0, 0)
        sl.addWidget(section("Services"))
        self.local_ai_banner = QLabel("")     # live "Lokale KI"-Status, see _local_ai_summary()
        self.local_ai_banner.setWordWrap(True)
        self.local_ai_banner.setVisible(False)
        sl.addWidget(self.local_ai_banner)
        sl.addWidget(self.scroll, 1)

        self._procs: list[dict] = []
        self._win: TopProcessesWindow | None = None
        pc, pl = card(12)
        ph = QHBoxLayout()
        ph.addWidget(section("Top processes (CPU / RAM)"))
        ph.addStretch(1)
        self.autolog = QCheckBox("Log every 60 s")
        self.autolog.toggled.connect(lambda on: self.log_timer.start(60000) if on else self.log_timer.stop())
        ph.addWidget(self.autolog)
        ph.addWidget(button("Log snapshot", "", self.log_snapshot, "Write the current top processes to the output log"))
        ph.addWidget(button("Open in window", "", self.open_window, "Free-standing, resizable window"))
        self.proc_collapse_btn = QPushButton("▾ Einklappen")
        self.proc_collapse_btn.setToolTip("Bereich einklappen, um mehr Platz für Start/Admin (Services) zu schaffen")
        self.proc_collapse_btn.clicked.connect(self._toggle_proc_collapse)
        ph.addWidget(self.proc_collapse_btn)
        pl.addLayout(ph)
        self.proc_table = ProcessTable(ctx, limit=12, show_kill=False)
        pl.addWidget(self.proc_table, 1)
        self.log_timer = QTimer(self)
        self.log_timer.timeout.connect(self.log_snapshot)
        self._pc_card = pc
        self._proc_collapsed = False

        split = QSplitter(Qt.Vertical)
        split.setChildrenCollapsible(False)
        split.addWidget(svc)
        split.addWidget(pc)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)
        split.setSizes([210, 240])
        self._split = split
        self._split_sizes_expanded = [210, 240]
        outer.addWidget(split, 1)

        # ---- bottom bar
        bar = QHBoxLayout()
        self.hint = QLabel("")
        self.hint.setWordWrap(True)
        self.hint.setObjectName("Muted")
        bar.addWidget(self.hint, 1)
        for text, fn in (("Open root folder", lambda: self._open(self.ctx.cfg.root)),
                         ("Open models folder", lambda: self._open(self.ctx.cfg.path("models"))),
                         ("Start all", self.start_all), ("Stop all", self.stop_all)):
            b = button(text, "Danger" if text == "Stop all" else "", fn)
            bar.addWidget(b)
        outer.addLayout(bar)

        ctx.monitor.sample.connect(self.on_sample)
        self.rebuild()
        self.poller = StatusPoller(lambda: [r.mod for r in self.rows], cfg=ctx.cfg)
        self.poller.result.connect(self._on_status)
        self.poller.start()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(1000)            # cheap: only reads the poller cache + process state
        self._toggle_proc_collapse()      # default: "Top processes" eingeklappt - mehr Platz für Services

    # ---------------------------------------------------------------- live data
    def on_sample(self, d: dict) -> None:
        if "error" in d:
            self.hint.setText(d["error"])
            return
        self.t_cpu.push(d["cpu"], f"{d['cpu']:.0f}%", f"{len(d['cores'])} threads")
        self.t_ram.push(d["ram_pct"], f"{d['ram_pct']:.0f}%", f"{fmt_size(d['ram_used'])} / {fmt_size(d['ram_total'])}")
        self.cores.set_cores(d["cores"])
        self.freq.setText(f"Clock {d['cpu_freq'] / 1000:.2f} GHz" if d["cpu_freq"] else "")
        ram = f"{fmt_size(d['ram_used'])} / {fmt_size(d['ram_total'])}"
        self.m_ram.set(d["ram_pct"], ram)
        sp = 100 * d["swap_used"] / d["swap_total"] if d["swap_total"] else 0
        self.m_swap.set(sp, f"{fmt_size(d['swap_used'])} / {fmt_size(d['swap_total'])}")
        rd, wr = d["disk_read"] / 1024 ** 2, d["disk_write"] / 1024 ** 2
        self.t_disk.push(rd + wr, f"{rd + wr:.1f} MB/s", f"R {rd:.0f} / W {wr:.0f}")
        dn, up = d["net_down"] / 1024 ** 2, d["net_up"] / 1024 ** 2
        self.t_net.push(dn + up, f"{dn + up:.2f} MB/s", f"↓ {dn:.1f} / ↑ {up:.1f}")
        g = d.get("gpu")
        if g:
            self._vram_mb = float(g["used_mb"])
            pct = 100 * g["used_mb"] / max(g["total_mb"], 1)
            self.t_gpu.push(g["util"], f"{g['util']}%", f"{g['clock']} MHz")
            self.t_vram.push(pct, f"{g['used_mb'] / 1024:.1f} GB", f"of {g['total_mb'] / 1024:.0f} GB")
            self.m_vram.set(pct, f"{g['used_mb'] / 1024:.1f} / {g['total_mb'] / 1024:.1f} GB")
            self.t_temp.push(g["temp"], f"{g['temp']} °C", "safe < 80 °C" if g["temp"] < 80 else "hot")
            self.t_power.push(g["power"], f"{g['power']:.0f} W", f"limit {g['power_limit']:.0f} W")
        else:
            self.t_gpu.push(0, "n/a", "no NVIDIA GPU")
            for t in (self.t_vram, self.t_temp, self.t_power):
                t.push(0, "n/a")
            self.m_vram.set(0, "n/a")
        if "gpus" in d:
            self._show_gpus(d["gpus"])
        if "procs" in d:
            self._procs = d["procs"]
            self.proc_table.fill(self._procs)

    GPU_TABS = (("rtx50", "RTX 50xx"), ("m1200", "M1200 (internal)"), ("intel", "Intel (internal)"))

    def _show_gpus(self, gpus: list[dict]) -> None:
        self._gpus = gpus
        # "other" adapters (AMD, Microsoft basic ...) get a tab only while present
        others = [g for g in gpus if g.get("group", "other") == "other"]
        has_other = self.gpu_tabs.count() > len(self.GPU_TABS)
        if others and not has_other:
            self.gpu_tabs.addTab("Other")
        elif not others and has_other:
            self.gpu_tabs.removeTab(len(self.GPU_TABS))
        self._render_gpu()

    def _render_gpu(self) -> None:
        keys = [k for k, _t in self.GPU_TABS] + ["other"]
        i = self.gpu_tabs.currentIndex()
        key = keys[i] if 0 <= i < len(keys) else keys[0]
        members = [g for g in self._gpus if g.get("group", "other") == key]
        if not self._gpus:
            self.gpu_list.setText(f'<span style="color:{MUTED}">no graphics adapter found</span>')
            return
        if not members:
            self.gpu_list.setText(f'<span style="color:{MUTED}">not detected</span>')
            return
        html = []
        for g in members:
            col = OK if g["active"] else MUTED
            mb = g.get("vram_mb")
            vram = (f"{mb / 1024:.0f} GB" if mb >= 1024 else f"{mb:.0f} MB") if mb else "n/a"
            if key == "intel" and mb:
                vram += " dedicated (rest shared with RAM)"
            html.append(f'<div style="margin-bottom:6px"><span style="color:{col}">●</span> <b>{g["name"]}</b><br>'
                        f'&nbsp;&nbsp;&nbsp;VRAM: <b>{vram}</b><br>'
                        f'&nbsp;&nbsp;&nbsp;Driver: <b>{g["driver"]}</b><br>'
                        f'&nbsp;&nbsp;&nbsp;Active: <b>{"yes" if g["active"] else "no"}</b></div>')
        self.gpu_list.setText("".join(html))

    # ---------------------------------------------------------------- top processes
    def _toggle_proc_collapse(self) -> None:
        """Collapse the 'Top processes' card down to its header bar, handing
        the freed vertical space in the splitter to Services (Start/Admin)."""
        self._proc_collapsed = not self._proc_collapsed
        self.proc_table.setVisible(not self._proc_collapsed)
        self.autolog.setVisible(not self._proc_collapsed)
        if self._proc_collapsed:
            self._split_sizes_expanded = self._split.sizes()
            self.proc_collapse_btn.setText("▸ Ausklappen")
            self._pc_card.setMaximumHeight(self._pc_card.sizeHint().height())
            total = sum(self._split_sizes_expanded) or 450
            self._split.setSizes([total - 40, 40])
        else:
            self.proc_collapse_btn.setText("▾ Einklappen")
            self._pc_card.setMaximumHeight(16777215)
            self._split.setSizes(self._split_sizes_expanded)

    def log_snapshot(self) -> None:
        for line in snapshot_lines(self._procs, 10):
            self.ctx.log(line)

    def open_window(self) -> None:
        if self._win is None:
            self._win = TopProcessesWindow(self.ctx, self.window())
            self._win._last = self._procs          # show data immediately, not after the next sample
        self._win.show()
        self._win.raise_()
        self._win.activateWindow()

    # ---------------------------------------------------------------- services
    def rebuild(self) -> None:
        body = QWidget()
        body.setObjectName("Page")
        lay = QVBoxLayout(body)
        lay.setSpacing(6)
        self.rows = []
        for mod in discover_modules(self.ctx.cfg):
            r = ServiceRow(mod)
            r.start.clicked.connect(lambda _=False, r=r: self.start_module(r))
            r.stop.clicked.connect(lambda _=False, r=r: self.stop_module(r))
            r.restart.clicked.connect(lambda _=False, r=r: self.restart_module(r))
            r.open.clicked.connect(lambda _=False, r=r: webbrowser.open(r.mod.url()))
            r.update.clicked.connect(lambda _=False, r=r: self.update_module(r))
            if r.backend_toggleable:
                r.backend.clicked.connect(lambda _=False, r=r: self.provider_menu(r))
            f, fl = card(10)
            # One grid per card, same columns in every card:
            #   0 dot+name (first element) | 1 selector zone | 2 info (stretch) | 3..9 badge Start Stop Restart Update Open
            grid = QGridLayout()
            grid.setHorizontalSpacing(8)
            grid.setVerticalSpacing(0)
            r.dot.setMaximumWidth(22)
            name_zone = QHBoxLayout()
            name_zone.setContentsMargins(0, 0, 0, 0)
            name_zone.setSpacing(6)
            name_zone.addWidget(r.dot)
            name_zone.addWidget(r.name)
            name_zone.addStretch(1)
            name_holder = QWidget()
            name_holder.setLayout(name_zone)
            name_holder.setMinimumWidth(NAME_W)
            r.name.setToolTip(r.mod.name)
            grid.addWidget(name_holder, 0, 0)          # left label is the grid's first element
            has_pick = False
            if r.model is not None:
                grid.addWidget(r.model, 0, COL_PROFILE, 1, 3)           # spans the three selector columns
                r.model.activated.connect(lambda _i, r=r: self.model_chosen(r))
                has_pick = True
            if r.profile is not None:
                grid.addWidget(r.profile, 0, COL_PROFILE)
                r.profile.clicked.connect(lambda _=False, r=r: self.profile_menu(r))
                self._profile_label(r)
                has_pick = True
            if r.aimodel is not None:
                grid.addWidget(r.aimodel, 0, COL_AIMODEL)
                r.aimodel.clicked.connect(lambda _=False, r=r: self.ai_model_menu(r))
                self._ai_model_label(r)
                has_pick = True
            if r.mcp is not None:
                grid.addWidget(r.mcp, 0, COL_MCP)
                r.mcp.clicked.connect(lambda _=False, r=r: self.mcp_menu(r))
                self._mcp_label(r)
                has_pick = True
            if has_pick:
                grid.addWidget(r.info, 0, COL_INFO)
            else:
                grid.addWidget(r.info, 0, COL_PROFILE, 1, 4)           # no selector: the info text uses the free space
            row_below = 1
            if r.tune_btn is not None:                                  # Ollama / llama.cpp: tuning values + shortcut, own line
                tune = QHBoxLayout()
                tune.setContentsMargins(0, 4, 0, 0)
                tune.addWidget(r.tune_label, 1)
                tune.addWidget(r.tune_btn)
                tune_holder = QWidget()
                tune_holder.setLayout(tune)
                grid.addWidget(tune_holder, row_below, COL_PROFILE, 1, 4)
                r.tune_btn.clicked.connect(lambda _=False, r=r: self.ctx.goto(f"tuning:{r.mod.id}"))
                row_below += 1
            if r.progress is not None:
                grid.addWidget(r.progress, row_below, COL_NAME, 1, 5)   # only visible while starting / loading
            # identical column widths in every card = selector fields line up under each other
            for col, (minw, stretch) in SEL_COLS.items():
                grid.setColumnMinimumWidth(col, minw)
                grid.setColumnStretch(col, stretch)
            grid.setColumnMinimumWidth(COL_NAME, NAME_W)
            grid.setColumnMinimumWidth(COL_INFO, 150)
            grid.setColumnStretch(COL_INFO, 3)
            # Local AI / API / Cloud badge sits right before Start, sized like the action buttons -
            # it always keeps its grid cell (even hidden) so the button columns stay aligned across rows.
            for i, b in enumerate(r.action_cells):
                grid.addWidget(b, 0, COL_ACT + i, Qt.AlignLeft | Qt.AlignVCenter)
                grid.setColumnMinimumWidth(COL_ACT + i, BTN_W)
            fl.addLayout(grid)
            lay.addWidget(f)
            self.rows.append(r)
        lay.addStretch(1)
        self.scroll.setWidget(body)
        self._fill_pickers()
        self.rescan_models()
        self.refresh()

    # ---------------------------------------------------------------- model pickers
    def on_show(self) -> None:
        self.rescan_models()

    def rescan_models(self) -> None:
        if self._scanner is not None and self._scanner.isRunning():
            return
        self._scanner = ModelScanner(self.ctx.cfg)
        self._scanner.result.connect(self._on_models)
        self._scanner.start()

    def _on_models(self, gguf: list, ollama: list) -> None:
        self._gguf, self._ollama = gguf, ollama
        self._fill_pickers()

    def _fill_pickers(self) -> None:
        for r in self.rows:
            if r.model is None:
                continue
            r.model.blockSignals(True)
            r.model.clear()
            if r.mod.id == "llamacpp":
                cur = str(self.ctx.cfg.data.get("llamacpp", {}).get("model", ""))
                items = list(self._gguf)
                if cur and cur not in [v for _l, v in items]:
                    items.insert(0, (os.path.basename(cur) + "   (configured)", cur))
                for label, value in items:
                    r.model.addItem(label, value)
                idx = r.model.findData(cur)
                r.model.setCurrentIndex(idx if idx >= 0 else -1)
                if not items:
                    r.model.setPlaceholderText("no LLM GGUF found (library or X_DOWNLOADS) - check the area in AI models > Manager")
            else:
                cur = str(self.ctx.cfg.data.get("ollama", {}).get("model", ""))
                r.model.addItem("(no preload - load on first request)", "")
                for name in self._ollama:
                    r.model.addItem(name, name)
                if cur and r.model.findData(cur) < 0:
                    gg = self._default_gguf_for(cur)
                    hint = (f"(NOT installed in Ollama - imported from GGUF on start, {os.path.getsize(gg) / 1024 ** 3:.1f} GB)"
                            if gg else "(NOT installed in Ollama)")
                    r.model.addItem(f"{cur}   {hint}", cur)
                r.model.setCurrentIndex(max(r.model.findData(cur), 0))
            r.model.blockSignals(False)

    def model_chosen(self, r: ServiceRow) -> None:
        value = r.model.currentData()
        section = "llamacpp" if r.mod.id == "llamacpp" else "ollama"
        self.ctx.cfg.data.setdefault(section, {})["model"] = value or ""
        try:
            self.ctx.cfg.save()
        except OSError as exc:
            self.ctx.log(f"[{r.mod.name}] could not save config: {exc}")
        self.ctx.log(f"[{r.mod.name}] model set: {value or '(none)'}")
        running = self._state(r) in ("running", "starting")
        if r.mod.id == "llamacpp" and running and self.ctx.procs.is_running(r.mod.id):
            if QMessageBox.question(self, r.mod.name, "Restart the server now with the new model?") == QMessageBox.Yes:
                self.restart_module(r)
        elif r.mod.id == "ollama" and running and value:
            self.preload_ollama(value)

    def _default_gguf_for(self, model: str) -> str:
        """GGUF path to import `model` from, if it is the configured default and the file exists - else ''."""
        sec = self.ctx.cfg.data.get("ollama", {})
        if not model or ollama_ops.normalize(model) != ollama_ops.normalize(str(sec.get("model", ""))):
            return ""
        raw = str(sec.get("default_gguf", "") or "")
        path = self.ctx.cfg.expand(raw) if raw else ""
        return path if path and os.path.isfile(path) else ""

    def preload_ollama(self, model: str) -> None:
        """Load the model into VRAM (keep_alive 30 min) so the first request is not slow."""
        cfg = self.ctx.cfg
        client = OllamaClient(cfg.host(), cfg.port("ollama"))
        load = {"model": model, "size": 0, "base_mb": self._vram_mb, "done": False}
        self._load = load
        self._update_progress()

        def work() -> None:
            gguf = self._default_gguf_for(model)
            if gguf and ollama_ops.normalize(model) not in ollama_ops.installed(cfg):
                self.logline.emit(f"[Ollama] default model {model} not installed - importing {os.path.basename(gguf)} (one-off, takes minutes) ...")
                try:
                    _sha, state = ollama_ops.create_linked(cfg, model.split(":")[0], gguf)   # hard link, no 2nd copy
                    self.logline.emit(f"[Ollama] blob {state}")
                    self.logline.emit(f"[Ollama] import of {model} done")
                except Exception as exc:        # noqa: BLE001
                    self.load_done.emit(model, False, f"import failed: {exc}")
                    return
            self.logline.emit(f"[Ollama] loading {model} into VRAM ...")
            try:                                     # model size -> progress estimate
                for m in client.tags():
                    if model in (m.get("name"), m.get("model")):
                        load["size"] = int(m.get("size") or 0)
            except Exception:        # noqa: BLE001 - progress just stays indeterminate
                pass
            try:                     # returns only when the model is completely loaded
                http_json(f"{client.base}/api/generate", data={"model": model, "keep_alive": "30m"}, timeout=600)
                self.load_done.emit(model, True, "")
            except Exception as exc:        # noqa: BLE001
                self.load_done.emit(model, False, str(exc))

        threading.Thread(target=work, daemon=True).start()

    def _on_load_done(self, model: str, ok: bool, err: str) -> None:
        load = self._load
        if load is None or load["model"] != model:          # superseded by a newer model choice
            return
        if not ok:
            self.ctx.log(f"[Ollama] could not load {model}: {err}")
            self._load = None
            self._update_progress()
            return
        load["done"] = True
        self.ctx.log(f"[Ollama] model {model} loaded and ready")
        win = self.window()
        if hasattr(win, "statusBar"):
            win.statusBar().showMessage(f"Ollama: {model} is loaded in VRAM and ready", 10000)
        self._update_progress()

        def clear() -> None:
            if self._load is load:
                self._load = None
                self._update_progress()

        QTimer.singleShot(3000, clear)

    def _update_progress(self) -> None:
        """Progress bar under the Ollama row: server start (busy) -> model load (VRAM estimate) -> ready."""
        r = next((x for x in self.rows if x.progress is not None), None)
        if r is None:
            return
        bar, load = r.progress, self._load
        if load is None:
            if self._state(r) == "starting":
                bar.setRange(0, 0)
                bar.setFormat("Starting Ollama server ...")
                bar.setVisible(True)
            else:
                bar.setVisible(False)
            return
        if load["done"]:
            bar.setRange(0, 100)
            bar.setValue(100)
            bar.setFormat(f"Ready - {load['model']} is loaded in VRAM (100 %)")
        elif load["size"] > 0:
            size_mb = load["size"] / 1048576
            used = max(self._vram_mb - load["base_mb"], 0.0)
            pct = int(min(used / size_mb, 0.97) * 100)      # 100 % only when Ollama confirms the load
            bar.setRange(0, 100)
            bar.setValue(pct)
            bar.setFormat(f"Loading {load['model']} into VRAM ...  {pct} %  ({used / 1024:.1f} / {size_mb / 1024:.1f} GB)")
        else:
            bar.setRange(0, 0)
            bar.setFormat(f"Loading {load['model']} ...")
        bar.setVisible(True)

    def _on_status(self, data: dict) -> None:
        self._cache = data
        self.refresh()

    def shutdown(self) -> None:
        self.log_timer.stop()
        if self._win is not None:
            self._win.close()
        self.timer.stop()
        self.poller.stop()
        if self._scanner is not None:
            self._scanner.wait(3000)

    def _state(self, r: ServiceRow) -> str:
        port = r.mod.port()
        c = self._cache.get(r.mod.id, (True, "", False, "", False))
        po, ready = bool(c[2]), bool(c[4])
        if r.mod.external:
            return "online" if po else "offline"
        if port and ready:
            return "running"
        if port and po and self.ctx.procs.is_running(r.mod.id):
            return "starting"            # port bound but not ready yet (e.g. model still loading)
        if port and po:
            return "running"
        if self.ctx.procs.is_running(r.mod.id):
            return "starting" if port else "running"
        return "stopped"

    def refresh(self) -> None:
        running = 0
        for r in self.rows:
            ok, msg, _po, ver, _rdy = self._cache.get(r.mod.id, (True, "", False, "", False))
            state = self._state(r)
            if state == "running" and r.mod.id in self._launched and r.mod.port():
                self._launched.discard(r.mod.id)
                self.notify_ready(r.mod)
            elif state == "stopped":
                self._launched.discard(r.mod.id)
            colour = STATE_COL[state] if ok else BAD
            _set_style(r.dot, f"color:{colour}; font-size:18px;")
            port = r.mod.port()
            extra = r.mod.url() if r.mod.external else (f"port {port}" if port else "")
            on_hold = None
            if r.mod.id.startswith("cli:") and state == "stopped":
                on_hold = self.ctx.procs.cli_limit_reason(r.mod.id)
            info_text = f"{state}" + (f"  |  {ver}" if ver else "") + (f"  |  {extra}" if extra else "") + ("" if ok else f"  |  {msg}")
            if on_hold:
                info_text += "  |  OnHold"
            _set_text(r.info, info_text)
            if r.tune_label is not None:
                _set_text(r.tune_label, self._tune_text(r.mod.id))
            _set_enabled(r.start, ok and state == "stopped" and not on_hold)
            _set_tooltip(r.start, f"{on_hold}. Zuerst eine andere CLI stoppen." if on_hold else "")
            _set_enabled(r.stop, self.ctx.procs.is_running(r.mod.id) or (state == "running" and not r.mod.external))
            _set_enabled(r.restart, self.ctx.procs.is_running(r.mod.id))
            if state in ("running", "starting") and r.mod.id in ("comfyui", "ollama"):
                running += 1
            if r.backend_toggleable:
                # CLI tool: live badge - "API" flips to "Local AI" the moment llama.cpp/Ollama comes
                # up (and to "Local (offline)" the moment it goes down again), no restart needed.
                self._update_backend_badge(r)
        _set_text(self.hint, "ComfyUI and Ollama both running: they share the GPU VRAM - large models can run out of memory."
                  if running == 2 else "")
        self._update_local_ai_banner()
        self._update_progress()

    def _tune_text(self, mid: str) -> str:
        """Current server-level tuning values from config.json (no network call - refresh() runs every second)."""
        d = self.ctx.cfg.data
        if mid == "llamacpp":
            s = d.get("llamacpp", {}) or {}
            ngl = str(s.get("ngl", "auto"))
            return (f"Kontext {s.get('ctx', 8192)} · GPU-Layer {ngl} · KV {s.get('kv_type', 'f16')} · "
                    f"Batch {s.get('batch', 'auto')} · Slots {s.get('parallel', 'auto')}")
        s = d.get("ollama", {}) or {}
        return (f"Kontext {s.get('context_length', 'Standard')} · KV {s.get('kv_cache_type', 'f16')} · "
                f"Slots {s.get('num_parallel', 'auto')}")

    # ---------------------------------------------------------------- Local/Cloud/API badge (CLI tools)
    def _update_backend_badge(self, r: ServiceRow) -> None:
        _style_backend(r.backend, r.mod.backend_kind(), r.backend_toggleable)

    def _update_local_ai_banner(self) -> None:
        """Cross-cutting status line above the Services list: at a glance, which CLI tools are
        actually running against the local GPU right now vs. still on their cloud API."""
        cli_rows = [r for r in self.rows if r.backend_toggleable]
        on_local = [r for r in cli_rows if provider.get_provider(
            self.ctx.cfg, profiles.selected(self.ctx.cfg, r.mod.bat.stem)) == "local"]
        src = provider.cached_local_source()      # called every second from refresh() - must not block
        if not on_local:
            text, colour = "", ""
            if src:
                text = (f"🖥️ {src[0]} läuft lokal, wird aber von keinem CLI-Tool genutzt - "
                        f"Badge \"API\" bei einem Tool anklicken, um es umzustellen.")
                colour = MUTED
        else:
            names = ", ".join(r.mod.name for r in on_local)
            if src:
                text = f"🖥️ Lokale KI aktiv: {src[0]} bedient gerade {names}."
                colour = OK
            else:
                text = (f"⚠️ {names} auf \"Lokal\" gestellt, aber weder llama.cpp noch Ollama laufen - "
                        f"startet stattdessen ganz normal gegen die Cloud-API.")
                colour = BAD
        if self.local_ai_banner.isVisible() != bool(text):
            self.local_ai_banner.setVisible(bool(text))
        _set_text(self.local_ai_banner, text)
        _set_style(self.local_ai_banner, f"color:{colour}; font-weight:600;" if text else "")

    def provider_menu(self, r: ServiceRow) -> None:
        """Badge click on a CLI tool: choose Cloud-API (default, unchanged behaviour) or route
        this profile to whichever local server (llama.cpp/Ollama) is running - see core/provider.py."""
        cfg, cli = self.ctx.cfg, r.mod.bat.stem
        profile = profiles.selected(cfg, cli)
        cur = provider.get_provider(cfg, profile)
        src = provider.local_source(cfg)

        menu = QMenu(self)
        cloud_act = menu.addAction("☁  Cloud-API (Standard)")
        cloud_act.setCheckable(True)
        cloud_act.setChecked(cur == "cloud")
        cloud_act.setData("cloud")
        local_status = f"{src[0]} läuft" if src else "kein lokaler Server aktiv"
        local_act = menu.addAction(f"🖥  Lokal (llama.cpp/Ollama) - {local_status}")
        local_act.setCheckable(True)
        local_act.setChecked(cur == "local")
        local_act.setData("local")
        if cli.lower() in provider.UNVERIFIED_TOOLS:
            menu.addSeparator()
            menu.addAction("⚠ ungeprüft: kein bestätigter lokaler Endpoint-Override für dieses Tool").setEnabled(False)
        picked = menu.exec(r.backend.mapToGlobal(r.backend.rect().bottomLeft()))
        if picked is None or picked.data() is None or picked.data() == cur:
            return

        new = picked.data()
        provider.save_provider(cfg, profile, new)
        self._update_backend_badge(r)
        self._update_local_ai_banner()
        if new == "local":
            msg = f"lokal ({src[0]})" if src else "lokal (kein Server aktiv - startet vorerst über die Cloud-API)"
        else:
            msg = "Cloud-API"
        self.ctx.log(f"[{r.mod.name}] Provider: {msg} - angewendet beim nächsten Start")

    # ---------------------------------------------------------------- profiles of the CLI tools
    def _profile_label(self, r: ServiceRow) -> None:
        r.profile.setText(f"Profile: {profiles.selected(self.ctx.cfg, r.mod.bat.stem)}  ▾")
        r.profile.setToolTip(r.profile.text().replace("  ▾", ""))

    def profile_menu(self, r: ServiceRow) -> None:
        cfg, cli = self.ctx.cfg, r.mod.bat.stem
        menu = QMenu(self)
        cur = profiles.selected(cfg, cli)
        names = profiles.list_profiles(cfg, cli)
        if cur not in names:
            names.insert(0, cur)                            # not created yet - the Node launcher creates it on first start
        for n in names:
            act = menu.addAction(n)
            act.setCheckable(True)
            act.setChecked(n == cur)
            act.setData(n)
        menu.addSeparator()
        menu.addAction("New profile ...").setData(None)
        picked = menu.exec(r.profile.mapToGlobal(r.profile.rect().bottomLeft()))
        if picked is None:
            return
        name = picked.data()
        if name is None:                                    # "New profile ..."
            name, ok = QInputDialog.getText(self, "New profile", f"Name of the new {cli} profile:")
            if not ok or not name.strip():
                return
            try:
                profiles.create(cfg, cli, name)
            except (ValueError, OSError) as exc:
                QMessageBox.warning(self, "New profile", str(exc))
                return
            name = name.strip()
            self.ctx.log(f"[{r.mod.name}] profile created: {name} - log in with /login at its first start")
        profiles.select(cfg, cli, name)
        try:
            cfg.save()
        except OSError as exc:
            self.ctx.log(f"[{r.mod.name}] could not save config: {exc}")
        self._profile_label(r)
        self.ctx.log(f"[{r.mod.name}] profile: {name} - applied at the next start")

    # ---------------------------------------------------------------- AI model + effort (core/models.py)
    def _ai_model_label(self, r: ServiceRow) -> None:
        cfg, cli = self.ctx.cfg, r.mod.bat.stem
        profile = profiles.selected(cfg, cli)
        r.aimodel.setText(f"Modell: {models.summary(cfg, profile, cli)}  ▾")
        r.aimodel.setToolTip(r.aimodel.text().replace("  ▾", ""))

    def ai_model_menu(self, r: ServiceRow) -> None:
        """Two-step picker in one menu: pick a model, then (if the tool supports it) pick its
        reasoning-effort/Stärke. Mirrors the Node launcher's terminal picker (model-select.mjs)
        exactly - same files, same catalog, so the GUI and `<Tool>.bat --select-model` never disagree."""
        cfg, cli = self.ctx.cfg, r.mod.bat.stem
        profile = profiles.selected(cfg, cli)
        tc = models.tool_catalog(cfg, cli)
        current = models.get_selected_model(cfg, profile, cli)

        menu = QMenu(self)
        for m in tc.get("models", []):
            act = menu.addAction(m.get("label", m["id"]))
            act.setCheckable(True)
            act.setChecked(m["id"] == current["model"])
            act.setData(("model", m["id"]))
        picked = menu.exec(r.aimodel.mapToGlobal(r.aimodel.rect().bottomLeft()))
        if picked is None or picked.data() is None:
            return
        _kind, model_id = picked.data()
        effort = current.get("effort")

        if models.supports_effort(cfg, cli):
            levels = models.effort_levels(cfg, cli)
            emenu = QMenu(self)
            for lvl in levels:
                act = emenu.addAction(models.effort_label(cfg, cli, lvl))
                act.setCheckable(True)
                act.setChecked(lvl == effort)
                act.setData(lvl)
            epicked = emenu.exec(r.aimodel.mapToGlobal(r.aimodel.rect().bottomLeft()))
            if epicked is not None and epicked.data() is not None:
                effort = epicked.data()

        models.save_selected_model(cfg, profile, cli, model_id, effort)
        self._ai_model_label(r)
        self.ctx.log(f"[{r.mod.name}] Modell: {models.model_label(cfg, cli, model_id)}"
                      f"{' (' + models.effort_label(cfg, cli, effort) + ')' if effort and models.supports_effort(cfg, cli) else ''}"
                      " - applied at the next start")

    # ---------------------------------------------------------------- MCP servers for the CLI tools
    def _mcp_label(self, r: ServiceRow) -> None:
        cfg, cli = self.ctx.cfg, r.mod.bat.stem
        names = [mcp.servers(cfg)[i].get("name", i).split(" (")[0] for i in mcp.enabled(cfg, cli)]
        text = "off" if mcp.is_off(cfg, cli) else (", ".join(names) if names else "none")
        r.mcp.setText(f"MCP: {text}  ▾")
        r.mcp.setToolTip(r.mcp.text().replace("  ▾", ""))

    def mcp_menu(self, r: ServiceRow) -> None:
        cfg, cli = self.ctx.cfg, r.mod.bat.stem
        menu = QMenu(self)
        on = set(mcp.enabled(cfg, cli))
        off = menu.addAction("Start without MCP")
        off.setCheckable(True)
        off.setChecked(mcp.is_off(cfg, cli))
        off.setData("__off__")
        menu.addSeparator()
        for sid, s in mcp.servers(cfg).items():
            act = menu.addAction(s.get("name", sid))
            act.setCheckable(True)
            act.setChecked(sid in on)
            act.setData(sid)
        if not menu.actions():
            menu.addAction("no MCP servers defined (config.json -> mcp.servers)").setEnabled(False)
        picked = menu.exec(r.mcp.mapToGlobal(r.mcp.rect().bottomLeft()))
        if picked is None or picked.data() is None:
            return
        sid = picked.data()
        if sid == "__off__":                               # switch MCP off / on, the ticked servers stay remembered
            mcp.set_off(cfg, cli, not mcp.is_off(cfg, cli))
            ordered = [] if mcp.is_off(cfg, cli) else [i for i in mcp.servers(cfg) if i in on]
        else:
            on.symmetric_difference_update({sid})          # toggle a server
            ordered = [i for i in mcp.servers(cfg) if i in on]
            mcp.set_enabled(cfg, cli, ordered)
            mcp.set_off(cfg, cli, False)                   # ticking a server switches MCP back on
        try:
            cfg.save()
        except OSError as exc:
            self.ctx.log(f"[{r.mod.name}] could not save config: {exc}")
        self._mcp_label(r)
        self.ctx.log(f"[{r.mod.name}] MCP servers: {', '.join(ordered) or '(none)'} - applied at the next start")

    def _start_with_mcp(self, r: ServiceRow) -> bool:
        """Start the MCP services a CLI needs first and launch the CLI once they answer. True = launch was deferred."""
        need = [d for d in self.rows if d.mod.id in mcp.required_modules(self.ctx.cfg, r.mod.bat.stem)]
        for d in need:
            if self._state(d) == "stopped" and self._cache.get(d.mod.id, (True,))[0]:
                self.ctx.log(f"[{r.mod.name}] starting {d.mod.name} first (MCP)")
                self.start_module(d, goto=False)
        if all(self._state(d) == "running" for d in need):
            return False
        r.start.setEnabled(False)
        deadline = {"left": 60}                            # x 0.5 s

        def wait() -> None:
            deadline["left"] -= 1
            waiting = [d for d in need if self._state(d) != "running"]
            if waiting and deadline["left"] > 0:
                QTimer.singleShot(500, wait)
                return
            if waiting:
                self.ctx.log(f"[{r.mod.name}] WARNING: {', '.join(d.mod.name for d in waiting)} not ready after 30 s - starting anyway")
            self.start_module(r, mcp_ready=True)

        QTimer.singleShot(500, wait)
        return True

    def start_module(self, r: ServiceRow, goto: bool = True, mcp_ready: bool = False) -> None:
        if r.mod.id.startswith("cli:"):
            reason = self.ctx.procs.cli_limit_reason(r.mod.id)
            if reason is not None:
                self.ctx.log(f"[{r.mod.name}] Start abgelehnt: {reason}. Zuerst eine andere CLI stoppen.")
                self.refresh()
                return
        if r.mod.interactive and not mcp_ready and r.mod.id.startswith("cli:") and mcp.supported(r.mod.bat.stem):
            if self._start_with_mcp(r):
                return
        if r.mod.id.startswith("cli:") and r.mod.interactive and goto:
            opener = getattr(self.window(), "start_cli_embedded", None)      # sub-tab in HUB Terminal
            if opener is not None and opener(r.mod.bat.stem):
                self.ctx.log(f"[{r.mod.name}] started in HUB Terminal")
                self.refresh()
                return
        try:
            for line in r.mod.prepare():
                self.ctx.log(f"[{r.mod.name}] {line}")
            spec = r.mod.build()
            # start() refuses instead of taking over: for a CLI this row shares its key with
            # (cli:<Name>), the holder may be the embedded terminal tab with a live conversation
            # in it. Stopping that silently is exactly what must not happen here.
            if not self.ctx.procs.start(r.mod.id, spec.cmd, spec.cwd, spec.env, title=r.mod.name,
                                        interactive=r.mod.interactive):
                holder = self.ctx.procs.holder_of(r.mod.id)
                where = {"embedded": "im eingebetteten Terminal-Tab", "console": "in einer externen Konsole",
                         "captured": "bereits über das Dashboard"}.get(holder, "bereits")
                self.ctx.log(f"[{r.mod.name}] Start abgelehnt: läuft {where}. Dort stoppen oder - im "
                              "Terminal-Tab - 'Wechseln' benutzen.")
                self.refresh()
                return
            self.ctx.log(f"[{r.mod.name}] started: {' '.join(spec.cmd)}")
            if r.mod.port() and not r.mod.external:
                self._launched.add(r.mod.id)      # message follows once the service answers
            else:
                self.notify_ready(r.mod, started_only=True)
            if goto and not r.mod.interactive:
                self.ctx.goto("processes")        # live log in its own tab
        except Exception as exc:        # noqa: BLE001
            self.ctx.log(f"[{r.mod.name}] ERROR: {exc}")
        self.refresh()

    def notify_ready(self, mod: Module, started_only: bool = False) -> None:
        """Message in log, status bar and a non-modal box that closes itself after 8 s."""
        if mod.id == "ollama" and not started_only:
            m = str(self.ctx.cfg.data.get("ollama", {}).get("model", ""))
            if m:
                self.preload_ollama(m)
        url = mod.url()
        text = f"{mod.name} started." if started_only else f"{mod.name} is running and ready" + (f" - {url}" if url else "")
        self.ctx.log(f"[{mod.name}] {text}")
        win = self.window()
        if hasattr(win, "statusBar"):
            win.statusBar().showMessage(text, 10000)
        if self._toast is not None:
            try:
                self._toast.close()
            except RuntimeError:          # C++ object already deleted (WA_DeleteOnClose)
                pass
            self._toast = None
        box = QMessageBox(QMessageBox.Information, "AI Workspace", text, QMessageBox.Ok, win)
        box.setWindowModality(Qt.NonModal)
        box.setAttribute(Qt.WA_DeleteOnClose)
        box.destroyed.connect(lambda *_: self._clear_toast(box))
        box.show()
        QTimer.singleShot(8000, lambda b=box: self._close_toast(b))
        self._toast = box

    def _clear_toast(self, box) -> None:
        if self._toast is box:
            self._toast = None

    @staticmethod
    def _close_toast(box) -> None:
        try:
            box.close()
        except RuntimeError:              # already deleted by user click
            pass

    def stop_module(self, r: ServiceRow) -> None:
        self._launched.discard(r.mod.id)
        self.ctx.procs.stop(r.mod.id)
        self.ctx.log(f"[{r.mod.name}] stopped")
        self.refresh()

    def update_module(self, r: ServiceRow) -> None:
        """Run one of the module's update commands in its own console (service is stopped first)."""
        opts = r.mod.update_options()
        if not opts:
            self.ctx.log(f"[{r.mod.name}] no update available (update script not found)")
            return
        if len(opts) == 1:
            label = next(iter(opts))
        else:
            menu = QMenu(self)
            acts = {menu.addAction(k): k for k in opts}
            picked = menu.exec(r.update.mapToGlobal(r.update.rect().bottomLeft()))
            if picked is None:
                return
            label = acts[picked]
        spec = opts[label]
        if self.ctx.procs.is_running(r.mod.id) or self._cache.get(r.mod.id, (0, 0, False))[2]:
            self.ctx.procs.stop(r.mod.id)
            self.ctx.log(f"[{r.mod.name}] stopped for update (if it was started outside the launcher, close it manually)")
        self.ctx.procs.start(f"update:{r.mod.id}", spec.cmd, spec.cwd, spec.env, title=f"{r.mod.name} update")
        self.ctx.log(f"[{r.mod.name}] update started: {label}")
        self.ctx.goto("processes")

    def restart_module(self, r: ServiceRow) -> None:
        self.stop_module(r)
        QTimer.singleShot(1500, lambda: self.start_module(r))

    def start_all(self) -> None:
        for r in self.rows:
            if not r.mod.external and r.mod.port() and r.start.isEnabled():
                self.start_module(r)

    def stop_all(self) -> None:
        self.ctx.procs.stop_all()
        self.ctx.log("All started services stopped")
        self.refresh()

    def _open(self, path) -> None:
        if hasattr(os, "startfile"):
            os.startfile(str(path))            # noqa: S606 - Windows only
        else:
            webbrowser.open(f"file://{path}")
