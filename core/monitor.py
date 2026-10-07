"""Real-time system monitor: CPU (total + per core), RAM, GPU/VRAM, disk + network throughput, top processes.

Sampling runs in its own thread so the GUI never blocks (nvidia-smi takes ~100 ms)."""
from __future__ import annotations

import time

from PySide6.QtCore import QThread, Signal

from .gpus import list_gpus
from .system import disk_usage, gpu_info

try:
    import psutil
except ImportError:          # runtime without psutil -> monitor shows "psutil missing"
    psutil = None


class Monitor(QThread):
    sample = Signal(dict)

    def __init__(self, interval: float = 1.0, root=None):
        super().__init__()
        self.interval = interval
        self.root = root                 # drive whose free space is reported (queried here, never in the UI thread)
        self._stop = False
        self.watch_names = ("ollama", "comfyui", "python", "node", "claude")

    def stop(self) -> None:
        self._stop = True
        self.wait(3000)

    def run(self) -> None:
        if psutil is None:
            self.sample.emit({"error": "psutil not installed - run Setup (Runtime) to install it"})
            return
        psutil.cpu_percent(percpu=True)
        last_io, last_net, last_t = psutil.disk_io_counters(), psutil.net_io_counters(), time.monotonic()
        procs: dict[int, psutil.Process] = {}
        tick = 0
        top: list[dict] = []
        disk_free = None
        gpus: list[dict] = []
        while not self._stop:
            time.sleep(self.interval)
            now = time.monotonic()
            dt = max(now - last_t, 1e-3)
            io, net = psutil.disk_io_counters(), psutil.net_io_counters()
            vm, sw = psutil.virtual_memory(), psutil.swap_memory()
            cores = psutil.cpu_percent(percpu=True)
            freq = psutil.cpu_freq()
            d = {
                "cpu": sum(cores) / max(len(cores), 1), "cores": cores,
                "cpu_freq": freq.current if freq else 0,
                "ram_used": vm.used, "ram_total": vm.total, "ram_pct": vm.percent,
                "swap_used": sw.used, "swap_total": sw.total,
                "disk_read": (io.read_bytes - last_io.read_bytes) / dt if io and last_io else 0,
                "disk_write": (io.write_bytes - last_io.write_bytes) / dt if io and last_io else 0,
                "net_down": (net.bytes_recv - last_net.bytes_recv) / dt,
                "net_up": (net.bytes_sent - last_net.bytes_sent) / dt,
                "gpu": gpu_info(),
            }
            last_io, last_net, last_t = io, net, now
            if tick % 3 == 0:            # process list every 3 s
                top = self._top_processes(procs)
            d["procs"] = top
            if self.root is not None and tick % 10 == 0:
                u = disk_usage(self.root)
                disk_free = u[2] if u else None
            d["disk_free"] = disk_free
            if tick % 30 == 0:           # adapter list / driver: slow WMI query, rarely changes
                gpus = list_gpus()
            d["gpus"] = gpus
            tick += 1
            self.sample.emit(d)

    def _top_processes(self, cache: dict) -> list[dict]:
        rows = []
        for p in psutil.process_iter(["pid", "name"]):
            try:
                pr = cache.setdefault(p.pid, p)
                cpu = pr.cpu_percent(None)
                mem = pr.memory_info().rss
                rows.append({"pid": p.pid, "name": p.info["name"] or "?", "cpu": cpu, "mem": mem})
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                cache.pop(p.pid, None)
        ncpu = psutil.cpu_count() or 1
        for r in rows:
            r["cpu"] = r["cpu"] / ncpu
        rows.sort(key=lambda r: (r["cpu"], r["mem"]), reverse=True)
        return rows[:15]
