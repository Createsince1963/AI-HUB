"""System information helpers (GPU, disk, formatting). Blocking - call from a worker thread."""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

_NOWIN = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


def fmt_size(n: float | int | None) -> str:
    if n is None:
        return "-"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def fmt_num(n: int | None) -> str:
    if n is None:
        return "-"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def gpu_info() -> dict | None:
    """First NVIDIA GPU via nvidia-smi, or None."""
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total,memory.used,utilization.gpu,temperature.gpu,power.draw,power.limit,clocks.sm",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=4, creationflags=_NOWIN).stdout.strip().splitlines()
        if not out:
            return None
        v = [x.strip() for x in out[0].split(",")]

        def num(x: str) -> float:
            try:
                return float(x)
            except ValueError:
                return 0.0
        return {"name": v[0], "total_mb": int(num(v[1])), "used_mb": int(num(v[2])), "util": int(num(v[3])),
                "temp": int(num(v[4])), "power": num(v[5]), "power_limit": num(v[6]), "clock": int(num(v[7]))}
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def disk_usage(path: Path) -> tuple[int, int, int] | None:
    """(total, used, free) of the drive holding *path*."""
    try:
        p = path
        while not p.exists() and p != p.parent:
            p = p.parent
        u = shutil.disk_usage(p)
        return u.total, u.used, u.free
    except OSError:
        return None


def vram_verdict(size_bytes: int | None, vram_gb: float) -> tuple[str, str]:
    """(label, colour) - does a model file of this size fit in VRAM (weights only, rough guide)."""
    if not size_bytes:
        return "", "#5f6b7a"
    gb = size_bytes / 1024 ** 3
    if gb <= vram_gb * 0.80:
        return "fits", "#2e9e4f"
    if gb <= vram_gb * 1.0:
        return "tight", "#c58a00"
    return "too big (offload)", "#d93025"
