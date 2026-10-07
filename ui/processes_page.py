"""Processes: one tab per started process (server, updater, installer) with its live log.

Replaces the separate cmd windows. Interactive CLIs (Claude, Codex, ...) get their own sub-tab in HUB Terminal.

Every process tab shows a live status strip above the log so that long-running jobs (e.g. the ComfyUI
update) can be followed: current step ("[3/5] Constraints ..."), a progress bar (real steps when the
script prints "[k/N]" headers, otherwise a busy indicator), elapsed time and the time since the last
output (so "silent but working" is distinguishable from "hung"). Carriage-return progress lines
(pip / git / downloads) overwrite the current log line instead of flooding the log."""
from __future__ import annotations

import re
import time
from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtGui import QColor, QFont, QGuiApplication, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import (QCheckBox, QFileDialog, QHBoxLayout, QLabel, QPlainTextEdit, QProgressBar, QTabWidget,
                               QVBoxLayout, QWidget)

from .context import Ctx
from .widgets import button, page_header

OK, BAD, MUTED, WARN = "#2e9e4f", "#d93025", "#5f6b7a", "#B26A00"
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
STEP = re.compile(r"^\s*\[(\d+)\s*/\s*(\d+)\]\s*(.*)$")          # "[3/5] Constraints (keep the CUDA stack)"
SILENT_HINT_S = 20                                                 # no output for this long -> "still working" hint


def _fmt_dur(sec: float) -> str:
    sec = int(max(0, sec))
    return f"{sec // 60:02d}:{sec % 60:02d}"


class ProcessTab(QWidget):
    def __init__(self, ctx: Ctx, key: str, title: str):
        super().__init__()
        self.ctx, self.key, self.title = ctx, key, title
        self._line = ""              # current (not yet newline-terminated) log line
        self._cr = False             # last separator was a bare "\r" -> next text overwrites the current line
        self._running = False
        self._t0 = time.monotonic()
        self._last_out = self._t0
        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 8, 8, 6)

        # ---- live status strip: step text | elapsed / last output, then a progress bar
        strip = QHBoxLayout()
        self.step_label = QLabel("starting ...")
        self.step_label.setStyleSheet("font-weight:700;")
        strip.addWidget(self.step_label, 1)
        self.timing = QLabel("")
        self.timing.setObjectName("Muted")
        strip.addWidget(self.timing)
        lay.addLayout(strip)
        self.bar = QProgressBar()
        self.bar.setTextVisible(True)
        self.bar.setFixedHeight(16)
        self.bar.setRange(0, 0)                                  # busy until the script reports real steps
        lay.addWidget(self.bar)

        self.out = QPlainTextEdit()
        self.out.setObjectName("Console")
        self.out.setReadOnly(True)
        self.out.setMaximumBlockCount(20000)
        self.out.setLineWrapMode(QPlainTextEdit.NoWrap)
        f = QFont("Cascadia Mono")
        f.setStyleHint(QFont.Monospace)
        f.setPointSize(9)
        self.out.setFont(f)
        lay.addWidget(self.out, 1)
        bar = QHBoxLayout()
        self.stop_btn = button("Stop", "Danger", lambda: self.ctx.procs.stop(self.key))
        bar.addWidget(self.stop_btn)
        bar.addWidget(button("Clear", "", self.out.clear))
        bar.addWidget(button("Copy all", "", lambda: QGuiApplication.clipboard().setText(self.out.toPlainText())))
        bar.addWidget(button("Save log...", "", self.save))
        self.wrap = QCheckBox("Word wrap")
        self.wrap.toggled.connect(lambda on: self.out.setLineWrapMode(
            QPlainTextEdit.WidgetWidth if on else QPlainTextEdit.NoWrap))
        self.auto = QCheckBox("Auto-scroll")
        self.auto.setChecked(True)
        bar.addWidget(self.wrap)
        bar.addWidget(self.auto)
        bar.addStretch(1)
        self.state = QLabel("running")
        self.state.setStyleSheet(f"color:{OK}; font-weight:700;")
        bar.addWidget(self.state)
        lay.addLayout(bar)

        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._update_strip)

    # ------------------------------------------------------------------ log rendering
    @staticmethod
    def _colour(line: str) -> str:
        low = line.lower()
        head = low[:24]
        if "error" in low or " e " in head or "failed" in low or "traceback" in low:
            return "#FF8A80"
        if " w " in head or "warn" in low:
            return "#F0B429"
        if STEP.match(line):
            return "#7FD1FF"
        return "#D7E3F1"

    def append(self, text: str) -> None:
        if not text:
            return
        self._last_out = time.monotonic()
        text = ANSI.sub("", text)
        cur = self.out.textCursor()
        cur.movePosition(QTextCursor.End)
        for tok in re.split(r"(\r\n|\n|\r)", text):
            if tok == "":
                continue
            if tok in ("\n", "\r\n"):
                self._line_done(self._line)
                self._line, self._cr = "", False
                cur.insertText("\n")
            elif tok == "\r":
                self._cr = True
            else:
                if self._cr:                      # progress line: replace what is on the current line
                    cur.movePosition(QTextCursor.StartOfBlock)
                    cur.movePosition(QTextCursor.End, QTextCursor.KeepAnchor)
                    cur.removeSelectedText()
                    self._line, self._cr = "", False
                self._line += tok
                fmt = QTextCharFormat()
                fmt.setForeground(QColor(self._colour(tok)))
                cur.insertText(tok, fmt)
        if self.auto.isChecked():
            self.out.moveCursor(QTextCursor.End)
        self._update_strip()

    def _line_done(self, line: str) -> None:
        m = STEP.match(line)
        if m:
            k, n = int(m.group(1)), int(m.group(2))
            if 0 < k <= n:
                self.bar.setRange(0, n)
                self.bar.setValue(k - 1)
                self.bar.setFormat(f"step {k} of {n}")
                self.step_label.setText(f"[{k}/{n}] {m.group(3).strip()}")

    # ------------------------------------------------------------------ status strip
    def _update_strip(self) -> None:
        if not self._running:
            return
        now = time.monotonic()
        idle = now - self._last_out
        txt = f"elapsed {_fmt_dur(now - self._t0)}  |  last output {int(idle)} s ago"
        if idle >= SILENT_HINT_S:
            txt += "  -  still working (download / git / pip can be silent for a while)"
            self.timing.setStyleSheet(f"color:{WARN};")
        else:
            self.timing.setStyleSheet("")
        self.timing.setText(txt)

    def set_running(self, running: bool, code: int = 0) -> None:
        self.stop_btn.setEnabled(running)
        self._running = running
        if running:
            self._t0 = self._last_out = time.monotonic()
            self.bar.setRange(0, 0)
            self.bar.setFormat("%p%")
            self.step_label.setText("running ...")
            self.step_label.setStyleSheet("font-weight:700;")
            self._timer.start()
            self.state.setText("running")
            self.state.setStyleSheet(f"color:{OK}; font-weight:700;")
            self._update_strip()
        else:
            self._timer.stop()
            took = _fmt_dur(time.monotonic() - self._t0)
            self.bar.setRange(0, 1)
            self.bar.setValue(1 if code == 0 else 0)
            self.bar.setFormat("done" if code == 0 else "failed")
            self.step_label.setText(("Finished" if code == 0 else f"Failed (exit {code})") + f" - {took}")
            self.step_label.setStyleSheet(f"font-weight:700; color:{OK if code == 0 else BAD};")
            self.timing.setText("")
            self.state.setText(f"ended (exit {code})")
            self.state.setStyleSheet(f"color:{OK if code == 0 else BAD}; font-weight:700;")

    def save(self) -> None:
        f, _ = QFileDialog.getSaveFileName(self, "Save log", f"{self.title.replace(' ', '_')}.log", "Text (*.log *.txt)")
        if f:
            Path(f).write_text(self.out.toPlainText(), encoding="utf-8")


class ProcessesPage(QWidget):
    def __init__(self, ctx: Ctx, header: bool = True):
        super().__init__()
        self.setObjectName("Page")
        self.ctx = ctx
        self.tabs_by_key: dict[str, ProcessTab] = {}
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 14, 18, 10)
        if header:        # HUB Terminal supplies the page header when this is embedded as a sub-tab
            lay.addWidget(page_header("Processes", "One tab per started service / installer with its live log"))
        self.empty = QLabel("No process started yet. Start a service on the Dashboard - its log appears here.")
        self.empty.setObjectName("Muted")
        lay.addWidget(self.empty)
        self.tabs = QTabWidget()
        self.tabs.setTabsClosable(True)
        self.tabs.setDocumentMode(True)
        self.tabs.tabCloseRequested.connect(self._close)
        lay.addWidget(self.tabs, 1)
        self.tabs.setVisible(False)
        p = ctx.procs
        p.spawned.connect(self._spawned)
        p.output.connect(self._output)
        p.ended.connect(self._ended)

    def _spawned(self, key: str, title: str) -> None:
        tab = self.tabs_by_key.get(key)
        if tab is None:
            tab = ProcessTab(self.ctx, key, title)
            self.tabs_by_key[key] = tab
            self.tabs.addTab(tab, title)
        else:
            tab.append("\n----- restarted -----\n")
        tab.set_running(True)
        self.tabs.setCurrentWidget(tab)
        self.empty.setVisible(False)
        self.tabs.setVisible(True)
        self._retitle(tab, True)

    def _output(self, key: str, text: str) -> None:
        tab = self.tabs_by_key.get(key)
        if tab:
            tab.append(text)

    def _ended(self, key: str, code: int) -> None:
        tab = self.tabs_by_key.get(key)
        if tab:
            tab.append(f"\n[process ended, exit code {code}]\n")
            tab.set_running(False, code)
            self._retitle(tab, False)

    def _retitle(self, tab: ProcessTab, running: bool) -> None:
        i = self.tabs.indexOf(tab)
        if i >= 0:
            self.tabs.setTabText(i, ("● " if running else "○ ") + tab.title)

    def _close(self, i: int) -> None:
        tab = self.tabs.widget(i)
        if isinstance(tab, ProcessTab):
            self.ctx.procs.stop(tab.key)          # closing a running tab stops the process
            self.tabs_by_key.pop(tab.key, None)
        self.tabs.removeTab(i)
        if self.tabs.count() == 0:
            self.tabs.setVisible(False)
            self.empty.setVisible(True)
