"""Live measurements of the local inference servers (Ollama, llama.cpp) for the Tuning page and the Dashboard.

Pure logic, no Qt: `Telemetry.collect()` runs in a worker thread (HTTP + psutil), `build_rows()` turns the raw
numbers into the display table. Every field says where it comes from; what a server does not expose is shown
as "-" with a short reason instead of an invented number.

Sources
  Ollama   : /api/version, /api/ps (name, size, size_vram, expires_at, context_length when the build reports it),
             /api/show (quantisation, layers, trained context)
  llama.cpp: /health (200 ready, 503 loading), /props (model_path, n_ctx, slots), /slots (busy slots),
             /metrics (needs --metrics: totals, requests_processing, requests_deferred), GGUF header of the model file
  Process  : psutil - start time (uptime), CPU %, RAM of the server process tree
  Requests : RequestStats - every request the Tuning page itself sends (Ollama has no server-wide counters)
"""
from __future__ import annotations

import os
import re
import socket
import time
import urllib.error
from dataclasses import dataclass, field
from pathlib import Path

from .net import http_json, http_open

try:
    import psutil
except ImportError:      # pragma: no cover
    psutil = None

GIB = 1024 ** 3
MIB = 1024 ** 2

# GGUF general.file_type -> name (llama.cpp enum llama_ftype)
FTYPE = {0: "F32", 1: "F16", 2: "Q4_0", 3: "Q4_1", 7: "Q8_0", 8: "Q5_0", 9: "Q5_1", 10: "Q2_K", 11: "Q3_K_S", 12: "Q3_K_M",
         13: "Q3_K_L", 14: "Q4_K_S", 15: "Q4_K_M", 16: "Q5_K_S", 17: "Q5_K_M", 18: "Q6_K", 19: "IQ2_XXS", 20: "IQ2_XS",
         21: "Q2_K_S", 22: "IQ3_XS", 23: "IQ3_XXS", 24: "IQ1_S", 25: "IQ4_NL", 26: "IQ3_S", 27: "IQ3_M", 28: "IQ2_S",
         29: "IQ2_M", 30: "IQ4_XS", 31: "IQ1_M", 32: "BF16", 36: "TQ1_0", 37: "TQ2_0", 38: "MXFP4"}


def fmt_dur(seconds: float | None) -> str:
    if seconds is None or seconds < 0:
        return "-"
    if seconds < 1:
        return f"{seconds * 1000:.0f} ms"
    if seconds < 120:
        return f"{seconds:.2f} s"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h} h {m:02d} min" if h else f"{m} min {s:02d} s"


def fmt_uptime(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    s = int(seconds)
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    return (f"{d} d " if d else "") + f"{h:02d}:{m:02d}:{s:02d}"


def parse_prometheus(text: str) -> dict[str, float]:
    """llama-server /metrics -> {metric name: value} (labels dropped, last sample wins)."""
    out: dict[str, float] = {}
    for line in (text or "").splitlines():
        if not line or line.startswith("#"):
            continue
        m = re.match(r"^([a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{[^}]*\})?\s+([-+0-9.eE]+|NaN|[+-]?Inf)\s*$", line)
        if m:
            try:
                out[m.group(1)] = float(m.group(2))
            except ValueError:
                pass
    return out


def parse_expires(value: str) -> float | None:
    """Ollama `expires_at` (RFC 3339, nanoseconds, zone) -> seconds from now; None if unparsable."""
    if not value:
        return None
    from datetime import datetime, timezone
    v = re.sub(r"(\.\d{6})\d+", r"\1", value.replace("Z", "+00:00"))
    try:
        dt = datetime.fromisoformat(v)
    except ValueError:
        return None
    if dt.year < 2000:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (dt - datetime.now(timezone.utc)).total_seconds()


# ------------------------------------------------------------------ own request statistics
@dataclass
class RequestStats:
    """Requests sent by the Tuning page (live test, probe steps). Ollama has no server-wide counters."""
    total: int = 0
    errors: int = 0
    active: int = 0
    last: dict = field(default_factory=dict)
    last_error: str = ""
    history: list = field(default_factory=list)      # per finished request: when, prompt_tps, gen_tps, ttft, prompt_s, gen_s, total_s, failed
    events: list = field(default_factory=list)       # timestamps of every finished request (ok or failed), for the requests-over-time chart

    def begin(self) -> None:
        self.active += 1

    def end(self) -> None:
        self.active = max(0, self.active - 1)

    def ok(self, final: dict, model: str = "") -> None:
        self.total += 1
        self.last = dict(final, model=model, when=time.time(), status=final.get("http_status", 200), failed=False)
        self.history.append({"when": self.last["when"], "failed": False,
                             **{k: final.get(k) for k in ("prompt_tps", "gen_tps", "ttft", "prompt_s", "gen_s", "total_s")}})
        self.events.append(self.last["when"])
        del self.history[:-120], self.events[:-2000]

    def fail(self, msg: str, endpoint: str = "", model: str = "") -> None:
        self.total += 1
        self.errors += 1
        self.last_error = msg
        m = re.search(r"HTTP (\d{3})", msg or "")
        self.last = {**{k: v for k, v in self.last.items() if k in ("prompt_tokens",)}, "endpoint": endpoint, "model": model,
                     "when": time.time(), "status": int(m.group(1)) if m else 0, "failed": True, "error": msg}
        self.history.append({"when": self.last["when"], "failed": True})
        self.events.append(self.last["when"])


# ------------------------------------------------------------------ process sampling
class ProcSampler:
    """Keeps psutil.Process objects so cpu_percent() has a baseline between calls."""

    def __init__(self) -> None:
        self._procs: dict[int, "psutil.Process"] = {}

    @staticmethod
    def _listening_pid(port: int) -> int | None:
        if psutil is None or not port:
            return None
        try:
            for c in psutil.net_connections(kind="tcp"):
                if c.status == psutil.CONN_LISTEN and c.laddr and c.laddr.port == port and c.pid:
                    return c.pid
        except (psutil.Error, OSError):
            pass
        return None

    def find(self, port: int, names: tuple[str, ...]) -> "psutil.Process | None":
        if psutil is None:
            return None
        pid = self._listening_pid(port)
        if pid:
            try:
                return psutil.Process(pid)
            except psutil.Error:
                pass
        for p in psutil.process_iter(["name"]):
            try:
                if any(n in (p.info["name"] or "").lower() for n in names):
                    return p
            except psutil.Error:
                continue
        return None

    def sample(self, port: int, names: tuple[str, ...]) -> dict:
        root = self.find(port, names)
        if root is None:
            self._procs.clear()
            return {}
        try:
            tree = [root] + root.children(recursive=True)
        except psutil.Error:
            tree = [root]
        ncpu = psutil.cpu_count() or 1
        cpu = rss = 0.0
        alive: dict[int, "psutil.Process"] = {}
        for p in tree:
            q = self._procs.get(p.pid) or p
            try:
                cpu += q.cpu_percent(None)
                rss += q.memory_info().rss
                alive[p.pid] = q
            except psutil.Error:
                continue
        self._procs = alive
        try:
            up = time.time() - root.create_time()
        except psutil.Error:
            up = None
        return {"pid": root.pid, "uptime_s": up, "cpu_pct": cpu / ncpu, "rss": rss, "procs": len(alive)}


# ------------------------------------------------------------------ collecting
class Telemetry:
    def __init__(self) -> None:
        self.proc = ProcSampler()
        self._show: dict[str, dict] = {}
        self._gguf: dict[str, dict] = {}

    # --- Ollama
    def _ollama_show(self, base: str, name: str) -> dict:
        if name in self._show:
            return self._show[name]
        out: dict = {}
        try:
            info = http_json(f"{base}/api/show", data={"model": name}, timeout=5)
            mi = info.get("model_info") or {}
            arch = str(mi.get("general.architecture", ""))
            det = info.get("details") or {}
            out = {"quant": det.get("quantization_level", ""), "format": str(det.get("format", "gguf")).upper(),
                   "params": det.get("parameter_size", ""), "layers": int(mi.get(f"{arch}.block_count", 0) or 0),
                   "ctx_train": int(mi.get(f"{arch}.context_length", 0) or 0)}
            self._show[name] = out
        except (RuntimeError, ValueError, OSError):
            pass
        return out

    def _collect_ollama(self, base: str, model: str) -> dict:
        d: dict = {"online": False}
        try:
            d["version"] = http_json(f"{base}/api/version", timeout=2).get("version", "")
            d["online"] = True
        except RuntimeError:
            return d
        try:
            ps = http_json(f"{base}/api/ps", timeout=3).get("models", [])
        except RuntimeError:
            ps = []
        want = model.lower()
        hit = next((m for m in ps if (m.get("name") or "").lower() == want or (m.get("model") or "").lower() == want), None)
        d["loaded"] = [m.get("name") for m in ps]
        d["model"] = (hit or (ps[0] if ps else {})).get("name") or model
        d["state"] = "loaded" if hit else "unloaded"
        if hit or ps:
            m = hit or ps[0]
            d["size"], d["size_vram"] = int(m.get("size") or 0), int(m.get("size_vram") or 0)
            d["keep_alive_s"] = parse_expires(m.get("expires_at", ""))
            d["ctx"] = int(m.get("context_length") or 0)
        d.update({f"show_{k}": v for k, v in self._ollama_show(base, d["model"]).items()} if d.get("model") else {})
        return d

    # --- llama.cpp
    def _gguf_info(self, path: str) -> dict:
        if not path:
            return {}
        if path in self._gguf:
            return self._gguf[path]
        out: dict = {}
        try:
            from .models import parse_quant
            from .tuning import read_gguf_meta
            kv = read_gguf_meta(path)
            arch = str(kv.get("general.architecture", ""))
            ft = kv.get("general.file_type")
            out = {"quant": FTYPE.get(int(ft), "") if ft is not None else "", "layers": int(kv.get(f"{arch}.block_count", 0) or 0),
                   "ctx_train": int(kv.get(f"{arch}.context_length", 0) or 0), "size": os.path.getsize(path), "arch": arch}
            out["quant"] = parse_quant(Path(path).name) or out["quant"]
            self._gguf[path] = out
        except (OSError, ValueError, KeyError):
            pass
        return out

    def _collect_llama(self, base: str, gguf_hint: str) -> dict:
        d: dict = {"online": False}
        try:
            with http_open(f"{base}/health", timeout=2) as r:
                d["online"], d["ready"] = True, r.status == 200
        except urllib.error.HTTPError as e:
            d["online"], d["ready"] = True, False          # 503 = model still loading
            d["health_code"] = e.code
        except (urllib.error.URLError, OSError, ValueError):
            return d
        props = {}
        try:
            props = http_json(f"{base}/props", timeout=3)
        except RuntimeError:
            pass
        gs = props.get("default_generation_settings") or {}
        d["model_path"] = str(props.get("model_path") or gguf_hint or "")
        d["ctx"] = int(gs.get("n_ctx") or 0)
        d["slots"] = props.get("total_slots")
        try:
            slots = http_json(f"{base}/slots", timeout=3)
            if isinstance(slots, list):
                d["slots"] = d["slots"] or len(slots)
                d["busy_slots"] = sum(1 for s in slots if s.get("is_processing"))
                if not d["ctx"] and slots:
                    d["ctx"] = int(slots[0].get("n_ctx") or 0)
        except RuntimeError:
            pass
        try:
            with http_open(f"{base}/metrics", timeout=3) as r:
                d["metrics"] = parse_prometheus(r.read().decode("utf-8", "replace"))
        except (urllib.error.URLError, OSError, ValueError):
            pass
        d.update({f"gguf_{k}": v for k, v in self._gguf_info(d["model_path"]).items()})
        return d

    def collect(self, backend: str, base: str, host: str, port: int, model: str = "", gguf_hint: str = "") -> dict:
        t0 = time.time()
        d = self._collect_ollama(base, model) if backend == "ollama" else self._collect_llama(base, gguf_hint)
        d["backend"], d["host"], d["port"], d["when"] = backend, host, port, t0
        names = ("ollama",) if backend == "ollama" else ("llama-server", "llama_server")
        d["proc"] = self.proc.sample(port, names) if d.get("online") else {}
        try:
            d["hostname"] = socket.gethostname()
            d["ip"] = socket.gethostbyname(d["hostname"])
        except OSError:
            d["hostname"], d["ip"] = "", ""
        return d


# ------------------------------------------------------------------ display table
def _gb(b: float) -> str:
    return f"{b / GIB:.2f} GB"


def build_rows(snap: dict, sample: dict | None, stats: RequestStats, *, cfg_llama: dict | None = None, ctx_cfg: int = 0,
               layers_cfg: int = 0, usable_vram: int = 0, busy_job: bool = False) -> list[tuple[str, str, str, str]]:
    """[(group, label, value, source)] - every measurement of the list the user asked for. "-" = not exposed."""
    snap = snap or {}
    sample = sample or {}
    gpu = dict(sample.get("gpu") or {})
    if gpu:
        for k in ("util", "temp", "power", "power_limit", "used_mb", "total_mb"):
            gpu.setdefault(k, 0)
    be = snap.get("backend", "")
    ollama = be == "ollama"
    proc = snap.get("proc") or {}
    online = bool(snap.get("online"))
    m = snap.get("metrics") or {}
    last = stats.last or {}
    R: list[tuple[str, str, str, str]] = []

    def add(group, label, value, source=""):
        R.append((group, label, "-" if value in (None, "") else str(value), source))

    # --- server
    if not online:
        status = "Offline"
    elif not ollama and not snap.get("ready"):
        status = "Online - Modell lädt"
    else:
        status = "Online"
    add("Server", "Serverstatus", status, "HTTP-Abfrage")
    addr = f"{snap.get('hostname', '')} · {snap.get('ip', '')} · {snap.get('host', '')}:{snap.get('port', '')}" if snap else ""
    add("Server", "Serveradresse", addr, "System")
    add("Server", "Serverlaufzeit", fmt_uptime(proc.get("uptime_s")) if proc else None, "Prozess-Startzeit")

    # --- model
    if ollama:
        name = snap.get("model")
        state = {"loaded": "Geladen", "unloaded": "Entladen"}.get(snap.get("state"), None) if online else None
        fmt = "Ollama-Modell (GGUF-Blob)" if online and name else None
        quant = snap.get("show_quant")
        size = _gb(snap["size"]) if snap.get("size") else None
        layers = snap.get("show_layers", 0)
        if snap.get("size") and snap.get("size_vram") is not None:
            frac = snap["size_vram"] / snap["size"] if snap["size"] else 0
            mode = "GPU" if frac >= 0.99 else ("CPU" if frac <= 0.01 else f"CPU/GPU-Hybrid ({frac * 100:.0f} % im VRAM)")
            off = f"≈ {round(layers * frac)} / {layers + 1} Layer ({frac * 100:.0f} % im VRAM)" if layers else f"{frac * 100:.0f} % der Gewichte im VRAM"
        else:
            mode = off = None
        ctx_max = snap.get("ctx") or ctx_cfg or 0
    else:
        mp = snap.get("model_path", "")
        name = Path(mp).name if mp else None
        state = (("Geladen" if snap.get("ready") else "Lädt") if online else None)
        fmt = "GGUF" if name else None
        quant = snap.get("gguf_quant")
        size = _gb(snap["gguf_size"]) if snap.get("gguf_size") else None
        layers = snap.get("gguf_layers", 0)
        ngl = (cfg_llama or {}).get("ngl", "auto")
        auto = str(ngl).strip().lower() in ("", "auto", "-1")
        total = layers + 1 if layers else 0
        too_big = bool(usable_vram and snap.get("gguf_size") and snap["gguf_size"] > usable_vram)
        if not total:
            mode = off = None
        elif not auto and int(ngl) == 0:
            mode, off = "CPU", f"0 / {total} Layer (konfiguriert)"
        elif not auto and int(ngl) < total:
            mode, off = f"CPU/GPU-Hybrid", f"{int(ngl)} / {total} Layer (konfiguriert)"
        elif auto and too_big:
            mode, off = "CPU/GPU-Hybrid (Modell größer als VRAM)", f"auto - nicht alle der {total} Layer passen"
        else:
            mode, off = "GPU", f"{total} / {total} Layer ({'auto' if auto else 'konfiguriert'})"
        ctx_max = snap.get("ctx") or ctx_cfg or 0
    add("Modell", "Aktives Modell", name, "Server")
    add("Modell", "Modellstatus", state or ("Entladen" if online and ollama else None), "Server")
    add("Modell", "Modellformat", fmt, "Server / GGUF")
    add("Modell", "Quantisierung", quant, "Modell-Metadaten")
    add("Modell", "Modellgröße", size, "Datei / Server")
    add("Modell", "Prozessorbetrieb", mode, "VRAM-Anteil (Ollama) bzw. Konfiguration (llama.cpp)")

    # --- hardware
    add("Hardware", "GPU-Modell", gpu.get("name") or None, "nvidia-smi")
    add("Hardware", "GPU-Auslastung", f"{gpu['util']} %" if gpu else None, "nvidia-smi")
    add("Hardware", "VRAM-Nutzung", f"{gpu['used_mb'] / 1024:.2f} / {gpu['total_mb'] / 1024:.2f} GB ({100 * gpu['used_mb'] / max(gpu['total_mb'], 1):.0f} %)" if gpu else None, "nvidia-smi (gesamt)")
    add("Hardware", "GPU-Offloading", off, "geschätzt (Ollama: VRAM-Anteil, llama.cpp: Konfiguration)")
    add("Hardware", "GPU-Temperatur", f"{gpu['temp']} °C" if gpu else None, "nvidia-smi")
    add("Hardware", "GPU-Leistungsaufnahme", (f"{gpu['power']:.0f} W" + (f" / {gpu['power_limit']:.0f} W" if gpu.get("power_limit") else "")) if gpu else None, "nvidia-smi")
    add("Hardware", "CPU-Auslastung", f"{sample['cpu']:.0f} %" if "cpu" in sample else None, "System")
    add("Hardware", "Prozessorauslastung (Server)", f"{proc['cpu_pct']:.1f} %" if proc else None, "Serverprozess")
    add("Hardware", "RAM-Nutzung", f"{sample['ram_used'] / GIB:.1f} / {sample['ram_total'] / GIB:.1f} GB ({sample['ram_pct']:.0f} %)" if "ram_used" in sample else None, "System")
    add("Hardware", "Prozessspeicher (Server)", (_gb(proc["rss"]) + (f" ({proc['procs']} Prozesse)" if proc.get("procs", 1) > 1 else "")) if proc else None, "Serverprozess")

    # --- context
    add("Kontext", "Kontextgröße", f"{ctx_max:,} Token".replace(",", ".") if ctx_max else None,
        "Server" if snap.get("ctx") else "Konfiguration")
    used = (last.get("prompt_tokens", 0) or 0) + (last.get("gen_tokens", 0) or 0)
    add("Kontext", "Kontextbelegung", (f"{used:,} / {ctx_max:,} Token ({100 * used / ctx_max:.0f} %)".replace(",", ".") if used and ctx_max else None),
        "letzte Anfrage")

    # --- last request
    ok = last and not last.get("failed")
    add("Letzte Anfrage", "Prompt-Token", last.get("prompt_tokens") if ok else None, "Server-Antwort")
    add("Letzte Anfrage", "Ausgabe-Token", last.get("gen_tokens") if ok else None, "Server-Antwort")
    add("Letzte Anfrage", "Prompt-Leistung", f"{last['prompt_tps']:.0f} Token/s" if ok and last.get("prompt_tps") else None, "Server-Antwort")
    add("Letzte Anfrage", "Generierungsleistung", f"{last['gen_tps']:.1f} Token/s" if ok and last.get("gen_tps") else None, "Server-Antwort")
    add("Letzte Anfrage", "Antwortbeginn (TTFT)", fmt_dur(last.get("ttft")) if ok else None, "gemessen")
    add("Letzte Anfrage", "Prompt-Dauer", fmt_dur(last.get("prompt_s")) if ok and last.get("prompt_s") is not None else None, "Server-Antwort")
    add("Letzte Anfrage", "Generierungsdauer", fmt_dur(last.get("gen_s")) if ok and last.get("gen_s") is not None else None, "Server-Antwort")
    add("Letzte Anfrage", "Gesamtdauer", fmt_dur(last.get("total_s")) if ok else None, "Server-Antwort")
    add("Letzte Anfrage", "Modell-Ladezeit", fmt_dur(last.get("load_s")) if ok and ollama else (None if not ok else "- (llama.cpp lädt beim Serverstart)"), "Server-Antwort")

    # --- requests
    if ollama:
        add("Anfragen", "Requests", f"{stats.total} (Tuning-Seite)", "Tuning-Seite - Ollama hat keinen Server-Zähler")
        active, queue = stats.active, None
        src_a, src_q = "Tuning-Seite", "Ollama liefert keine Warteschlange"
    else:
        srv = (f" · Server gesamt: {int(m.get('llamacpp:tokens_predicted_total', 0))} gen / "
               f"{int(m.get('llamacpp:prompt_tokens_total', 0))} Prompt-Token") if m else ""
        add("Anfragen", "Requests", f"{stats.total} (Tuning-Seite){srv}", "Tuning-Seite + llama.cpp /metrics")
        if m:
            active, queue = int(m.get("llamacpp:requests_processing", 0)), int(m.get("llamacpp:requests_deferred", 0))
            src_a = src_q = "llama.cpp /metrics"
        else:
            active, queue = snap.get("busy_slots"), None
            src_a, src_q = "llama.cpp /slots", "benötigt --metrics (wird beim nächsten Dienststart aktiviert)"
    if busy_job and not active:
        active = stats.active or 1
    add("Anfragen", "Aktive Requests", active if online else None, src_a)
    quote = f"{stats.errors} ({100 * stats.errors / stats.total:.0f} %)" if stats.total else "0"
    add("Anfragen", "Fehler", quote, "Tuning-Seite")
    add("Anfragen", "Warteschlange", queue, src_q)
    ka = snap.get("keep_alive_s")
    add("Anfragen", "Keep-Alive", (fmt_dur(ka) if ka is not None and ka > 0 else ("abgelaufen" if ka is not None else None)) if ollama else "- (bleibt geladen)", "Ollama /api/ps")
    add("Anfragen", "API-Endpunkt", last.get("endpoint"), "letzte Anfrage")
    add("Anfragen", "Antwortstatus", (f"HTTP {last['status']}" if last.get("status") else None) if last else None, "letzte Anfrage")
    add("Anfragen", "Zeitstempel", time.strftime("%d.%m.%Y %H:%M:%S", time.localtime(snap.get("when", time.time()))) if snap else None, "Messzeitpunkt")
    return R
