"""Real-time monitor page: per-core CPU, RAM, GPU, VRAM, temperature, power, disk/net throughput, top processes."""
from __future__ import annotations

from PySide6.QtWidgets import (QComboBox, QGridLayout, QHBoxLayout, QLabel, QMessageBox, QTableWidgetItem, QVBoxLayout,
                               QWidget)

from core.system import fmt_size
from .context import Ctx
from .theme import ACCENT
from .widgets import CoreBars, Meter, NumItem, SparkChart, button, card, make_table, page_header, section

try:
    import psutil
except ImportError:
    psutil = None


class MonitorPage(QWidget):
    def __init__(self, ctx: Ctx):
        super().__init__()
        self.setObjectName("Page")
        self.ctx = ctx
        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 14, 18, 10)
        head = QHBoxLayout()
        head.addWidget(page_header("System monitor", "Real-time CPU cores, memory, GPU and VRAM"), 1)
        head.addWidget(QLabel("Refresh"))
        self.interval = QComboBox()
        for label, v in (("0.5 s", 0.5), ("1 s", 1.0), ("2 s", 2.0), ("5 s", 5.0)):
            self.interval.addItem(label, v)
        self.interval.setCurrentIndex(1)
        self.interval.currentIndexChanged.connect(lambda: setattr(ctx.monitor, "interval", self.interval.currentData()))
        head.addWidget(self.interval)
        outer.addLayout(head)

        g = QGridLayout()
        g.setSpacing(10)
        self.cpu = SparkChart("CPU total", ACCENT)
        self.ram = SparkChart("RAM", "#7a5af8")
        self.gpu = SparkChart("GPU load", "#e8590c")
        self.vram = SparkChart("VRAM", "#c2255c")
        self.temp = SparkChart("GPU temp", "#d9480f", maximum=100, unit=" °C")
        self.power = SparkChart("GPU power", "#f59f00", maximum=100, unit=" W", auto_scale=True)
        self.disk = SparkChart("Disk read+write", "#2A9D8F", unit=" MB/s", auto_scale=True)
        self.net = SparkChart("Network down+up", "#1c7ed6", unit=" MB/s", auto_scale=True)
        charts = (self.cpu, self.ram, self.gpu, self.vram, self.temp, self.power, self.disk, self.net)
        for i, c in enumerate(charts):
            g.addWidget(c, i // 4, i % 4)
            g.setColumnStretch(i % 4, 1)
        outer.addLayout(g)

        mid = QHBoxLayout()
        f, fl = card(12)
        fl.addWidget(section("CPU cores (logical)"))
        self.cores = CoreBars()
        fl.addWidget(self.cores)
        self.freq = QLabel("")
        self.freq.setObjectName("Muted")
        fl.addWidget(self.freq)
        mid.addWidget(f, 3)
        f2, f2l = card(12)
        f2l.addWidget(section("Memory"))
        self.m_ram = Meter("RAM")
        self.m_swap = Meter("Page file")
        self.m_vram = Meter("VRAM")
        for m in (self.m_ram, self.m_swap, self.m_vram):
            f2l.addWidget(m)
        mid.addWidget(f2, 2)
        outer.addLayout(mid)

        outer.addWidget(section("Top processes (CPU / RAM)"))
        self.table = make_table(["Process", "PID", "CPU %", "RAM"], 0)
        outer.addWidget(self.table, 1)
        bar = QHBoxLayout()
        self.gpu_name = QLabel("")
        self.gpu_name.setObjectName("Muted")
        bar.addWidget(self.gpu_name, 1)
        bar.addWidget(button("End selected process", "Danger", self.kill_selected))
        outer.addLayout(bar)
        ctx.monitor.sample.connect(self.on_sample)

    def on_sample(self, d: dict) -> None:
        if "error" in d:
            self.gpu_name.setText(d["error"])
            return
        self.cpu.push(d["cpu"], f"{d['cpu']:.0f}%", f"{len(d['cores'])} threads")
        self.cores.set_cores(d["cores"])
        self.freq.setText(f"Clock {d['cpu_freq'] / 1000:.2f} GHz" if d["cpu_freq"] else "")
        self.ram.push(d["ram_pct"], f"{d['ram_pct']:.0f}%", f"{fmt_size(d['ram_used'])} / {fmt_size(d['ram_total'])}")
        self.m_ram.set(d["ram_pct"], f"{fmt_size(d['ram_used'])} / {fmt_size(d['ram_total'])}")
        sp = 100 * d["swap_used"] / d["swap_total"] if d["swap_total"] else 0
        self.m_swap.set(sp, f"{fmt_size(d['swap_used'])} / {fmt_size(d['swap_total'])}")
        rd, wr = d["disk_read"] / 1024 ** 2, d["disk_write"] / 1024 ** 2
        self.disk.push(rd + wr, f"{rd + wr:.1f} MB/s", f"R {rd:.0f} / W {wr:.0f}")
        dn, up = d["net_down"] / 1024 ** 2, d["net_up"] / 1024 ** 2
        self.net.push(dn + up, f"{dn + up:.2f} MB/s", f"↓ {dn:.1f} / ↑ {up:.1f}")
        g = d.get("gpu")
        if g:
            pct = 100 * g["used_mb"] / max(g["total_mb"], 1)
            self.gpu.push(g["util"], f"{g['util']}%", f"{g['clock']} MHz")
            self.vram.push(pct, f"{g['used_mb'] / 1024:.1f} GB", f"of {g['total_mb'] / 1024:.0f} GB")
            self.m_vram.set(pct, f"{g['used_mb'] / 1024:.1f} / {g['total_mb'] / 1024:.1f} GB")
            self.temp.push(g["temp"], f"{g['temp']} °C")
            self.power.push(g["power"], f"{g['power']:.0f} W", f"limit {g['power_limit']:.0f} W")
            self.gpu_name.setText(g["name"])
        else:
            self.gpu_name.setText("No NVIDIA GPU / nvidia-smi not found")
        self._fill(d.get("procs", []))

    def _fill(self, procs: list[dict]) -> None:
        sel = self._selected_pid()
        t = self.table
        t.setSortingEnabled(False)
        t.setRowCount(len(procs))
        for i, p in enumerate(procs):
            t.setItem(i, 0, QTableWidgetItem(p["name"]))
            t.setItem(i, 1, NumItem(str(p["pid"]), p["pid"]))
            t.setItem(i, 2, NumItem(f"{p['cpu']:.1f}", p["cpu"]))
            t.setItem(i, 3, NumItem(fmt_size(p["mem"]), p["mem"]))
            if p["pid"] == sel:
                t.selectRow(i)
        t.setSortingEnabled(True)

    def _selected_pid(self) -> int | None:
        rows = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        if not rows:
            return None
        it = self.table.item(rows[0].row(), 1)
        return int(it.text()) if it else None

    def kill_selected(self) -> None:
        pid = self._selected_pid()
        if pid is None or psutil is None:
            return
        name = self.table.item(self.table.selectionModel().selectedRows()[0].row(), 0).text()
        if QMessageBox.question(self, "End process", f"End {name} (PID {pid})?") != QMessageBox.Yes:
            return
        try:
            psutil.Process(pid).terminate()
            self.ctx.log(f"Process ended: {name} ({pid})")
        except Exception as exc:        # noqa: BLE001
            self.ctx.log(f"Could not end {name} ({pid}): {exc}")
