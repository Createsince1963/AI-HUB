"""Top processes: reusable table (dashboard card) and a free-standing, resizable window."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QLabel, QMessageBox, QTableWidgetItem, QVBoxLayout, QWidget

from core.system import fmt_size
from .context import Ctx
from .widgets import NumItem, button, make_table

try:
    import psutil
except ImportError:
    psutil = None


def snapshot_lines(procs: list[dict], n: int = 10) -> list[str]:
    """Text block for the output log."""
    lines = [f"TOP PROCESSES (CPU / RAM) - {min(n, len(procs))} of {len(procs)}"]
    for i, p in enumerate(procs[:n], 1):
        lines.append(f"  {i:>2}. {p['name'][:30]:<30} PID {p['pid']:>6}   CPU {p['cpu']:5.1f} %   RAM {fmt_size(p['mem']):>9}")
    return lines


class ProcessTable(QWidget):
    """Sortable process table with 'End process' button. Keeps the selection across refreshes."""

    def __init__(self, ctx: Ctx, limit: int | None = None, show_kill: bool = True):
        super().__init__()
        self.ctx, self.limit = ctx, limit
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        self.table = make_table(["Process", "PID", "CPU %", "RAM"], 0)
        self.table.horizontalHeader().setSortIndicator(2, Qt.DescendingOrder)
        self.table.horizontalHeader().setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        lay.addWidget(self.table, 1)
        if show_kill:
            bar = QHBoxLayout()
            bar.addStretch(1)
            bar.addWidget(button("End selected process", "Danger", self.kill_selected))
            lay.addLayout(bar)

    def fill(self, procs: list[dict]) -> None:
        rows = procs[: self.limit] if self.limit else procs
        sel = self.selected_pid()
        t = self.table
        col, order = t.horizontalHeader().sortIndicatorSection(), t.horizontalHeader().sortIndicatorOrder()
        t.setSortingEnabled(False)
        t.setRowCount(len(rows))
        for i, p in enumerate(rows):
            t.setItem(i, 0, QTableWidgetItem(p["name"]))
            t.setItem(i, 1, NumItem(str(p["pid"]), p["pid"]))
            cpu = NumItem(f"{p['cpu']:.1f}", p["cpu"])
            if p["cpu"] >= 50:
                cpu.setForeground(Qt.red)
            elif p["cpu"] >= 20:
                cpu.setForeground(Qt.darkYellow)
            t.setItem(i, 2, cpu)
            t.setItem(i, 3, NumItem(fmt_size(p["mem"]), p["mem"]))
        t.setSortingEnabled(True)
        t.sortItems(col, order)
        if sel is not None:
            for r in range(t.rowCount()):
                if t.item(r, 1).text() == str(sel):
                    t.selectRow(r)
                    break

    def selected_pid(self) -> int | None:
        sm = self.table.selectionModel()
        rows = sm.selectedRows() if sm else []
        it = self.table.item(rows[0].row(), 1) if rows else None
        return int(it.text()) if it else None

    def kill_selected(self) -> None:
        pid = self.selected_pid()
        if pid is None or psutil is None:
            return
        row = self.table.selectionModel().selectedRows()[0].row()
        name = self.table.item(row, 0).text()
        if QMessageBox.question(self, "End process", f"End {name} (PID {pid})?") != QMessageBox.Yes:
            return
        try:
            psutil.Process(pid).terminate()
            self.ctx.log(f"Process ended: {name} ({pid})")
        except Exception as exc:        # noqa: BLE001
            self.ctx.log(f"Could not end {name} ({pid}): {exc}")


class TopProcessesWindow(QWidget):
    """Independent top-level window (own title bar, minimize / maximize / resize). Closing only hides it."""

    def __init__(self, ctx: Ctx, parent: QWidget | None = None):
        super().__init__(parent, Qt.Window)
        self.setWindowTitle("Top processes")
        self.setMinimumSize(320, 200)
        self.resize(760, 520)
        self.setObjectName("Page")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self._last: list[dict] = []
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 10, 12, 10)
        head = QHBoxLayout()
        title = QLabel("Top processes (CPU / RAM)")
        title.setObjectName("Section")
        head.addWidget(title)
        head.addStretch(1)
        head.addWidget(button("Log snapshot", "", lambda: self._log(ctx)))
        lay.addLayout(head)
        self.tbl = ProcessTable(ctx)
        lay.addWidget(self.tbl, 1)
        ctx.monitor.sample.connect(self._on_sample)

    def _log(self, ctx: Ctx) -> None:
        for line in snapshot_lines(self._last, 15):
            ctx.log(line)

    def _on_sample(self, d: dict) -> None:
        if "procs" in d:
            self._last = d["procs"]
            if self.isVisible():
                self.tbl.fill(self._last)

    def showEvent(self, e) -> None:            # noqa: N802
        self.tbl.fill(self._last)
        super().showEvent(e)
