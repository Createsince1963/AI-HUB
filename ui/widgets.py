"""Reusable widgets: cards, async helper, sortable table items, live charts."""
from __future__ import annotations

import os
import subprocess
import sys
from collections import deque
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QObject, QRect, QRectF, QRunnable, Qt, QThreadPool, Signal
from PySide6.QtGui import QColor, QFont, QLinearGradient, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (QFileDialog, QFrame, QHBoxLayout, QLabel, QLineEdit, QPushButton, QTableWidget,
                               QTableWidgetItem, QVBoxLayout, QWidget)

from . import theme
from .theme import ACCENT, BAD, MUTED, OK, WARN


def open_folder(path: Path | str, select: bool = False) -> None:
    """Open a folder (or, with select=True, a file's parent with the file highlighted) in the OS
    file manager. Shared by every page that offers an 'Open ... folder' button (Models/Downloads,
    Settings path rows, ...) so there is exactly one place that knows how to do this per platform."""
    p = str(path)
    if sys.platform == "win32":
        if select:
            subprocess.Popen(["explorer", "/select,", p])
        else:
            os.startfile(p)            # noqa: S606
    else:
        import webbrowser
        webbrowser.open(f"file://{p}")


# ------------------------------------------------------------------ async helper
class _Signals(QObject):
    result = Signal(object)
    error = Signal(str)


class _Task(QRunnable):
    def __init__(self, fn: Callable, args: tuple):
        super().__init__()
        self.fn, self.args, self.sig = fn, args, _Signals()
        self.setAutoDelete(False)

    def run(self) -> None:
        try:
            self.sig.result.emit(self.fn(*self.args))
        except Exception as exc:        # noqa: BLE001 - surfaced through the error callback
            self.sig.error.emit(str(exc))


_alive: set = set()


def run_async(fn: Callable, *args, on_result: Callable | None = None, on_error: Callable | None = None) -> None:
    """Run fn(*args) in the thread pool; callbacks run in the GUI thread."""
    task = _Task(fn, args)
    _alive.add(task)

    def done(cb):
        def wrapper(v):
            _alive.discard(task)
            if cb:
                cb(v)
        return wrapper
    task.sig.result.connect(done(on_result))
    task.sig.error.connect(done(on_error))
    QThreadPool.globalInstance().start(task)


# ------------------------------------------------------------------ layout helpers
def card(margin: int = 14) -> tuple[QFrame, QVBoxLayout]:
    f = QFrame()
    f.setObjectName("Card")
    lay = QVBoxLayout(f)
    lay.setContentsMargins(margin, margin, margin, margin)
    return f, lay


def section(text: str) -> QLabel:
    lab = QLabel(text.upper())
    lab.setObjectName("Section")
    return lab


def button(text: str, kind: str = "", slot: Callable | None = None, tip: str = "") -> QPushButton:
    b = QPushButton(text)
    if kind:
        b.setObjectName(kind)
    if slot:
        b.clicked.connect(lambda _=False: slot())
    if tip:
        b.setToolTip(tip)
    return b


def page_header(title: str, subtitle: str = "") -> QWidget:
    w = QWidget()
    lay = QVBoxLayout(w)
    lay.setContentsMargins(0, 0, 0, 4)
    lay.setSpacing(0)
    t = QLabel(title)
    t.setObjectName("Title")
    lay.addWidget(t)
    if subtitle:
        s = QLabel(subtitle)
        s.setObjectName("Subtitle")
        lay.addWidget(s)
    return w


class NumItem(QTableWidgetItem):
    """Table item that sorts by a number but shows text."""

    def __init__(self, text: str, key: float):
        super().__init__(text)
        self.key = key
        self.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)

    def __lt__(self, other) -> bool:            # noqa: D105
        return self.key < getattr(other, "key", 0)


def make_table(headers: list[str], stretch_col: int = 0) -> QTableWidget:
    t = QTableWidget(0, len(headers))
    t.setHorizontalHeaderLabels(headers)
    t.setSelectionBehavior(QTableWidget.SelectRows)
    t.setEditTriggers(QTableWidget.NoEditTriggers)
    t.setAlternatingRowColors(True)
    t.verticalHeader().setVisible(False)
    t.verticalHeader().setDefaultSectionSize(26)
    t.setSortingEnabled(True)
    t.setShowGrid(False)
    t.horizontalHeader().setStretchLastSection(False)
    hh = t.horizontalHeader()
    for c in range(len(headers)):
        hh.setSectionResizeMode(c, hh.ResizeMode.Stretch if c == stretch_col else hh.ResizeMode.ResizeToContents)
    return t


class PathRow(QWidget):
    """Label + line edit + Browse + Open (file manager) + status mark."""
    changed = Signal(str)

    def __init__(self, folder: bool = True, filt: str = ""):
        super().__init__()
        self.folder, self.filt = folder, filt
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.edit = QLineEdit()
        self.edit.editingFinished.connect(lambda: self.changed.emit(self.edit.text().strip()))
        self.btn = QPushButton("...")
        self.btn.setMinimumWidth(36)
        self.btn.setStyleSheet("padding:6px 0;")
        self.btn.setToolTip("Choose a different folder")
        self.btn.clicked.connect(self._browse)
        self.open_btn = QPushButton("Open")
        self.open_btn.setMinimumWidth(48)
        self.open_btn.setStyleSheet("padding:6px 0;")
        self.open_btn.setToolTip("Open this folder in the file manager")
        self.open_btn.clicked.connect(self._open)
        self.mark = QLabel()
        self.mark.setMinimumWidth(110)
        lay.addWidget(self.edit, 1)
        lay.addWidget(self.btn)
        lay.addWidget(self.open_btn)
        lay.addWidget(self.mark)

    def _browse(self) -> None:
        start = self.edit.text() or ""
        if self.folder:
            p = QFileDialog.getExistingDirectory(self, "Select folder", start)
        else:
            p, _ = QFileDialog.getOpenFileName(self, "Select file", start, self.filt)
        if p:
            self.edit.setText(p.replace("/", "\\") if "\\" in start else p)
            self.changed.emit(self.edit.text())

    def _open(self) -> None:
        p = self.edit.text().strip()
        if p and Path(p).exists():
            open_folder(p)

    def set_mark(self, text: str, colour: str) -> None:
        self.mark.setText(text)
        self.mark.setStyleSheet(f"color:{colour}; font-weight:600;")
        self.open_btn.setEnabled(bool(self.edit.text().strip()) and Path(self.edit.text().strip()).exists())


# ------------------------------------------------------------------ live charts
def load_colour(pct: float) -> QColor:
    return QColor(BAD if pct >= 90 else WARN if pct >= 70 else OK)


class SparkChart(QWidget):
    """Live area chart with title, current value and rolling history."""

    def __init__(self, title: str, colour: str = ACCENT, maximum: float = 100.0, unit: str = "%", history: int = 90,
                 auto_scale: bool = False):
        super().__init__()
        self.title, self.colour, self.max, self.unit = title, QColor(colour), maximum, unit
        self.auto_scale = auto_scale
        self.data: deque[float] = deque([0.0] * history, maxlen=history)
        self.value_text = "-"
        self.sub_text = ""
        self.setMinimumSize(170, 74)      # dashboard caps tiles at 84 px; a larger minimum would override that

    def push(self, value: float, text: str | None = None, sub: str = "") -> None:
        self.data.append(value)
        self.value_text = text if text is not None else f"{value:.0f}{self.unit}"
        self.sub_text = sub
        self.update()

    def paintEvent(self, _e) -> None:            # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        p.setBrush(QColor(theme.col("card")))
        p.setPen(QPen(QColor(theme.col("border")), 1))
        p.drawRoundedRect(r, 10, 10)
        p.setPen(QColor(theme.col("muted")))
        f = QFont(self.font())
        f.setPointSizeF(8.5)
        f.setBold(True)
        p.setFont(f)
        p.drawText(QRectF(r.left() + 10, r.top() + 6, r.width() - 20, 16), Qt.AlignLeft, self.title.upper())
        f.setPointSizeF(14)
        p.setFont(f)
        last = self.data[-1]
        p.setPen(load_colour(last) if self.unit == "%" and last >= 70 else QColor(theme.col("title")))
        p.drawText(QRectF(r.left() + 10, r.top() + 20, r.width() - 20, 26), Qt.AlignLeft, self.value_text)
        if self.sub_text:
            f.setPointSizeF(8.5)
            f.setBold(False)
            p.setFont(f)
            p.setPen(QColor(theme.col("muted")))
            p.drawText(QRectF(r.left() + 10, r.top() + 6, r.width() - 20, 16), Qt.AlignRight, self.sub_text)
        plot = QRectF(r.left() + 8, r.top() + 48, r.width() - 16, r.height() - 56)
        if plot.height() < 8:
            return
        mx = max(max(self.data) * 1.15, 1.0) if self.auto_scale else self.max
        n = len(self.data)
        path = QPainterPath()
        pts = []
        for i, v in enumerate(self.data):
            x = plot.left() + plot.width() * i / max(n - 1, 1)
            y = plot.bottom() - plot.height() * min(v / mx, 1.0)
            pts.append((x, y))
        path.moveTo(pts[0][0], plot.bottom())
        for x, y in pts:
            path.lineTo(x, y)
        path.lineTo(pts[-1][0], plot.bottom())
        grad = QLinearGradient(0, plot.top(), 0, plot.bottom())
        c1, c2 = QColor(self.colour), QColor(self.colour)
        c1.setAlpha(110)
        c2.setAlpha(10)
        grad.setColorAt(0, c1)
        grad.setColorAt(1, c2)
        p.setPen(Qt.NoPen)
        p.setBrush(grad)
        p.drawPath(path)
        p.setPen(QPen(self.colour, 1.6))
        line = QPainterPath()
        line.moveTo(*pts[0])
        for x, y in pts[1:]:
            line.lineTo(x, y)
        p.setBrush(Qt.NoBrush)
        p.drawPath(line)


class CoreBars(QWidget):
    """One vertical bar per logical CPU core."""

    def __init__(self):
        super().__init__()
        self.cores: list[float] = []
        self.setMinimumHeight(110)

    def set_cores(self, cores: list[float]) -> None:
        self.cores = cores
        self.update()

    def paintEvent(self, _e) -> None:            # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        n = len(self.cores)
        if not n:
            return
        w, h = self.width(), self.height() - 16
        gap = 4
        bw = max((w - gap * (n - 1)) / n, 4)
        f = QFont(self.font())
        f.setPointSizeF(7.5)
        p.setFont(f)
        for i, v in enumerate(self.cores):
            x = i * (bw + gap)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(theme.col("track")))
            p.drawRoundedRect(QRectF(x, 0, bw, h), 3, 3)
            bh = h * min(v, 100) / 100
            p.setBrush(load_colour(v))
            p.drawRoundedRect(QRectF(x, h - bh, bw, bh), 3, 3)
            p.setPen(QColor(theme.col("muted")))
            p.drawText(QRect(int(x - 4), h + 1, int(bw + 8), 14), Qt.AlignCenter, str(i))


class Meter(QWidget):
    """Label + coloured bar + value text (RAM, VRAM, disk...)."""

    def __init__(self, title: str):
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)
        row = QHBoxLayout()
        self.title = QLabel(title)
        self.title.setObjectName("Muted")
        self.value = QLabel("-")
        self.value.setAlignment(Qt.AlignRight)
        row.addWidget(self.title)
        row.addWidget(self.value, 1)
        lay.addLayout(row)
        from PySide6.QtWidgets import QProgressBar
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.setTextVisible(False)
        self.bar.setMaximumHeight(10)
        lay.addWidget(self.bar)

    def set(self, pct: float, text: str) -> None:
        self.bar.setValue(int(pct * 10))
        col = BAD if pct >= 90 else WARN if pct >= 75 else theme.col("chunk")
        self.bar.setStyleSheet(f"QProgressBar::chunk{{background:{col};border-radius:5px;}}")
        self.value.setText(text)
