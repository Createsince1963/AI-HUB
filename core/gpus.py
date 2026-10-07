"""Graphics adapters: name, driver version, active yes/no.

Windows: all adapters (NVIDIA, Intel/AMD iGPU, ...) from Win32_VideoController via PowerShell/CIM.
NVIDIA adapters are enriched with nvidia-smi (real driver version, VRAM, load).
'active' = the adapter drives a display right now, or (NVIDIA) it is doing work (load > 0 or > 500 MB VRAM in use).
Slow (~1 s) - call from the monitor thread only, and not on every sample."""
from __future__ import annotations

import json
import re
import subprocess
import sys

IS_WIN = sys.platform == "win32"
_NOWIN = 0x08000000 if IS_WIN else 0


def _run(cmd: list[str], timeout: float = 8.0) -> str:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, creationflags=_NOWIN)
        return r.stdout if r.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def _nvidia_smi() -> list[dict]:
    q = "name,driver_version,memory.total,memory.used,utilization.gpu,display_active"
    out = _run(["nvidia-smi", f"--query-gpu={q}", "--format=csv,noheader,nounits"])
    rows = []
    for line in out.strip().splitlines():
        p = [x.strip() for x in line.split(",")]
        if len(p) < 6:
            continue

        def num(v: str) -> float:
            try:
                return float(v)
            except ValueError:
                return 0.0

        rows.append({"name": p[0], "driver": p[1], "vram_mb": num(p[2]), "used_mb": num(p[3]),
                     "util": num(p[4]), "display": p[5].lower().startswith("enabled")})
    return rows


def _wmi_adapters() -> list[dict]:
    if not IS_WIN:
        return []
    ps = ("Get-CimInstance Win32_VideoController | Select-Object Name,DriverVersion,Status,"
          "CurrentHorizontalResolution,AdapterRAM | ConvertTo-Json -Compress")
    out = _run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps], timeout=12)
    try:
        data = json.loads(out) if out.strip() else []
    except ValueError:
        return []
    if isinstance(data, dict):
        data = [data]
    return [d for d in data if isinstance(d, dict) and d.get("Name")]


def _registry_vram() -> dict[str, float]:
    """{adapter description (lower case): dedicated video memory in MB} from the display driver registry keys."""
    if not IS_WIN:
        return {}
    ps = (r"$c='HKLM:\SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}';"
          r"Get-ChildItem $c -ErrorAction SilentlyContinue | Where-Object { $_.PSChildName -match '^\d{4}$' } | ForEach-Object {"
          r"$p=Get-ItemProperty $_.PSPath; $m=$p.'HardwareInformation.qwMemorySize';"
          r"if($m -is [byte[]]){ $m=[BitConverter]::ToUInt64($m,0) } elseif($m -eq $null){ $m=$p.'HardwareInformation.MemorySize'; "
          r"if($m -is [byte[]]){ $m=[BitConverter]::ToUInt32($m,0) } };"
          r"[pscustomobject]@{Desc=$p.DriverDesc;Mem=[double]$m} } | ConvertTo-Json -Compress")
    out = _run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps], timeout=12)
    try:
        data = json.loads(out) if out.strip() else []
    except ValueError:
        return {}
    if isinstance(data, dict):
        data = [data]
    res: dict[str, float] = {}
    for d in data:
        if isinstance(d, dict) and d.get("Desc") and d.get("Mem"):
            res[str(d["Desc"]).lower()] = float(d["Mem"]) / 1024 ** 2
    return res


def classify(name: str) -> str:
    """Display group of an adapter: 'rtx50' | 'm1200' | 'intel' | 'other'."""
    n = name.lower()
    if re.search(r"rtx\s*50\d\d", n):
        return "rtx50"
    if "intel" in n:
        return "intel"
    if "nvidia" in n or "quadro" in n or "m1200" in n:
        return "m1200"            # every other NVIDIA card = the internal Quadro
    return "other"


def _nv_user_version(win_ver: str) -> str:
    """Windows driver version 32.0.15.7602 -> NVIDIA version 576.02."""
    parts = (win_ver or "").split(".")
    if len(parts) == 4 and all(p.isdigit() for p in parts):
        digits = (parts[2] + parts[3]).lstrip("0")[-5:].rjust(5, "0")
        return f"{int(digits[:3])}.{digits[3:]}"
    return win_ver or "?"


def list_gpus() -> list[dict]:
    """[{name, vendor, driver, active, vram_mb|None, status}] - never raises."""
    try:
        smi = _nvidia_smi()
        smi_left = list(smi)
        reg = _registry_vram()
        result: list[dict] = []
        for a in _wmi_adapters():
            name = str(a["Name"])
            shows_display = a.get("CurrentHorizontalResolution") not in (None, 0)
            match = next((x for x in smi_left if x["name"].lower() == name.lower()), None) if "nvidia" in name.lower() else None
            if match is not None:
                s = match
                smi_left.remove(match)
                busy = s["util"] > 0 or s["used_mb"] > 500
                result.append({"name": s["name"], "vendor": "NVIDIA", "driver": s["driver"],
                               "active": bool(s["display"] or shows_display or busy),
                               "vram_mb": s["vram_mb"], "status": str(a.get("Status") or "")})
            else:
                vram = reg.get(name.lower())
                if not vram:                                # partial name match, then WMI AdapterRAM (32 bit, max 4 GB)
                    vram = next((v for k, v in reg.items() if k in name.lower() or name.lower() in k), None)
                if not vram and a.get("AdapterRAM"):
                    vram = float(a["AdapterRAM"]) / 1024 ** 2
                vendor = "Intel" if "intel" in name.lower() else "AMD" if ("amd" in name.lower() or "radeon" in name.lower()) else \
                    "Microsoft" if "microsoft" in name.lower() else ""
                result.append({"name": name, "vendor": vendor, "driver": str(a.get("DriverVersion") or "?"),
                               "active": shows_display, "vram_mb": vram, "status": str(a.get("Status") or "")})
        for s in smi_left:            # nvidia-smi only (non-Windows, or WMI failed)
            result.append({"name": s["name"], "vendor": "NVIDIA", "driver": s["driver"],
                           "active": bool(s["display"] or s["util"] > 0 or s["used_mb"] > 500),
                           "vram_mb": s["vram_mb"], "status": ""})
        for g in result:
            g["group"] = classify(g["name"])
        return result
    except Exception:      # noqa: BLE001 - monitoring must never crash the GUI
        return []
