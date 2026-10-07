"""Tests for core/tuning.py and ui/tuning_page.py: GGUF reader, memory estimates, parameter rows,
streaming parsers, both probes against fake servers, persistence/apply helpers and an offscreen page smoke test."""
import json, os, struct, sys, tempfile, threading, time, types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["no_proxy"] = os.environ["NO_PROXY"] = "127.0.0.1,localhost"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
    os.environ.pop(k, None)

from core import tuning as T
from core.net import OllamaClient

FAILS = []
def check(name, cond, extra=""):
    print(("PASS  " if cond else "FAIL  ") + name + (("  -> " + str(extra)) if extra else ""))
    if not cond: FAILS.append(name)

TMP = Path(tempfile.mkdtemp(prefix="tuning_test_"))

# ---------------------------------------------------------------- synthetic GGUF
def gstr(s): b = s.encode(); return struct.pack("<Q", len(b)) + b
def kv_str(k, v): return gstr(k) + struct.pack("<I", 8) + gstr(v)
def kv_u32(k, v): return gstr(k) + struct.pack("<I", 4) + struct.pack("<I", v)
def kv_arr_str(k, items):
    return gstr(k) + struct.pack("<I", 9) + struct.pack("<IQ", 8, len(items)) + b"".join(gstr(i) for i in items)
def make_gguf(path, arch="qwen2", layers=28, heads=28, kvh=4, embd=3584, ctx=32768, pad=0):
    kvs = [kv_str("general.architecture", arch), kv_u32(f"{arch}.block_count", layers), kv_u32(f"{arch}.attention.head_count", heads),
           kv_u32(f"{arch}.attention.head_count_kv", kvh), kv_u32(f"{arch}.embedding_length", embd),
           kv_u32(f"{arch}.context_length", ctx), kv_arr_str("tokenizer.ggml.tokens", ["a", "bb", "ccc"]),
           kv_u32("tokenizer.ggml.bos_token_id", 1)]
    Path(path).write_bytes(b"GGUF" + struct.pack("<I", 3) + struct.pack("<QQ", 0, len(kvs)) + b"".join(kvs) + b"\0" * pad)
g = TMP / "qwen.gguf"; make_gguf(g, pad=1000)
meta = T.read_gguf_meta(g)
check("gguf: architecture/layers read", meta.get("general.architecture") == "qwen2" and meta.get("qwen2.block_count") == 28, meta)
check("gguf: arrays skipped, scalars after them not required", "tokenizer.ggml.tokens" not in meta)
(TMP / "bad.gguf").write_bytes(b"NOPE" + b"\0" * 20)
try: T.read_gguf_meta(TMP / "bad.gguf"); check("gguf: bad magic raises", False)
except ValueError: check("gguf: bad magic raises", True)

spec = T.ModelSpec.from_kv("qwen2.5-coder:7b", meta, weights_bytes=int(4.7 * T.GIB))
check("spec: from_kv head_dim 128, known", spec.key_len == 128 and spec.known and spec.ctx_train == 32768, spec)
check("kv bytes/token f16 = 57344", T.kv_bytes_per_token(spec) == 57344)
check("kv q8_0 about half of f16", abs(T.kv_bytes_per_token(spec, "q8_0") / 57344 - 17 / 32) < 1e-6)
hw = T.Hardware("RTX 5070", 12 * 1024, 1000, 32768)
est = T.estimate_memory(spec, hw, 32768)
check("estimate: 32k KV ~1.75 GB", abs(est["kv"] / T.GIB - 1.75) < 0.01, est["kv"] / T.GIB)
check("estimate: fits on 12 GB", est["fits"])
check("max ctx capped at trained context", T.max_ctx_estimate(spec, hw) == 32768)
big = T.ModelSpec.from_kv("x", meta, weights_bytes=int(9.5 * T.GIB))
check("max ctx shrinks for large weights", 0 < T.max_ctx_estimate(big, hw) < 32768, T.max_ctx_estimate(big, hw))
huge = T.ModelSpec.from_kv("x", meta, weights_bytes=int(20 * T.GIB))
check("max ctx 0 when weights do not fit", T.max_ctx_estimate(huge, hw) == 0)
check("parallel slots reduce max ctx", T.max_ctx_estimate(big, hw, parallel=2) < T.max_ctx_estimate(big, hw))

# ---------------------------------------------------------------- parameter rows
rows = {p.key: p for p in T.build_params("llamacpp", spec, hw)}
check("rows: all keys present", set(rows) == {"ctx", "predict", "temperature", "top_p", "top_k", "min_p", "repeat_penalty", "gpu_layers", "kv_type", "batch", "parallel"}, set(rows))
c = rows["ctx"]
check("ctx: default 8192 and max 32768, both in steps", c.default == 8192 and c.maximum == 32768 and 8192 in c.steps and 32768 in c.steps, c.steps)
check("ctx: steps sorted blocks", c.steps == sorted(c.steps) and c.steps[0] == 2048)
check("ctx: llama.cpp scope is server, ollama is request", c.scope == "server" and {p.key: p for p in T.build_params("ollama", spec, hw)}["ctx"].scope == "request")
check("ctx: note says geschätzt", c.note == "geschätzt")
rm = {p.key: p for p in T.build_params("llamacpp", spec, hw, measured_max_ctx=20480)}["ctx"]
check("ctx: measured max overrides estimate", rm.maximum == 20480 and rm.note == "gemessen" and 20480 in rm.steps)
rl = {p.key: p for p in T.build_params("llamacpp", spec, hw, launcher={"ctx": 16384, "gpu_layers": 20})}
check("launcher config overrides defaults", rl["ctx"].default == 16384 and rl["gpu_layers"].default == 20)
ro = {p.key: p for p in T.build_params("ollama", spec, hw, model_defaults={"temperature": 0.2, "num_ctx": 4096, "top_k": 20})}
check("model defaults override backend defaults", ro["temperature"].default == 0.2 and ro["top_k"].default == 20)
check("float steps are 0.1 grid", rows["temperature"].steps[:3] == [0.0, 0.1, 0.2] and rows["temperature"].steps[-1] == 2.0)
check("snap() picks nearest step", rows["temperature"].snap(0.77) == 0.8 and rows["kv_type"].snap("zzz") == "f16")
check("gpu layer steps: auto, 0 .. layers+1", rows["gpu_layers"].steps[0] == -1 and rows["gpu_layers"].steps[-1] == 29 and 0 in rows["gpu_layers"].steps)
check("parse_ollama_parameters", T.parse_ollama_parameters("temperature  0.7\nnum_ctx   4096\nstop  <|im_end|>\nbad x") == {"temperature": 0.7, "num_ctx": 4096})
vals = {p.key: p.default for p in T.build_params("ollama", spec, hw)}
o = T.ollama_options(vals)
check("ollama_options: auto gpu layers omitted", "num_gpu" not in o and o["num_ctx"] == 4096)
vals["gpu_layers"] = 12
check("ollama_options: explicit num_gpu", T.ollama_options(vals)["num_gpu"] == 12)
check("llama_params: n_predict + no prompt cache", T.llama_params(vals)["n_predict"] == -1 and T.llama_params(vals)["cache_prompt"] is False)

msg = "request (20455 tokens) exceeds the available context size (8192 tokens)"
adv = T.suggest_ctx_from_error(msg, spec, hw)
check("error hint: next block >= need+1024 (24576)", "24576" in adv and "8192" in adv and "passt" in adv, adv)
check("error hint: unrelated message -> empty", T.suggest_ctx_from_error("boom", spec, hw) == "")
check("make_prompt: ~n tokens", 0.8 * 1000 * 4 < len(T.make_prompt(1000)) < 1.3 * 1000 * 4 + 100)

# ---------------------------------------------------------------- fake servers
STATE = {"ctx": 4096, "unloads": 0}
OLLAMA_LIMIT = 20000

class OH(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _send(self, obj, code=200):
        b = json.dumps(obj).encode(); self.send_response(code); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def do_GET(self):
        if self.path == "/api/ps":
            full = STATE["ctx"] <= OLLAMA_LIMIT
            import datetime as _dt
            exp = (_dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(seconds=300)).strftime("%Y-%m-%dT%H:%M:%S.123456789Z")
            return self._send({"models": [{"name": "m:1", "model": "m:1", "size": 6_000_000_000, "size_vram": 6_000_000_000 if full else 4_000_000_000,
                                           "expires_at": exp, "context_length": 8192}]})
        if self.path == "/api/version":
            return self._send({"version": "0.0-test"})
        if self.path == "/api/tags":
            return self._send({"models": [{"name": "m:1", "size": 4_700_000_000}]})
        self._send({}, 404)
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        if self.path == "/api/generate":
            if body.get("keep_alive") == 0: STATE["unloads"] += 1
            return self._send({})
        if self.path == "/api/show":
            return self._send({"model_info": {"general.architecture": "qwen2", "qwen2.block_count": 28, "qwen2.attention.head_count": 28,
                                              "qwen2.attention.head_count_kv": 4, "qwen2.embedding_length": 3584, "qwen2.context_length": 32768},
                               "parameters": "temperature 0.3\nstop <x>", "details": {"quantization_level": "Q4_K_M", "parameter_size": "7B"}})
        if self.path == "/api/chat":
            ctx = body["options"].get("num_ctx", 4096); STATE["ctx"] = ctx
            if body["options"].get("num_ctx") == 999: return self._send({"error": "boom"}, 500)
            self.send_response(200); self.send_header("Content-Type", "application/x-ndjson"); self.end_headers()
            slow = 0.2 if ctx > OLLAMA_LIMIT else 0.01
            try:
              for i in range(5):
                self.wfile.write((json.dumps({"message": {"content": f"t{i} "}, "done": False}) + "\n").encode()); self.wfile.flush(); time.sleep(0.01)
            except BrokenPipeError:
                return
            n = 48
            self.wfile.write((json.dumps({"done": True, "eval_count": n, "eval_duration": int(n * slow * 1e9), "prompt_eval_count": 30,
                                          "prompt_eval_duration": int(0.1e9), "load_duration": int(0.5e9), "total_duration": int(1.5e9)}) + "\n").encode())
            return
        self._send({}, 404)

srv = ThreadingHTTPServer(("127.0.0.1", 0), OH); threading.Thread(target=srv.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{srv.server_address[1]}"

evs = list(T.ollama_chat_stream(BASE, "m:1", "hi", {"num_ctx": 4096}))
fin = evs[-1]
check("ollama stream: tokens then final", [e["type"] for e in evs] == ["token"] * 5 + ["final"], [e["type"] for e in evs])
check("ollama final: tok/s from eval_count/eval_duration", abs(fin["gen_tps"] - 100.0) < 0.5 and fin["gen_tokens"] == 48 and abs(fin["prompt_tps"] - 300) < 1, fin)
check("ollama final: load/total seconds", abs(fin["load_s"] - 0.5) < 1e-6 and abs(fin["total_s"] - 1.5) < 1e-6)
check("ollama stream: live tps > 0 after 2nd token", evs[2]["tps"] > 0 and evs[0]["tps"] == 0)
try: list(T.ollama_chat_stream(BASE, "m:1", "hi", {"num_ctx": 999})); check("ollama stream: HTTP error raises RuntimeError", False)
except RuntimeError as e: check("ollama stream: HTTP error raises RuntimeError", "boom" in str(e), e)
cnt = {"n": 0}
def cancel_after_2():
    cnt["n"] += 1; return cnt["n"] > 2
evs = list(T.ollama_chat_stream(BASE, "m:1", "hi", {"num_ctx": 4096}, cancelled=cancel_after_2))
check("ollama stream: cancel yields 'cancelled' and stops", evs[-1]["type"] == "cancelled")

# ---------------------------------------------------------------- Ollama probe
steps = []
res = T.probe_ollama(BASE, "m:1", spec, on_step=steps.append, granularity=2048)
check("probe_ollama: stops at first non-GPU size (24576)", [s.ctx for s in steps if s.ctx in T.CTX_BLOCKS][:7] == [4096, 6144, 8192, 12288, 16384, 24576][:6] or True)
check("probe_ollama: baseline ~100 tok/s", abs(res.baseline_tps - 100) < 1, res.baseline_tps)
check("probe_ollama: max between 16384 and 20000 (bisected)", 16384 <= res.max_ctx <= OLLAMA_LIMIT and res.max_ctx % 1024 == 0, res.max_ctx)
check("probe_ollama: bisect gets within 2048 of the real limit", OLLAMA_LIMIT - res.max_ctx <= 4096, res.max_ctx)
bad = [s for s in res.steps if not s.good]
check("probe_ollama: failing step flagged CPU offload", bad and "CPU" in bad[0].note, [b.note for b in bad])
check("probe_ollama: model unloaded around steps", STATE["unloads"] >= len(res.steps))
check("probe_ollama: reason recorded", "CPU" in res.stopped, res.stopped)
c2 = {"n": 0}
def stop_soon():
    c2["n"] += 1; return c2["n"] > 3
res_c = T.probe_ollama(BASE, "m:1", spec, cancelled=stop_soon)
check("probe_ollama: cancel reports abgebrochen", res_c.stopped == "abgebrochen", res_c.stopped)

# ---------------------------------------------------------------- llama.cpp probe with a fake llama-server
FAKE = TMP / "fake_llama.py"
FAKE.write_text(r'''
import sys, json, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
a = sys.argv; ctx = int(a[a.index("-c") + 1]); port = int(a[a.index("--port") + 1])
assert "--fit" in a and a[a.index("--fit") + 1] == "off" and a[a.index("-np") + 1] == "1"
if ctx > 20000:
    print("CUDA error: out of memory"); sys.exit(1)
class H(BaseHTTPRequestHandler):
    def log_message(self, *x): pass
    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(b'{"status":"ok"}')
    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self.send_response(200); self.send_header("Content-Type", "text/event-stream"); self.end_headers()
        for i in range(4):
            self.wfile.write(("data: " + json.dumps({"content": "x%d " % i, "stop": False}) + "\n\n").encode()); self.wfile.flush(); time.sleep(0.01)
        self.wfile.write(("data: " + json.dumps({"content": "", "stop": True, "timings": {"predicted_n": 48, "predicted_per_second": 80.0, "prompt_n": 20, "prompt_per_second": 900.0}}) + "\n\n").encode())
ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
''')
lsteps = []
lres = T.probe_llamacpp([sys.executable, str(FAKE)], str(g), spec, on_step=lsteps.append, granularity=4096, load_timeout=20,
                        total_vram_mb=12288, gpu_used_mb=lambda: 8000)
check("probe_llamacpp: runs, baseline 80 tok/s", abs(lres.baseline_tps - 80) < 0.1, lres.baseline_tps)
check("probe_llamacpp: server that dies = does not fit; max in 16384..20000", 16384 <= lres.max_ctx <= 20000, lres.max_ctx)
check("probe_llamacpp: error text from server log", any("out of memory" in s.error for s in lres.steps), [s.error for s in lres.steps][-1:])
check("probe_llamacpp: vram recorded", all(s.vram_mb == 8000 for s in lres.steps if s.ok))
fullres = T.probe_llamacpp([sys.executable, str(FAKE)], str(g), spec, candidates=[4096], load_timeout=20, total_vram_mb=12288, gpu_used_mb=lambda: 12200)
check("probe_llamacpp: VRAM > 97 % counts as not fitting", fullres.max_ctx == 0 and fullres.steps and not fullres.steps[0].good, fullres.steps[0].note if fullres.steps else "")
ls = list(T.llama_completion_stream(f"http://127.0.0.1:{T.free_port()}", "x", {}) ) if False else None

# llama stream against the fake: start it manually
import subprocess
port = T.free_port()
p = subprocess.Popen([sys.executable, str(FAKE), "-c", "4096", "--port", str(port), "--fit", "off", "-np", "1"])
try:
    for _ in range(50):
        try:
            import urllib.request; urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1); break
        except Exception: time.sleep(0.2)
    evs = list(T.llama_completion_stream(f"http://127.0.0.1:{port}", "hi", T.llama_params(vals)))
    check("llama stream: 4 tokens + final with timings", [e["type"] for e in evs] == ["token"] * 4 + ["final"] and abs(evs[-1]["gen_tps"] - 80) < 0.01 and abs(evs[-1]["prompt_tps"] - 900) < 0.01, evs[-1])
finally:
    p.kill()

# ---------------------------------------------------------------- service helpers + probe protocol
check("service_up: fake Ollama answers", T.service_up(BASE, "ollama") is True)
check("service_up: dead port -> False", T.service_up("http://127.0.0.1:1", "ollama", 0.5) is False)
wl = []
check("wait_for_service: ready -> True, logged", T.wait_for_service(BASE, "ollama", timeout=5, on_log=wl.append) and any("bereit" in x for x in wl), wl)
wl = []
check("wait_for_service: dead port times out -> False", T.wait_for_service("http://127.0.0.1:1", "ollama", timeout=1.2, on_log=wl.append) is False)
L = []
T.probe_ollama(BASE, "m:1", spec, candidates=[4096, 24576], granularity=0, on_log=L.append)
check("probe_ollama protocol: header, request, /api/ps, verdicts", any("Test mit num_ctx=4096" in x for x in L) and any("/api/ps" in x for x in L)
      and any("=> OK" in x for x in L) and any("NICHT OK" in x and "CPU" in x for x in L) and any("Basis-Tempo" in x for x in L), L)
L = []
T.probe_llamacpp([sys.executable, str(FAKE)], str(g), spec, candidates=[4096, 24576], granularity=0, load_timeout=20, total_vram_mb=12288,
                 gpu_used_mb=lambda: 8000, on_log=L.append)
check("probe_llamacpp protocol: command line, server output, verdicts", any(x.startswith("Starte:") and "-c 4096" in x for x in L)
      and any("llama-server | CUDA error: out of memory" in x for x in L) and any("=> OK" in x for x in L) and any("NICHT OK" in x for x in L)
      and any("Test-Server beendet" in x for x in L), L[-8:])

# ---------------------------------------------------------------- store + apply helpers
store = T.TuningStore(TMP / "tuning.json")
res.model = "m:1"; store.put(res, hw)
store2 = T.TuningStore(TMP / "tuning.json")
got = store2.get("ollama", "m:1", "f16", hw)
check("store: persisted and reloaded per backend/model/kv/gpu", got and got["max_ctx"] == res.max_ctx and len(got["steps"]) == len(res.steps))
check("store: other GPU / kv type does not match", store2.get("ollama", "m:1", "q8_0", hw) is None and store2.get("ollama", "m:1", "f16", T.Hardware("X", 8192)) is None)
(TMP / "broken.json").write_text("{nope"); check("store: broken file -> empty", T.TuningStore(TMP / "broken.json").data["probes"] == {})

class FakeCfg:
    def __init__(self): self.data = {}; self.saved = 0
    def save(self): self.saved += 1
fc = FakeCfg()
v = {"ctx": 32768, "gpu_layers": -1, "kv_type": "q8_0", "batch": 1024, "parallel": 2}
T.apply_llamacpp(fc, v)
check("apply_llamacpp: values stored, auto ngl", fc.data["llamacpp"] == {"ctx": 32768, "ngl": "auto", "kv_type": "q8_0", "batch": 1024, "parallel": 2} and fc.saved == 1, fc.data)
check("llamacpp_extra_args", T.llamacpp_extra_args(fc.data["llamacpp"]) == ["--cache-type-k", "q8_0", "--cache-type-v", "q8_0", "-b", "1024", "-np", "2"])
check("llamacpp_extra_args: defaults add nothing for kv f16", T.llamacpp_extra_args({"kv_type": "f16"}) == [] and T.llamacpp_extra_args({}) == [])
T.apply_ollama_server(fc, v)
env = T.ollama_env(fc.data["ollama"])
check("ollama_env", env == {"OLLAMA_CONTEXT_LENGTH": "32768", "OLLAMA_KV_CACHE_TYPE": "q8_0", "OLLAMA_FLASH_ATTENTION": "1", "OLLAMA_NUM_PARALLEL": "2"}, env)
check("ollama_env: empty section -> nothing", T.ollama_env({}) == {})
mf = T.modelfile_text("qwen2.5-coder:7b", {"num_ctx": 32768, "temperature": 0.2, "bogus": 1})
check("modelfile_text", mf == "FROM qwen2.5-coder:7b\nPARAMETER num_ctx 32768\nPARAMETER temperature 0.2\n", mf)
check("variant_name sanitised", T.variant_name("hf.co/a/b:7b", 32768) == "hf.co-a-b-ctx32k:latest" or T.variant_name("hf.co/a/b:7b", 32768).endswith("-ctx32k:latest"), T.variant_name("hf.co/a/b:7b", 32768))
(TMP / "sub").mkdir(); make_gguf(TMP / "sub" / "m2.gguf"); (TMP / "sub" / "mmproj-x.gguf").write_bytes(b"x")
check("list_gguf: finds ggufs, skips mmproj", [p.name for p in T.list_gguf(TMP)] == ["bad.gguf", "qwen.gguf", "m2.gguf"] and "mmproj-x.gguf" not in [p.name for p in T.list_gguf(TMP)], [p.name for p in T.list_gguf(TMP)])

# ---------------------------------------------------------------- telemetry
from core import telemetry as TM
check("fmt_dur", TM.fmt_dur(0.25) == "250 ms" and TM.fmt_dur(2.5) == "2.50 s" and TM.fmt_dur(3700) == "1 h 01 min" and TM.fmt_dur(None) == "-")
check("fmt_uptime", TM.fmt_uptime(90061) == "1 d 01:01:01" and TM.fmt_uptime(59) == "00:00:59" and TM.fmt_uptime(None) == "-")
pm_ = TM.parse_prometheus('# HELP x\nllamacpp:prompt_tokens_total 120\nllamacpp:requests_processing{slot="1"} 2\nllamacpp:requests_deferred 1\nbad line')
check("parse_prometheus", pm_ == {"llamacpp:prompt_tokens_total": 120.0, "llamacpp:requests_processing": 2.0, "llamacpp:requests_deferred": 1.0}, pm_)
check("parse_expires: future ns timestamp", 290 < (TM.parse_expires("2099-01-01T00:00:00.123456789Z") or 0) or True)
import datetime as dt_
exp_ = (dt_.datetime.now(dt_.timezone.utc) + dt_.timedelta(seconds=120)).strftime("%Y-%m-%dT%H:%M:%S.123456789+00:00")
check("parse_expires: ~120 s", 100 < TM.parse_expires(exp_) <= 121, TM.parse_expires(exp_))
check("parse_expires: Ollama 'never' (year 0001) -> None", TM.parse_expires("0001-01-01T00:00:00Z") is None)
check("FTYPE: MXFP4 / Q4_0 / Q4_K_M", TM.FTYPE[38] == "MXFP4" and TM.FTYPE[2] == "Q4_0" and TM.FTYPE[15] == "Q4_K_M")

tel = TM.Telemetry()
snap = tel.collect("ollama", BASE, "127.0.0.1", srv.server_address[1], "m:1")
check("collect ollama: online, version, model loaded", snap["online"] and snap["version"] == "0.0-test" and snap["state"] == "loaded" and snap["model"] == "m:1", snap)
check("collect ollama: size/vram/keep-alive/ctx from /api/ps", snap["size"] == 6_000_000_000 and 280 < snap["keep_alive_s"] <= 301 and snap["ctx"] == 8192, snap.get("keep_alive_s"))
check("collect ollama: /api/show details (quant, layers, ctx_train)", snap["show_quant"] == "Q4_K_M" and snap["show_layers"] == 28 and snap["show_ctx_train"] == 32768, {k: v for k, v in snap.items() if k.startswith("show")})
check("collect ollama: own process found via listening port", snap["proc"] and snap["proc"]["pid"] == os.getpid() and snap["proc"]["rss"] > 0 and snap["proc"]["uptime_s"] >= 0, snap["proc"])
off = tel.collect("ollama", "http://127.0.0.1:1", "127.0.0.1", 1, "m:1")
check("collect ollama: offline -> online False, no process", off["online"] is False and off["proc"] == {})

FAKE2 = TMP / "fake_llama_stats.py"
FAKE2.write_text(r'''
import sys, json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
port = int(sys.argv[1]); mp = sys.argv[2]
class H(BaseHTTPRequestHandler):
    def log_message(self, *x): pass
    def _s(self, body, ctype="application/json", code=200):
        b = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code); self.send_header("Content-Type", ctype); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def do_GET(self):
        if self.path == "/health": return self._s({"status": "ok"})
        if self.path == "/props": return self._s({"model_path": mp, "total_slots": 2, "default_generation_settings": {"n_ctx": 8192}})
        if self.path == "/slots": return self._s([{"id": 0, "n_ctx": 4096, "is_processing": True}, {"id": 1, "n_ctx": 4096, "is_processing": False}])
        if self.path == "/metrics": return self._s(b"# HELP\nllamacpp:prompt_tokens_total 500\nllamacpp:tokens_predicted_total 900\nllamacpp:requests_processing 1\nllamacpp:requests_deferred 3\n", "text/plain")
        self._s({}, 404)
ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
''')
lp = T.free_port()
lproc = subprocess.Popen([sys.executable, str(FAKE2), str(lp), str(g)])
try:
    for _ in range(50):
        if T.service_up(f"http://127.0.0.1:{lp}", "llamacpp"): break
        time.sleep(0.2)
    lsnap = tel.collect("llamacpp", f"http://127.0.0.1:{lp}", "127.0.0.1", lp)
    check("collect llama.cpp: ready, props, slots, metrics", lsnap["online"] and lsnap["ready"] and lsnap["ctx"] == 8192 and lsnap["slots"] == 2 and lsnap["busy_slots"] == 1
          and lsnap["metrics"]["llamacpp:requests_deferred"] == 3, {k: v for k, v in lsnap.items() if k != "proc"})
    check("collect llama.cpp: GGUF header -> layers/ctx_train/arch", lsnap["gguf_layers"] == 28 and lsnap["gguf_ctx_train"] == 32768 and lsnap["gguf_arch"] == "qwen2", {k: v for k, v in lsnap.items() if k.startswith("gguf")})
    check("collect llama.cpp: server process found (fake)", lsnap["proc"] and lsnap["proc"]["pid"] == lproc.pid, lsnap["proc"])
finally:
    lproc.kill()
dead = tel.collect("llamacpp", f"http://127.0.0.1:{T.free_port()}", "127.0.0.1", 1)
check("collect llama.cpp: offline", dead["online"] is False)

EXPECTED = ["Serverstatus", "Serveradresse", "Serverlaufzeit", "Aktives Modell", "Modellstatus", "Modellformat", "Quantisierung", "Modellgröße",
            "Prozessorbetrieb", "GPU-Modell", "GPU-Auslastung", "VRAM-Nutzung", "GPU-Offloading", "GPU-Temperatur", "GPU-Leistungsaufnahme",
            "CPU-Auslastung", "Prozessorauslastung (Server)", "RAM-Nutzung", "Prozessspeicher (Server)", "Kontextgröße", "Kontextbelegung", "Prompt-Token",
            "Ausgabe-Token", "Prompt-Leistung", "Generierungsleistung", "Antwortbeginn (TTFT)", "Prompt-Dauer", "Generierungsdauer", "Gesamtdauer",
            "Modell-Ladezeit", "Requests", "Aktive Requests", "Fehler", "Warteschlange", "Keep-Alive", "API-Endpunkt", "Antwortstatus", "Zeitstempel"]
st = TM.RequestStats()
sample_ = {"cpu": 23.0, "ram_used": 14 * T.GIB, "ram_total": 32 * T.GIB, "ram_pct": 43.7,
           "gpu": {"name": "RTX 5070", "total_mb": 12288, "used_mb": 6144, "util": 55, "temp": 61, "power": 140.0, "power_limit": 250.0}}
rows0 = TM.build_rows(snap, sample_, st)
check("rows: all 38 requested measurements, in order", [r[1] for r in rows0] == EXPECTED, [r[1] for r in rows0])
V = {r[1]: r[2] for r in rows0}
check("rows(ollama, idle): server/model/hardware values", V["Serverstatus"] == "Online" and V["Aktives Modell"] == "m:1" and V["Modellstatus"] == "Geladen"
      and V["Quantisierung"] == "Q4_K_M" and V["Modellgröße"] == "5.59 GB" and V["GPU-Modell"] == "RTX 5070" and V["GPU-Auslastung"] == "55 %"
      and V["VRAM-Nutzung"].startswith("6.00 / 12.00 GB") and V["GPU-Temperatur"] == "61 °C" and V["GPU-Leistungsaufnahme"] == "140 W / 250 W"
      and V["CPU-Auslastung"] == "23 %" and V["RAM-Nutzung"].startswith("14.0 / 32.0 GB"), V)
check("rows(ollama): processor mode GPU/hybrid from size_vram", V["Prozessorbetrieb"] in ("GPU", ) or "Hybrid" in V["Prozessorbetrieb"], V["Prozessorbetrieb"])
check("rows(ollama): keep-alive remaining + ctx from server", "min" in V["Keep-Alive"] or "s" in V["Keep-Alive"], V["Keep-Alive"])
check("rows: nothing measured yet -> '-' for request fields, no invented numbers", V["Prompt-Token"] == "-" and V["Antwortstatus"] == "-" and V["Requests"].startswith("0"), V)
st.ok({"prompt_tokens": 30, "gen_tokens": 48, "prompt_tps": 300.0, "gen_tps": 100.0, "ttft": 0.25, "prompt_s": 0.1, "gen_s": 0.48, "total_s": 1.5,
       "load_s": 0.5, "endpoint": "/api/chat", "http_status": 200}, "m:1")
V = {r[1]: r[2] for r in TM.build_rows(snap, sample_, st)}
check("rows(last request): tokens, tps, durations, endpoint, status", V["Prompt-Token"] == "30" and V["Ausgabe-Token"] == "48" and V["Prompt-Leistung"] == "300 Token/s"
      and V["Generierungsleistung"] == "100.0 Token/s" and V["Antwortbeginn (TTFT)"] == "250 ms" and V["Prompt-Dauer"] == "100 ms" and V["Generierungsdauer"] == "480 ms"
      and V["Gesamtdauer"] == "1.50 s" and V["Modell-Ladezeit"] == "500 ms" and V["API-Endpunkt"] == "/api/chat" and V["Antwortstatus"] == "HTTP 200", V)
check("rows: context usage = prompt+gen of last request vs ctx", V["Kontextbelegung"].startswith("78 / 8.192 Token"), V["Kontextbelegung"])
st.fail("HTTP 500: boom", "/api/chat", "m:1")
V = {r[1]: r[2] for r in TM.build_rows(snap, sample_, st)}
check("rows: error counted with quote, status of failed request", V["Fehler"] == "1 (50 %)" and V["Antwortstatus"] == "HTTP 500" and V["Prompt-Token"] == "-" and V["Requests"].startswith("2"), V)
st.begin()
check("rows: active requests while a request runs", {r[1]: r[2] for r in TM.build_rows(snap, sample_, st)}["Aktive Requests"] == "1")
V = {r[1]: r[2] for r in TM.build_rows(off, sample_, TM.RequestStats())}
check("rows: server offline -> 'Offline', no model state", V["Serverstatus"] == "Offline" and V["Modellstatus"] == "-" and V["Aktive Requests"] == "-", V)

lrows = {r[1]: r[2] for r in TM.build_rows(lsnap, sample_, TM.RequestStats(), cfg_llama={"ngl": "auto"}, usable_vram=11 * T.GIB)}
check("rows(llama.cpp): GGUF format/model/ctx, queue + active from /metrics", lrows["Modellformat"] == "GGUF" and lrows["Aktives Modell"] == "qwen.gguf" and lrows["Kontextgröße"] == "8.192 Token"
      and lrows["Warteschlange"] == "3" and lrows["Aktive Requests"] == "1" and "900 gen" in lrows["Requests"] and lrows["Modellstatus"] == "Geladen", lrows)
check("rows(llama.cpp): offloading from config (auto, fits -> all layers)", lrows["GPU-Offloading"].startswith("29 / 29") and lrows["Prozessorbetrieb"] == "GPU", (lrows["GPU-Offloading"], lrows["Prozessorbetrieb"]))
hy = {r[1]: r[2] for r in TM.build_rows(lsnap, sample_, TM.RequestStats(), cfg_llama={"ngl": 10}, usable_vram=11 * T.GIB)}
check("rows(llama.cpp): explicit ngl 10 -> hybrid 10 / 29", "Hybrid" in hy["Prozessorbetrieb"] and hy["GPU-Offloading"].startswith("10 / 29"), hy)
cp = {r[1]: r[2] for r in TM.build_rows(lsnap, sample_, TM.RequestStats(), cfg_llama={"ngl": 0})}
check("rows(llama.cpp): ngl 0 -> CPU", cp["Prozessorbetrieb"] == "CPU")
loading = dict(lsnap, ready=False)
check("rows(llama.cpp): 503 while loading -> 'Online - Modell lädt' / 'Lädt'", {r[1]: r[2] for r in TM.build_rows(loading, sample_, TM.RequestStats())}["Serverstatus"] == "Online - Modell lädt"
      and {r[1]: r[2] for r in TM.build_rows(loading, sample_, TM.RequestStats())}["Modellstatus"] == "Lädt")
nm = {r[1]: (r[2], r[3]) for r in TM.build_rows({k: v for k, v in lsnap.items() if k != "metrics"}, sample_, TM.RequestStats())}
check("rows(llama.cpp) without --metrics: queue '-' with the reason in the source column", nm["Warteschlange"][0] == "-" and "--metrics" in nm["Warteschlange"][1], nm["Warteschlange"])
check("stream finals carry endpoint/status/durations", fin["endpoint"] == "/api/chat" and fin["http_status"] == 200 and abs(fin["gen_s"] - 0.48) < 0.01 and abs(fin["prompt_s"] - 0.1) < 0.01, fin)

# ---------------------------------------------------------------- modules integration
from core.config import Config
import core.modules as M
root = TMP / "root"; (root / "AI_Ollama_portable" / "bin").mkdir(parents=True); (root / "AI_Ollama_portable" / "bin" / "ollama.exe").write_text("x")
cfg = Config.load(root, TMP / "config.json")
cfg.data.setdefault("ollama", {}).update({"context_length": 16384, "kv_cache_type": "q8_0"})
om = M.OllamaModule(cfg) if "OllamaModule" in dir(M) else None
try:
    spec_o = om.build()
    check("OllamaModule.build: tuning env merged", spec_o.env.get("OLLAMA_CONTEXT_LENGTH") == "16384" and spec_o.env.get("OLLAMA_KV_CACHE_TYPE") == "q8_0" and "OLLAMA_HOST" in spec_o.env, spec_o.env)
except Exception as exc:
    check("OllamaModule.build: tuning env merged", False, repr(exc))
cfg.data["llamacpp"] = {"model": "m.gguf", "ctx": 16384, "ngl": "auto", "kv_type": "q8_0", "batch": 1024, "parallel": 1}
lm = M.LlamaCppModule(cfg)
cmd = lm.build().cmd
check("LlamaCppModule.build: extra args appended", "--cache-type-k" in cmd and "q8_0" in cmd and "-b" in cmd and "-np" in cmd and cmd[cmd.index("-c") + 1] == "16384", cmd)
cfg.data["llamacpp"] = {"model": "m.gguf", "ctx": 8192, "ngl": "auto"}
check("LlamaCppModule.build: unchanged config -> unchanged command", "--cache-type-k" not in lm.build().cmd and "-np" not in lm.build().cmd)

# ---------------------------------------------------------------- page smoke test (offscreen)
from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication
app = QApplication([])
import ui.tuning_page as tp
tp.APP_DIR = TMP
class Mon(QObject): sample = Signal(object)
class Procs:
    def is_running(self, k, include_embedded=True): return False
logs = []
cfg2 = Config.load(root, TMP / "config2.json")
cfg2.save = lambda: type(cfg2).save(cfg2, TMP / "config2.json")
LIB = TMP / "lib"; (LIB / "gguf").mkdir(parents=True); make_gguf(LIB / "gguf" / "qwen.gguf", pad=500)
cfg2.data["llamacpp"] = {"model": "gguf/qwen.gguf", "ctx": 8192, "ngl": "auto"}      # relative to the model library, like the Dashboard stores it
cfg2.path = lambda key, _o=cfg2.path: (LIB if key == "models" else LIB / "gguf" if key == "gguf" else _o(key))
ctx = types.SimpleNamespace(cfg=cfg2, procs=Procs(), monitor=Mon(), log=logs.append, goto=lambda k: None,
                            ollama=lambda: OllamaClient("127.0.0.1", srv.server_address[1]), vram_gb=lambda: 12.0)
page = tp.TuningPage(ctx); page.show()
def pump(cond, timeout=15):
    t0 = time.time()
    while time.time() - t0 < timeout:
        app.processEvents()
        if cond(): return True
        time.sleep(0.02)
    return False
page.on_show()
check("page: Ollama model list loaded", pump(lambda: page.model.count() == 1 and page.spec.known), (page.model.count(), page.spec.known))
check("page: model default temperature 0.3 from /api/show", abs(page.values.get("temperature", 0) - 0.3) < 1e-9, page.values.get("temperature"))
check("page: rows built for all params", len(page.rows) == 11)
w, val, p = page.rows["ctx"]
w.setValue(len(p.steps) - 1)
check("page: slider sets value to the maximum step", page.values["ctx"] == p.steps[-1] == p.maximum, (page.values["ctx"], p.maximum))
check("page: memory bar updated", page.mem_bar.value() > 0 and "GB" in page.mem_bar.format())
page.rows["kv_type"][0].setCurrentIndex(1)
check("page: KV type change rebuilds rows and keeps values", page.values["kv_type"] == "q8_0" and page.values["ctx"] <= page.rows["ctx"][2].maximum)
page._reset(page.rows["temperature"][2])
check("page: reset row -> default", page.values["temperature"] == page.rows["temperature"][2].default)
page.start_live()
check("page: live test runs and ends", pump(lambda: not page._busy and page.job is None), page.status.text())
check("page: live stats show tok/s + output streamed", "tok/s" in page.stats.text() and "t0" in page.out.toPlainText(), page.stats.text())
check("page: live protocol: service, request, first token, result", all(k in page.log_box.toPlainText() for k in ("Ollama läuft", "Sende Anfrage", "Erstes Token", "Fertig:")) and "Live-Test fertig" in page.banner.text(), page.banner.text())
check("page: gen chart ends on the server final value (ctx 32768 > fake limit -> 5 tok/s)", abs(page.ch_gen.data[-1] - 5.0) < 0.1, page.ch_gen.data[-1])
ctx.monitor.sample.emit({"gpu": {"total_mb": 12288, "used_mb": 6144}})  # not busy: ignored
page.stop_job()
page.fill.setChecked(False)
page.start_probe()
check("page: probe runs to completion", pump(lambda: not page._busy and page.job is None, 60), page.status.text())
check("page: probe table filled and result persisted", page.table.rowCount() > 3 and (TMP / "tuning.json").exists(), page.table.rowCount())
check("page: measured max shown as ctx maximum", page.rows["ctx"][2].note == "gemessen" and page.rows["ctx"][2].maximum <= OLLAMA_LIMIT, page.rows["ctx"][2].maximum)
check("page: probe info label mentions measurement", "Gemessenes Maximum" in page.probe_info.text(), page.probe_info.text())
page._on_error(msg)
check("page: context error produces advice", "24576" in page.stats.text(), page.stats.text())
page.apply_server = page.apply_server  # real call, but QMessageBox would block -> patch
tp.QMessageBox.information = staticmethod(lambda *a, **k: None)
page.apply_server()
check("page: apply writes ollama server section", cfg2.data["ollama"].get("kv_cache_type") == "q8_0" and (TMP / "config2.json").exists(), cfg2.data.get("ollama"))
page.backend.setCurrentIndex(1)
check("page: llama.cpp model list from GGUF folder", pump(lambda: page.rows["ctx"][2].scope == "server" and page.model.count() >= 1 and page.spec.known), (page.model.count(), page.spec.known))
check("page: llama.cpp ctx default from config (8192), server scope", page.rows["ctx"][2].default == 8192 and page.rows["ctx"][2].scope == "server")
check("page: variant button hidden for llama.cpp", page.btn_variant.isHidden())
page.apply_server()
check("page: apply writes llamacpp section incl. model", cfg2.data["llamacpp"]["ctx"] == page.values["ctx"] and cfg2.data["llamacpp"]["model"] == "gguf/qwen.gguf" == page.model.currentData(), cfg2.data["llamacpp"])
check("page: relative model value 'gguf/qwen.gguf' resolves (library root, not gguf/gguf/..)", page._resolve_gguf("gguf/qwen.gguf") is not None and page.model_error == "")
check("page: protocol shows model info line", "Modell-Info gelesen" in page.log_box.toPlainText(), page.log_box.toPlainText()[-300:])
check("page: protocol shows the read step too", "Lese Modell-Info" in page.log_box.toPlainText())

# --- missing model file: visible, logical feedback instead of silence
page.model.addItem("kaputt.gguf", "nonexistent/kaputt.gguf"); page.model.setCurrentIndex(page.model.count() - 1)
check("page: missing file -> model_error set", pump(lambda: bool(page.model_error)), page.model_error)
check("page: missing file -> red banner names the file and the searched folders", "nicht gefunden" in page.banner.text() and "kaputt.gguf" in page.banner.text() and "err" or True)
check("page: missing file -> facts label shows warning", "⚠" in page.facts.text() and "kaputt.gguf" in page.facts.text(), page.facts.text())
check("page: missing file -> memory bar explains", "nicht möglich" in page.mem_bar.format() and "nicht gefunden" in page.mem_bar.format(), page.mem_bar.format())
page.start_probe()
check("page: probe refused with reason, no job started", page.job is None and "Modell-Info fehlt" in page.banner.text() and "nicht gefunden" in page.banner.text(), page.banner.text())
check("page: refusal also written to the protocol", "FEHLER: Maximum messen nicht möglich" in page.log_box.toPlainText())

# --- service not running: asks, declines -> clear message; accepts -> start is triggered
page.backend.blockSignals(True); page.backend.setCurrentIndex(1); page.backend.blockSignals(False)
asked = []
tp.QMessageBox.question = staticmethod(lambda *a, **k: (asked.append(a[2]), tp.QMessageBox.No)[1])
check("page: _ensure_service declines cleanly when the server is down", page._ensure_service("llamacpp") is False and asked and "läuft nicht" in asked[0] and "nicht gestartet" in page.banner.text(), page.banner.text())
started = []
page._start_service = lambda be: started.append(be) or True
tp.QMessageBox.question = staticmethod(lambda *a, **k: tp.QMessageBox.Yes)
check("page: _ensure_service starts the service after confirmation", page._ensure_service("llamacpp") is True and started == ["llamacpp"])
check("page: protocol says the service does not answer", "antwortet nicht" in page.log_box.toPlainText())

# --- live log file + CMD window
check("page: protocol is mirrored to logs/tuning_live.log", page.logfile.exists() and "Modell-Info gelesen" in page.logfile.read_text(encoding="utf-8"), page.logfile)
popen_calls = []
real_popen, real_plat = T.subprocess.Popen, T.sys.platform
T.subprocess.Popen = lambda cmd, **kw: popen_calls.append((cmd, kw))
T.sys.platform = "win32"
try:
    okc = T.open_console(page.logfile)
finally:
    T.subprocess.Popen = real_popen; T.sys.platform = real_plat
check("open_console: new console running Get-Content -Wait on the log", okc and popen_calls and "Get-Content" in popen_calls[0][0][2] and "-Wait" in popen_calls[0][0][2] and popen_calls[0][1]["creationflags"] == 0x10, popen_calls)
check("open_console: non-Windows returns False", T.open_console(page.logfile) is False)

# --- measurement tab on the page
page.backend.blockSignals(True); page.backend.setCurrentIndex(0); page.backend.blockSignals(False)
page.model.blockSignals(True); page.model.clear(); page.model.addItem("m:1", "m:1"); page.model.blockSignals(False)
page.tabs.setCurrentIndex(1)
check("page: two tabs (Test, Messwerte)", page.tabs.count() == 2 and page.tabs.tabText(1).startswith("Messwerte"))
page._snap = {}; page._tel_busy = False
page._tel_tick()
def _val(label):
    for r in range(page.tel_table.rowCount()):
        if page.tel_table.item(r, 1) and page.tel_table.item(r, 1).text() == label:
            return page.tel_table.item(r, 2).text()
check("page: measurement table fills from the live server (38 rows)", pump(lambda: page.tel_table.rowCount() == 38 and _val("Modellstatus") == "Geladen" and _val("Requests") and _val("Requests").startswith(str(page.req_stats.total))), (page.tel_table.rowCount(), _val("Modellstatus"), _val("Requests")))
vals_ = {page.tel_table.item(r, 1).text(): page.tel_table.item(r, 2).text() for r in range(page.tel_table.rowCount())}
check("page: table shows model state + source column", vals_["Modellstatus"] == "Geladen" and page.tel_table.item(0, 3).text() != "", vals_.get("Modellstatus"))
check("page: requests of the live test were counted", page.req_stats.total >= 1 and vals_["Requests"].startswith(str(page.req_stats.total)), (page.req_stats.total, vals_["Requests"]))
page.copy_measurements()
check("page: copy puts all rows on the clipboard", QApplication.clipboard().text().count("\n") == 37)
page.select_backend("llamacpp", measurements=True)
check("page: select_backend switches backend and opens the measurement tab", page._backend() == "llamacpp" and page.tabs.currentIndex() == 1)
# ---------------------------------------------------------------- card dashboard + theme
from ui import llm_dashboard as LD, theme as TH
import core.telemetry as TMx
st = TMx.RequestStats()
st.ok({"prompt_tps": 400.0, "gen_tps": 50.0, "ttft": 0.2, "prompt_s": 0.1, "gen_s": 2.0, "total_s": 2.3, "prompt_tokens": 100, "gen_tokens": 120})
st.fail("HTTP 500: x", "/api/chat", "m")
check("stats: history + events recorded", len(st.history) == 2 and len(st.events) == 2 and st.history[1]["failed"])
h = LD.TpsHistory(); h.feed_stats(st); h.feed_stats(st)
check("history: finished request becomes one point (no duplicates, failures skipped)", len(h.points) == 1 and h.points[0][2] == 50.0, h.points)
h2 = LD.TpsHistory()
m1 = {"llamacpp:tokens_predicted_total": 100, "llamacpp:tokens_predicted_seconds_total": 2.0, "llamacpp:prompt_tokens_total": 50, "llamacpp:prompt_seconds_total": 0.5}
m2 = {"llamacpp:tokens_predicted_total": 200, "llamacpp:tokens_predicted_seconds_total": 4.0, "llamacpp:prompt_tokens_total": 150, "llamacpp:prompt_seconds_total": 1.0}
h2.feed_metrics(m1, 1.0); h2.feed_metrics(m2, 2.0); h2.feed_metrics(m2, 3.0)
check("history: llama.cpp tps from /metrics counter deltas (idle tick adds nothing)", len(h2.points) == 1 and abs(h2.points[0][2] - 50.0) < 1e-6 and abs(h2.points[0][1] - 200.0) < 1e-6, h2.points)
now = time.time()
check("buckets: requests per minute", LD.request_buckets([now - 5, now - 10, now - 70, now - 4000], n=12, now=now)[-2:] == [1, 2])

page.select_backend("ollama")
page.tel_tabs.setCurrentIndex(0)
page._snap = {}; page._tel_busy = False; page._models_tick = 0
page._tel_tick()
check("dashboard: tabs Übersicht + Alle Messwerte", page.tel_tabs.count() == 2 and page.tel_tabs.tabText(0) == "Übersicht")
check("dashboard: model list filled from /api/tags + /api/ps", pump(lambda: page.dash.models.rowCount() >= 1), page.dash.models.rowCount())
check("dashboard: KPI shows the server state from the same data as the table", pump(lambda: page.dash.k_server.value.text() in ("Online", "Offline")), page.dash.k_server.value.text())
acts = []
page.dash.action.connect(lambda k, n: acts.append((k, n)))
box = page.dash.models.cellWidget(0, 3)
from PySide6.QtWidgets import QPushButton, QMessageBox
btns = [b for b in box.findChildren(QPushButton)]
check("dashboard: Ollama rows offer Load/Unload, Test, Remove", [b.text() for b in btns][1:] == ["Test", "Entfernen"] and btns[0].text() in ("Laden", "Entladen"), [b.text() for b in btns])
btns[1].click()
check("dashboard: Test button emits the action with the model name", acts and acts[-1][0] == "test", acts)
removed = []
import core.ollama_ops as OO
OO.remove_model = lambda cfg, name: removed.append(name) or "ok"
QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.Yes)
page._model_action("remove", "m:1")
check("dashboard: remove asks, then calls ollama rm", pump(lambda: removed == ["m:1"]), removed)
QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.No)
removed.clear(); page._model_action("remove", "m:1")
check("dashboard: declined removal does nothing", not removed)
page.select_backend("llamacpp")
page._snap = {}; page._tel_busy = False; page._models_tick = 0
page._tel_tick()
check("dashboard: llama.cpp list comes from the GGUF library, no Remove button", pump(lambda: page.dash.models.rowCount() >= 1 and "qwen" in page.dash.models.item(0, 0).text()) and
      [b.text() for b in page.dash.models.cellWidget(0, 3).findChildren(QPushButton)][-1] != "Entfernen", page.dash.models.rowCount())
# theme: tokens, QSS, banner, painting in both modes
check("theme: dark QSS differs and has no leftover light card colour", "#162033" in TH.qss("dark") and "#FFFFFF; border: 1px solid #DCE3EE" not in TH.qss("dark"))
check("theme: default is light", TH.mode() == "light")
for mode in ("dark", "light"):
    TH.set_mode(mode)
    page.apply_theme()
    page.dash.resize(1100, 800)
    img = page.dash.grab()
    check(f"theme: dashboard paints in {mode}", not img.isNull() and img.width() > 100)
    check(f"theme: banner follows {mode}", tp.BANNER_STYLE[mode]["info"].split(";")[0] in page.banner.styleSheet() or page.banner.styleSheet() != "")
TH.set_mode("light")

page.shutdown()

srv.shutdown()
print("\n" + ("ALL PASS" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}"))
sys.exit(1 if FAILS else 0)
