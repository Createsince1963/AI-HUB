"""Tuning page: parameter defaults / hardware maxima / block steps, live tokens-per-second test and the
maximum-context probe for Ollama and llama.cpp. All logic lives in core/tuning.py; this file is the view.

Threading: tests run in a plain daemon thread that pushes events into a queue.Queue; a QTimer drains the
queue on the GUI thread (no QThread lifecycle, nothing can block the window from closing)."""
from __future__ import annotations

import html
import queue
import re
import threading
import time
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QBrush, QColor, QFont, QGuiApplication
from PySide6.QtWidgets import (QCheckBox, QComboBox, QFrame, QInputDialog, QGridLayout, QHBoxLayout, QLabel, QMessageBox, QPlainTextEdit,
                               QProgressBar, QScrollArea, QSizePolicy, QSlider, QSplitter, QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget)

from core import library
from core import modules as M
from core import telemetry as TM
from core import autotune as AT
from core import tuning as T
from core.config import APP_DIR
from core.net import OllamaClient
from core.ollama_ops import find_exe

from . import theme
from .context import Ctx
from .llm_dashboard import LlmDashboard, TpsHistory
from .widgets import SparkChart, button, card, make_table, page_header, run_async, section

BANNER_STYLE = {
    "light": {
        "info": "background:#EAF1FB; color:#1F3A5F; border:1px solid #C9D9F0;",
        "ok": "background:#E6F6EE; color:#0F5132; border:1px solid #B7E1CD;",
        "err": "background:#FDECEC; color:#8A1C1C; border:1px solid #F3B8B8;",
        "warn": "background:#FFF4DB; color:#7A4B00; border:1px solid #F0D79A;",
    },
    "dark": {
        "info": "background:#3A4664; color:#FFFFFF; border:1px solid #8FB8F0;",
        "ok": "background:#3A4664; color:#FFFFFF; border:1px solid #8BD12B; border-left:4px solid #8BD12B;",
        "err": "background:#3A4664; color:#FFFFFF; border:1px solid #8FB8F0; border-left:4px solid #8FB8F0; font-weight:700;",
        "warn": "background:#3A4664; color:#FFFFFF; border:1px solid #8FB8F0; border-left:4px solid #8FB8F0;",
    },
}

PROMPT_CHOICES = [("Kurz (~30 Tokens)", 0), ("1.000 Tokens", 1000), ("4.000 Tokens", 4000),
                  ("8.000 Tokens", 8000), ("50 % des Kontexts", -50)]


class _Job:
    """One background test. fn(emit, cancelled) runs in a daemon thread; events arrive via .q."""

    def __init__(self, fn):
        self.q: queue.Queue = queue.Queue()
        self.cancel = threading.Event()
        self.thread = threading.Thread(target=self._run, args=(fn,), daemon=True, name="tuning-job")
        self.thread.start()

    def _run(self, fn) -> None:
        try:
            fn(lambda kind, payload=None: self.q.put((kind, payload)), self.cancel.is_set)
        except Exception as exc:      # noqa: BLE001 - surfaced in the GUI, never crashes the thread silently
            self.q.put(("error", str(exc)))
        finally:
            self.q.put(("end", None))


def fmt_value(p: T.ParamDef, v) -> str:
    if v in p.auto_values:
        return "∞" if p.key in ("predict", "top_k") and v == -1 else ("Auto" if v == -1 else "aus")
    if p.kind == "float":
        return f"{float(v):.2f}"
    if p.kind == "int" and isinstance(v, int) and v >= 1024 and p.unit == "Tokens":
        return f"{v:,}".replace(",", ".")
    return str(v)


# (key, caption) of the live strip in the page header; values come from telemetry.build_rows labels
# row 1 = server + hardware, row 2 = context + last request (the fields of "Alle Messwerte" the user watches while tuning)
LIVE_ROWS = [
    [("server", "Server", "Serverstatus"), ("uptime", "Laufzeit", "Serverlaufzeit"), ("gpu", "GPU-Auslastung", "GPU-Auslastung"),
     ("temp", "GPU-Temperatur", "GPU-Temperatur"), ("vram", "VRAM", "VRAM-Nutzung"), ("offload", "GPU-Offloading", "GPU-Offloading"),
     ("power", "Leistung", "GPU-Leistungsaufnahme"), ("cpu", "CPU", "CPU-Auslastung"), ("scpu", "CPU Server", "Prozessorauslastung (Server)"),
     ("ram", "RAM", "RAM-Nutzung"), ("sram", "RAM Server", "Prozessspeicher (Server)")],
    [("ctxuse", "Kontext", "Kontextbelegung"), ("ptok", "Prompt-Token", "Prompt-Token"), ("otok", "Ausgabe-Token", "Ausgabe-Token"),
     ("ptps", "Prompt-Leistung", "Prompt-Leistung"), ("gtps", "Generierung", "Generierungsleistung"), ("ttft", "Antwortbeginn", "Antwortbeginn (TTFT)"),
     ("pdur", "Prompt-Dauer", "Prompt-Dauer"), ("gdur", "Generierungsdauer", "Generierungsdauer"), ("tdur", "Gesamtdauer", "Gesamtdauer")],
]


class FactTile(QFrame):
    """Modern stat tile: small upper-case caption, large bold value, optional muted sub line."""

    def __init__(self, caption: str, compact: bool = False):
        super().__init__()
        self.compact = compact
        self.setObjectName("FactTile")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 5, 10, 5) if compact else lay.setContentsMargins(12, 8, 12, 8)
        lay.setSpacing(2)
        self.cap = QLabel(caption.upper())
        self.val = QLabel("-")
        self.sub = QLabel("")
        self.val.setTextInteractionFlags(Qt.TextSelectableByMouse)
        for w in (self.cap, self.val, self.sub):
            w.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)     # tile can shrink, text is clipped
            w.setMinimumWidth(0)
            lay.addWidget(w)
        self.setMinimumWidth(56)
        self.sub.setVisible(False)
        self.restyle()

    def set(self, value: str, sub: str = "", tip: str = "") -> None:
        self.val.setText(value)
        self.sub.setText(sub)
        self.sub.setVisible(bool(sub))
        self.setToolTip(tip or value)

    def restyle(self, accent: bool = False, colour: str | None = None) -> None:
        c = theme.col
        big = "11pt" if self.compact else "13pt"
        self.setStyleSheet(
            f"QFrame#FactTile {{ background:{c('card')}; border:1px solid {c('border')}; border-radius:10px;"
            f" border-left:4px solid {colour or (c('accent') if accent else c('border'))}; }}")
        self.cap.setStyleSheet(f"color:{c('muted')}; font-size:7.5pt; font-weight:700; letter-spacing:1px; border:none;")
        self.val.setStyleSheet(f"color:{colour or c('title')}; font-size:{big}; font-weight:700; border:none;")
        self.sub.setStyleSheet(f"color:{c('muted')}; font-size:8pt; border:none;")


class TuningPage(QWidget):
    def __init__(self, ctx: Ctx):
        super().__init__()
        self.ctx = ctx
        self.hw = T.Hardware(vram_total_mb=int(ctx.vram_gb() * 1024))
        self.store = T.TuningStore(APP_DIR / "tuning.json")
        self.spec = T.ModelSpec(name="")
        self.params: list[T.ParamDef] = []
        self.values: dict = {}
        self.rows: dict = {}
        self.model_defaults: dict = {}
        self.launcher: dict = {}
        self.job: _Job | None = None
        self._busy = False
        self._counted = False
        self._loading = False
        self._last_tokens = 0
        self._shown = False
        self.model_error = ""
        self.logfile = APP_DIR / "logs" / "tuning_live.log"
        self._console_opened = False
        self._log_buf: list[str] = []
        self._log_timer = QTimer(self)
        self._log_timer.setSingleShot(True)
        self._log_timer.setInterval(250)
        self._log_timer.timeout.connect(self._flush_log)
        self.tel = TM.Telemetry()
        self.req_stats = TM.RequestStats()
        self._snap: dict = {}
        self._sample: dict = {}
        self._tel_busy = False

        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 14, 18, 14)
        head = QHBoxLayout()
        head.addWidget(page_header("Tuning", "Parameter, Hardware-Grenzen und Live-Tempo für Ollama und llama.cpp"))
        head.addSpacing(16)
        # live overview - always visible, refreshed every second from the same measurement as the "Messwerte" tab
        # static hardware + model facts sit in the header (they change only with the model)
        self.tiles: dict[str, FactTile] = {}
        for key, cap in (("gpu", "GPU"), ("vram", "VRAM"), ("ram", "RAM"), ("model", "Modell"), ("arch", "Architektur"),
                         ("layers", "Layer"), ("kv", "KV-Köpfe"), ("ctx", "Trainierter Kontext"), ("weights", "Gewichte")):
            t = FactTile(cap, compact=True)
            self.tiles[key] = t
            head.addWidget(t, 3 if key in ("gpu", "model") else 1)
        outer.addLayout(head)

        # ---- model selection
        top, tl = card()
        row = QHBoxLayout()
        self.backend = QComboBox()
        self.backend.addItem("Ollama", "ollama")
        self.backend.addItem("llama.cpp", "llamacpp")
        self.model = QComboBox()
        self.model.setMinimumWidth(180)
        self.model.setMaximumWidth(560)            # model picker stays compact - server controls sit right next to it
        self.model.setEditable(False)
        self.model.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.btn_collapse = button("◀ Links", "", self.toggle_params,
                                   "Linken Bereich (Parameter / Messwerte) ein-/ausklappen - Testbereich nutzt dann die volle Breite")
        self.btn_collapse.setMinimumWidth(90)
        row.addWidget(self.btn_collapse)
        row.addSpacing(8)
        row.addWidget(QLabel("Backend"))
        row.addWidget(self.backend)
        row.addWidget(QLabel("Modell"))
        row.addWidget(self.model, 1)
        row.addWidget(button("Neu laden", "", self.reload_models, "Modellliste neu einlesen"))
        row.addSpacing(16)
        # server state + start / stop / test right next to the model: load the chosen model into the server and test it
        self.srv_state = QLabel("")
        self.srv_state.setTextFormat(Qt.RichText)
        self.srv_state.setMinimumWidth(150)
        self.btn_srv_start = button("Server starten", "Go", self.start_server,
                                    "Startet den Dienst mit dem gewählten Modell und den aktuellen Parametern "
                                    "(läuft llama.cpp mit einem anderen Modell, wird neu gestartet)")
        self.btn_srv_stop = button("Stoppen", "Danger", self.stop_server, "Beendet den Dienst")
        self.btn_top_test = button("Live-Test", "Primary", self.start_live, "Live-Test mit dem gewählten Modell (startet den Dienst bei Bedarf)")
        row.addWidget(self.srv_state)
        row.addWidget(self.btn_srv_start)
        row.addWidget(self.btn_srv_stop)
        row.addWidget(self.btn_top_test)
        row.addStretch(1)
        tl.addLayout(row)
        # hardware + model facts as tiles (hardware tiles carry an accent bar on the left)
        # live values (refreshed every second) - two rows below the model selection
        self.live: dict[str, FactTile] = {}
        self.live_src: dict[str, str] = {}
        for row_def in LIVE_ROWS:
            hl = QHBoxLayout()
            hl.setSpacing(6)
            for key, cap, src in row_def:
                t = FactTile(cap)
                self.live[key] = t
                self.live_src[key] = src
                hl.addWidget(t, 2 if key in ("server", "vram", "ram", "ctxuse", "offload") else 1)
            tl.addLayout(hl)
        self.facts = QLabel("")                    # errors / warnings below the tiles only
        self.facts.setWordWrap(True)
        self.facts.setTextFormat(Qt.RichText)
        self.facts.setVisible(False)
        tl.addWidget(self.facts)
        outer.addWidget(top)

        # parameters | test + measurements side by side in a splitter - the user can drag the divider, position is remembered
        self.split = QSplitter(Qt.Horizontal)
        self.split.setChildrenCollapsible(False)
        self.split.setHandleWidth(8)
        outer.addWidget(self.split, 1)

        # ---- left: parameters
        left_w = QWidget()
        left_w.setMinimumWidth(260)
        left = QVBoxLayout(left_w)
        left.setContentsMargins(0, 0, 4, 0)
        self.split.addWidget(left_w)
        self.left_w = left_w
        # left column = tabs "Parameter" | "Messwerte (live)" - the right side keeps test, auto-tune and probe
        self.left_tabs = QTabWidget()
        left.addWidget(self.left_tabs, 1)
        par_page = QWidget()
        left = QVBoxLayout(par_page)             # parameter widgets below go into the first left tab
        left.setContentsMargins(0, 8, 0, 0)
        self.left_tabs.addTab(par_page, "Parameter")
        pc, pl = card()
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        inner = QWidget()
        self.grid = QGridLayout(inner)
        self.grid.setColumnStretch(1, 1)
        self.grid.setVerticalSpacing(6)
        scroll.setWidget(inner)
        pl.addWidget(scroll, 1)
        self.mem_bar = QProgressBar()
        self.mem_bar.setTextVisible(True)
        self.mem_label = QLabel("")
        self.mem_label.setWordWrap(True)
        self.mem_label.setObjectName("Muted")
        pl.addWidget(self.mem_bar)
        pl.addWidget(self.mem_label)
        brow = QHBoxLayout()
        self.btn_defaults = button("Alle auf Standard", "", self.reset_all)
        self.btn_apply = button("Auf Server anwenden", "Primary", self.apply_server,
                                "Schreibt Kontext, GPU-Layer, KV-Typ, Batch, Slots in config.json - gilt ab dem nächsten Dienst-Start")
        self.btn_variant = button("Als Modell-Variante speichern", "", self.save_variant,
                                  "Ollama: legt per Modelfile ein Modell mit diesen Werten an (z. B. qwen2.5-coder-ctx32k)")
        brow.addWidget(self.btn_defaults)
        brow.addStretch(1)
        brow.addWidget(self.btn_variant)
        brow.addWidget(self.btn_apply)
        pl.addLayout(brow)
        left.addWidget(pc, 1)

        # ---- right: live test + probe (scrolls instead of being cut off when the window / splitter is narrow)
        right_w = QWidget()
        right = QVBoxLayout(right_w)
        right.setContentsMargins(4, 0, 0, 0)
        right_scroll = QScrollArea()
        right_scroll.setWidgetResizable(True)
        right_scroll.setFrameShape(QScrollArea.NoFrame)
        right_scroll.setMinimumWidth(300)
        right_scroll.setWidget(right_w)
        self.split.addWidget(right_scroll)
        self.split.setStretchFactor(0, 4)
        self.split.setStretchFactor(1, 6)
        self._restore_split()
        if (self.ctx.cfg.data.get("ui", {}) or {}).get("tuning_params_collapsed"):
            QTimer.singleShot(0, lambda: self.toggle_params(save=False))
        self.split.splitterMoved.connect(lambda _p, _i: self._save_split())
        # one always-visible line that says what is happening / what went wrong / what to do next
        self.banner = QLabel("Bereit - Modell wählen, dann Live-Test oder Maximum messen.")
        self.banner.setWordWrap(True)
        self.status = self.banner
        self._say(self.banner.text(), "info")
        right.addWidget(self.banner)
        self.tabs = QTabWidget()
        right.addWidget(self.tabs, 1)
        t1 = QWidget()
        right = QVBoxLayout(t1)                 # everything below goes into the first tab
        right.setContentsMargins(0, 8, 0, 0)
        self.tabs.addTab(t1, "Test und Maximum")

        # ---- Auto-Tune (sweet spot) + profiles
        right.addWidget(section("Auto-Tune - Sweetspot für Modell und Hardware"))
        ac, al = card()
        arow = QHBoxLayout()
        self.at_goal = QComboBox()
        for k, t in AT.GOALS.items():
            self.at_goal.addItem(t, k)
        self.at_goal.setToolTip("Ausgewogen = Kontext und Tempo gewichtet · Max. Tempo = schnellste Antwort · "
                                "Max. Kontext = größter Kontext, der komplett auf der GPU läuft")
        self.at_level = QComboBox()
        for k, (t, f) in AT.LEVELS.items():
            self.at_level.addItem(f"{t}" + (f" (Kontext {int(f * 100)} % gefüllt)" if f else " (kurzer Prompt)"), k)
        self.at_level.setCurrentIndex(1)
        self.btn_autotune = button("Auto-Tune starten", "Primary", self.start_autotune,
                                   f"Max. {AT.MAX_ROUNDS} Durchläufe: schätzen, messen, nachjustieren - "
                                   "der beste Durchlauf wird als Profil (Standard) gespeichert")
        arow.addWidget(QLabel("Ziel"))
        arow.addWidget(self.at_goal)
        arow.addWidget(QLabel("Gründlichkeit"))
        arow.addWidget(self.at_level)
        arow.addStretch(1)
        arow.addWidget(self.btn_autotune)
        al.addLayout(arow)
        self.at_bar = QProgressBar()
        self.at_bar.setRange(0, 100)
        self.at_bar.setValue(0)
        self.at_bar.setFormat("bereit")
        self.at_bar.setTextVisible(True)
        al.addWidget(self.at_bar)
        self.at_table = make_table(["#", "Kontext", "KV", "GPU-Layer", "Batch", "Gen tok/s", "Prompt tok/s", "VRAM MB", "Ergebnis"], 8)
        self.at_table.setSortingEnabled(False)
        self.at_table.setMinimumHeight(110)
        self.at_table.setMaximumHeight(150)
        al.addWidget(self.at_table)
        prow = QHBoxLayout()
        self.prof = QComboBox()
        self.prof.setMinimumWidth(160)
        self.prof.setToolTip("Gespeicherte Profile für dieses Modell auf dieser Hardware (★ = Standard, wird beim Modellwechsel geladen)")
        prow.addWidget(QLabel("Profil"))
        prow.addWidget(self.prof, 1)
        self.btn_prof_load = button("Laden", "", self.profile_load, "Werte des Profils in die Parameter links übernehmen")
        self.btn_prof_save = button("Speichern ...", "", self.profile_save, "Aktuelle Parameter links als Profil speichern")
        self.btn_prof_apply = button("An Server übergeben", "Go", self.profile_apply,
                                     "llama.cpp: config.json · Ollama: Modell-Variante mit diesen PARAMETERn anlegen")
        self.btn_prof_def = button("★ Standard", "", self.profile_default, "Profil als Standard für dieses Modell setzen")
        self.btn_prof_del = button("Löschen", "Danger", self.profile_delete)
        for b in (self.btn_prof_load, self.btn_prof_save, self.btn_prof_apply, self.btn_prof_def, self.btn_prof_del):
            prow.addWidget(b)
        al.addLayout(prow)
        right.addWidget(ac)
        self._at_result = None

        right.addWidget(section("Live-Test"))
        lc, ll = card()
        charts = QHBoxLayout()
        self.ch_gen = SparkChart("Generierung", theme.col("series_a"), 150.0, " tok/s", auto_scale=True)
        self.ch_prompt = SparkChart("Prompt-Verarbeitung", theme.col("series_b"), 2000.0, " tok/s", auto_scale=True)
        self.ch_vram = SparkChart("VRAM", theme.col("series_c"), 100.0, " %")
        for c in (self.ch_gen, self.ch_prompt, self.ch_vram):
            charts.addWidget(c)
        ll.addLayout(charts)
        lc.setMinimumHeight(330)
        ctl = QHBoxLayout()
        self.prompt_len = QComboBox()
        self.prompt_len.setMinimumWidth(150)
        self.prompt_len.setToolTip("Prompt-Länge passt sich dem eingestellten Kontext an (Kontext minus Antwort minus Puffer)")
        self.prompt_range = QLabel("")
        self.prompt_range.setObjectName("Muted")
        self.btn_live = button("Live-Test starten", "Primary", self.start_live)
        self.btn_stop = button("Stopp", "", self.stop_job)
        self.btn_stop.setEnabled(False)
        ctl.addWidget(QLabel("Prompt"))
        ctl.addWidget(self.prompt_len)
        ctl.addWidget(self.prompt_range)
        ctl.addStretch(1)
        ctl.addWidget(self.btn_live)
        ctl.addWidget(self.btn_stop)
        ll.addLayout(ctl)
        self.stats = QLabel("-")
        self.stats.setWordWrap(True)
        ll.addWidget(self.stats)
        self.out = QPlainTextEdit(readOnly=True)
        self.out.setMaximumBlockCount(500)
        self.out.setMinimumHeight(70)
        self.out.setMaximumHeight(140)
        ll.addWidget(self.out)
        right.addWidget(lc)

        right.addWidget(section("Maximum ermitteln"))
        mc, ml = card()
        self.probe_info = QLabel("")
        self.probe_info.setWordWrap(True)
        ml.addWidget(self.probe_info)
        pr = QHBoxLayout()
        self.fill = QCheckBox("Kontext zur Hälfte füllen (genauer, langsamer)")
        self.btn_probe = button("Maximum messen", "", self.start_probe,
                                "Lädt das Modell mit wachsendem Kontext, bis es nicht mehr komplett auf die GPU passt oder das Tempo einbricht")
        pr.addWidget(self.fill)
        pr.addStretch(1)
        pr.addWidget(self.btn_probe)
        ml.addLayout(pr)
        self.table = make_table(["Kontext", "Ergebnis", "Gen tok/s", "Prompt tok/s", "GPU", "VRAM MB"], 1)
        self.table.setSortingEnabled(False)
        self.table.setMinimumHeight(90)
        ml.addWidget(self.table, 1)
        right.addWidget(mc, 1)

        right.addWidget(section("Protokoll - alle Schritte und Meldungen"))
        gc, gl = card()
        self.log_box = QPlainTextEdit(readOnly=True)
        self.log_box.setMaximumBlockCount(3000)
        self.log_box.setMinimumHeight(150)
        self.log_box.setStyleSheet("font-family:Consolas,'Courier New',monospace; font-size:9pt;")
        gl.addWidget(self.log_box, 1)
        lr = QHBoxLayout()
        self.auto_console = QCheckBox("CMD-Fenster bei Teststart automatisch öffnen")
        lr.addWidget(self.auto_console)
        lr.addStretch(1)
        lr.addWidget(button("CMD-Fenster mit Live-Log", "", self.open_console,
                            "Öffnet ein Konsolenfenster, das das Protokoll live mitliest (Datei logs\\tuning_live.log)"))
        lr.addWidget(button("Protokoll leeren", "", self.log_box.clear))
        gl.addLayout(lr)
        right.addWidget(gc, 1)

        # ---- second tab: complete live measurement table
        t2 = QWidget()
        v2 = QVBoxLayout(t2)
        v2.setContentsMargins(0, 8, 0, 0)
        hdr = QHBoxLayout()
        self.tel_info = QLabel("Messung läuft sekündlich, solange diese Seite offen ist. \"-\" = der Server liefert diesen Wert nicht (Quelle siehe rechts).")
        self.tel_info.setObjectName("Muted")
        self.tel_info.setWordWrap(True)
        hdr.addWidget(self.tel_info, 1)
        hdr.addWidget(button("Als Text kopieren", "", self.copy_measurements))
        self.tel_table = make_table(["Bereich", "Messwert", "Wert", "Quelle"], 2)
        self.tel_table.setSortingEnabled(False)
        t_all = QWidget()
        va = QVBoxLayout(t_all)
        va.setContentsMargins(0, 8, 0, 0)
        va.addLayout(hdr)
        va.addWidget(self.tel_table, 1)
        self.dash = LlmDashboard()
        self.dash.action.connect(self._model_action)
        self.hist = {"ollama": TpsHistory(), "llamacpp": TpsHistory()}
        self._models_tick = 0
        v2.addWidget(self.dash, 1)
        self.left_tabs.addTab(t2, "Messwerte (live)")      # left: live dashboard next to the parameters
        self.tabs.addTab(t_all, "Alle Messwerte")          # right: complete table next to test + maximum
        ui_cfg = self.ctx.cfg.data.get("ui", {}) or {}
        self.left_tabs.setCurrentIndex(int(ui_cfg.get("tuning_left_tab", 0) or 0) if self.left_tabs.count() > 1 else 0)
        self.left_tabs.currentChanged.connect(self._left_tab_changed)

        self.srv_timer = QTimer(self)
        self.srv_timer.setInterval(2000)
        self.srv_timer.timeout.connect(self._srv_check)
        self._srv_busy = False
        self._set_srv_state(None)

        self.tel_timer = QTimer(self)
        self.tel_timer.setInterval(1000)
        self.tel_timer.timeout.connect(self._tel_tick)
        self.timer = QTimer(self)
        self.timer.setInterval(100)
        self.timer.timeout.connect(self._poll)
        self.backend.currentIndexChanged.connect(lambda _i: (self.reload_models(), self._set_srv_state(None), self._srv_check()))
        self.model.currentIndexChanged.connect(lambda _i: self.load_model())
        ctx.monitor.sample.connect(self._on_sample)

    # ------------------------------------------------------------------ feedback: banner + protocol + console
    def apply_theme(self) -> None:
        """Called by the main window after a light/dark switch: banner colours are inline styles, charts repaint from tokens."""
        self._refresh_facts()
        self._say(*getattr(self, "_banner_state", (self.banner.text(), "info")))
        self._render_rows()
        for w in self.dash.findChildren(QWidget):
            w.update()

    def _say(self, text: str, kind: str = "info") -> None:
        self._banner_state = (text, kind)
        self.banner.setText(text)
        self.banner.setStyleSheet(f"{BANNER_STYLE[theme.mode()].get(kind, BANNER_STYLE[theme.mode()]['info'])} border-radius:6px; padding:7px 10px; font-weight:600;")

    def _log(self, line: str) -> None:
        """Buffered: lines are collected and written to the protocol box + log file in one go every 250 ms.
        Writing each line separately (file open/close on the external SSD + a text-box relayout) blocked the
        GUI thread for seconds while llama-server printed hundreds of start-up lines -> "Keine Rückmeldung"."""
        self._log_buf.append(f"{time.strftime('%H:%M:%S')}  {line}")
        if not self._log_timer.isActive():
            self._log_timer.start()

    def _flush_log(self) -> None:
        if not self._log_buf:
            return
        lines, self._log_buf = self._log_buf, []
        if len(lines) > 400:                       # the box keeps 3000 blocks anyway; the file gets everything
            shown = lines[:50] + [f"... {len(lines) - 100} Zeilen ausgelassen (vollständig in logs\\tuning_live.log) ..."] + lines[-50:]
        else:
            shown = lines
        self.log_box.appendPlainText("\n".join(shown))
        try:
            self.logfile.parent.mkdir(parents=True, exist_ok=True)
            with open(self.logfile, "a", encoding="utf-8") as fh:
                fh.write("\n".join(lines) + "\n")
        except OSError:
            pass

    def _fail(self, msg: str, hint: str = "") -> None:
        self._log("FEHLER: " + msg)
        if hint:
            self._log("  -> " + hint)
        self._say(msg + (f"  -  {hint}" if hint else ""), "err")

    def open_console(self) -> None:
        try:
            self.logfile.parent.mkdir(parents=True, exist_ok=True)
            if not self.logfile.exists():
                self.logfile.write_text(f"{time.strftime('%H:%M:%S')}  Tuning Live-Log\n", encoding="utf-8")
        except OSError as exc:
            self._fail(f"Log-Datei nicht anlegbar: {exc}")
            return
        if T.open_console(self.logfile):
            self._console_opened = True
            self._log("CMD-Fenster geöffnet (liest " + str(self.logfile) + ")")
        else:
            self._say("CMD-Fenster ist nur unter Windows verfügbar - das Protokoll steht hier in der Seite.", "warn")

    # ------------------------------------------------------------------ lifecycle
    def on_show(self) -> None:
        if not self._shown:
            self._shown = True
            run_async(lambda: T.detect_hardware(self.ctx.vram_gb()), on_result=self._hw_ready)
            self.reload_models()

    def shutdown(self) -> None:
        if self.job:
            self.job.cancel.set()
        self.timer.stop()
        self.tel_timer.stop()

    def showEvent(self, e) -> None:            # noqa: N802 - measure only while the page is visible
        super().showEvent(e)
        self.srv_timer.start()
        self._srv_check()
        self.tel_timer.start()
        self._tel_tick()

    def hideEvent(self, e) -> None:            # noqa: N802
        super().hideEvent(e)
        self.tel_timer.stop()
        self.srv_timer.stop()

    def select_backend(self, be: str, measurements: bool = False) -> None:
        i = self.backend.findData(be)
        if i >= 0 and i != self.backend.currentIndex():
            self.backend.setCurrentIndex(i)
        if measurements:
            self.left_tabs.setCurrentIndex(1)
            if not self.left_w.isVisible():
                self.toggle_params()

    # ------------------------------------------------------------------ live measurements
    def _tel_args(self) -> tuple:
        be = self._backend()
        cfg = self.ctx.cfg
        host = "127.0.0.1" if cfg.host() in ("0.0.0.0", "") else cfg.host()
        port = cfg.port("ollama" if be == "ollama" else "llamacpp")
        hint = ""
        if be == "llamacpp":
            p = self._resolve_gguf(self.model.currentData()) if self.model.currentData() else None
            hint = str(p) if p else ""
        return be, self._base_url(), host, port, self.model.currentText() if be == "ollama" else "", hint

    def _tel_tick(self) -> None:
        if self._tel_busy or not self.isVisible():
            return
        self._tel_busy = True
        args = self._tel_args()

        def ready(snap):
            self._tel_busy = False
            if args[0] == self._backend():
                self._snap = snap
                h = self.hist[args[0]]
                if snap.get("metrics"):
                    h.feed_metrics(snap["metrics"], snap.get("when"))
                else:
                    h.feed_stats(self.req_stats)
                self._render_rows()
                self._models_tick -= 1
                if self._models_tick <= 0:
                    self._models_tick = 5
                    self._refresh_model_list()

        def failed(_m):
            self._tel_busy = False
        run_async(lambda: self.tel.collect(*args), on_result=ready, on_error=failed)

    def _rows(self) -> list:
        cfg_l = self.ctx.cfg.data.get("llamacpp", {}) or {}
        ctx_cfg = int(cfg_l.get("ctx") or 0) if self._backend() == "llamacpp" else int(self.values.get("ctx") or 0)
        return TM.build_rows(self._snap, self._sample, self.req_stats, cfg_llama=cfg_l, ctx_cfg=ctx_cfg,
                             usable_vram=self.hw.usable_vram_bytes, busy_job=self._busy)

    def _render_rows(self) -> None:
        rows = self._rows()
        be = self._backend()
        snap = self._snap if self._snap.get("backend") == be else {}
        ctx_cfg = int((self.ctx.cfg.data.get("llamacpp", {}) or {}).get("ctx") or 0) if be == "llamacpp" else int(self.values.get("ctx") or 0)
        self._render_live({l: v for _g, l, v, _s in rows})
        self.dash.update_view({l: v for _g, l, v, _s in rows}, snap, self._sample, self.req_stats, self.hist[be],
                              ctx_max=int(snap.get("ctx") or ctx_cfg or 0))
        t = self.tel_table
        if t.rowCount() != len(rows):
            t.setRowCount(len(rows))
        prev = None
        bold = QFont()
        bold.setBold(True)
        for r, (grp, label, value, src) in enumerate(rows):
            cells = [grp if grp != prev else "", label, value, src]
            prev = grp
            for c, txt in enumerate(cells):
                it = t.item(r, c)
                if it is None:
                    it = QTableWidgetItem()
                    t.setItem(r, c, it)
                if it.text() != txt:
                    it.setText(txt)
                if c == 0:
                    it.setFont(bold)
                if c == 2:
                    it.setFont(bold)
                    it.setForeground(QBrush(QColor(theme.col("muted") if value == "-" else theme.col("title"))))
                if c == 3:
                    it.setForeground(QBrush(QColor(theme.col("muted"))))

    # ------------------------------------------------------------------ model list of the overview (+ quick actions)
    def _model_list(self, be: str, snap: dict) -> tuple[list[dict], str]:
        """Worker thread. Ollama: /api/tags + /api/ps. llama.cpp: GGUF library; active = the file the running server loaded."""
        out: list[dict] = []
        if be == "ollama":
            cli = self.ctx.ollama()
            loaded = {m.get("name") for m in cli.ps()}
            for m in cli.tags():
                out.append({"name": m["name"], "size": f"{m.get('size', 0) / T.GIB:.1f} GB",
                            "status": "Geladen" if m["name"] in loaded else "Verfügbar"})
            return out, "" if out else "In Ollama ist noch kein Modell installiert (Dashboard: Modell wählen, wird beim Start aus der GGUF importiert)."
        active = Path(snap.get("model_path", "")).name if snap.get("online") and snap.get("model_path") else ""
        for label, value in library.list_gguf(self.ctx.cfg):
            p = self._resolve_gguf(value)
            name = p.name if p else Path(str(value)).name
            out.append({"name": str(value), "size": f"{p.stat().st_size / T.GIB:.1f} GB" if p else "-",
                        "status": "Aktiv" if active and name == active else "Verfügbar"})
        return out, "" if out else "Keine GGUF-Dateien in der Modell-Bibliothek gefunden."

    def _refresh_model_list(self) -> None:
        be, snap = self._backend(), dict(self._snap)

        def done(res):
            if be == self._backend():
                self.dash.set_models(res[0], can_remove=(be == "ollama"), note=res[1])

        def failed(msg):
            if be == self._backend():
                self.dash.set_models([], can_remove=False, note=f"Modellliste nicht verfügbar: {msg} (Dienst läuft nicht?)")
        run_async(lambda: self._model_list(be, snap), on_result=done, on_error=failed)

    def _select_model(self, name: str) -> bool:
        i = self.model.findData(name) if self._backend() == "llamacpp" else self.model.findText(name)
        if i < 0:
            self._say(f"Modell {name} steht in der Auswahl oben nicht zur Verfügung - bitte 'Neu laden'.", "warn")
            return False
        if i != self.model.currentIndex():
            self.model.setCurrentIndex(i)
        return True

    def _model_action(self, kind: str, name: str) -> None:
        """Buttons of the model list. Everything is logged in the protocol; failures show in the banner."""
        be = self._backend()
        self._log(f"Modell-Aktion: {kind} {name} ({self._svc_name(be)})")
        if kind == "test":
            if self._busy:
                self._say("Es läuft bereits ein Test - zuerst abwarten oder abbrechen.", "warn")
                return
            if self._select_model(name):
                self.start_live()
            return
        if kind == "remove":
            if QMessageBox.question(self, "Modell entfernen", f"Modell {name} aus Ollama löschen?\n"
                                    "Die GGUF-Datei in der Modell-Bibliothek bleibt erhalten.") != QMessageBox.Yes:
                return
            from core.ollama_ops import remove_model
            run_async(lambda: remove_model(self.ctx.cfg, name),
                      on_result=lambda _o: (self._say(f"{name} aus Ollama entfernt.", "ok"), self._log("  entfernt"), self._refresh_model_list()),
                      on_error=lambda m: self._fail(f"Entfernen fehlgeschlagen: {m}"))
            return
        if kind == "unload":
            if be == "ollama":
                run_async(lambda: self.ctx.ollama().unload(name),
                          on_result=lambda _o: (self._say(f"{name} aus dem Speicher entladen.", "ok"), self._log("  entladen"), self._refresh_model_list()),
                          on_error=lambda m: self._fail(f"Entladen fehlgeschlagen: {m}"))
            else:
                self.ctx.procs.stop("llamacpp")
                self._say("llama.cpp-Server wird gestoppt (Modell wird entladen).", "ok")
                self._log("  Dienst gestoppt")
            return
        if kind == "load":
            if be == "ollama":
                self._say(f"Lade {name} in den Speicher ...", "info")
                run_async(lambda: T.http_json(f"{self.ctx.ollama().base}/api/generate", data={"model": name, "keep_alive": "5m"}, timeout=180),
                          on_result=lambda _o: (self._say(f"{name} geladen.", "ok"), self._log("  geladen"), self._refresh_model_list()),
                          on_error=lambda m: self._fail(f"Laden fehlgeschlagen: {m}"))
                return
            if not self._select_model(name):
                return
            if T.service_up(self._base_url(), be, 0.8):
                self.ctx.procs.stop("llamacpp")
                self._log("  laufenden Server gestoppt, starte mit neuem Modell")
                QTimer.singleShot(1500, lambda: self._say("Server mit neuem Modell wird gestartet ...", "info") if self._start_service(be) else None)
            elif self._start_service(be):
                self._say("llama.cpp-Server startet mit dem gewählten Modell ...", "info")

    def copy_measurements(self) -> None:
        QGuiApplication.clipboard().setText("\n".join(f"{g}\t{l}\t{v}\t{s}" for g, l, v, s in self._rows()))
        self._say("Messwerte in die Zwischenablage kopiert.", "ok")

    def _hw_ready(self, hw: T.Hardware) -> None:
        self.hw = hw
        self._refresh_facts()
        self._rebuild(keep=True)

    # ------------------------------------------------------------------ model list + info
    def _backend(self) -> str:
        return self.backend.currentData()

    def reload_models(self) -> None:
        if self._busy:
            return
        be = self._backend()
        self._loading = True
        self.model.clear()

        def fetch():
            if be == "ollama":
                return [(m["name"], m["name"]) for m in self.ctx.ollama().tags()]
            cur = str(self.ctx.cfg.data.get("llamacpp", {}).get("model", ""))
            out = list(library.list_gguf(self.ctx.cfg))          # same list + values the Dashboard picker uses
            if cur and cur not in [v for _l, v in out]:
                out.insert(0, (Path(cur).name + "   (configured)", cur))
            return out

        def done(items):
            self._loading = False
            cur = str(self.ctx.cfg.data.get("llamacpp", {}).get("model", ""))
            for label, data in items:
                self.model.addItem(label, data)
            if be == "llamacpp":
                self.model.setCurrentIndex(max(0, self.model.findData(cur)))
            self._log(f"{len(items)} Modell(e) gefunden ({'Ollama' if be == 'ollama' else 'llama.cpp / GGUF-Bibliothek'})")
            if not items:
                self._say("Keine Modelle gefunden" + (" - läuft Ollama? (Dashboard > Ollama starten)" if be == "ollama"
                                                      else " - GGUF-Ordner prüfen (AI models > Manager)."), "warn")
            self.load_model()

        def fail(msg):
            self._loading = False
            self._fail(f"Modellliste nicht lesbar: {msg}", "Ollama gestartet? (Dashboard > Ollama starten)" if be == "ollama" else "")

        run_async(fetch, on_result=done, on_error=fail)

    def load_model(self) -> None:
        if self._loading or self.model.currentIndex() < 0:
            return
        be, name, data = self._backend(), self.model.currentText(), self.model.currentData()

        def fetch():
            if be == "ollama":
                cl = self.ctx.ollama()
                info = cl.show(name)
                size = next((int(m.get("size") or 0) for m in cl.tags() if m.get("name") == name), 0)
                det = info.get("details") or {}
                spec = T.ModelSpec.from_kv(name, info.get("model_info") or {}, size, det.get("quantization_level", ""),
                                           det.get("parameter_size", ""))
                return spec, T.parse_ollama_parameters(info.get("parameters", ""))
            p = self._resolve_gguf(data)
            if p is None:
                raise FileNotFoundError(f"Modelldatei nicht gefunden: {data} (gesucht: {', '.join(str(c) for c in self._gguf_candidates(data))})")
            kv = T.read_gguf_meta(p)
            quant = str(kv.get("general.file_type", ""))
            return T.ModelSpec.from_kv(name, kv, p.stat().st_size, quant), {}, str(p)

        def done(res):
            self.spec, self.model_defaults = res[0], res[1]
            self.model_error = ""
            sp = self.spec
            self._log(f"Modell-Info gelesen: {name}" + (f" ({res[2]})" if len(res) > 2 else " (Ollama /api/show)")
                      + (f" - {sp.arch}, {sp.n_layers} Layer, {sp.n_kv_heads} KV-Köpfe, trainierter Kontext {sp.ctx_train}, "
                         f"Gewichte {sp.weights_bytes / T.GIB:.1f} GB" if sp.known else " - ACHTUNG: Architektur-Angaben fehlen, keine Schätzung möglich"))
            self.launcher = self._launcher_values(be)
            self.values = {}
            self._rebuild(keep=False)
            self._refresh_profiles()
            dp = self.store.default_profile(be, self._model_key(), self.hw)
            if dp:
                self._apply_values(dp.get("values") or {})
                self._say(f"Modell geladen: {name} - Standard-Profil '{dp['name']}' übernommen.", "ok")
            else:
                self._say(f"Modell geladen: {name}. Auto-Tune starten oder Parameter einstellen, dann Live-Test.", "info")

        def fail(msg):
            self.spec = T.ModelSpec(name=name)
            self.model_defaults = {}
            self.model_error = str(msg)
            self._fail(f"Modell-Info nicht lesbar: {msg}")
            self.launcher = self._launcher_values(be)
            self.values = {}
            self._rebuild(keep=False)

        self._log(f"Lese Modell-Info: {name} ...")
        run_async(fetch, on_result=done, on_error=fail)

    def _gguf_candidates(self, value) -> list[Path]:
        """Same lookup order as LlamaCppModule._model: as given, GGUF folder, model library root."""
        m = str(value)
        return [Path(m), self.ctx.cfg.path("gguf") / m, self.ctx.cfg.path("models") / m]

    def _resolve_gguf(self, value) -> Path | None:
        return next((c for c in self._gguf_candidates(value) if c.is_file()), None)

    def _launcher_values(self, be: str) -> dict:
        d = self.ctx.cfg.data
        if be == "llamacpp":
            s = d.get("llamacpp", {}) or {}
            ngl = str(s.get("ngl", "auto")).strip().lower()
            return {"ctx": s.get("ctx"), "gpu_layers": -1 if ngl in ("", "auto") else int(ngl),
                    "kv_type": s.get("kv_type"), "batch": s.get("batch"), "parallel": s.get("parallel")}
        s = d.get("ollama", {}) or {}
        return {"ctx": s.get("context_length"), "kv_type": s.get("kv_cache_type"), "parallel": s.get("num_parallel")}

    # ------------------------------------------------------------------ parameter rows
    def _kv(self) -> str:
        return str(self.values.get("kv_type") or self.launcher.get("kv_type") or "f16")

    def _par(self) -> int:
        return int(self.values.get("parallel") or self.launcher.get("parallel") or 1)

    def _measured(self) -> dict | None:
        return self.store.get(self._backend(), self.model.currentData() if self._backend() == "llamacpp"
                              else self.model.currentText(), self._kv(), self.hw)

    def _rebuild(self, keep: bool) -> None:
        be = self._backend()
        m = self._measured()
        self.params = T.build_params(be, self.spec, self.hw, model_defaults=self.model_defaults, launcher=self.launcher,
                                     measured_max_ctx=int(m["max_ctx"]) if m else 0, kv_type=self._kv(),
                                     parallel=self._par())
        old = dict(self.values) if keep else {}
        self.values = {p.key: p.snap(old.get(p.key, p.default)) for p in self.params}
        self._build_rows()
        self._refresh_facts()
        self._update_memory()
        self._update_prompt_choices()
        self.btn_variant.setVisible(be == "ollama")
        self.btn_apply.setText("Als Server-Standard (Neustart)" if be == "ollama" else "Auf Server anwenden")
        self.table.setRowCount(0)
        if m:
            for s in m.get("steps", []):
                self._add_step(T.ProbeStep(**s))

    def _prompt_limits(self) -> tuple[int, int]:
        """(min, max) prompt tokens that fit the current context: context - answer (max 512) - safety margin."""
        ctx = int(self.values.get("ctx") or 4096)
        pred = int(self.values.get("predict") or -1)
        ans = min(pred if pred > 0 else 256, 512)
        margin = max(256, int(ctx * 0.05))                  # chat template + tokenizer differences
        return 30, max(30, ctx - ans - margin)

    def _update_prompt_choices(self) -> None:
        """Prompt sizes follow the context slider, so a live test never exceeds the server's n_ctx."""
        lo, hi = self._prompt_limits()
        prev = self.prompt_len.currentIndex()
        frac_prev = [0, 25, 50, 75, 100][prev] if 0 <= prev < 5 else 50
        self.prompt_len.blockSignals(True)
        self.prompt_len.clear()
        self.prompt_len.addItem(f"Kurz (~{lo} Tokens)", 0)
        for pct in (25, 50, 75, 100):
            n = int(hi * pct / 100) // 100 * 100 if pct < 100 else hi
            self.prompt_len.addItem(f"{pct} % · {n:,} Tokens".replace(",", ".") + ("  (Maximum)" if pct == 100 else ""), n)
        self.prompt_len.setCurrentIndex([0, 25, 50, 75, 100].index(frac_prev) if frac_prev in (0, 25, 50, 75, 100) else 2)
        self.prompt_len.blockSignals(False)
        self.prompt_range.setText(f"min {lo} · max {hi:,} Tokens".replace(",", "."))

    def _build_rows(self) -> None:
        while self.grid.count():
            it = self.grid.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        self.rows.clear()
        for r, p in enumerate(self.params):
            label = QLabel(p.label + (" ⟳" if p.scope == "server" else ""))
            label.setToolTip(p.help + ("\n⟳ = wird beim Dienst-Start gelesen (Neustart nötig)" if p.scope == "server" else ""))
            self.grid.addWidget(label, r, 0)
            if p.kind == "choice":
                w = QComboBox()
                for s in p.steps:
                    w.addItem(str(s), s)
                w.setCurrentIndex(max(0, p.steps.index(self.values[p.key])))
                w.currentIndexChanged.connect(lambda _i, p=p, w=w: self._changed(p, w.currentData()))
            else:
                w = QSlider(Qt.Horizontal)
                w.setRange(0, len(p.steps) - 1)
                w.setValue(p.steps.index(self.values[p.key]))
                w.setTickPosition(QSlider.TicksBelow)
                w.setTickInterval(1)
                w.valueChanged.connect(lambda i, p=p: self._changed(p, p.steps[i]))
            w.setToolTip(p.help)
            self.grid.addWidget(w, r, 1)
            val = QLabel(fmt_value(p, self.values[p.key]) if p.kind != "choice" else "")
            val.setMinimumWidth(64)
            val.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self.grid.addWidget(val, r, 2)
            maxtxt = ""
            if p.maximum is not None:
                maxtxt = f"Standard {fmt_value(p, p.default)} · Max {fmt_value(p, p.maximum)}"
                if p.note:
                    maxtxt += f" ({p.note})"
            else:
                maxtxt = f"Standard {p.default}"
            info = QLabel(maxtxt)
            info.setObjectName("Muted")
            self.grid.addWidget(info, r, 3)
            rb = button("↺", "", lambda p=p: self._reset(p), "Auf Standard")
            rb.setMaximumWidth(32)
            self.grid.addWidget(rb, r, 4)
            self.rows[p.key] = (w, val, p)

    def _changed(self, p: T.ParamDef, v) -> None:
        self.values[p.key] = v
        w, val, _p = self.rows[p.key]
        if p.kind != "choice":
            val.setText(fmt_value(p, v))
        if p.key in ("kv_type", "parallel"):
            self._rebuild(keep=True)         # the VRAM-dependent maximum of the context changes
        else:
            self._update_memory()
            if p.key in ("ctx", "predict"):
                self._update_prompt_choices()

    def _reset(self, p: T.ParamDef) -> None:
        w = self.rows[p.key][0]
        if p.kind == "choice":
            w.setCurrentIndex(p.steps.index(p.default))
        else:
            w.setValue(p.steps.index(p.snap(p.default)))

    def reset_all(self) -> None:
        self.values = {}
        self._rebuild(keep=False)

    def _update_memory(self) -> None:
        if not self.spec.known or not self.values:
            self.mem_bar.setValue(0)
            self.mem_bar.setFormat("VRAM-Schätzung nicht möglich: " + (self.model_error[:80] or "Modell-Info fehlt"))
            self.mem_label.setText("")
            return
        m = T.estimate_memory(self.spec, self.hw, int(self.values["ctx"]), self._kv(), self._par())
        use = m["usable"] or 1
        self.mem_bar.setRange(0, 1000)
        self.mem_bar.setValue(min(1000, int(1000 * m["total"] / use)))
        self.mem_bar.setFormat(f"{m['total'] / T.GIB:.1f} / {m['usable'] / T.GIB:.1f} GB VRAM (Schätzung)")
        self.mem_bar.setStyleSheet("QProgressBar::chunk{background:%s;}" % (theme.col("ok") if m["fits"] else theme.col("bad")))
        self.mem_label.setText(
            f"Gewichte {m['weights'] / T.GIB:.1f} GB + KV-Cache {m['kv'] / T.GIB:.2f} GB + Puffer "
            f"{m['overhead'] / T.GIB:.1f} GB. " + ("Passt komplett in den VRAM." if m["fits"] else
                                                   "Passt nicht komplett - Teile laufen auf der CPU (deutlich langsamer)."))

    def _refresh_facts(self) -> None:
        s, hw = self.spec, self.hw
        tl = self.tiles
        tl["gpu"].set(hw.gpu_name or "GPU")
        tl["vram"].set(f"{hw.vram_total_mb / 1024:.0f} GB", f"nutzbar {hw.usable_vram_bytes / T.GIB:.1f} GB")
        tl["ram"].set(f"{hw.ram_total_mb / 1024:.0f} GB" if hw.ram_total_mb else "-")
        if s.known:
            tl["model"].set(s.name, f"{s.weights_bytes / T.GIB:.1f} GB", s.name)
            tl["arch"].set(s.arch or "-")
            tl["layers"].set(str(s.n_layers))
            tl["kv"].set(str(s.n_kv_heads))
            tl["ctx"].set(f"{s.ctx_train:,}".replace(",", "."), "Tokens")
            tl["weights"].set(f"{s.weights_bytes / T.GIB:.1f} GB")
        else:
            tl["model"].set(s.name or "kein Modell", "", s.name)
            for k in ("arch", "layers", "kv", "ctx", "weights"):
                tl[k].set("-")
        for k, t in tl.items():
            t.restyle(accent=k in ("gpu", "vram", "ram"))
        m = self._measured()
        notes = []
        if s.known and s.conservative:
            notes.append(f"<span style='color:{theme.col('bad')}'><b>Architektur mit Sonderfall:</b> VRAM-Schätzung eher pessimistisch</span>")
        if self.model_error:
            notes.append(f"<span style='color:{theme.col('bad')}'><b>{html.escape(self.model_error)}</b></span>")
        self.facts.setText("<br>".join(notes))
        self.facts.setVisible(bool(notes))
        self.probe_info.setText(
            (f"Gemessenes Maximum: {m['max_ctx']:,} Tokens (Basis {m['baseline_tps']:.0f} tok/s, Abbruch: {m['stopped']})".replace(",", ".")
             if m else "Noch nicht gemessen - das angezeigte Maximum ist eine Schätzung aus Modell und VRAM."))

    # ------------------------------------------------------------------ jobs
    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        for b in (self.btn_live, self.btn_probe, self.btn_apply, self.btn_variant, self.btn_defaults,
                  self.btn_top_test, self.btn_srv_start, self.btn_srv_stop, self.btn_autotune,
                  self.btn_prof_load, self.btn_prof_save, self.btn_prof_apply, self.btn_prof_def, self.btn_prof_del):
            b.setEnabled(not busy)
        self.btn_stop.setEnabled(busy)
        self.backend.setEnabled(not busy)
        self.model.setEnabled(not busy)
        if busy:
            self.timer.start()
            if self.auto_console.isChecked() and not self._console_opened:
                self.open_console()
        else:
            self.timer.stop()

    def _launch(self, fn) -> None:
        self._set_busy(True)
        self.job = _Job(fn)

    def stop_job(self) -> None:
        if self.job:
            self.job.cancel.set()
            self._log("Abbruch angefordert ...")
            self._say("Wird abgebrochen ...", "warn")

    def _need_ready(self) -> bool:
        if not self.model.currentText():
            self._say("Bitte zuerst ein Modell wählen.", "warn")
            return False
        return True

    def _base_url(self) -> str:
        if self._backend() == "ollama":
            return self.ctx.ollama().base
        return f"http://127.0.0.1:{self.ctx.cfg.port('llamacpp')}"

    def _svc_name(self, be: str) -> str:
        return "Ollama" if be == "ollama" else "llama.cpp-Server"

    def _ensure_service(self, be: str) -> bool:
        """GUI thread. True = the server answers or was just started (the worker then waits for it)."""
        base = self._base_url()
        if T.service_up(base, be, 0.8):
            self._log(f"{self._svc_name(be)} läuft ({base})")
            return True
        self._log(f"{self._svc_name(be)} antwortet nicht unter {base} - Dienst läuft nicht")
        extra = (f"\nEs wird mit Modell {self.model.currentText()}, Kontext {self.values.get('ctx')} gestartet "
                 "(Werte werden in config.json gespeichert)." if be == "llamacpp" else "")
        if QMessageBox.question(self, "Tuning", f"{self._svc_name(be)} läuft nicht. Jetzt starten?{extra}") != QMessageBox.Yes:
            self._say(f"{self._svc_name(be)} läuft nicht - Test nicht gestartet. Dienst im Dashboard starten oder hier bestätigen.", "warn")
            return False
        return self._start_service(be)

    def _start_service(self, be: str) -> bool:
        cfg = self.ctx.cfg
        if be == "llamacpp":
            cfg.data.setdefault("llamacpp", {})["model"] = self.model.currentData()
            T.apply_llamacpp(cfg, self.values)
            self._log("config.json aktualisiert: Modell, Kontext, GPU-Layer, KV-Typ, Batch, Slots")
        mod = M.OllamaModule(cfg) if be == "ollama" else M.LlamaCppModule(cfg)
        ok, msg = mod.check()
        if not ok:
            self._fail(f"{mod.name} kann nicht starten: {msg}")
            return False
        try:
            if hasattr(mod, "prepare"):
                for ln in mod.prepare() or []:
                    self._log("  " + str(ln))
            spec = mod.build()
        except Exception as exc:      # noqa: BLE001
            self._fail(f"{mod.name}: Startkommando nicht bildbar: {exc}")
            return False
        self._log("Starte: " + " ".join(f'"{c}"' if " " in c else c for c in spec.cmd))
        if spec.env:
            self._log("  Umgebung: " + ", ".join(f"{k}={v}" for k, v in spec.env.items()))
        if not self.ctx.procs.start(mod.id, spec.cmd, spec.cwd, spec.env, title=mod.name):
            self._fail(f"{mod.name}: Start abgelehnt (läuft bereits oder Prozess-Limit)")
            return False
        self._log(f"{mod.name} gestartet - warte auf Bereitschaft (Modell laden kann bis zu 2 Minuten dauern)")
        return True

    # ------------------------------------------------------------------ splitter position (remembered in config.json > ui)
    def _restore_split(self) -> None:
        sizes = (self.ctx.cfg.data.get("ui", {}) or {}).get("tuning_split")
        if isinstance(sizes, list) and len(sizes) == 2 and all(isinstance(x, int) and x > 0 for x in sizes):
            self.split.setSizes(sizes)
        else:
            self.split.setSizes([450, 650])

    def _left_tab_changed(self, i: int) -> None:
        self.ctx.cfg.data.setdefault("ui", {})["tuning_left_tab"] = int(i)
        self._save_cfg()

    def toggle_params(self, save: bool = True) -> None:
        """Hide / show the parameter column; the test + measurement area then uses the whole width."""
        hide = self.left_w.isVisible()
        self.left_w.setVisible(not hide)
        self.btn_collapse.setText("▶ Links" if hide else "◀ Links")
        if save:
            self.ctx.cfg.data.setdefault("ui", {})["tuning_params_collapsed"] = hide
            self._save_cfg()

    def _render_live(self, vals: dict) -> None:
        """Live strip: server, hardware, context and last request - same values as "Alle Messwerte"."""
        c = theme.col
        online = (vals.get("Serverstatus") or "").lower().startswith("online")
        for key, t in self.live.items():
            v = vals.get(self.live_src[key]) or "-"
            sub = ""
            if " (" in v and v.endswith(")"):            # "6.94 / 11.94 GB (58 %)" -> value "58 %", sub "6.94 / 11.94 GB"
                main, pct = v[:-1].split(" (", 1)
                v, sub = (pct, main) if key in ("vram", "ram", "ctxuse") else (main, pct)
            if key == "server":
                sub = vals.get("Aktives Modell", "") if online else self._svc_name(self._backend())
            elif key == "power" and " / " in v:
                v, sub = v.split(" / ", 1)[0], "max " + v.split(" / ", 1)[1]
            v = v.replace(" Token/s", " tok/s")
            t.set(v, sub)
            if key == "server":
                t.restyle(colour=c("ok") if online else c("bad"))
            else:
                t.restyle(accent=key in ("gtps", "ptps", "vram", "ctxuse"))

    def _save_split(self) -> None:
        self.ctx.cfg.data.setdefault("ui", {})["tuning_split"] = [int(x) for x in self.split.sizes()]
        if not hasattr(self, "_split_timer"):            # save once the user stops dragging, not on every pixel
            self._split_timer = QTimer(self)
            self._split_timer.setSingleShot(True)
            self._split_timer.setInterval(600)
            self._split_timer.timeout.connect(self._save_cfg)
        self._split_timer.start()

    def _save_cfg(self) -> None:
        try:
            self.ctx.cfg.save()
        except OSError as exc:
            self._log(f"config.json nicht speicherbar: {exc}")

    # ------------------------------------------------------------------ server start / stop from the model row
    def _mod_key(self, be: str) -> str:
        return "ollama" if be == "ollama" else "llamacpp"

    def _set_srv_state(self, up: bool | None) -> None:
        be = self._backend()
        name = self._svc_name(be)
        if up is None:
            txt, col = "prüfe ...", theme.col("muted")
        elif up:
            txt, col = "online", theme.col("ok")
        elif self.ctx.procs.is_running(self._mod_key(be)):
            txt, col = "startet ...", theme.col("series_a")
        else:
            txt, col = "offline", theme.col("bad")
        self.srv_state.setText(f"<span style='color:{col}; font-weight:600'>&#9679; {name}: {txt}</span>")
        running = bool(up) or self.ctx.procs.is_running(self._mod_key(be))
        self.btn_srv_stop.setEnabled(running and not self._busy)
        self.btn_srv_start.setText("Neu starten" if running and be == "llamacpp" else "Server starten")

    def _srv_check(self) -> None:
        if self._srv_busy:
            return
        self._srv_busy = True
        be, base = self._backend(), self._base_url()

        def done(up):
            self._srv_busy = False
            if be == self._backend():
                self._set_srv_state(bool(up))

        def failed(_m):
            self._srv_busy = False
            self._set_srv_state(False)

        run_async(lambda: T.service_up(base, be, 0.8), on_result=done, on_error=failed)

    def start_server(self) -> None:
        """Starts (llama.cpp: restarts) the service with the selected model and the current parameters."""
        if self._busy or not self._need_ready():
            return
        be = self._backend()
        key = self._mod_key(be)
        if self.ctx.procs.is_running(key):
            if be == "ollama":
                self._say("Ollama läuft bereits - Modell wird beim ersten Request / Live-Test geladen.", "info")
                return
            self._log("llama.cpp-Server läuft - Neustart mit dem gewählten Modell")
            self.ctx.procs.stop(key)
            QTimer.singleShot(1500, lambda: self._start_and_report(be))       # give the old process time to free the port / VRAM
        else:
            self._start_and_report(be)
        self._set_srv_state(None)

    def _start_and_report(self, be: str) -> None:
        if self._start_service(be):
            self._say(f"{self._svc_name(be)} startet mit {self.model.currentText()} - Status oben wird grün, dann Live-Test.", "info")
        self._srv_check()

    def stop_server(self) -> None:
        be = self._backend()
        key = self._mod_key(be)
        if not self.ctx.procs.is_running(key):
            self._say(f"{self._svc_name(be)} wurde nicht von hier gestartet - im Dashboard stoppen.", "warn")
            return
        self.ctx.procs.stop(key)
        self._log(f"{self._svc_name(be)} gestoppt")
        self._set_srv_state(False)

    # ------------------------------------------------------------------ Auto-Tune
    def _model_key(self) -> str:
        return str(self.model.currentData()) if self._backend() == "llamacpp" else self.model.currentText()

    def _apply_values(self, vals: dict) -> None:
        """Puts values into the parameter rows (snapped to the allowed steps); rebuilds when KV / slots change the limits."""
        if any(k in vals and vals[k] != self.values.get(k) for k in ("kv_type", "parallel")):
            self.values.update({k: vals[k] for k in ("kv_type", "parallel") if k in vals})
            self._rebuild(keep=True)
        for k, v in vals.items():
            row = self.rows.get(k)
            if not row:
                continue
            w, _val, p = row
            v = p.snap(v)
            if p.kind == "choice":
                w.setCurrentIndex(max(0, p.steps.index(v)))
            else:
                w.setValue(p.steps.index(v))
            self.values[k] = v
        self._update_memory()
        self._update_prompt_choices()

    def start_autotune(self) -> None:
        if not self._need_ready():
            return
        be = self._backend()
        if not self.spec.known:
            self._fail("Auto-Tune nicht möglich: Modell-Info fehlt" + (f" ({self.model_error})" if self.model_error else ""))
            return
        goal, level = self.at_goal.currentData(), self.at_level.currentData()
        spec, hw, name, data = self.spec, self.hw, self.model.currentText(), self.model.currentData()
        current, defaults = dict(self.values), {p.key: p.default for p in self.params}
        pre_wait = 0
        if be == "llamacpp":
            if self.ctx.procs.is_running("llamacpp"):
                if QMessageBox.question(self, "Auto-Tune", "Der llama.cpp-Server läuft und belegt VRAM - Auto-Tune startet eigene "
                                        "Test-Instanzen.\nDienst jetzt stoppen?") != QMessageBox.Yes:
                    return
                self.ctx.procs.stop("llamacpp")
                pre_wait = 3
            exe = self.ctx.cfg.path("llamacpp") / "bin" / "llama-server.exe"
            path = self._resolve_gguf(data)
            if not exe.is_file() or path is None:
                self._fail(f"Auto-Tune nicht möglich: {'llama-server.exe' if not exe.is_file() else 'Modelldatei'} nicht gefunden")
                return

            def used() -> int:
                try:
                    from core.system import gpu_info
                    return int((gpu_info() or {}).get("used_mb", 0))
                except Exception:      # noqa: BLE001
                    return 0

            def measure_factory(log, cancelled):
                def measure(v: dict, fill: float) -> T.ProbeStep:
                    res = T.probe_llamacpp([str(exe)], str(path), spec, kv_type=v["kv_type"], candidates=[int(v["ctx"])],
                                           fill_frac=fill, granularity=0, ngl=99 if int(v["gpu_layers"]) < 0 else int(v["gpu_layers"]),
                                           batch=int(v["batch"]), total_vram_mb=hw.vram_total_mb, gpu_used_mb=used,
                                           on_log=log, cancelled=cancelled)
                    return res.steps[0] if res.steps else T.ProbeStep(int(v["ctx"]), error=res.stopped or "kein Ergebnis")
                return measure
        else:
            if not self._ensure_service("ollama"):
                return
            cl = self.ctx.ollama()

            def measure_factory(log, cancelled):
                def measure(v: dict, fill: float) -> T.ProbeStep:
                    res = T.probe_ollama(cl.base, name, spec, kv_type=v["kv_type"], candidates=[int(v["ctx"])], fill_frac=fill,
                                         granularity=0, on_log=log, cancelled=cancelled)
                    return res.steps[0] if res.steps else T.ProbeStep(int(v["ctx"]), error=res.stopped or "kein Ergebnis")
                return measure

        self.at_table.setRowCount(0)
        self.at_bar.setValue(0)
        key = self._model_key()

        def run(emit, cancelled):
            log = lambda t: emit("log", t)       # noqa: E731
            if pre_wait:
                log(f"llama.cpp-Server gestoppt - warte {pre_wait} s, bis der VRAM frei ist ...")
                time.sleep(pre_wait)
            if be == "ollama" and not T.wait_for_service(cl.base, "ollama", on_log=log, cancelled=cancelled):
                raise RuntimeError("Ollama wurde nicht bereit (Zeitlimit)")
            res = AT.run(be, key, spec, hw, current, goal=goal, level=level, measure=measure_factory(log, cancelled),
                         model_defaults=defaults, on_round=lambda r: emit("at_round", r),
                         on_progress=lambda pct, t: emit("at_progress", (pct, t)), on_log=log, cancelled=cancelled)
            emit("at_done", res)

        self._log(f"=== Auto-Tune: {self._svc_name(be)}, Modell {name} ===")
        self._launch(run)

    def _at_add_round(self, r: "AT.Round") -> None:
        v = r.values
        row = self.at_table.rowCount()
        self.at_table.insertRow(row)
        cells = [str(r.no), f"{int(v['ctx']):,}".replace(",", "."), str(v["kv_type"]),
                 "Auto" if int(v["gpu_layers"]) < 0 else str(v["gpu_layers"]), str(v["batch"]),
                 f"{r.gen_tps:.1f}" if r.ok else "-", f"{r.prompt_tps:.0f}" if r.ok else "-", str(r.vram_mb or "-"),
                 ("✓ " if r.good else "✗ ") + (r.decision or r.note)]
        for c, t in enumerate(cells):
            it = QTableWidgetItem(t)
            it.setToolTip(r.decision or r.note)
            if c == 8:
                it.setForeground(QBrush(QColor(theme.col("ok") if r.good else theme.col("bad"))))
            self.at_table.setItem(row, c, it)
        if r.ok:
            self.ch_gen.push(r.gen_tps, f"{r.gen_tps:.1f} tok/s", f"Durchlauf {r.no}, Kontext {v['ctx']}")

    def _at_done(self, res: "AT.AutoTuneResult") -> None:
        self._at_result = res
        b = res.best
        if not b:
            self._fail(f"Auto-Tune: kein passender Durchlauf ({res.stopped})", "Protokoll prüfen - ggf. kleineres Modell / Quantisierung")
            return
        self._apply_values(b.values)
        names = [p["name"] for p in self.store.profiles(res.backend, res.model, self.hw)]
        pname = AT.default_profile_name(res.model, res.goal, names)
        prof = {"name": pname, "goal": res.goal, "level": res.level, "values": dict(b.values), "when": time.time(),
                "result": {"ctx": int(b.values["ctx"]), "gen_tps": b.gen_tps, "prompt_tps": b.prompt_tps, "vram_mb": b.vram_mb},
                "rounds": len(res.rounds), "stopped": res.stopped}
        self.store.save_profile(res.backend, res.model, self.hw, prof, make_default=True)
        self._refresh_profiles(select=pname)
        msg = (f"Sweetspot: Kontext {int(b.values['ctx']):,}, KV {b.values['kv_type']}, {b.gen_tps:.1f} tok/s - "
               f"als Profil '{pname}' (Standard) gespeichert.").replace(",", ".")
        self._log(msg)
        self._say(msg, "ok")
        if QMessageBox.question(self, "Auto-Tune", msg + "\n\nJetzt an den Server übergeben?\n"
                                + ("llama.cpp: Werte in config.json, Server neu starten." if res.backend == "llamacpp"
                                   else "Ollama: Modell-Variante mit diesen Parametern anlegen.")) == QMessageBox.Yes:
            self.profile_apply()

    # ------------------------------------------------------------------ profiles
    def _profiles(self) -> list[dict]:
        return self.store.profiles(self._backend(), self._model_key(), self.hw) if self.model.currentIndex() >= 0 else []

    def _refresh_profiles(self, select: str | None = None) -> None:
        cur = select or self.prof.currentData()
        self.prof.clear()
        for p in sorted(self._profiles(), key=lambda p: p.get("when", 0), reverse=True):
            r = p.get("result") or {}
            v = p.get("values") or {}
            extra = f"  ·  ctx {int(v.get('ctx', 0)):,}".replace(",", ".") + (f" · {r['gen_tps']:.0f} tok/s" if r.get("gen_tps") else "")
            self.prof.addItem(("★ " if p.get("default") else "") + p["name"] + extra, p["name"])
        if not self.prof.count():
            self.prof.addItem("(noch kein Profil - Auto-Tune starten oder speichern)", None)
        i = self.prof.findData(cur)
        self.prof.setCurrentIndex(max(0, i))
        has = self.prof.currentData() is not None
        for b in (self.btn_prof_load, self.btn_prof_apply, self.btn_prof_def, self.btn_prof_del):
            b.setEnabled(has and not self._busy)

    def _cur_profile(self) -> dict | None:
        n = self.prof.currentData()
        return next((p for p in self._profiles() if p.get("name") == n), None)

    def profile_load(self) -> None:
        p = self._cur_profile()
        if p:
            self._apply_values(p.get("values") or {})
            self._say(f"Profil '{p['name']}' in die Parameter übernommen.", "ok")

    def profile_save(self) -> None:
        if not self._need_ready() or not self.values:
            return
        be, key = self._backend(), self._model_key()
        names = [p["name"] for p in self._profiles()]
        sugg = AT.default_profile_name(key, "manuell", names)
        name, ok = QInputDialog.getText(self, "Profil speichern", "Name des Profils (frei wählbar):", text=sugg)
        name = (name or "").strip()
        if not ok or not name:
            return
        if name in names and QMessageBox.question(self, "Profil speichern", f"Profil '{name}' überschreiben?") != QMessageBox.Yes:
            return
        make_def = QMessageBox.question(self, "Profil speichern", "Als Standard für dieses Modell setzen?") == QMessageBox.Yes
        self.store.save_profile(be, key, self.hw, {"name": name, "goal": "manuell", "level": "", "values": dict(self.values),
                                                    "when": time.time(), "result": {}}, make_default=make_def)
        self._refresh_profiles(select=name)
        self._say(f"Profil '{name}' gespeichert.", "ok")

    def profile_default(self) -> None:
        p = self._cur_profile()
        if p:
            self.store.set_default(self._backend(), self._model_key(), self.hw, p["name"])
            self._refresh_profiles(select=p["name"])
            self._say(f"'{p['name']}' ist jetzt Standard für dieses Modell.", "ok")

    def profile_delete(self) -> None:
        p = self._cur_profile()
        if p and QMessageBox.question(self, "Profil löschen", f"Profil '{p['name']}' löschen?") == QMessageBox.Yes:
            self.store.delete_profile(self._backend(), self._model_key(), self.hw, p["name"])
            self._refresh_profiles()

    def profile_apply(self) -> None:
        """llama.cpp: values -> config.json (+ restart offer). Ollama: Modelfile variant named after the profile."""
        p = self._cur_profile()
        if p:
            self._apply_values(p.get("values") or {})
        be = self._backend()
        if be == "llamacpp":
            self.apply_server()
            if self.ctx.procs.is_running("llamacpp") and QMessageBox.question(
                    self, "Profil", "llama.cpp-Server jetzt mit diesen Werten neu starten?") == QMessageBox.Yes:
                self.start_server()
            return
        exe = find_exe(self.ctx.cfg)
        if not exe:
            self._fail("ollama.exe nicht gefunden")
            return
        base, opts = self.model.currentText(), T.ollama_options(self.values)
        stem = base.split(":")[0]
        tag = re.sub(r"[^a-zA-Z0-9._-]+", "-", (p or {}).get("name", f"ctx{int(self.values['ctx']) // 1024}k"))[-40:]
        new, ok = QInputDialog.getText(self, "An Ollama übergeben", "Name der Modell-Variante:", text=f"{stem}:{tag}")
        new = (new or "").strip()
        if not ok or not new:
            return
        if QMessageBox.question(self, "An Ollama übergeben", "Auch KV-Typ und Slots als Ollama-Server-Standard speichern?\n"
                                "(wirkt nach Neustart von Ollama)") == QMessageBox.Yes:
            T.apply_ollama_server(self.ctx.cfg, self.values)
        self._set_busy(True)

        def work():
            return T.create_variant(str(exe), f"{self.ctx.cfg.host()}:{self.ctx.cfg.port('ollama')}", base, new, opts)

        def done(_out):
            self._set_busy(False)
            self._say(f"Ollama-Variante '{new}' angelegt - nutzbar in Claude / OpenWebUI / Dashboard.", "ok")
            self._log(f"Ollama-Variante {new} aus {base}: " + ", ".join(f"{k}={v}" for k, v in opts.items()))
            self.ctx.log(f"[tuning] Variante {new} aus {base} angelegt")

        def fail(msg):
            self._set_busy(False)
            self._fail(f"Variante nicht angelegt: {msg}")

        run_async(work, on_result=done, on_error=fail)

    def start_live(self) -> None:
        if not self._need_ready():
            return
        if self.model_error or not self.spec.known:
            self._log("Hinweis: Modell-Info fehlt - Live-Test läuft trotzdem, aber ohne VRAM-Schätzung")
        be, base, name = self._backend(), self._base_url(), self.model.currentText()
        if not self._ensure_service(be):
            return
        vals = dict(self.values)
        n = int(self.prompt_len.currentData() or 0)
        prompt = T.make_prompt(n)
        pred = vals["predict"] if vals["predict"] > 0 else 256
        vals["predict"] = min(pred, 512)
        self.out.clear()
        self.stats.setText("Starte ...")
        self._last_tokens = 0
        self.req_stats.begin()
        self._counted = True
        self._log(f"=== Live-Test: {self._svc_name(be)}, Modell {name}, Prompt ca. {n or 30} Tokens, max {vals['predict']} Antwort-Tokens ===")
        self._say("Live-Test läuft ...", "info")
        want_ctx, want_model = int(vals["ctx"]), Path(str(self.model.currentData())).name

        def run(emit, cancelled):
            log = lambda t: emit("log", t)       # noqa: E731
            if not T.wait_for_service(base, be, on_log=log, cancelled=cancelled):
                raise RuntimeError("Dienst wurde nicht bereit (Zeitlimit 3 Minuten) - Dashboard-Log prüfen")
            if be == "llamacpp":
                props = T.server_props(base)
                n_ctx = (props.get("default_generation_settings") or {}).get("n_ctx")
                model_path = str(props.get("model_path", ""))
                log(f"Server meldet: Modell {Path(model_path).name or '?'}, n_ctx {n_ctx or '?'}, Slots {props.get('total_slots', '?')}")
                if n_ctx and int(n_ctx) != want_ctx:
                    log(f"HINWEIS: Der Server läuft mit n_ctx={n_ctx}, eingestellt sind {want_ctx}. 'Auf Server anwenden' und Dienst neu starten.")
                if model_path and Path(model_path).name != want_model:
                    log(f"HINWEIS: Der Server hat {Path(model_path).name} geladen, ausgewählt ist {want_model}.")
            else:
                for m in T.http_json(f"{base}/api/ps", timeout=5).get("models", []):
                    log(f"Ollama hat geladen: {m.get('name')} ({int(m.get('size', 0)) / T.GIB:.1f} GB, davon VRAM {int(m.get('size_vram', 0)) / T.GIB:.1f} GB)")
            log("Sende Anfrage ...")
            if be == "ollama":
                stream = T.ollama_chat_stream(base, name, prompt, T.ollama_options(vals), cancelled=cancelled)
            else:
                stream = T.llama_completion_stream(base, prompt, T.llama_params(vals), cancelled=cancelled)
            first = True
            for ev in stream:
                if ev["type"] == "token" and first:
                    first = False
                    log(f"Erstes Token nach {ev['ttft']:.2f} s - Antwort wird erzeugt ...")
                if ev["type"] == "final":
                    log(f"Fertig: {ev['gen_tokens']} Tokens @ {ev['gen_tps']:.1f} tok/s, Prompt {ev['prompt_tokens']} Tokens @ "
                        f"{ev['prompt_tps']:.0f} tok/s, Laden {ev['load_s']:.1f} s, gesamt {ev['total_s']:.1f} s")
                emit(ev["type"], ev)

        self._launch(run)

    def start_probe(self) -> None:
        if not self._need_ready():
            return
        be = self._backend()
        if not self.spec.known:
            self._fail("Maximum messen nicht möglich: Modell-Info fehlt" + (f" ({self.model_error})" if self.model_error else ""),
                       "Modell neu laden bzw. Modelldatei prüfen")
            return
        kv = self._kv()
        fill = 0.5 if self.fill.isChecked() else 0.0
        spec, hw, name, data = self.spec, self.hw, self.model.currentText(), self.model.currentData()
        self.table.setRowCount(0)
        pre_wait = 0
        if be == "llamacpp":
            if self.ctx.procs.is_running("llamacpp"):
                if QMessageBox.question(self, "Tuning", "Der llama.cpp-Server läuft und belegt VRAM - der Test startet eigene "
                                        "Server-Instanzen.\nDienst jetzt stoppen?") != QMessageBox.Yes:
                    self._say("Test nicht gestartet: llama.cpp-Server läuft (VRAM belegt). Erst stoppen.", "warn")
                    return
                self.ctx.procs.stop("llamacpp")
                self._log("llama.cpp-Server gestoppt (VRAM wird frei)")
                pre_wait = 3
            exe = self.ctx.cfg.path("llamacpp") / "bin" / "llama-server.exe"
            if not exe.is_file():
                self._fail(f"llama-server.exe nicht gefunden: {exe}")
                return
            path = self._resolve_gguf(data)
            if path is None:
                self._fail(f"Modelldatei nicht gefunden: {data}")
                return
            ngl = 99 if int(self.values["gpu_layers"]) < 0 else int(self.values["gpu_layers"])
            batch = int(self.values["batch"])

            def used() -> int:
                try:
                    from core.system import gpu_info
                    return int((gpu_info() or {}).get("used_mb", 0))
                except Exception:      # noqa: BLE001
                    return 0

            def run(emit, cancelled):
                log = lambda t: emit("log", t)       # noqa: E731
                if pre_wait:
                    log(f"Warte {pre_wait} s, bis der VRAM frei ist ...")
                    time.sleep(pre_wait)
                log(f"Modell: {path}  |  GPU-Layer {ngl}, Batch {batch}, KV {kv}")
                res = T.probe_llamacpp([str(exe)], str(path), spec, kv_type=kv, fill_frac=fill, ngl=ngl, batch=batch,
                                       total_vram_mb=hw.vram_total_mb, gpu_used_mb=used,
                                       on_step=lambda s: emit("step", s), on_status=lambda t: emit("status", t),
                                       on_log=log, cancelled=cancelled)
                emit("result", (res, str(path)))
        else:
            if not self._ensure_service("ollama"):
                return
            cl = self.ctx.ollama()

            def run(emit, cancelled):
                log = lambda t: emit("log", t)       # noqa: E731
                if not T.wait_for_service(cl.base, "ollama", on_log=log, cancelled=cancelled):
                    raise RuntimeError("Ollama wurde nicht bereit (Zeitlimit)")
                for m in cl.ps():                    # other loaded models would distort the VRAM measurement
                    if m.get("name") != name:
                        log(f"Entlade fremdes Modell {m.get('name')} (würde die VRAM-Messung verfälschen)")
                        cl.unload(m.get("name"))
                res = T.probe_ollama(cl.base, name, spec, kv_type=kv, fill_frac=fill, on_step=lambda s: emit("step", s),
                                     on_status=lambda t: emit("status", t), on_log=log, cancelled=cancelled)
                emit("result", (res, name))

        self._log(f"=== Maximum messen: {self._svc_name(be)}, Modell {name}, KV {kv}"
                  f"{', Kontext zur Hälfte gefüllt' if fill else ''} ===")
        self._say("Maximum-Messung läuft - jeder Schritt steht im Protokoll.", "info")
        self._launch(run)

    # ------------------------------------------------------------------ event pump
    def _poll(self) -> None:
        if not self.job:
            return
        text, last = [], None
        t_end = time.monotonic() + 0.03            # time budget per tick: the GUI stays responsive even if the
        n = 0                                      # worker produces events faster than they can be shown
        try:
            while True:
                n += 1
                if n > 400 or time.monotonic() > t_end:
                    break
                kind, payload = self.job.q.get_nowait()
                if kind == "token":
                    text.append(payload["text"])
                    last = payload
                elif kind == "final":
                    self.req_stats.ok(payload, self.model.currentText())
                    self._on_final(payload)
                    last = None                # the server's own final number wins over the last streamed estimate
                elif kind == "step":
                    self._record_step(payload)
                    self._add_step(payload)
                elif kind == "log":
                    self._log(str(payload))
                elif kind == "status":
                    self._say(payload, "info")
                elif kind == "result":
                    self._on_result(*payload)
                elif kind == "at_round":
                    self._at_add_round(payload)
                elif kind == "at_progress":
                    self.at_bar.setValue(int(payload[0]))
                    self.at_bar.setFormat(f"{payload[0]} %  ·  {payload[1]}")
                    self._say(payload[1], "info")
                elif kind == "at_done":
                    self._at_done(payload)
                elif kind == "cancelled":
                    self.stats.setText(self.stats.text() + " · abgebrochen")
                    self._log("Abgebrochen.")
                    self._say("Abgebrochen.", "warn")
                elif kind == "error":
                    self.req_stats.fail(str(payload), "/api/chat" if self._backend() == "ollama" else "/completion", self.model.currentText())
                    self._on_error(payload)
                elif kind == "end":
                    self._log("--- Ende ---")
                    if self._counted:
                        self._counted = False
                        self.req_stats.end()
                    self.job = None
                    self._set_busy(False)
                    break
        except queue.Empty:
            pass
        if text:
            self.out.moveCursor(self.out.textCursor().MoveOperation.End)
            self.out.insertPlainText("".join(text))
        if last:
            self.ch_gen.push(last["tps"], f"{last['tps']:.1f} tok/s", f"{last['n']} Tokens · erstes Token {last['ttft']:.2f} s")
            self.stats.setText(f"{last['n']} Tokens · {last['tps']:.1f} tok/s · erstes Token nach {last['ttft']:.2f} s")

    def _record_step(self, s: T.ProbeStep) -> None:
        ep = "/api/chat" if self._backend() == "ollama" else "/completion"
        if s.ok:
            self.req_stats.ok({"endpoint": ep, "http_status": 200, "gen_tps": s.gen_tps, "prompt_tps": s.prompt_tps,
                               "load_s": s.load_s, "gen_tokens": 48}, self.model.currentText())
        else:
            self.req_stats.fail(s.error or s.note or "Schritt fehlgeschlagen", ep, self.model.currentText())

    def _on_final(self, f: dict) -> None:
        self.ch_gen.push(f["gen_tps"], f"{f['gen_tps']:.1f} tok/s", "Endwert")
        self.ch_prompt.push(f["prompt_tps"], f"{f['prompt_tps']:.0f} tok/s", f"{f['prompt_tokens']} Prompt-Tokens")
        self._say(f"Live-Test fertig: {f['gen_tps']:.1f} tok/s beim Generieren, {f['prompt_tps']:.0f} tok/s beim Prompt.", "ok")
        self.stats.setText(f"Generierung {f['gen_tps']:.1f} tok/s ({f['gen_tokens']} Tokens) · Prompt {f['prompt_tps']:.0f} tok/s "
                           f"({f['prompt_tokens']} Tokens) · Laden {f['load_s']:.1f} s · gesamt {f['total_s']:.1f} s")

    def _on_error(self, msg: str) -> None:
        hint = T.suggest_ctx_from_error(msg, self.spec, self.hw, self._kv())
        low = msg.lower()
        if "10061" in msg or "refused" in low or "network error" in low or "verbindung" in low:
            hint = hint or f"Server nicht erreichbar ({self._base_url()}) - läuft der Dienst? Im Dashboard starten."
        self._fail(msg[:400], hint)
        self.stats.setText("Fehler" + (f" - {hint}" if hint else ""))
        if hint:
            self.out.appendPlainText(hint)

    def _add_step(self, s: T.ProbeStep) -> None:
        r = self.table.rowCount()
        self.table.insertRow(r)
        res = "✓ passt" if s.good else ("✗ " + (s.error or s.note or "zu langsam"))
        vals = [f"{s.ctx:,}".replace(",", "."), res, f"{s.gen_tps:.1f}" if s.ok else "-", f"{s.prompt_tps:.0f}" if s.ok else "-",
                f"{s.gpu_pct:.0f} %" if s.ok else "-", str(s.vram_mb) if s.vram_mb else "-"]
        for c, v in enumerate(vals):
            self.table.setItem(r, c, QTableWidgetItem(v))
        self.table.scrollToBottom()
        if s.ok and self._busy:
            self.ch_gen.push(s.gen_tps, f"{s.gen_tps:.1f} tok/s", f"Kontext {s.ctx}")

    def _on_result(self, res: T.ProbeResult, model_key: str) -> None:
        res.model = model_key
        if res.max_ctx:
            self.store.put(res, self.hw)
            msg = f"Fertig: Maximum {res.max_ctx:,} Tokens ({res.stopped}).".replace(",", ".")
            self._log(msg)
            self._say(msg + " Der Wert ist jetzt als Maximum der Kontext-Zeile gesetzt.", "ok")
            self._rebuild(keep=True)
            self.table.setRowCount(0)
            for s in res.steps:
                self._add_step(s)
        else:
            self._fail(f"Kein passender Kontext gefunden ({res.stopped})",
                       "Schon der kleinste Test (4096) lief nicht sauber - Protokoll prüfen (Server-Ausgabe steht dort)")

    def _on_sample(self, d: dict) -> None:
        if d and "error" not in d:
            self._sample = d
        g = (d or {}).get("gpu")
        if g and self._busy and g.get("total_mb"):
            pct = 100.0 * g["used_mb"] / g["total_mb"]
            self.ch_vram.push(pct, f"{g['used_mb'] / 1024:.1f} GB", f"{pct:.0f} % von {g['total_mb'] / 1024:.0f} GB")

    # ------------------------------------------------------------------ apply
    def apply_server(self) -> None:
        be = self._backend()
        if be == "llamacpp":
            sect = self.ctx.cfg.data.setdefault("llamacpp", {})
            sect["model"] = self.model.currentData()          # config value (path relative to the library), not the label
            T.apply_llamacpp(self.ctx.cfg, self.values)
            msg = "llama.cpp-Einstellungen gespeichert."
        else:
            T.apply_ollama_server(self.ctx.cfg, self.values)
            msg = "Ollama-Server-Standard gespeichert (Kontext, KV-Typ, Slots)."
        self.launcher = self._launcher_values(be)
        running = self.ctx.procs.is_running(be)
        tail = (" Der Dienst läuft - im Dashboard neu starten, damit es wirkt." if running
                else " Wirkt beim nächsten Start des Dienstes.")
        self._log(msg + tail)
        self._say(msg + tail, "ok")
        self.ctx.log(f"[tuning] {msg}")

    def save_variant(self) -> None:
        if not self._need_ready():
            return
        exe = find_exe(self.ctx.cfg)
        if not exe:
            self._fail("ollama.exe nicht gefunden")
            return
        base, opts = self.model.currentText(), T.ollama_options(self.values)
        new = T.variant_name(base, int(self.values["ctx"]))
        self._set_busy(True)

        def work():
            return T.create_variant(str(exe), f"{self.ctx.cfg.host()}:{self.ctx.cfg.port('ollama')}", base, new, opts)

        def done(_out):
            self._set_busy(False)
            self._log(f"Modell-Variante '{new}' angelegt")
            self._say(f"Modell-Variante '{new}' angelegt (Modell-Liste mit 'Neu laden' aktualisieren).", "ok")
            self.ctx.log(f"[tuning] Variante {new} aus {base} angelegt")

        def fail(msg):
            self._set_busy(False)
            self._fail(f"Variante nicht angelegt: {msg}")

        run_async(work, on_result=done, on_error=fail)
