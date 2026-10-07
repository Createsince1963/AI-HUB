"""Card dashboard ("Übersicht") for the Tuning page's live measurements.

Design follows the dark Ollama dashboard mock-up (KPI cards, ring gauges, token chart, model list); the DATA comes only
from core/telemetry.py. A value the server does not expose is shown as "-" - nothing is invented. All colours are read from
ui/theme.py at paint time, so a theme switch only needs update()."""
from __future__ import annotations

import time

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QConicalGradient, QFont, QLinearGradient, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (QAbstractItemView, QFrame, QGridLayout, QHBoxLayout, QLabel, QScrollArea, QTableWidget,
                               QSizePolicy, QTableWidgetItem, QVBoxLayout, QWidget)

from . import theme
from core.models import parse_quant
from .widgets import button, make_table

HIST_MAX = 90


# ------------------------------------------------------------------ throughput history (no Qt - unit-testable)
class TpsHistory:
    """(t, prompt_tps, gen_tps) points. llama.cpp: derived from /metrics counter deltas; Ollama: finished Tuning-page requests."""

    def __init__(self) -> None:
        self.points: list[tuple[float, float | None, float | None]] = []
        self._prev: dict | None = None
        self._seen = 0.0

    def feed_stats(self, stats) -> None:
        for h in stats.history:
            if h["when"] > self._seen and not h.get("failed"):
                self.points.append((h["when"], h.get("prompt_tps") or None, h.get("gen_tps") or None))
        if stats.history:
            self._seen = max(self._seen, stats.history[-1]["when"])
        del self.points[:-HIST_MAX]

    def feed_metrics(self, m: dict, now: float | None = None) -> None:
        now = now or time.time()
        cur = {k: m.get(f"llamacpp:{k}") for k in ("tokens_predicted_total", "tokens_predicted_seconds_total",
                                                     "prompt_tokens_total", "prompt_seconds_total")}
        prev, self._prev = self._prev, cur
        if not prev or any(v is None for v in cur.values()) or any(v is None for v in prev.values()):
            return
        gs, ps = cur["tokens_predicted_seconds_total"] - prev["tokens_predicted_seconds_total"], cur["prompt_seconds_total"] - prev["prompt_seconds_total"]
        gen = (cur["tokens_predicted_total"] - prev["tokens_predicted_total"]) / gs if gs > 0.001 else None
        pro = (cur["prompt_tokens_total"] - prev["prompt_tokens_total"]) / ps if ps > 0.001 else None
        if gen is not None or pro is not None:
            self.points.append((now, pro, gen))
            del self.points[:-HIST_MAX]


def request_buckets(events: list[float], n: int = 12, width_s: int = 60, now: float | None = None) -> list[int]:
    """Finished requests per `width_s` bucket, oldest first, last bucket = the current one."""
    now = now or time.time()
    out = [0] * n
    for t in events:
        i = int((now - t) // width_s)
        if 0 <= i < n:
            out[n - 1 - i] += 1
    return out


# ------------------------------------------------------------------ painted widgets
def _font(base: QFont, size: float, bold: bool = False) -> QFont:
    f = QFont(base)
    f.setPointSizeF(size)
    f.setBold(bold)
    return f


def _c(tok: str, alpha: int | None = None) -> QColor:
    c = QColor(theme.col(tok))
    if alpha is not None:
        c.setAlpha(alpha)
    return c


class Icon(QWidget):
    """Small line icon drawn with QPainter (no image files): server, model, chat, bolt, gpu, ram."""

    def __init__(self, kind: str, tok: str = "muted") -> None:
        super().__init__()
        self.kind, self.tok = kind, tok
        self.setMinimumSize(30, 30)
        self.setMaximumSize(38, 38)

    def paintEvent(self, _e) -> None:            # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        s = min(self.width(), self.height())
        r = QRectF((self.width() - s) / 2 + 3, (self.height() - s) / 2 + 3, s - 6, s - 6)
        c = _c(self.tok)
        p.setPen(QPen(c, 2, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        p.setBrush(Qt.NoBrush)
        k = self.kind
        if k == "server":
            for i in range(2):
                b = QRectF(r.left(), r.top() + i * r.height() / 2 + 1, r.width(), r.height() / 2 - 3)
                p.drawRoundedRect(b, 3, 3)
                p.drawLine(QPointF(b.left() + 5, b.center().y()), QPointF(b.left() + r.width() * 0.45, b.center().y()))
                p.drawPoint(QPointF(b.right() - 5, b.center().y()))
        elif k == "model":
            p.drawRoundedRect(r.adjusted(4, 4, -4, -4), 4, 4)
            for i in range(3):
                x = r.left() + 8 + i * (r.width() - 16) / 2
                p.drawLine(QPointF(x, r.top()), QPointF(x, r.top() + 4))
                p.drawLine(QPointF(x, r.bottom() - 4), QPointF(x, r.bottom()))
        elif k == "chat":
            b = r.adjusted(0, 0, 0, -5)
            path = QPainterPath()
            path.addRoundedRect(b, 6, 6)
            p.drawPath(path)
            p.drawLine(QPointF(b.left() + 7, b.bottom()), QPointF(b.left() + 4, r.bottom()))
            for i in range(3):
                p.drawPoint(QPointF(b.center().x() + (i - 1) * 6, b.center().y()))
        elif k == "bolt":
            p.setPen(Qt.NoPen)
            p.setBrush(_c("ok"))
            w, h, x, y = r.width(), r.height(), r.left(), r.top()
            pts = [QPointF(x + w * .62, y), QPointF(x + w * .2, y + h * .58), QPointF(x + w * .48, y + h * .58),
                   QPointF(x + w * .36, y + h), QPointF(x + w * .82, y + h * .38), QPointF(x + w * .52, y + h * .38)]
            p.drawPolygon(pts)
        elif k in ("gpu", "ram"):
            b = r.adjusted(0, r.height() * .22, 0, -r.height() * .22)
            p.drawRoundedRect(b, 3, 3)
            if k == "gpu":
                p.drawEllipse(QPointF(b.center().x() + 3, b.center().y()), b.height() * .28, b.height() * .28)
                p.drawLine(QPointF(b.left() + 4, b.bottom() + 3), QPointF(b.left() + 4, b.top() - 2))
            else:
                n = 4
                for i in range(n):
                    x = b.left() + 4 + i * (b.width() - 8) / (n - 1)
                    p.drawLine(QPointF(x, b.bottom()), QPointF(x, b.bottom() + 4))
                p.drawRect(b.adjusted(4, 4, -4, -b.height() / 2))


class AreaChart(QWidget):
    """Prompt + generation tokens/s over time: smooth lines with gradient-filled areas, time axis, own scale per series
    (prompt is typically ~20x the generation speed, so the left axis = prompt, right axis = generation)."""

    def __init__(self) -> None:
        super().__init__()
        self.points: list = []
        self.setMinimumHeight(170)

    def set_points(self, pts: list) -> None:
        self.points = list(pts)
        self.update()

    @staticmethod
    def _scale(vals: list) -> float:
        top = max([v for v in vals if v] or [0])
        if not top:
            return 50.0
        step = 10 ** (len(str(int(top))) - 1) if top >= 10 else 1
        return max(step * (int(top / step) + 1), 5)

    def paintEvent(self, _e) -> None:            # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect()).adjusted(42, 24, -42, -22)
        p.setFont(_font(self.font(), 8))
        series = (("Prompt Token/s", "series_a", 1), ("Generierung Token/s", "series_b", 2))
        lx = r.left()                                              # legend on top
        for name, tok, _i in series:
            p.setPen(Qt.NoPen)
            p.setBrush(_c(tok))
            p.drawEllipse(QPointF(lx + 5, 9), 4, 4)
            p.setPen(_c("muted"))
            p.drawText(QRectF(lx + 13, 1, 150, 16), Qt.AlignLeft | Qt.AlignVCenter, name)
            lx += 150
        ymax = {i: self._scale([pt[i] for pt in self.points]) for _n, _t, i in series}
        for i in range(5):
            y = r.bottom() - r.height() * i / 4
            p.setPen(QPen(_c("grid"), 1, Qt.DotLine if i else Qt.SolidLine))
            p.drawLine(QPointF(r.left(), y), QPointF(r.right(), y))
            p.setPen(_c("series_a"))
            p.drawText(QRectF(0, y - 8, 38, 16), Qt.AlignRight | Qt.AlignVCenter, f"{ymax[1] * i / 4:.0f}")
            p.setPen(_c("series_b"))
            p.drawText(QRectF(r.right() + 4, y - 8, 38, 16), Qt.AlignLeft | Qt.AlignVCenter, f"{ymax[2] * i / 4:.0f}")
        if not self.points:
            p.setPen(_c("muted"))
            p.drawText(r, Qt.AlignCenter, "Noch keine Messpunkte - Live-Test starten")
            return
        t0, t1 = self.points[0][0], self.points[-1][0]
        span = max(t1 - t0, 60.0)
        for k in range(5):                                         # time axis
            t = t0 + span * k / 4
            x = r.left() + r.width() * k / 4
            p.setPen(_c("muted"))
            p.drawText(QRectF(x - 30, r.bottom() + 4, 60, 14), Qt.AlignCenter, time.strftime("%H:%M", time.localtime(t)))
        for _name, tok, ci in series:
            pts = [QPointF(r.left() + r.width() * (pt[0] - t0) / span, r.bottom() - r.height() * min(pt[ci], ymax[ci]) / ymax[ci])
                   for pt in self.points if pt[ci] is not None]
            if not pts:
                continue
            line = QPainterPath(pts[0])
            for a, b in zip(pts, pts[1:]):                         # smooth curve through the points
                mx = (a.x() + b.x()) / 2
                line.cubicTo(QPointF(mx, a.y()), QPointF(mx, b.y()), b)
            area = QPainterPath(line)
            area.lineTo(QPointF(pts[-1].x(), r.bottom()))
            area.lineTo(QPointF(pts[0].x(), r.bottom()))
            area.closeSubpath()
            grad = QLinearGradient(0, r.top(), 0, r.bottom())
            grad.setColorAt(0, _c(tok, 150))
            grad.setColorAt(1, _c(tok, 15))
            p.setPen(Qt.NoPen)
            p.setBrush(QBrush(grad))
            p.drawPath(area)
            p.setPen(QPen(_c(tok), 2))
            p.setBrush(Qt.NoBrush)
            p.drawPath(line)
            if len(pts) == 1:
                p.setBrush(_c(tok))
                p.drawEllipse(pts[0], 3, 3)


class Gauge(QWidget):
    """Ring gauge with a green -> blue gradient arc and the value in the middle."""

    def __init__(self, caption: str) -> None:
        super().__init__()
        self.caption, self.pct, self.text = caption, None, "-"
        self.setMinimumSize(110, 120)

    def set(self, pct: float | None, text: str) -> None:
        self.pct, self.text = pct, text
        self.update()

    def paintEvent(self, _e) -> None:            # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        d = min(self.width() - 8, self.height() - 26)
        box = QRectF((self.width() - d) / 2, 4, d, d).adjusted(7, 7, -7, -7)
        p.setPen(QPen(_c("track"), 11, Qt.SolidLine, Qt.FlatCap))
        p.drawEllipse(box)
        if self.pct is not None:
            v = max(0.0, min(self.pct, 100.0))
            grad = QConicalGradient(box.center(), 90)
            if v >= 90:
                grad.setColorAt(0, _c("bad"))
                grad.setColorAt(1, _c("bad"))
            else:
                grad.setColorAt(0.0, _c("series_b"))
                grad.setColorAt(0.5, _c("series_b"))
                grad.setColorAt(1.0, _c("series_a"))
            p.setPen(QPen(QBrush(grad), 11, Qt.SolidLine, Qt.RoundCap))
            p.drawArc(box, 90 * 16, int(-360 * 16 * v / 100))
        p.setPen(_c("title"))
        p.setFont(_font(self.font(), 15, True))
        p.drawText(box, Qt.AlignCenter, self.text)
        p.setPen(_c("muted"))
        p.setFont(_font(self.font(), 8.5))
        p.drawText(QRectF(0, d + 6, self.width(), 16), Qt.AlignCenter, self.caption)


class HBars(QWidget):
    """Horizontal bars: label | bar | value."""

    def __init__(self) -> None:
        super().__init__()
        self.items: list[tuple[str, float | None, str, str]] = []
        self.setMinimumHeight(120)

    def set_items(self, items: list) -> None:
        self.items = list(items)
        self.update()

    def paintEvent(self, _e) -> None:            # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        if not self.items:
            return
        row_h = self.height() / len(self.items)
        mx = max([v for _l, v, _t, _c2 in self.items if v] or [1.0])
        lab_w, val_w = 120, 64
        p.setFont(_font(self.font(), 9))
        for i, (label, v, text, tok) in enumerate(self.items):
            y = i * row_h
            p.setPen(_c("text"))
            p.drawText(QRectF(0, y, lab_w, row_h), Qt.AlignLeft | Qt.AlignVCenter, label)
            bar = QRectF(lab_w, y + row_h / 2 - 5, max(self.width() - lab_w - val_w, 10), 10)
            p.setPen(Qt.NoPen)
            p.setBrush(_c("track"))
            p.drawRoundedRect(bar, 5, 5)
            if v:
                p.setBrush(_c(tok))
                p.drawRoundedRect(QRectF(bar.left(), bar.top(), max(bar.width() * v / mx, 8), bar.height()), 5, 5)
            p.setPen(_c("title"))
            p.drawText(QRectF(self.width() - val_w, y, val_w, row_h), Qt.AlignRight | Qt.AlignVCenter, text)


class VBars(QWidget):
    """Vertical bars: finished requests per minute (green), with a light grid."""

    def __init__(self) -> None:
        super().__init__()
        self.counts: list[int] = []
        self.setMinimumHeight(120)

    def set_counts(self, counts: list[int]) -> None:
        self.counts = list(counts)
        self.update()

    def paintEvent(self, _e) -> None:            # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        n = len(self.counts)
        if not n:
            return
        r = QRectF(self.rect()).adjusted(24, 6, -4, -18)
        mx = max(max(self.counts), 1)
        top = max(mx, 4)
        p.setFont(_font(self.font(), 7.5))
        for i in range(3):
            y = r.bottom() - r.height() * i / 2
            p.setPen(QPen(_c("grid"), 1, Qt.DotLine if i else Qt.SolidLine))
            p.drawLine(QPointF(r.left(), y), QPointF(r.right(), y))
            p.setPen(_c("muted"))
            p.drawText(QRectF(0, y - 7, 20, 14), Qt.AlignRight | Qt.AlignVCenter, f"{top * i / 2:.0f}")
        gap = 4
        bw = (r.width() - gap * (n - 1)) / n
        for i, c in enumerate(self.counts):
            x = r.left() + i * (bw + gap)
            if c:
                h = max(r.height() * c / top, 3)
                grad = QLinearGradient(0, r.bottom() - h, 0, r.bottom())
                grad.setColorAt(0, _c("series_b"))
                grad.setColorAt(1, _c("series_b", 120))
                p.setPen(Qt.NoPen)
                p.setBrush(QBrush(grad))
                p.drawRoundedRect(QRectF(x, r.bottom() - h, bw, h), 2, 2)
            if i in (0, n - 1) or (n - 1 - i) % 4 == 0:
                p.setPen(_c("muted"))
                p.drawText(QRectF(x - 12, r.bottom() + 3, bw + 24, 13), Qt.AlignCenter, "jetzt" if i == n - 1 else f"-{n - 1 - i}m")


class StackBar(QWidget):
    """Context usage: one bar split into prompt (blue) and generated (green) tokens."""

    def __init__(self) -> None:
        super().__init__()
        self.parts: tuple[float, float] = (0.0, 0.0)
        self.setMinimumHeight(16)
        self.setMaximumHeight(18)

    def set(self, prompt_pct: float, gen_pct: float) -> None:
        self.parts = (max(prompt_pct, 0.0), max(gen_pct, 0.0))
        self.update()

    def paintEvent(self, _e) -> None:            # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect()).adjusted(0, 2, 0, -2)
        p.setPen(Qt.NoPen)
        p.setBrush(_c("track"))
        p.drawRoundedRect(r, 6, 6)
        a, b = self.parts
        tot = min(a + b, 100.0)
        if tot <= 0:
            return
        clip = QPainterPath()
        clip.addRoundedRect(r, 6, 6)
        p.setClipPath(clip)
        wa = r.width() * min(a, 100.0) / 100
        p.setBrush(_c("series_a"))
        p.drawRect(QRectF(r.left(), r.top(), wa, r.height()))
        p.setBrush(_c("series_b"))
        p.drawRect(QRectF(r.left() + wa, r.top(), r.width() * tot / 100 - wa, r.height()))


class UsageBar(StackBar):
    """Single-value bar (kept for callers that set one percentage)."""

    def set(self, pct: float | None, _unused: float = 0.0) -> None:            # noqa: D102
        super().set(pct or 0.0, 0.0)


# ------------------------------------------------------------------ cards
def _title(text: str) -> QLabel:
    t = QLabel(text)
    t.setStyleSheet(f"font-size:11pt; font-weight:700; color:{theme.col('title')}; margin:0;")
    t.setObjectName("CardTitle")
    return t


def _card(title: str, body: QWidget | None = None, tip: str = "", right: QWidget | None = None) -> tuple[QFrame, QVBoxLayout]:
    f = QFrame()
    f.setObjectName("Card")
    v = QVBoxLayout(f)
    v.setContentsMargins(14, 12, 14, 12)
    v.setSpacing(8)
    hl = QHBoxLayout()
    hl.addWidget(_title(title))
    hl.addStretch(1)
    if right is not None:
        hl.addWidget(right)
    v.addLayout(hl)
    if tip:
        f.setToolTip(tip)
    if body is not None:
        v.addWidget(body, 1)
    return f, v


class KV(QWidget):
    """Key/value list: muted key on the left, bold value on the right (optional status dot)."""

    def __init__(self, keys: list[str]) -> None:
        super().__init__()
        g = QGridLayout(self)
        g.setContentsMargins(0, 0, 0, 0)
        g.setHorizontalSpacing(14)
        g.setVerticalSpacing(5)
        self.vals: dict[str, QLabel] = {}
        for i, k in enumerate(keys):
            a = QLabel(k)
            a.setObjectName("Muted")
            b = QLabel("-")
            b.setTextInteractionFlags(Qt.TextSelectableByMouse)
            b.setStyleSheet("font-weight:600;")
            g.addWidget(a, i, 0)
            g.addWidget(b, i, 1)
            self.vals[k] = b
        g.setColumnStretch(1, 1)
        g.setRowStretch(len(keys), 1)

    def set(self, key: str, value: str, dot: str | None = None) -> None:
        lab = self.vals[key]
        v = value or "-"
        if dot:
            lab.setText(f"<span style='color:{theme.col(dot)}'>●</span>&nbsp;{v}")
        else:
            lab.setText(v)
        lab.setToolTip(value or "")


class Kpi(QFrame):
    """KPI card: caption, large value, sub line, line icon on the right."""

    def __init__(self, title: str, icon: str) -> None:
        super().__init__()
        self.setObjectName("Card")
        h = QHBoxLayout(self)
        h.setContentsMargins(14, 10, 12, 10)
        v = QVBoxLayout()
        v.setSpacing(3)
        t = QLabel(title)
        t.setStyleSheet("font-weight:600;")
        self.value = QLabel("-")
        self.sub = QLabel("")
        self.sub.setObjectName("Muted")
        for w in (t, self.value, self.sub):
            w.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
            v.addWidget(w)
        h.addLayout(v, 1)
        self.icon = Icon(icon, "series_a" if icon in ("gpu", "ram") else "muted")
        h.addWidget(self.icon, 0, Qt.AlignTop)
        self.set("-")

    def set(self, value: str, sub: str = "", tone: str = "") -> None:
        col = {"ok": theme.col("ok"), "bad": theme.col("bad")}.get(tone, theme.col("title"))
        dot = f"<span style='color:{col}; font-size:15pt'>●</span>&nbsp;" if tone in ("ok", "bad") else ""
        self.value.setText(f"{dot}<span style='font-size:16pt; font-weight:700; color:{col}'>{value}</span>")
        self.value.setToolTip(value)
        self.sub.setText(sub)
        self.sub.setToolTip(sub)


# ------------------------------------------------------------------ the dashboard
class LlmDashboard(QScrollArea):
    """Signals: action(kind, model) with kind in load / unload / test / remove."""
    action = Signal(str, str)
    WIDE = 1150                       # from this width the model list moves to its own column on the right

    def __init__(self) -> None:
        super().__init__()
        self.setWidgetResizable(True)
        self.setFrameShape(QScrollArea.NoFrame)
        inner = QWidget()
        inner.setObjectName("Page")
        self.setWidget(inner)
        self.g = g = QGridLayout(inner)
        g.setContentsMargins(0, 4, 4, 4)
        g.setHorizontalSpacing(10)
        g.setVerticalSpacing(10)

        # KPI row
        self.k_server, self.k_model, self.k_req = Kpi("Server-Status", "server"), Kpi("Aktives Modell", "model"), Kpi("Requests", "chat")
        self.k_tps, self.k_gpu, self.k_ram = Kpi("Token-Tempo", "bolt"), Kpi("GPU-Auslastung", "gpu"), Kpi("RAM-Nutzung", "ram")
        self.kpis = (self.k_server, self.k_model, self.k_req, self.k_tps, self.k_gpu, self.k_ram)

        # model information
        self.info = KV(["Aktives Modell", "Modellstatus", "Modellformat", "Quantisierung", "Modellgröße",
                        "Prozessorbetrieb", "GPU-Offloading", "Kontextgröße"])
        self.c_info, _ = _card("Modell-Information", self.info)

        # token chart + summary column
        self.chart = AreaChart()
        cw = QWidget()
        ch = QHBoxLayout(cw)
        ch.setContentsMargins(0, 0, 0, 0)
        ch.addWidget(self.chart, 1)
        summ = QVBoxLayout()
        summ.setSpacing(2)
        self.s_prompt, self.s_gen, self.s_avg = QLabel("-"), QLabel("-"), QLabel("-")
        for cap, lab, tok in (("Prompt", self.s_prompt, "series_a"), ("Generierung", self.s_gen, "series_b"), ("Ø Generierung", self.s_avg, "title")):
            c = QLabel(cap)
            c.setObjectName("Muted")
            lab.setStyleSheet(f"font-size:14pt; font-weight:700; color:{theme.col(tok)};")
            summ.addWidget(c)
            summ.addWidget(lab)
            summ.addSpacing(6)
        summ.addStretch(1)
        ch.addLayout(summ)
        self.c_chart, _ = _card("Token-Leistung", cw,
                                "llama.cpp: aus /metrics abgeleitet (Server-Zähler, alle Anfragen). Ollama: Anfragen der Tuning-Seite.")

        # GPU / memory / CPU cards: gauge + key/value list
        def gauge_card(title, caption, keys):
            w = QWidget()
            h = QHBoxLayout(w)
            h.setContentsMargins(0, 0, 0, 0)
            gauge = Gauge(caption)
            kv = KV(keys)
            h.addWidget(gauge)
            h.addWidget(kv, 1)
            f, _ = _card(title, w)
            return f, gauge, kv
        self.c_gpu, self.g_gpu, self.kv_gpu = gauge_card("GPU", "GPU-Auslastung", ["VRAM", "Temperatur", "Leistung", "GPU-Layer"])
        self.c_mem, self.g_ram, self.kv_ram = gauge_card("Arbeitsspeicher", "RAM-Nutzung", ["Belegt", "Gesamt", "Server-Prozess"])
        self.c_cpu, self.g_cpu, self.kv_cpu = gauge_card("CPU", "CPU-Auslastung", ["Gesamt", "Server-Prozess"])

        # context usage
        cw2 = QWidget()
        cv = QVBoxLayout(cw2)
        cv.setContentsMargins(0, 0, 0, 0)
        self.ctx_bar = StackBar()
        self.ctx_txt = QLabel("-")
        self.ctx_txt.setStyleSheet("font-weight:600;")
        self.ctx_kv = KV(["Prompt-Token", "Ausgabe-Token", "Frei"])
        self.ctx_note = QLabel("")
        self.ctx_note.setObjectName("Muted")
        self.ctx_note.setWordWrap(True)
        for w in (self.ctx_bar, self.ctx_txt, self.ctx_kv, self.ctx_note):
            cv.addWidget(w)
        self.c_ctx, _ = _card("Kontext-Belegung", cw2, "Belegung der letzten Anfrage (Prompt + Ausgabe) im Verhältnis zur Kontextgröße.")

        self.times = HBars()
        self.c_times, _ = _card("Antwortzeiten (letzte Anfrage)", self.times)

        self.reqs = VBars()
        self.req_head = QLabel("")
        self.req_head.setTextFormat(Qt.RichText)
        self.c_reqs, _ = _card("Requests (letzte 12 Min.)", self.reqs, "Nur Anfragen der Tuning-Seite (Ollama zählt fremde Anfragen nicht).",
                               right=self.req_head)

        # model list + quick actions
        self.models = make_table(["Name", "Größe", "Quant", "Status"], 0)
        self.models.setSortingEnabled(False)
        self.models.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.models.setSelectionMode(QAbstractItemView.SingleSelection)
        self.models.verticalHeader().setDefaultSectionSize(30)
        self.models.setMinimumHeight(180)
        self._model_sig: tuple = ()
        self._can_remove = False
        self.models_note = QLabel("")
        self.models_note.setObjectName("Muted")
        self.models_note.setWordWrap(True)
        qa = QHBoxLayout()
        self.b_load = button("Laden", "Go", lambda: self._act("load"), "Modell laden bzw. Dienst mit diesem Modell starten")
        self.b_unload = button("Entladen", "Danger", lambda: self._act("unload"), "Modell aus dem Speicher nehmen bzw. Dienst stoppen")
        self.b_test = button("Test", "", lambda: self._act("test"), "Live-Test mit diesem Modell")
        self.b_remove = button("Entfernen", "", lambda: self._act("remove"), "Modell aus Ollama löschen (mit Rückfrage)")
        for b in (self.b_load, self.b_unload, self.b_test, self.b_remove):
            qa.addWidget(b)
        qa_lab = _title("Schnellaktionen")
        mw = QWidget()
        mv = QVBoxLayout(mw)
        mv.setContentsMargins(0, 0, 0, 0)
        mv.addWidget(self.models, 1)
        mv.addWidget(self.models_note)
        mv.addSpacing(6)
        mv.addWidget(qa_lab)
        mv.addLayout(qa)
        self.c_models, _ = _card("Modell-Liste", mw)
        self.models.itemSelectionChanged.connect(self._sel_changed)
        self._wide = None
        self._place()

    # ------------------------------------------------------------------ responsive placement
    def _place(self) -> None:
        wide = self.viewport().width() >= self.WIDE
        if wide == self._wide:
            return
        self._wide = wide
        g = self.g
        for w in (*self.kpis, self.c_info, self.c_chart, self.c_gpu, self.c_mem, self.c_cpu, self.c_ctx, self.c_times,
                  self.c_reqs, self.c_models):
            g.removeWidget(w)
        for c in range(12):
            g.setColumnStretch(c, 0)
        for r in range(8):
            g.setRowStretch(r, 0)
        if wide:   # 9 columns main area + 3 columns model list (like the reference layout)
            for c in range(12):
                g.setColumnStretch(c, 1)
            for i, k in enumerate(self.kpis):
                g.addWidget(k, 0, i * 2, 1, 2)
            g.addWidget(self.c_info, 1, 0, 1, 4)
            g.addWidget(self.c_chart, 1, 4, 1, 5)
            g.addWidget(self.c_gpu, 2, 0, 1, 3)
            g.addWidget(self.c_mem, 2, 3, 1, 3)
            g.addWidget(self.c_cpu, 2, 6, 1, 3)
            g.addWidget(self.c_models, 1, 9, 2, 3)
            g.addWidget(self.c_ctx, 3, 0, 1, 4)
            g.addWidget(self.c_times, 3, 4, 1, 4)
            g.addWidget(self.c_reqs, 3, 8, 1, 4)
            g.setRowStretch(4, 1)
        else:      # narrow (left column of the Tuning page): KPIs 2 per row, every card full width
            for c in range(2):
                g.setColumnStretch(c, 1)
            for i, k in enumerate(self.kpis):
                g.addWidget(k, i // 2, i % 2)
            for r, w in enumerate((self.c_info, self.c_chart, self.c_gpu, self.c_mem, self.c_cpu, self.c_ctx, self.c_times,
                                   self.c_reqs, self.c_models), start=3):
                g.addWidget(w, r, 0, 1, 2)

    def resizeEvent(self, e) -> None:            # noqa: N802
        super().resizeEvent(e)
        self._place()

    # ------------------------------------------------------------------ data in
    def update_view(self, rows: dict, snap: dict, sample: dict, stats, hist: TpsHistory, ctx_max: int = 0) -> None:
        snap, sample = snap or {}, sample or {}
        gpu = sample.get("gpu") or {}
        proc = snap.get("proc") or {}
        last = stats.last or {}
        ok = bool(last) and not last.get("failed")
        g = lambda k: rows.get(k, "-") or "-"          # noqa: E731
        status = g("Serverstatus")
        self.k_server.set(status, f"Laufzeit {g('Serverlaufzeit')}" if status != "Offline" else g("Serveradresse"),
                          "bad" if status == "Offline" else "ok" if status == "Online" else "")
        self.k_model.set(g("Aktives Modell"), g("Modellstatus"))
        err_pct = f" ({100 * stats.errors / stats.total:.0f} %)" if stats.total else ""
        self.k_req.set(str(stats.total), f"{stats.errors} Fehler{err_pct} · aktiv {g('Aktive Requests')}", "")
        gens = [pt[2] for pt in hist.points if pt[2]]
        pros = [pt[1] for pt in hist.points if pt[1]]
        cur_gen = last["gen_tps"] if ok and last.get("gen_tps") else (gens[-1] if gens else None)
        self.k_tps.set(f"{cur_gen:.1f} tok/s" if cur_gen else "-", f"TTFT {g('Antwortbeginn (TTFT)')}")
        self.k_gpu.set(g("GPU-Auslastung"), g("VRAM-Nutzung").split(" (")[0])
        self.k_ram.set(f"{sample['ram_pct']:.0f} %" if "ram_pct" in sample else "-", g("RAM-Nutzung").split(" (")[0])

        for k in self.info.vals:
            v = g(k)
            self.info.set(k, v, "ok" if k == "Modellstatus" and v in ("Geladen", "Aktiv") else None)

        self.chart.set_points(hist.points)
        self.s_prompt.setText(f"{pros[-1]:.0f} tok/s" if pros else "-")
        self.s_gen.setText(f"{gens[-1]:.1f} tok/s" if gens else "-")
        self.s_avg.setText(f"{sum(gens) / len(gens):.1f} tok/s" if gens else "-")

        self.g_gpu.set(float(gpu["util"]) if gpu.get("util") is not None else None, g("GPU-Auslastung"))
        self.kv_gpu.set("VRAM", g("VRAM-Nutzung").split(" (")[0])
        self.kv_gpu.set("Temperatur", g("GPU-Temperatur"))
        self.kv_gpu.set("Leistung", g("GPU-Leistungsaufnahme"))
        self.kv_gpu.set("GPU-Layer", g("GPU-Offloading"))
        if "ram_pct" in sample:
            self.g_ram.set(float(sample["ram_pct"]), f"{sample['ram_pct']:.0f} %")
        else:
            self.g_ram.set(None, "-")
        ram = g("RAM-Nutzung").split(" (")[0]
        self.kv_ram.set("Belegt", ram.split(" / ")[0] + (" GB" if " / " in ram else ""))
        self.kv_ram.set("Gesamt", ram.split(" / ")[1] if " / " in ram else "-")
        self.kv_ram.set("Server-Prozess", g("Prozessspeicher (Server)"))
        cpu_txt = g("CPU-Auslastung")
        try:
            cpu_v = float(cpu_txt.replace("%", "").strip())
        except ValueError:
            cpu_v = None
        self.g_cpu.set(cpu_v, cpu_txt)
        self.kv_cpu.set("Gesamt", cpu_txt)
        self.kv_cpu.set("Server-Prozess", f"{float(proc.get('cpu_pct', 0)):.0f} %" if proc else g("Prozessorauslastung (Server)"))

        pt = (last.get("prompt_tokens", 0) or 0) if ok else 0
        gt = (last.get("gen_tokens", 0) or 0) if ok else 0
        fmt = lambda n: f"{n:,}".replace(",", ".")          # noqa: E731
        if ctx_max:
            self.ctx_bar.set(100 * pt / ctx_max, 100 * gt / ctx_max)
            self.ctx_txt.setText(f"{fmt(pt + gt)} / {fmt(ctx_max)} Token ({100 * (pt + gt) / ctx_max:.0f} %)")
            self.ctx_kv.set("Prompt-Token", fmt(pt) if ok else "-", "series_a")
            self.ctx_kv.set("Ausgabe-Token", fmt(gt) if ok else "-", "series_b")
            self.ctx_kv.set("Frei", fmt(max(ctx_max - pt - gt, 0)), "track")
            self.ctx_note.setText("" if ok else "Noch keine Anfrage - Belegung wird nach dem ersten Test angezeigt.")
        else:
            self.ctx_bar.set(0, 0)
            self.ctx_txt.setText("-")
            for k in ("Prompt-Token", "Ausgabe-Token", "Frei"):
                self.ctx_kv.set(k, "-")
            self.ctx_note.setText("Kontextgröße vom Server nicht bekannt.")

        def sec(key):
            return last.get(key) if ok else None
        self.times.set_items([
            ("Antwortbeginn", sec("ttft"), g("Antwortbeginn (TTFT)"), "series_a"),
            ("Prompt", sec("prompt_s"), g("Prompt-Dauer"), "series_a"),
            ("Generierung", sec("gen_s"), g("Generierungsdauer"), "series_b"),
            ("Gesamt", sec("total_s"), g("Gesamtdauer"), "muted"),
        ])
        self.reqs.set_counts(request_buckets(stats.events))
        self.req_head.setText(f"Gesamt: <b>{stats.total}</b> &nbsp; <span style='color:{theme.col('bad') if stats.errors else theme.col('muted')}'>"
                              f"Fehler: {stats.errors}</span>")

    # ------------------------------------------------------------------ model list
    def _selected(self) -> dict | None:
        r = self.models.currentRow()
        return self._items[r] if 0 <= r < len(getattr(self, "_items", [])) else None

    def _sel_changed(self) -> None:
        m = self._selected()
        active = bool(m) and m["status"] in ("Aktiv", "Geladen")
        self.b_load.setEnabled(bool(m) and not active)
        self.b_unload.setEnabled(active)
        self.b_test.setEnabled(bool(m))
        self.b_remove.setEnabled(bool(m) and self._can_remove)

    def _act(self, kind: str) -> None:
        m = self._selected()
        if m:
            self.action.emit(kind, m["name"])

    def set_models(self, items: list[dict], can_remove: bool, note: str = "") -> None:
        """items: [{name, size, status}] with status Aktiv / Geladen / Verfügbar."""
        sig = (tuple((m["name"], m["size"], m["status"]) for m in items), can_remove)
        self.models_note.setText(note)
        self.models_note.setVisible(bool(note))
        if sig == self._model_sig:
            return
        self._model_sig = sig
        self._items, self._can_remove = list(items), can_remove
        keep = (self._selected() or {}).get("name")
        t: QTableWidget = self.models
        t.setRowCount(len(items))
        for r, m in enumerate(items):
            active = m["status"] in ("Aktiv", "Geladen")
            cells = (m["name"].split("/")[-1], m["size"], parse_quant(m["name"]) or "-", ("● " if active else "○ ") + m["status"])
            for c, txt in enumerate(cells):
                it = t.item(r, c)
                if it is None:
                    it = QTableWidgetItem()
                    t.setItem(r, c, it)
                it.setText(txt)
                it.setToolTip(m["name"])
                if c == 3:
                    it.setForeground(QColor(theme.col("ok") if active else theme.col("muted")))
            if m["name"] == keep or (keep is None and active):
                t.selectRow(r)
        t.resizeColumnsToContents()
        t.horizontalHeader().setStretchLastSection(True)
        self._sel_changed()
