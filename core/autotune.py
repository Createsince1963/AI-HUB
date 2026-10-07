"""Auto-Tune: finds the sweet spot of one model on this hardware in at most 3 measured rounds.

Round 1 starts from an estimate (model metadata + VRAM), every further round adjusts the server parameters
from what the previous round measured (fits fully on the GPU? speed? VRAM headroom?). The planning is a pure
function (`initial_values`, `next_values`, `pick_best`) so it can be tested without a GPU; the actual measurement
is injected by the caller (`measure(values, fill_frac) -> ProbeStep`).

Goals    : speed (fast generation, context only as large as useful), balanced, context (largest context that
           still runs completely on the GPU at an acceptable speed).
Levels   : quick (short prompt), medium (context 25 % filled), thorough (context 50 % filled - realistic speed
           at a full context, slower).
"""
from __future__ import annotations

import re
import time
from dataclasses import asdict, dataclass, field
from typing import Callable

from . import tuning as T

GOALS = {"balanced": "Ausgewogen", "speed": "Max. Tempo", "context": "Max. Kontext"}
LEVELS = {"quick": ("Schnell", 0.0), "medium": ("Mittel", 0.25), "thorough": ("Gründlich", 0.5)}
MAX_ROUNDS = 3
MIN_TPS = {"speed": 0.0, "balanced": 0.6, "context": 0.35}     # share of the round-1 speed a later round must keep
ABS_MIN_TPS = 8.0                                               # below this a model is not usable interactively
SAMPLING_KEYS = ("temperature", "top_p", "top_k", "min_p", "repeat_penalty")


@dataclass
class Round:
    no: int
    values: dict
    ok: bool = False
    good: bool = False
    gen_tps: float = 0.0
    prompt_tps: float = 0.0
    vram_mb: int = 0
    gpu_pct: float = 0.0
    note: str = ""
    decision: str = ""             # what the planner concluded after this round


@dataclass
class AutoTuneResult:
    backend: str
    model: str
    goal: str
    level: str
    rounds: list = field(default_factory=list)
    best: Round | None = None
    stopped: str = ""
    when: float = 0.0

    def as_dict(self) -> dict:
        d = asdict(self)
        return d


# ------------------------------------------------------------------ planning (pure)
def _snap_down(ctx: int, spec: T.ModelSpec) -> int:
    top = spec.ctx_train or 131072
    blocks = [b for b in T.CTX_BLOCKS if T.MIN_USEFUL_CTX <= b <= min(ctx, top)]
    return blocks[-1] if blocks else T.MIN_USEFUL_CTX


def _block_step(ctx: int, spec: T.ModelSpec, up: bool) -> int:
    top = spec.ctx_train or 131072
    blocks = [b for b in T.CTX_BLOCKS if T.MIN_USEFUL_CTX <= b <= top]
    if up:
        return next((b for b in blocks if b > ctx), ctx)
    lower = [b for b in blocks if b < ctx]
    return lower[-1] if lower else ctx


def sampling_for(name: str, defaults: dict) -> dict:
    """Sampling does not change speed - take the model's own recommendation, code models a bit more deterministic."""
    out = {k: defaults[k] for k in SAMPLING_KEYS if k in defaults}
    if re.search(r"coder|code|devstral|starcoder", name or "", re.I):
        # 1.05 = mild guard against loops; higher values hurt code, which legitimately repeats names and syntax
        out.update({"temperature": 0.2, "top_p": 0.9, "repeat_penalty": 1.05})
    return out


def initial_values(goal: str, backend: str, spec: T.ModelSpec, hw: T.Hardware, current: dict) -> dict:
    """Round 1: estimate from model + VRAM."""
    v = dict(current)
    v["parallel"] = 1
    est = {kv: T.max_ctx_estimate(spec, hw, kv) for kv in T.KV_CHOICES}
    if backend == "ollama":                     # KV type is a server setting of Ollama - keep what the server runs
        kv = str(current.get("kv_type") or "f16")
    elif goal == "speed":
        kv = "f16"
    elif goal == "balanced":
        kv = "f16" if est["f16"] >= 32768 else "q8_0"
    else:
        kv = "q8_0" if est["q8_0"] >= 16384 else "q4_0"
    v["kv_type"] = kv
    e = est.get(kv) or 0
    if e <= 0:                                  # weights alone do not fit: offload only part of the layers
        layers = spec.n_layers + 1
        share = max(0.1, min(0.95, (hw.usable_vram_bytes - T.COMPUTE_BUFFER_MB * T.MIB) / max(1, spec.weights_bytes)))
        v["gpu_layers"] = max(1, int(layers * share * 0.9))
        v["ctx"] = 8192 if goal != "context" else 16384
    else:
        v["gpu_layers"] = -1
        target = {"speed": min(16384, e), "balanced": min(32768, int(e * 0.6)), "context": int(e * 0.9)}[goal]
        v["ctx"] = _snap_down(max(target, T.MIN_USEFUL_CTX), spec)
    v["batch"] = 2048 if e and e >= 16384 else 512
    v["predict"] = 1024 if goal == "speed" else 2048
    return v


def next_values(goal: str, backend: str, spec: T.ModelSpec, hw: T.Hardware, rounds: list[Round]) -> tuple[dict | None, str]:
    """Next round's values from the measured rounds, or (None, reason) when the sweet spot is reached."""
    last = rounds[-1]
    v = dict(last.values)
    base_tps = next((r.gen_tps for r in rounds if r.good), 0.0)
    if not last.good:                            # did not fit / spilled to the CPU / failed -> one step smaller
        smaller = _block_step(int(v["ctx"]), spec, up=False)
        if smaller < int(v["ctx"]):
            v["ctx"] = smaller
            return v, f"passte nicht ({last.note or 'Fehler'}) - Kontext auf {smaller} verkleinern"
        if backend != "ollama" and v.get("kv_type") == "f16":
            v["kv_type"] = "q8_0"
            return v, "passte nicht - KV-Cache auf q8_0 (halber Speicher)"
        if int(v.get("gpu_layers", -1)) != 0:
            layers = spec.n_layers + 1
            cur = layers if int(v.get("gpu_layers", -1)) < 0 else int(v["gpu_layers"])
            v["gpu_layers"] = max(1, int(cur * 0.75))
            return v, f"passte nicht - GPU-Layer auf {v['gpu_layers']} reduzieren (Rest läuft auf der CPU)"
        return None, "kleinste Einstellung passt nicht"
    ratio = MIN_TPS.get(goal, 0.5)
    if base_tps and last.gen_tps < max(ABS_MIN_TPS, ratio * base_tps) and len(rounds) > 1:
        v["ctx"] = _block_step(int(v["ctx"]), spec, up=False)
        return v, f"zu langsam ({last.gen_tps:.0f} tok/s) - Kontext zurück auf {v['ctx']}"
    used = last.vram_mb * T.MIB if last.vram_mb else T.estimate_memory(spec, hw, int(v["ctx"]), v.get("kv_type", "f16"))["total"]
    total = hw.vram_total_mb * T.MIB or used
    head = 1.0 - used / total if total else 0.0
    bigger = _block_step(int(v["ctx"]), spec, up=True)
    est_next = T.estimate_memory(spec, hw, bigger, v.get("kv_type", "f16"))
    if goal == "speed":
        if int(v.get("batch", 512)) < 2048 and head > 0.10:
            v["batch"] = 2048
            return v, "läuft - Batch auf 2048 für schnellere Prompt-Verarbeitung"
        return None, f"Sweetspot: {last.gen_tps:.0f} tok/s bei Kontext {v['ctx']}"
    want_head = 0.25 if goal == "balanced" else 0.06
    if bigger > int(v["ctx"]) and head > want_head and est_next["fits"]:
        v["ctx"] = bigger
        return v, f"läuft, {head * 100:.0f} % VRAM frei - Kontext auf {bigger} vergrößern"
    return None, f"Sweetspot: Kontext {v['ctx']} bei {last.gen_tps:.0f} tok/s, {head * 100:.0f} % VRAM frei"


def pick_best(goal: str, rounds: list[Round]) -> Round | None:
    good = [r for r in rounds if r.good]
    if not good:
        return None
    if goal == "speed":
        return max(good, key=lambda r: r.gen_tps)
    if goal == "context":
        return max(good, key=lambda r: (int(r.values["ctx"]), r.gen_tps))
    top_ctx = max(int(r.values["ctx"]) for r in good) or 1
    top_tps = max(r.gen_tps for r in good) or 1.0
    return max(good, key=lambda r: 0.5 * int(r.values["ctx"]) / top_ctx + 0.5 * r.gen_tps / top_tps)


# ------------------------------------------------------------------ run
def run(backend: str, model: str, spec: T.ModelSpec, hw: T.Hardware, current: dict, *, goal: str, level: str,
        measure: Callable[[dict, float], T.ProbeStep], model_defaults: dict | None = None,
        on_round: Callable[[Round], None] = lambda r: None, on_progress: Callable[[int, str], None] = lambda p, t: None,
        on_log: Callable[[str], None] = lambda s: None, cancelled: Callable[[], bool] = lambda: False) -> AutoTuneResult:
    res = AutoTuneResult(backend, model, goal, level, when=time.time())
    fill = LEVELS[level][1]
    values = initial_values(goal, backend, spec, hw, current)
    values.update(sampling_for(model, model_defaults or {}))
    on_log(f"=== Auto-Tune: Ziel {GOALS[goal]}, Gründlichkeit {LEVELS[level][0]}, max {MAX_ROUNDS} Durchläufe ===")
    for no in range(1, MAX_ROUNDS + 1):
        if cancelled():
            res.stopped = "abgebrochen"
            break
        desc = (f"Kontext {values['ctx']}, KV {values['kv_type']}, GPU-Layer "
                f"{'Auto' if int(values['gpu_layers']) < 0 else values['gpu_layers']}, Batch {values['batch']}")
        on_progress(int((no - 1) * 100 / MAX_ROUNDS), f"Durchlauf {no}/{MAX_ROUNDS}: {desc}")
        on_log(f"--- Durchlauf {no}: {desc} ---")
        step = measure(dict(values), fill)
        r = Round(no, dict(values), ok=step.ok, good=step.good, gen_tps=step.gen_tps, prompt_tps=step.prompt_tps,
                  vram_mb=step.vram_mb, gpu_pct=step.gpu_pct, note=step.error or step.note)
        res.rounds.append(r)
        if step.error == "abgebrochen" or cancelled():
            r.decision = "abgebrochen"
            on_round(r)
            res.stopped = "abgebrochen"
            break
        nxt, why = next_values(goal, backend, spec, hw, res.rounds)
        if nxt is not None and no == MAX_ROUNDS:
            why = "Limit 3 Durchläufe - " + ("übernommen" if r.good else "bester vorheriger Durchlauf wird übernommen")
        r.decision = why
        on_round(r)
        on_log(f"Durchlauf {no}: {'passt' if r.good else 'passt nicht'}, {r.gen_tps:.1f} tok/s -> {why}")
        if nxt is None:
            res.stopped = why
            break
        if no == MAX_ROUNDS:
            res.stopped = "3 Durchläufe erreicht - bester Durchlauf wird übernommen"
            break
        values = nxt
    res.best = pick_best(goal, res.rounds)
    on_progress(100, "Fertig" if res.best else "Kein passender Durchlauf")
    return res


def default_profile_name(model: str, goal: str, existing: list[str]) -> str:
    stem = re.sub(r"\.gguf$", "", model.split("/")[-1].split(":")[0], flags=re.I)
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", stem)[:40]
    n = 1
    while f"{stem}-{goal}-p{n}" in existing:
        n += 1
    return f"{stem}-{goal}-p{n}"
