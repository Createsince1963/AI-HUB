"""Hardware- and model-aware parameter tuning for Ollama and llama.cpp.

What lives here (pure logic, no Qt - everything is unit-testable and runs in worker threads):

  * read_gguf_meta()       - minimal GGUF header reader (layers, KV heads, trained context, ...)
  * ModelSpec / Hardware   - what the model needs and what the GPU offers
  * estimate_memory() & max_ctx_estimate()
                           - weights + KV cache + compute buffer vs. usable VRAM (an ESTIMATE; the
                             probes below are the authority)
  * build_params()         - the parameter rows for the GUI: default, hardware/model-dependent maximum
                             and the block steps in between
  * ollama_chat_stream() / llama_completion_stream()
                           - streaming test requests that yield live tokens/s events
  * probe_ollama() / probe_llamacpp()
                           - walk the context sizes upwards, stop at the first size that no longer fits
                             on the GPU (or whose speed collapses) and report the last good one
  * TuningStore            - remembers measured maxima per backend/model/KV type/GPU
  * apply_*() / modelfile_text()
                           - persist settings the servers read at start (config.json) or per model

Facts used (checked against the Ollama API docs and the llama.cpp server README):
  Ollama   : final response carries eval_count/eval_duration/prompt_eval_count/prompt_eval_duration/
             load_duration; tokens/s = eval_count / eval_duration * 1e9. /api/ps lists size and size_vram
             per loaded model (size_vram < size = partly on the CPU).
  llama.cpp: -c = context (0 = from model), -np = slots (context is shared between slots),
             --cache-type-k/-v, -fa, -ngl, -b/-ub, --fit; completion responses carry `timings`
             (prompt_per_second, predicted_per_second, prompt_n, predicted_n).
"""
from __future__ import annotations

import json
import os
import re
import collections
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Iterator

from .net import http_json, http_open

MIB = 1024 * 1024
GIB = 1024 * MIB
_NOWIN = 0x08000000 if sys.platform == "win32" else 0

# ------------------------------------------------------------------ GGUF header
_GGUF_SCALAR = {0: ("B", 1), 1: ("b", 1), 2: ("H", 2), 3: ("h", 2), 4: ("I", 4), 5: ("i", 4), 6: ("f", 4),
                7: ("?", 1), 10: ("Q", 8), 11: ("q", 8), 12: ("d", 8)}


def read_gguf_meta(path: str | Path) -> dict:
    """Metadata key/values of a GGUF file (arrays are skipped - tokenizer tables are huge and useless
    here). Stops once the tokenizer section starts and the architecture keys have been seen."""
    with open(path, "rb") as f:
        if f.read(4) != b"GGUF":
            raise ValueError("not a GGUF file")
        (version,) = struct.unpack("<I", f.read(4))
        if version < 2:
            raise ValueError(f"unsupported GGUF version {version}")
        _n_tensors, n_kv = struct.unpack("<QQ", f.read(16))

        def rstr() -> str:
            (n,) = struct.unpack("<Q", f.read(8))
            return f.read(n).decode("utf-8", "replace")

        def rval(t: int):
            if t == 8:
                return rstr()
            if t == 9:                                   # array: skip, never materialise
                et, n = struct.unpack("<IQ", f.read(12))
                if et in _GGUF_SCALAR:
                    f.seek(n * _GGUF_SCALAR[et][1], 1)
                elif et == 8:
                    for _ in range(n):
                        (ln,) = struct.unpack("<Q", f.read(8))
                        f.seek(ln, 1)
                else:
                    for _ in range(n):
                        rval(et)
                return None
            fmt, size = _GGUF_SCALAR[t]
            return struct.unpack("<" + fmt, f.read(size))[0]

        out: dict = {}
        for _ in range(n_kv):
            key = rstr()
            (t,) = struct.unpack("<I", f.read(4))
            val = rval(t)
            if val is not None:
                out[key] = val
            if key.startswith("tokenizer.") and "general.architecture" in out:
                arch = out["general.architecture"]
                if f"{arch}.block_count" in out:
                    break
        return out


# ------------------------------------------------------------------ model + hardware
KV_BYTES = {"f32": 4.0, "f16": 2.0, "bf16": 2.0, "q8_0": 34 / 32, "q5_1": 24 / 32, "q5_0": 22 / 32,
            "q4_1": 20 / 32, "q4_0": 18 / 32, "iq4_nl": 18 / 32}
KV_CHOICES = ["f16", "q8_0", "q4_0"]
COMPUTE_BUFFER_MB = 600           # compute/scratch buffers of the inference engine (rough, model dependent)
VRAM_RESERVE_MB = 1024            # CUDA context + desktop compositor; never available to the model
# Architectures whose KV cache is smaller than the plain formula (sliding window / hybrid / SSM layers):
# the estimate is conservative there, the probe finds the real limit.
CONSERVATIVE_ARCHS = ("gpt-oss", "gemma2", "gemma3", "qwen3next", "qwen35", "mamba", "jamba", "lfm2", "granite")


@dataclass
class ModelSpec:
    name: str
    arch: str = ""
    n_layers: int = 0
    n_heads: int = 0
    n_kv_heads: int = 0
    key_len: int = 0
    val_len: int = 0
    n_embd: int = 0
    ctx_train: int = 0
    weights_bytes: int = 0
    quant: str = ""
    params_text: str = ""

    @classmethod
    def from_kv(cls, name: str, kv: dict, weights_bytes: int = 0, quant: str = "", params_text: str = "") -> "ModelSpec":
        arch = str(kv.get("general.architecture", ""))

        def g(key: str, default: int = 0) -> int:
            try:
                return int(kv.get(f"{arch}.{key}", default) or default)
            except (TypeError, ValueError):
                return default
        n_heads = g("attention.head_count")
        n_kv = g("attention.head_count_kv", n_heads) or n_heads
        n_embd = g("embedding_length")
        key_len = g("attention.key_length") or (n_embd // n_heads if n_heads else 0)
        val_len = g("attention.value_length") or key_len
        return cls(name=name, arch=arch, n_layers=g("block_count"), n_heads=n_heads, n_kv_heads=n_kv,
                   key_len=key_len, val_len=val_len, n_embd=n_embd, ctx_train=g("context_length"),
                   weights_bytes=weights_bytes, quant=quant or str(kv.get("general.file_type", "")),
                   params_text=params_text or str(kv.get("general.size_label", "")))

    @property
    def conservative(self) -> bool:
        return any(self.arch.startswith(a) for a in CONSERVATIVE_ARCHS)

    @property
    def known(self) -> bool:
        return bool(self.n_layers and self.n_kv_heads and self.key_len)


@dataclass
class Hardware:
    gpu_name: str = ""
    vram_total_mb: int = 0
    vram_used_mb: int = 0
    ram_total_mb: int = 0

    @property
    def usable_vram_bytes(self) -> int:
        return max(0, (self.vram_total_mb - VRAM_RESERVE_MB)) * MIB

    @property
    def key(self) -> str:
        return f"{self.gpu_name}|{self.vram_total_mb}"


def detect_hardware(vram_gb_fallback: float = 12.0) -> Hardware:
    hw = Hardware(vram_total_mb=int(vram_gb_fallback * 1024))
    try:
        from .system import gpu_info
        g = gpu_info()
        if g:
            hw.gpu_name, hw.vram_total_mb, hw.vram_used_mb = g["name"], int(g["total_mb"]), int(g["used_mb"])
    except Exception:      # noqa: BLE001 - detection must never break the page
        pass
    try:
        import psutil
        hw.ram_total_mb = int(psutil.virtual_memory().total / MIB)
    except Exception:      # noqa: BLE001
        pass
    return hw


def kv_bytes_per_token(spec: ModelSpec, kv_type: str = "f16") -> float:
    return spec.n_layers * spec.n_kv_heads * (spec.key_len + spec.val_len) * KV_BYTES.get(kv_type, 2.0)


def estimate_memory(spec: ModelSpec, hw: Hardware, ctx: int, kv_type: str = "f16", parallel: int = 1) -> dict:
    weights = spec.weights_bytes
    kv = int(kv_bytes_per_token(spec, kv_type) * ctx * max(1, parallel))
    overhead = COMPUTE_BUFFER_MB * MIB
    total = weights + kv + overhead
    usable = hw.usable_vram_bytes
    return {"weights": weights, "kv": kv, "overhead": overhead, "total": total, "usable": usable,
            "fits": total <= usable, "free_after": usable - total}


def max_ctx_estimate(spec: ModelSpec, hw: Hardware, kv_type: str = "f16", step: int = 1024, parallel: int = 1) -> int:
    """Largest context (multiple of `step`) whose weights + KV cache + compute buffer fit in usable VRAM.
    0 = the weights alone do not fit (partial CPU offload needed). Capped at the trained context."""
    per = kv_bytes_per_token(spec, kv_type) * max(1, parallel)
    avail = hw.usable_vram_bytes - spec.weights_bytes - COMPUTE_BUFFER_MB * MIB
    if avail <= 0 or per <= 0:
        return 0
    c = int(avail // per) // step * step
    return min(c, spec.ctx_train) if spec.ctx_train else min(c, 131072)


# ------------------------------------------------------------------ parameter rows
CTX_BLOCKS = [2048, 4096, 6144, 8192, 12288, 16384, 24576, 32768, 49152, 65536, 98304, 131072, 196608, 262144,
              393216, 524288, 1048576]
PREDICT_BLOCKS = [256, 512, 1024, 2048, 4096, 8192, 16384, 32768, 65536]
BATCH_BLOCKS = [128, 256, 512, 1024, 2048, 4096]
MIN_USEFUL_CTX = 4096

OLLAMA_DEFAULTS = {"ctx": 4096, "predict": -1, "temperature": 0.8, "top_p": 0.9, "top_k": 40, "min_p": 0.0,
                   "repeat_penalty": 1.1, "gpu_layers": -1, "kv_type": "f16", "batch": 512, "parallel": 1}
LLAMA_DEFAULTS = {"ctx": 8192, "predict": -1, "temperature": 0.8, "top_p": 0.95, "top_k": 40, "min_p": 0.05,
                  "repeat_penalty": 1.0, "gpu_layers": -1, "kv_type": "f16", "batch": 2048, "parallel": 1}


@dataclass
class ParamDef:
    key: str
    label: str
    kind: str                      # "int" | "float" | "choice"
    default: object
    steps: list
    maximum: object = None         # hardware/model dependent upper limit shown next to the default
    unit: str = ""
    scope: str = "request"         # "request" = per request/test; "server" = read at server start (restart)
    help: str = ""
    note: str = ""                 # e.g. "gemessen" / "geschätzt"
    auto_values: tuple = ()        # step values that mean "automatic / unlimited" (rendered as text)

    def snap(self, value):
        if value in self.steps:
            return value
        if self.kind == "choice" or not self.steps:
            return self.default
        nums = [s for s in self.steps if isinstance(s, (int, float))]
        return min(nums, key=lambda s: abs(s - value)) if nums else self.default


def blocks_between(blocks: list[int], lo: int, hi: int, must: tuple[int, ...] = ()) -> list[int]:
    vals = {b for b in blocks if lo <= b <= hi} | {m for m in must if m}
    return sorted(vals)


def _frange(lo: float, hi: float, step: float) -> list[float]:
    n = int(round((hi - lo) / step))
    return [round(lo + i * step, 4) for i in range(n + 1)]


def parse_ollama_parameters(text: str) -> dict:
    """The `parameters` block of /api/show ('temperature   0.7' lines) -> {key: number}. Models can ship
    their own sampling defaults - those are the real defaults, not Ollama's generic ones."""
    out: dict = {}
    for line in (text or "").splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) != 2 or parts[0] == "stop":
            continue
        try:
            out[parts[0]] = int(parts[1]) if re.fullmatch(r"-?\d+", parts[1].strip()) else float(parts[1])
        except ValueError:
            continue
    return out


def build_params(backend: str, spec: ModelSpec, hw: Hardware, *, model_defaults: dict | None = None,
                 launcher: dict | None = None, measured_max_ctx: int = 0, kv_type: str = "f16",
                 parallel: int = 1) -> list[ParamDef]:
    """The GUI parameter rows. backend = "ollama" | "llamacpp".

    default  : the backend's documented default, overridden by the model's own parameters (Ollama
               /api/show) or the launcher config (llama.cpp).
    maximum  : context -> measured maximum if a probe exists, else the VRAM estimate (never above the
               trained context); GPU layers -> layer count; num_predict -> context.
    steps    : blocks between a sensible minimum and the maximum (default and maximum always included)."""
    ollama = backend == "ollama"
    base = dict(OLLAMA_DEFAULTS if ollama else LLAMA_DEFAULTS)
    for k_model, k_ui in (("num_ctx", "ctx"), ("num_predict", "predict"), ("temperature", "temperature"),
                          ("top_p", "top_p"), ("top_k", "top_k"), ("min_p", "min_p"),
                          ("repeat_penalty", "repeat_penalty")):
        if model_defaults and k_model in model_defaults:
            base[k_ui] = model_defaults[k_model]
    for k, v in (launcher or {}).items():
        if k in base and v not in (None, ""):
            base[k] = v
    est = max_ctx_estimate(spec, hw, kv_type, parallel=parallel) if spec.known else 0
    note = "gemessen" if measured_max_ctx else ("geschätzt" if est else "unbekannt")
    ctx_max = measured_max_ctx or est or max(MIN_USEFUL_CTX, int(base["ctx"]))
    if spec.ctx_train:
        ctx_max = min(ctx_max, spec.ctx_train)
    ctx_max = max(ctx_max, MIN_USEFUL_CTX)
    ctx_def = int(base["ctx"])
    ctx_steps = blocks_between(CTX_BLOCKS, 2048, max(ctx_max, ctx_def), (ctx_def, ctx_max))
    layers = max(spec.n_layers + 1, 1)          # +1: the output layer is offloaded too
    gpu_steps = [-1] + sorted({0, layers // 4, layers // 2, (layers * 3) // 4, layers})
    pred_steps = blocks_between(PREDICT_BLOCKS, 256, ctx_max, ()) + [-1]
    srv = "request" if ollama else "server"
    rows = [
        ParamDef("ctx", "Kontext (Tokens)", "int", ctx_def, ctx_steps, ctx_max, "Tokens", srv,
                 "Wie viel Text (Prompt + Antwort) das Modell gleichzeitig sieht. Größer = mehr VRAM für den KV-Cache.",
                 note),
        ParamDef("predict", "Max. Antwortlänge", "int", int(base["predict"]), pred_steps, ctx_max, "Tokens",
                 "request", "Obergrenze für die Antwort. ∞ = bis das Modell von selbst endet.", "", (-1,)),
        ParamDef("temperature", "Temperature", "float", float(base["temperature"]), _frange(0.0, 2.0, 0.1), 2.0, "",
                 "request", "Höher = kreativer/zufälliger, niedriger = deterministischer (Code: 0.1–0.4)."),
        ParamDef("top_p", "Top-P", "float", float(base["top_p"]), _frange(0.1, 1.0, 0.05), 1.0, "", "request",
                 "Nucleus-Sampling: nur die wahrscheinlichsten Tokens bis zu dieser Summe."),
        ParamDef("top_k", "Top-K", "int", int(base["top_k"]), [0, 1, 5, 10, 20, 40, 60, 80, 100], 100, "", "request",
                 "Nur die K wahrscheinlichsten Tokens. 0 = aus.", "", (0,)),
        ParamDef("min_p", "Min-P", "float", float(base["min_p"]), [0.0, 0.01, 0.02, 0.05, 0.1, 0.15, 0.2, 0.3], 0.3,
                 "", "request", "Verwirft Tokens unter Anteil × bestem Token. 0 = aus."),
        ParamDef("repeat_penalty", "Wiederholungsstrafe", "float", float(base["repeat_penalty"]),
                 _frange(1.0, 1.5, 0.05), 1.5, "", "request", "Über 1.0 = weniger Wiederholungen. 1.0 = aus."),
        ParamDef("gpu_layers", "GPU-Layer", "int", int(base["gpu_layers"]), gpu_steps, layers, "Layer", srv,
                 "Wie viele Schichten im VRAM liegen. Auto = so viele wie passen. Weniger = langsamer, spart VRAM.",
                 "", (-1,)),
        ParamDef("kv_type", "KV-Cache-Typ", "choice", base["kv_type"], list(KV_CHOICES), None, "", "server",
                 "f16 = Standard, q8_0 halbiert den Cache, q4_0 viertelt ihn (leichter Qualitätsverlust). "
                 "Benötigt Flash-Attention."),
        ParamDef("batch", "Batch-Größe", "int", int(base["batch"]), BATCH_BLOCKS, 4096, "Tokens", srv,
                 "Prompt-Verarbeitung in Blöcken. Größer = schnellerer Prompt, etwas mehr VRAM."),
        ParamDef("parallel", "Parallele Slots", "int", int(base["parallel"]), [1, 2, 3, 4], 4, "", "server",
                 "Gleichzeitige Anfragen. Der Kontext wird pro Slot belegt (Ollama) bzw. geteilt (llama.cpp) - "
                 "mehr Slots = weniger Kontext je Anfrage."),
    ]
    return rows


def ollama_options(values: dict) -> dict:
    """UI values -> Ollama `options` for a request. Automatic/unset values are omitted."""
    o = {"num_ctx": int(values["ctx"]), "num_predict": int(values["predict"]), "temperature": float(values["temperature"]),
         "top_p": float(values["top_p"]), "top_k": int(values["top_k"]), "min_p": float(values["min_p"]),
         "repeat_penalty": float(values["repeat_penalty"]), "num_batch": int(values["batch"])}
    if int(values.get("gpu_layers", -1)) >= 0:
        o["num_gpu"] = int(values["gpu_layers"])
    return o


def llama_params(values: dict) -> dict:
    """UI values -> llama-server /completion body fields (sampling only; ctx/ngl/... are start arguments)."""
    return {"n_predict": int(values["predict"]), "temperature": float(values["temperature"]),
            "top_p": float(values["top_p"]), "top_k": int(values["top_k"]), "min_p": float(values["min_p"]),
            "repeat_penalty": float(values["repeat_penalty"]), "cache_prompt": False}


def suggest_ctx_from_error(msg: str, spec: ModelSpec | None, hw: Hardware, kv_type: str = "f16") -> str:
    """Turns "request (20455 tokens) exceeds the available context size (8192 tokens)" into advice."""
    m = re.search(r"request \((\d+) tokens\) exceeds the available context size \((\d+) tokens\)", msg or "")
    if not m:
        return ""
    need, have = int(m.group(1)), int(m.group(2))
    want = next((b for b in CTX_BLOCKS if b >= need + 1024), CTX_BLOCKS[-1])
    text = f"Der Prompt hat {need} Tokens, der Kontext nur {have}. Mindestens {want} Tokens einstellen"
    if spec is not None and spec.known:
        mem = estimate_memory(spec, hw, want, kv_type)
        text += (f" (KV-Cache ca. {mem['kv'] / GIB:.1f} GB, gesamt ca. {mem['total'] / GIB:.1f} von "
                 f"{mem['usable'] / GIB:.1f} GB nutzbarem VRAM - {'passt' if mem['fits'] else 'passt NICHT vollständig'})")
    return text + "."


# ------------------------------------------------------------------ live metrics: streaming requests
def make_prompt(n_tokens: int, question: str = "Fasse den Text in einem Satz zusammen.") -> str:
    """Filler prompt of roughly n_tokens (about 4 characters per token) - measures prompt-processing speed."""
    if n_tokens <= 0:
        return "Schreibe eine kurze Erklärung, wie ein Lüfter mit PWM-Steuerung funktioniert, in mindestens fünf Sätzen."
    unit = "Der schnelle braune Fuchs springt über den faulen Hund und läuft durch den Wald. "
    reps = max(1, int(n_tokens * 4 / len(unit)))
    return unit * reps + "\n\n" + question


def _http_error(e: urllib.error.HTTPError) -> RuntimeError:
    try:
        body = e.read().decode("utf-8", "replace")
        try:
            j = json.loads(body)
            err = j.get("error")
            body = err.get("message") if isinstance(err, dict) else (err or body)
        except ValueError:
            pass
    except Exception:      # noqa: BLE001
        body = e.reason
    return RuntimeError(f"HTTP {e.code}: {body}")


def ollama_chat_stream(base: str, model: str, prompt: str, options: dict, *,
                       cancelled: Callable[[], bool] = lambda: False, keep_alive: str = "5m",
                       timeout: float = 900) -> Iterator[dict]:
    """Yields {"type":"token", text, n, tps, ttft, t} per streamed chunk and one {"type":"final", ...} with the
    server's own numbers. Closing the connection (cancel) makes Ollama stop generating."""
    body = {"model": model, "messages": [{"role": "user", "content": prompt}], "stream": True,
            "options": options, "keep_alive": keep_alive}
    t0 = time.monotonic()
    n, t_first = 0, None
    try:
        r = http_open(f"{base}/api/chat", {"Content-Type": "application/json"}, json.dumps(body).encode(), "POST", timeout)
    except urllib.error.HTTPError as e:
        raise _http_error(e) from e
    with r:
        for raw in r:
            if cancelled():
                yield {"type": "cancelled"}
                return
            line = raw.strip()
            if not line:
                continue
            d = json.loads(line)
            if d.get("error"):
                raise RuntimeError(str(d["error"]))
            if d.get("done"):
                ev = d.get("eval_duration") or 0
                pe = d.get("prompt_eval_duration") or 0
                yield {"type": "final", "gen_tokens": d.get("eval_count", 0),
                       "gen_tps": (d.get("eval_count", 0) / ev * 1e9) if ev else 0.0,
                       "prompt_tokens": d.get("prompt_eval_count", 0),
                       "prompt_tps": (d.get("prompt_eval_count", 0) / pe * 1e9) if pe else 0.0,
                       "load_s": (d.get("load_duration") or 0) / 1e9, "total_s": (d.get("total_duration") or 0) / 1e9,
                       "ttft": t_first or 0.0, "t": time.monotonic() - t0, "prompt_s": pe / 1e9, "gen_s": ev / 1e9,
                       "endpoint": "/api/chat", "http_status": getattr(r, "status", 200)}
                return
            msg = d.get("message") or {}
            txt = msg.get("content") or msg.get("thinking") or ""
            if txt:
                n += 1
                now = time.monotonic() - t0
                if t_first is None:
                    t_first = now
                tps = (n - 1) / (now - t_first) if n > 1 and now > t_first else 0.0
                yield {"type": "token", "text": txt, "n": n, "tps": tps, "ttft": t_first, "t": now}


def llama_completion_stream(base: str, prompt: str, params: dict, *, cancelled: Callable[[], bool] = lambda: False,
                            timeout: float = 900) -> Iterator[dict]:
    """Same event shape for llama-server's native /completion endpoint (server-sent events; the last event
    carries `timings`)."""
    body = {"prompt": prompt, "stream": True, **params}
    t0 = time.monotonic()
    n, t_first = 0, None
    try:
        r = http_open(f"{base}/completion", {"Content-Type": "application/json"}, json.dumps(body).encode(), "POST", timeout)
    except urllib.error.HTTPError as e:
        raise _http_error(e) from e
    with r:
        for raw in r:
            if cancelled():
                yield {"type": "cancelled"}
                return
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            d = json.loads(line[5:])
            if d.get("error"):
                err = d["error"]
                raise RuntimeError(err.get("message") if isinstance(err, dict) else str(err))
            if d.get("content"):
                n += 1
                now = time.monotonic() - t0
                if t_first is None:
                    t_first = now
                tps = (n - 1) / (now - t_first) if n > 1 and now > t_first else 0.0
                yield {"type": "token", "text": d["content"], "n": n, "tps": tps, "ttft": t_first, "t": now}
            if d.get("stop"):
                t = d.get("timings") or {}
                yield {"type": "final", "gen_tokens": t.get("predicted_n", n), "gen_tps": float(t.get("predicted_per_second") or 0),
                       "prompt_tokens": t.get("prompt_n", 0), "prompt_tps": float(t.get("prompt_per_second") or 0),
                       "load_s": 0.0, "total_s": time.monotonic() - t0, "ttft": t_first or 0.0, "t": time.monotonic() - t0,
                       "prompt_s": float(t.get("prompt_ms") or 0) / 1000, "gen_s": float(t.get("predicted_ms") or 0) / 1000,
                       "endpoint": "/completion", "http_status": getattr(r, "status", 200)}
                return


# ------------------------------------------------------------------ service helpers + live console
def service_up(base: str, backend: str, timeout: float = 1.5) -> bool:
    """True if the server answers (llama.cpp /health 200 = model loaded, Ollama /api/version)."""
    try:
        with http_open(base + ("/api/version" if backend == "ollama" else "/health"), timeout=timeout) as r:
            return r.status == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


def wait_for_service(base: str, backend: str, *, timeout: float = 180.0, on_log: Callable[[str], None] = lambda s: None,
                     cancelled: Callable[[], bool] = lambda: False) -> bool:
    """Waits until the server is ready, logging every ~5 s so the user sees that something is happening."""
    t0 = time.monotonic()
    last = -5.0
    while time.monotonic() - t0 < timeout and not cancelled():
        if service_up(base, backend):
            on_log(f"Dienst bereit nach {time.monotonic() - t0:.0f} s ({base})")
            return True
        if time.monotonic() - t0 - last >= 5:
            last = time.monotonic() - t0
            on_log(f"... warte auf {base} ({last:.0f} s)")
        time.sleep(0.5)
    return False


def server_props(base: str) -> dict:
    """llama-server /props (model path, n_ctx of slot 0) - {} if unavailable."""
    try:
        return http_json(f"{base}/props", timeout=3)
    except RuntimeError:
        return {}


def open_console(logfile: str | Path, title: str = "Tuning Live-Log") -> bool:
    """Opens a real console window that follows the log file (PowerShell Get-Content -Wait). Windows only."""
    if sys.platform != "win32":
        return False
    p = str(logfile).replace("'", "''")
    cmd = ["cmd.exe", "/k", f"title {title} & powershell -NoProfile -Command "
           f"\"Get-Content -LiteralPath '{p}' -Wait -Tail 300 -Encoding UTF8\""]
    try:
        subprocess.Popen(cmd, creationflags=0x00000010)          # CREATE_NEW_CONSOLE
        return True
    except OSError:
        return False


def _log_step(on_log: Callable[[str], None], step: "ProbeStep") -> None:
    if step.good:
        on_log(f"  => OK: Kontext {step.ctx} passt ({step.gen_tps:.1f} tok/s, GPU {step.gpu_pct:.0f} %, VRAM {step.vram_mb} MB)")
    else:
        on_log(f"  => NICHT OK: Kontext {step.ctx}: {step.error or step.note or 'unbekannt'}"
               + (f" ({step.gen_tps:.1f} tok/s, GPU {step.gpu_pct:.0f} %)" if step.ok else ""))


# ------------------------------------------------------------------ probes
@dataclass
class ProbeStep:
    ctx: int
    ok: bool = False
    good: bool = False             # fits fully on the GPU AND speed did not collapse
    gen_tps: float = 0.0
    prompt_tps: float = 0.0
    gpu_pct: float = 0.0           # Ollama: size_vram / size
    vram_mb: int = 0
    load_s: float = 0.0
    error: str = ""
    note: str = ""


@dataclass
class ProbeResult:
    backend: str
    model: str
    kv_type: str
    max_ctx: int = 0
    baseline_tps: float = 0.0
    steps: list = field(default_factory=list)
    stopped: str = ""              # why the walk ended
    when: float = 0.0


def probe_candidates(spec: ModelSpec, start: int = 4096) -> list[int]:
    top = spec.ctx_train or 131072
    return [c for c in CTX_BLOCKS if start <= c <= top]


def _classify(step: ProbeStep, baseline: float, min_ratio: float, gpu_needed: bool) -> None:
    step.good = step.ok and (not gpu_needed or step.gpu_pct >= 99.0) and \
        (baseline <= 0 or step.gen_tps >= min_ratio * baseline)
    if step.ok and not step.good:
        step.note = ("läuft teilweise auf der CPU" if gpu_needed and step.gpu_pct < 99.0
                     else f"Tempo unter {int(min_ratio * 100)} % der Basis")


def probe_ollama(base: str, model: str, spec: ModelSpec, *, kv_type: str = "f16", candidates: list[int] | None = None,
                 fill_frac: float = 0.0, min_tps_ratio: float = 0.5, granularity: int = 4096,
                 on_step: Callable[[ProbeStep], None] = lambda s: None,
                 on_status: Callable[[str], None] = lambda s: None,
                 on_log: Callable[[str], None] = lambda s: None,
                 cancelled: Callable[[], bool] = lambda: False) -> ProbeResult:
    """Load the model with growing num_ctx; the last size that stays 100 % on the GPU at >= min_tps_ratio of the
    baseline speed is the maximum. Ollama allocates the KV cache at load time, so a fit is decided by the load
    itself (/api/ps size_vram vs size). After the first failure the gap to the last good size is bisected down
    to `granularity` tokens."""
    res = ProbeResult("ollama", model, kv_type, when=time.time())
    cands = candidates or probe_candidates(spec)

    def unload() -> None:
        try:
            http_json(f"{base}/api/generate", data={"model": model, "keep_alive": 0}, timeout=30)
        except RuntimeError:
            pass

    def run(ctx: int) -> ProbeStep:
        step = ProbeStep(ctx)
        on_status(f"Teste Kontext {ctx} ...")
        on_log(f"--- Ollama: Test mit num_ctx={ctx} ---")
        on_log("Entlade Modell (damit es mit neuem Kontext frisch geladen wird) ...")
        unload()
        prompt = make_prompt(int(ctx * fill_frac)) if fill_frac > 0 else make_prompt(0)
        on_log(f"Sende Anfrage (Prompt ca. {int(ctx * fill_frac) if fill_frac > 0 else 30} Tokens, max 48 Antwort-Tokens); Laden kann dauern ...")
        final = None
        try:
            for ev in ollama_chat_stream(base, model, prompt, {"num_ctx": ctx, "num_predict": 48, "temperature": 0},
                                         cancelled=cancelled, keep_alive="2m", timeout=600):
                if ev["type"] == "final":
                    final = ev
                elif ev["type"] == "cancelled":
                    step.error = "abgebrochen"
                    return step
        except (RuntimeError, OSError, ValueError) as exc:
            step.error = str(exc)[:200]
            return step
        if not final:
            step.error = "keine Antwort"
            return step
        step.ok = True
        step.gen_tps, step.prompt_tps, step.load_s = final["gen_tps"], final["prompt_tps"], final["load_s"]
        on_log(f"Antwort: Generierung {step.gen_tps:.1f} tok/s, Prompt {step.prompt_tps:.0f} tok/s, Laden {step.load_s:.1f} s")
        try:
            for m in http_json(f"{base}/api/ps", timeout=5).get("models", []):
                if m.get("name") == model or m.get("model") == model:
                    size, vram = int(m.get("size") or 0), int(m.get("size_vram") or 0)
                    step.gpu_pct = 100.0 * vram / size if size else 0.0
                    step.vram_mb = int(vram / MIB)
                    on_log(f"/api/ps: {size / GIB:.2f} GB geladen, davon {vram / GIB:.2f} GB im VRAM = {step.gpu_pct:.0f} % GPU")
                    break
        except RuntimeError:
            pass
        return step

    last_good, first_bad = 0, 0
    for ctx in cands:
        if cancelled():
            res.stopped = "abgebrochen"
            break
        step = run(ctx)
        _classify(step, res.baseline_tps, min_tps_ratio, gpu_needed=True)
        if step.good and not res.baseline_tps:
            res.baseline_tps = step.gen_tps
            on_log(f"Basis-Tempo festgelegt: {res.baseline_tps:.1f} tok/s (Grenze: {int(min_tps_ratio * 100)} % davon)")
        res.steps.append(step)
        _log_step(on_log, step)
        on_step(step)
        if step.error == "abgebrochen":
            res.stopped = "abgebrochen"
            break
        if step.good:
            last_good = ctx
            continue
        first_bad = ctx
        res.stopped = step.error or step.note
        break
    else:
        if not res.stopped:
            res.stopped = "Modellgrenze erreicht"
    if first_bad and last_good and granularity:         # bisect the gap
        on_log(f"Grenze liegt zwischen {last_good} (ok) und {first_bad} (nicht ok) - grenze genauer ein ...")
        lo, hi = last_good, first_bad
        while hi - lo > granularity and not cancelled():
            mid = (lo + hi) // 2 // 1024 * 1024
            if mid <= lo or mid >= hi:
                break
            step = run(mid)
            _classify(step, res.baseline_tps, min_tps_ratio, gpu_needed=True)
            res.steps.append(step)
            _log_step(on_log, step)
            on_step(step)
            if step.good:
                lo = mid
            else:
                hi = mid
        last_good = lo
    res.max_ctx = last_good
    unload()
    return res


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def probe_llamacpp(exe_cmd: list[str], model_path: str, spec: ModelSpec, *, kv_type: str = "f16",
                   candidates: list[int] | None = None, fill_frac: float = 0.0, min_tps_ratio: float = 0.5,
                   granularity: int = 4096, ngl: int = 99, batch: int = 0, total_vram_mb: int = 0,
                   gpu_used_mb: Callable[[], int] = lambda: 0, load_timeout: float = 240.0,
                   on_step: Callable[[ProbeStep], None] = lambda s: None,
                   on_status: Callable[[str], None] = lambda s: None,
                   on_log: Callable[[str], None] = lambda s: None,
                   cancelled: Callable[[], bool] = lambda: False) -> ProbeResult:
    """llama.cpp cannot change -c at runtime, so each size is a throw-away llama-server on a private port:
    start with `-c N -ngl 99 --fit off` (no silent shrinking), wait for /health, run one short completion,
    read `timings`, kill it. A start that dies (CUDA OOM) or a VRAM use above the card = does not fit.
    `exe_cmd` = [llama-server.exe] (list so tests can inject a fake)."""
    res = ProbeResult("llamacpp", model_path, kv_type, when=time.time())
    cands = candidates or probe_candidates(spec)

    def run(ctx: int) -> ProbeStep:
        step = ProbeStep(ctx)
        port = free_port()
        cmd = [*exe_cmd, "-m", model_path, "-c", str(ctx), "-ngl", str(ngl), "--fit", "off", "-np", "1",
               "--flash-attn", "on", "--host", "127.0.0.1", "--port", str(port),
               "--cache-type-k", kv_type, "--cache-type-v", kv_type]
        if batch:
            cmd += ["-b", str(batch)]
        on_status(f"Teste Kontext {ctx} (llama-server startet) ...")
        on_log(f"--- llama.cpp: Test mit -c {ctx} ---")
        on_log("Starte: " + " ".join(f'"{c}"' if " " in c else c for c in cmd))
        tail: collections.deque = collections.deque(maxlen=40)
        t0 = time.monotonic()
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, creationflags=_NOWIN)
        except OSError as exc:
            step.error = str(exc)
            on_log(f"Start fehlgeschlagen: {exc}")
            return step

        def pump() -> None:
            for raw in iter(proc.stdout.readline, b""):
                line = raw.decode("utf-8", "replace").rstrip()
                if line:
                    tail.append(line)
                    on_log("   llama-server | " + line[:200])
        reader = threading.Thread(target=pump, daemon=True, name="probe-out")
        reader.start()
        base = f"http://127.0.0.1:{port}"
        try:
            ready = False
            last_note = 0.0
            while time.monotonic() - t0 < load_timeout and not cancelled():
                if time.monotonic() - t0 - last_note >= 5:
                    last_note = time.monotonic() - t0
                    on_log(f"... warte auf /health ({last_note:.0f} s)")
                if proc.poll() is not None:
                    break
                try:
                    with http_open(f"{base}/health", timeout=2) as r:
                        if r.status == 200:
                            ready = True
                            break
                except (urllib.error.URLError, OSError):
                    pass
                time.sleep(0.4)
            if cancelled():
                step.error = "abgebrochen"
                return step
            if not ready:
                step.error = ("Server beendet: " if proc.poll() is not None else "Zeitüberschreitung: ") + " | ".join(list(tail)[-3:])[:220]
                return step
            step.load_s = time.monotonic() - t0
            on_log(f"Server bereit nach {step.load_s:.1f} s; sende Test-Anfrage (48 Tokens) ...")
            step.vram_mb = gpu_used_mb()
            final = None
            prompt = make_prompt(int(ctx * fill_frac)) if fill_frac > 0 else make_prompt(0)
            try:
                for ev in llama_completion_stream(base, prompt, {"n_predict": 48, "temperature": 0, "cache_prompt": False},
                                                  cancelled=cancelled, timeout=600):
                    if ev["type"] == "final":
                        final = ev
            except (RuntimeError, OSError, ValueError) as exc:
                step.error = str(exc)[:200]
                return step
            if not final:
                step.error = "keine Antwort"
                return step
            step.ok = True
            step.gen_tps, step.prompt_tps = final["gen_tps"], final["prompt_tps"]
            on_log(f"Antwort: Generierung {step.gen_tps:.1f} tok/s, Prompt {step.prompt_tps:.0f} tok/s; VRAM belegt {step.vram_mb} MB")
            step.gpu_pct = 100.0
            if total_vram_mb and step.vram_mb > total_vram_mb * 0.97:
                step.gpu_pct = 0.0
                step.note = "VRAM voll"
            return step
        finally:
            try:
                proc.kill()
                proc.wait(10)
            except (OSError, subprocess.SubprocessError):
                pass
            reader.join(2)
            on_log("Test-Server beendet.")

    last_good, first_bad = 0, 0
    for ctx in cands:
        if cancelled():
            res.stopped = "abgebrochen"
            break
        step = run(ctx)
        _classify(step, res.baseline_tps, min_tps_ratio, gpu_needed=bool(total_vram_mb))
        if step.good and not res.baseline_tps:
            res.baseline_tps = step.gen_tps
        res.steps.append(step)
        _log_step(on_log, step)
        on_step(step)
        if step.error == "abgebrochen":
            res.stopped = "abgebrochen"
            break
        if step.good:
            last_good = ctx
            continue
        first_bad = ctx
        res.stopped = step.error or step.note
        break
    else:
        if not res.stopped:
            res.stopped = "Modellgrenze erreicht"
    if first_bad and last_good and granularity:
        on_log(f"Grenze liegt zwischen {last_good} (ok) und {first_bad} (nicht ok) - grenze genauer ein ...")
        lo, hi = last_good, first_bad
        while hi - lo > granularity and not cancelled():
            mid = (lo + hi) // 2 // 1024 * 1024
            if mid <= lo or mid >= hi:
                break
            step = run(mid)
            _classify(step, res.baseline_tps, min_tps_ratio, gpu_needed=bool(total_vram_mb))
            res.steps.append(step)
            _log_step(on_log, step)
            on_step(step)
            if step.good:
                lo = mid
            else:
                hi = mid
        last_good = lo
    res.max_ctx = last_good
    return res


# ------------------------------------------------------------------ persistence
class TuningStore:
    """Measured maxima, remembered per backend / model / KV type / GPU (tuning.json beside config.json)."""

    def __init__(self, path: Path):
        self.path = Path(path)
        try:
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.data = {}
        self.data.setdefault("probes", {})

    @staticmethod
    def key(backend: str, model: str, kv_type: str, hw: Hardware) -> str:
        return f"{backend}|{model}|{kv_type}|{hw.key}"

    def get(self, backend: str, model: str, kv_type: str, hw: Hardware) -> dict | None:
        return self.data["probes"].get(self.key(backend, model, kv_type, hw))

    def put(self, res: ProbeResult, hw: Hardware) -> None:
        self.data["probes"][self.key(res.backend, res.model, res.kv_type, hw)] = {
            "max_ctx": res.max_ctx, "baseline_tps": res.baseline_tps, "when": res.when, "stopped": res.stopped,
            "steps": [asdict(s) for s in res.steps]}
        self.path.write_text(json.dumps(self.data, indent=2, ensure_ascii=False), encoding="utf-8")


    # ---------------------------------------------------------------- tuning profiles (Auto-Tune results / user saved)
    @staticmethod
    def pkey(backend: str, model: str, hw: Hardware) -> str:
        return f"{backend}|{model}|{hw.key}"

    def profiles(self, backend: str, model: str, hw: Hardware) -> list[dict]:
        return list(self.data.get("profiles", {}).get(self.pkey(backend, model, hw), []))

    def save_profile(self, backend: str, model: str, hw: Hardware, prof: dict, make_default: bool = False) -> None:
        lst = self.data.setdefault("profiles", {}).setdefault(self.pkey(backend, model, hw), [])
        lst[:] = [p for p in lst if p.get("name") != prof["name"]]
        if make_default:
            for p in lst:
                p["default"] = False
        prof["default"] = bool(make_default or not lst)
        lst.append(prof)
        self._write()

    def delete_profile(self, backend: str, model: str, hw: Hardware, name: str) -> None:
        lst = self.data.setdefault("profiles", {}).setdefault(self.pkey(backend, model, hw), [])
        lst[:] = [p for p in lst if p.get("name") != name]
        if lst and not any(p.get("default") for p in lst):
            lst[-1]["default"] = True
        self._write()

    def set_default(self, backend: str, model: str, hw: Hardware, name: str) -> None:
        for p in self.data.setdefault("profiles", {}).setdefault(self.pkey(backend, model, hw), []):
            p["default"] = p.get("name") == name
        self._write()

    def default_profile(self, backend: str, model: str, hw: Hardware) -> dict | None:
        return next((p for p in self.profiles(backend, model, hw) if p.get("default")), None)

    def _write(self) -> None:
        self.path.write_text(json.dumps(self.data, indent=2, ensure_ascii=False), encoding="utf-8")


def apply_llamacpp(cfg, values: dict) -> dict:
    """Server-level llama.cpp settings -> config.json `llamacpp` (read by LlamaCppModule.build at next start)."""
    sect = cfg.data.setdefault("llamacpp", {})
    sect["ctx"] = int(values["ctx"])
    sect["ngl"] = "auto" if int(values["gpu_layers"]) < 0 else int(values["gpu_layers"])
    sect["kv_type"] = str(values["kv_type"])
    sect["batch"] = int(values["batch"])
    sect["parallel"] = int(values["parallel"])
    cfg.save()
    return sect


def apply_ollama_server(cfg, values: dict) -> dict:
    """Server-level Ollama settings -> config.json `ollama` (become OLLAMA_* env vars at next start)."""
    sect = cfg.data.setdefault("ollama", {})
    sect["context_length"] = int(values["ctx"])
    sect["kv_cache_type"] = str(values["kv_type"])
    sect["flash_attention"] = True if values["kv_type"] != "f16" else sect.get("flash_attention", True)
    sect["num_parallel"] = int(values["parallel"])
    cfg.save()
    return sect


def ollama_env(section: dict) -> dict[str, str]:
    """config.json `ollama` section -> environment for `ollama serve` (only what the user actually set)."""
    env: dict[str, str] = {}
    if section.get("context_length"):
        env["OLLAMA_CONTEXT_LENGTH"] = str(int(section["context_length"]))
    if section.get("kv_cache_type") and section["kv_cache_type"] != "f16":
        env["OLLAMA_KV_CACHE_TYPE"] = str(section["kv_cache_type"])
    if section.get("flash_attention"):
        env["OLLAMA_FLASH_ATTENTION"] = "1"
    if section.get("num_parallel"):
        env["OLLAMA_NUM_PARALLEL"] = str(int(section["num_parallel"]))
    return env


def llamacpp_extra_args(section: dict) -> list[str]:
    """config.json `llamacpp` section -> extra llama-server arguments (only what the user actually set)."""
    args: list[str] = []
    kv = section.get("kv_type")
    if kv and kv != "f16":
        args += ["--cache-type-k", str(kv), "--cache-type-v", str(kv)]
    if section.get("batch"):
        args += ["-b", str(int(section["batch"]))]
    if section.get("parallel"):
        args += ["-np", str(int(section["parallel"]))]
    return args


_NAME_OK = re.compile(r"[^a-zA-Z0-9._:-]+")


def modelfile_text(base_model: str, options: dict) -> str:
    """Modelfile for a tuned variant: `FROM <base>` + PARAMETER lines (request-level options only)."""
    lines = [f"FROM {base_model}"]
    for k in ("num_ctx", "num_predict", "temperature", "top_p", "top_k", "min_p", "repeat_penalty", "num_gpu", "num_batch"):
        if k in options:
            lines.append(f"PARAMETER {k} {options[k]}")
    return "\n".join(lines) + "\n"


def variant_name(base_model: str, ctx: int) -> str:
    stem = base_model.split(":")[0]
    return _NAME_OK.sub("-", f"{stem}-ctx{ctx // 1024}k") + ":latest"


def create_variant(exe: str, host_port: str, base_model: str, new_name: str, options: dict, timeout: float = 180) -> str:
    """`ollama create <new_name> -f Modelfile` against the running server. Returns the CLI output."""
    with tempfile.TemporaryDirectory() as td:
        mf = Path(td) / "Modelfile"
        mf.write_text(modelfile_text(base_model, options), encoding="utf-8")
        env = {**os.environ, "OLLAMA_HOST": host_port}
        r = subprocess.run([exe, "create", new_name, "-f", str(mf)], capture_output=True, text=True, timeout=timeout,
                           env=env, creationflags=_NOWIN)
        out = (r.stdout + r.stderr).strip()
        if r.returncode != 0:
            raise RuntimeError(out or f"ollama create failed ({r.returncode})")
        return out


def list_gguf(folder: Path, limit: int = 400) -> list[Path]:
    out: list[Path] = []
    try:
        for p in folder.rglob("*.gguf"):
            if "mmproj" in p.name.lower():
                continue
            out.append(p)
            if len(out) >= limit:
                break
    except OSError:
        pass
    return sorted(out)
