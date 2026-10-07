"""Provider-Routing (Cloud-API vs. lokal via llama.cpp/Ollama) je CLI-Profil.

Gleiches Dateiablage-Muster wie models.py / profiles.py: eine kleine JSON-Datei pro Profil,
neben .model.json, damit die Wahl Root-Umzuege (F: -> G:/K:) uebersteht und kein Code
angefasst werden muss, wenn spaeter ein weiterer lokaler Server dazukommt.

EHRLICHKEIT STATT SCHEIN: nur die Claude Code CLI (ANTHROPIC_BASE_URL) und die Codex CLI
(OPENAI_BASE_URL) haben einen offiziell dokumentierten Weg, ihren Provider per Umgebungs-
variable umzubiegen - siehe core/modules.py BatModule.build(). Bei Copilot und Antigravity
ist unklar/unwahrscheinlich, dass sie das respektieren (beides proprietaere Cloud-only-Tools,
siehe die Warnhinweise dazu in launcher/models.json). Der Umschalter ist trotzdem sichtbar,
aber im Dashboard klar als "ungeprueft" markiert (UNVERIFIED_TOOLS) - kein Tool wird mit
einer Umgebungsvariable versorgt, die es nachweislich ignoriert oder an der es sich
verschluckt; fuer diese beiden bleibt build() bewusst unveraendert.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from .config import Config
from .procs import port_open

# local_source() used to open a live TCP socket (up to 0.3s timeout, x2 for llama.cpp+Ollama) every
# single time it was called - and it was being called from ui/dashboard.py's refresh(), which runs
# on the UI thread every second for every CLI row. That's exactly the kind of blocking-call-on-the-
# UI-thread StatusPoller (ui/dashboard.py) exists to avoid ("doing these calls in the UI thread made
# the window show 'not responding'") - this one just wasn't routed through it. cached_local_source()
# below never blocks: StatusPoller's background thread (already off the UI thread, ~every 2s) calls
# the real local_source() and warms this cache as a side effect; everything on the UI thread
# (BatModule.backend_kind(), Dashboard._update_local_ai_banner()) reads the cache instead.
_cache: dict[str, tuple[float, tuple[str, str] | None]] = {}


def cached_local_source() -> tuple[str, str] | None:
    """Non-blocking: last value local_source() computed (in the background poller thread), or None
    if it hasn't run yet. Safe to call from the UI thread at any frequency."""
    entry = _cache.get("v")
    return entry[1] if entry is not None else None

# CLI-Tool (BAT-Stem, klein geschrieben) -> Umgebungsvariablen-Vorlage, die es beim Start auf
# einen lokalen OpenAI/Anthropic-kompatiblen Endpoint umbiegt. "{url}" wird durch die Basis-URL
# des gerade laufenden lokalen Servers ersetzt (siehe local_source()).
LOCAL_ENV_TEMPLATE: dict[str, dict[str, str]] = {
    "claude": {"ANTHROPIC_BASE_URL": "{url}", "ANTHROPIC_API_KEY": "local-no-key-needed"},
    "codex": {"OPENAI_BASE_URL": "{url}", "OPENAI_API_KEY": "local-no-key-needed"},
}
# Werkzeuge, bei denen der Umschalter zwar angeboten wird, aber kein bestaetigter
# Override-Mechanismus bekannt ist - build() setzt hier bewusst keine Umgebungsvariablen.
UNVERIFIED_TOOLS = frozenset({"copilot", "antigravity"})
# Alle Tools, fuer die das Provider-Konzept ueberhaupt sichtbar ist (Happy reicht nur an
# Claude/Codex durch, siehe core/models.py - dafuer gibt es hier bewusst keinen Eintrag).
SUPPORTED_TOOLS = frozenset(LOCAL_ENV_TEMPLATE) | UNVERIFIED_TOOLS


def _file(cfg: Config, profile_name: str) -> Path:
    return cfg.path("cli") / "profiles" / profile_name / ".provider.json"


def get_provider(cfg: Config, profile_name: str) -> str:
    """'cloud' (Default, unveraendertes Verhalten) oder 'local'."""
    try:
        data = json.loads(_file(cfg, profile_name).read_text(encoding="utf-8"))
        if data.get("provider") in ("cloud", "local"):
            return data["provider"]
    except (OSError, ValueError):
        pass
    return "cloud"


def save_provider(cfg: Config, profile_name: str, provider_name: str) -> None:
    """Best-effort, wie save_selected_model() in models.py: schlaegt der Schreibvorgang fehl,
    fragt der Picker beim naechsten Mal einfach wieder."""
    f = _file(cfg, profile_name)
    try:
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps({"provider": provider_name}, indent=2), encoding="utf-8")
    except OSError:
        pass


def local_source(cfg: Config) -> tuple[str, str] | None:
    """(Anzeigename, Basis-URL) des gerade tatsaechlich erreichbaren lokalen Servers, oder None.

    llama.cpp hat Vorrang vor Ollama: es bedient genau das in config.json konfigurierte GGUF-
    Modell, waehrend Ollama je nach zuletzt geladenem Modell wechselt.

    BLOCKING (opens a real TCP socket, up to 0.3s timeout each) - call this only from a background
    thread (e.g. StatusPoller) or right before a start where the answer must be current (build()).
    UI code that just wants to display the current state should use cached_local_source() instead,
    which never blocks."""
    llama_port = cfg.port("llamacpp")
    if port_open(llama_port):
        result = ("llama.cpp", f"http://127.0.0.1:{llama_port}/v1")
    else:
        ollama_port = cfg.port("ollama")
        result = ("Ollama", f"http://127.0.0.1:{ollama_port}/v1") if port_open(ollama_port) else None
    _cache["v"] = (time.monotonic(), result)
    return result


def env_override(tool: str, base_url: str) -> dict[str, str]:
    """Umgebungsvariablen fuer diesen Tool-Namen (BAT-Stem, klein), leer wenn keine Vorlage
    existiert (z.B. Copilot/Antigravity - siehe UNVERIFIED_TOOLS)."""
    tmpl = LOCAL_ENV_TEMPLATE.get(tool.lower(), {})
    return {k: v.format(url=base_url) for k, v in tmpl.items()}
