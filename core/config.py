"""Configuration with root detection.

All paths are stored with a "{ROOT}" placeholder, so the whole portable tree can be
copied to another drive letter (F: -> G:) or plugged into another PC without changes.
ROOT is the parent directory of the AI_Launcher folder (or --root / AI_ROOT override).
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent          # ...\AI_Launcher
CONFIG_FILE = APP_DIR / "config.json"
ROOT_TOKEN = "{ROOT}"

DEFAULTS: dict = {
    "version": 1,
    "paths": {
        "models": "{ROOT}/AI_Modells",
        "comfyui": "{ROOT}/COMFYUI",
        "ollama": "{ROOT}/AI_Ollama_portable",
        "ollama_models": "{ROOT}/AI_Ollama_portable/models",
        "llamacpp": "{ROOT}/AI_Ollama_CCP",   # llama.cpp (llama-server.exe in bin\\)
        "gguf": "{ROOT}/AI_Modells/gguf",     # GGUF files served by llama-server / imported into Ollama
        "cli": "{ROOT}/AI_CLI",
        "downloads": "{ROOT}/X_DOWNLOADS",   # target of all new downloads (Hugging Face / Civitai)
        "runtime": "{ROOT}/@Runtime",     # shared platform: python, later ffmpeg, git, nodejs, ...
        "scrapling": "{ROOT}/AI_Scrapling",   # Scrapling (site-packages\\, browsers\\, scrapling_cli.py)
        "tools": "{ROOT}/@Runtime/Tools",     # portable CLI tools: exiftool\\, ffmpeg\\bin\\ (see core/runtime_tools.py)
    },
    # Ollama default model. If it is not installed yet and default_gguf exists, the dashboard imports it
    # (ollama create) on the first preload. gpt-oss MXFP4 needs Ollama >= 0.11.
    "ollama": {"model": "gpt-oss-20b:latest", "default_gguf": "{ROOT}/AI_Modells/gguf/gpt-oss-20b-MXFP4.gguf"},
    "llamacpp": {"model": "llm/Qwen3.8/Qwen3.8-27B-BF16-SSMFIX-UD-Q3_K_XL.gguf", "ctx": 8192, "ngl": "auto"},   # llama-server start options
    "vram_gb": 12,                # GPU memory used for the "fits in VRAM" hints (RTX 5070 = 12 GB)
    "monitor_interval": 1.0,      # seconds between live-monitor samples
    "host": "127.0.0.1",          # 127.0.0.1 = this PC only, 0.0.0.0 = reachable in LAN
    "ports": {"comfyui": 8188, "ollama": 11434, "llamacpp": 8081, "scrapling": 8100},
    # Extra tools, e.g.
    # {"id": "n8n", "name": "n8n", "command": ["{ROOT}/N8N/start.bat"], "cwd": "{ROOT}/N8N",
    #  "port": 5678, "url": "http://127.0.0.1:5678"}
    "custom_tools": [],
    # Services that run elsewhere (always on) - only monitored + opened, never started/stopped here.
    "external_services": [
        {"id": "openwebui-ha", "name": "OpenWebUI (Home Assistant server)", "url": "http://192.168.178.21:8080/"}
    ],
    # Profile chosen last per CLI tool (AI_CLI\profiles\<name>); passed as --profile at start, see core/profiles.py
    "cli_profiles": {"Claude": "default"},
    # MCP servers offered to the CLI tools (see core/mcp.py). "module" = launcher service that is started
    # automatically before the CLI; "enabled" = per CLI name (BAT file stem) the ticked servers.
    "mcp": {
        "servers": {
            "scrapling": {"name": "Scrapling (web scraping)", "url": "http://{HOST}:{PORT}/mcp",
                          "port": "scrapling", "module": "scrapling"},
        },
        "enabled": {},
        "off": {},          # per CLI name: True = start without MCP (selection above is kept)
    },
}

PATH_LABELS = {
    "models": "Central model library",
    "comfyui": "ComfyUI portable, NVIDIA (folder with python_embeded + ComfyUI)",
    "ollama": "Ollama (folder with ollama.exe)",
    "ollama_models": "Ollama models (OLLAMA_MODELS)",
    "llamacpp": "llama.cpp (folder with bin\\llama-server.exe)",
    "gguf": "GGUF models (llama.cpp / Ollama import)",
    "cli": "AI_CLI (Claude/Codex/... launchers)",
    "downloads": "Downloads (target of new model downloads)",
    "runtime": "Shared runtime (@Runtime)",
    "scrapling": "Scrapling (folder with scrapling_cli.py)",
    "tools": "Runtime tools (ExifTool, FFmpeg)",
}


def detect_root(cli_root: str | None = None) -> Path:
    if cli_root:
        return Path(cli_root).resolve()
    env = os.environ.get("AI_ROOT")
    if env:
        return Path(env).resolve()
    return APP_DIR.parent


class Config:
    def __init__(self, root: Path, data: dict | None = None):
        self.root = root
        self.data = _merge(copy.deepcopy(DEFAULTS), data or {})

    # ---- persistence -------------------------------------------------------
    @classmethod
    def load(cls, root: Path, file: Path = CONFIG_FILE) -> "Config":
        data = None
        if file.exists():
            try:
                data = json.loads(file.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                data = None
        return cls(root, data)

    def save(self, file: Path = CONFIG_FILE) -> None:
        file.write_text(json.dumps(self.data, indent=2, ensure_ascii=False), encoding="utf-8")

    def reset(self) -> None:
        self.data = copy.deepcopy(DEFAULTS)

    # ---- path handling -----------------------------------------------------
    def expand(self, value: str) -> str:
        return os.path.normpath(value.replace(ROOT_TOKEN, str(self.root)))

    def path(self, key: str) -> Path:
        return Path(self.expand(self.data["paths"][key]))

    def raw(self, key: str) -> str:
        return self.data["paths"][key]

    def set_absolute(self, key: str, absolute: str) -> None:
        """Store a user-selected path; relative to ROOT (portable) if it lies below it."""
        self.data["paths"][key] = self.tokenize(absolute)

    def tokenize(self, absolute: str) -> str:
        p = os.path.normpath(absolute)
        root = os.path.normpath(str(self.root))
        try:
            if os.path.commonpath([os.path.normcase(p), os.path.normcase(root)]) == os.path.normcase(root):
                rel = os.path.relpath(p, root)
                return ROOT_TOKEN if rel == "." else f"{ROOT_TOKEN}/{rel.replace(os.sep, '/')}"
        except ValueError:      # different drive -> not below root
            pass
        return p.replace("\\", "/")

    def is_portable(self, key: str) -> bool:
        return self.data["paths"][key].startswith(ROOT_TOKEN)

    def host(self) -> str:
        return self.data.get("host", "127.0.0.1")

    def port(self, key: str) -> int:
        return int(self.data["ports"][key])


def _merge(base: dict, override: dict) -> dict:
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _merge(base[k], v)
        else:
            base[k] = v
    return base
