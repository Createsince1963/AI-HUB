"""Model + reasoning-effort selection for the CLI tools (Claude, Codex, Copilot, Antigravity, Happy).

Mirrors the Node launcher's own model-select.mjs exactly, so the GUI and the terminal
picker (`<Tool>.bat --select-model`) read and write the very same files and never disagree:

  AI_CLI/launcher/models.json          -> catalog: models + effort levels per tool (user-editable)
  AI_CLI/profiles/<profile>/.model.json -> the profile's stored choice: {"model": <id>, "effort": <level|null>}

No code change is needed when a provider ships a new model or effort level - edit models.json.
"""
from __future__ import annotations

import json
import re
import os
import time
from dataclasses import dataclass
from pathlib import Path

from .config import Config

CATALOG_RELATIVE = Path("launcher") / "models.json"

# ------------------------------------------------------------------ model library (AI models page, HF downloads)
# File extensions recognised as model weights when scanning the library / importing the audit CSV.
MODEL_EXT = frozenset({".gguf", ".safetensors", ".ckpt", ".pt", ".pth", ".bin", ".onnx"})

# Default "Einsatzbereich" (area) choices offered in the area picker, before any user-defined ones.
AREAS = ["LLM", "Image", "Video", "Audio"]

# Default "Kategorie" (category) choices offered in the category picker, before any folder-derived ones.
# Mirrors the standard AI_Modells subfolder names (see HANDOFF.md).
CATEGORIES = [
    "checkpoints", "loras", "vae", "text_encoders", "diffusion_models",
    "gguf", "controlnet", "upscale_models", "clip_vision", "embeddings",
]


@dataclass(frozen=True)
class LocalModel:
    """One model weight file found on disk by scan_library()."""
    path: str            # absolute path
    name: str             # file name including extension
    size: int             # bytes
    mtime: float          # epoch seconds
    ext: str              # lower-case suffix including the dot, e.g. ".gguf"
    category: str          # first folder under the scanned root, or "(root)"


def scan_library(root: Path | str, skip: frozenset[str] = frozenset()) -> list[LocalModel]:
    """Recursively list every model weight file under root (MODEL_EXT).

    Folders whose name is in `skip` (case-insensitive) are not descended into - used to
    ignore an "old" archive folder inside X_DOWNLOADS, for example."""
    root = Path(root)
    out: list[LocalModel] = []
    if not root.is_dir():
        return out
    skip_lower = {s.lower() for s in skip}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d.lower() not in skip_lower]
        for fname in filenames:
            ext = Path(fname).suffix.lower()
            if ext not in MODEL_EXT:
                continue
            full = os.path.join(dirpath, fname)
            try:
                st = os.stat(full)
            except OSError:
                continue
            try:
                rel_dir = os.path.relpath(dirpath, root)
            except ValueError:          # other drive
                rel_dir = ""
            if rel_dir in ("", "."):
                category = "(root)"
            else:
                category = rel_dir.replace("\\", "/").split("/")[0]
            out.append(LocalModel(path=full, name=fname, size=st.st_size, mtime=st.st_mtime,
                                  ext=ext, category=category))
    return out


def find_duplicates(models: list[LocalModel]) -> set[str]:
    """Paths that share (name, size) with at least one other file in the list."""
    seen: dict[tuple[str, int], str] = {}
    dups: set[str] = set()
    for m in models:
        key = (m.name.lower(), m.size)
        if key in seen:
            dups.add(m.path)
            dups.add(seen[key])
        else:
            seen[key] = m.path
    return dups


def fmt_age(mtime: float) -> str:
    """Human-readable age of a file mtime, e.g. '3h', '5d', '2mo', '1y'."""
    seconds = max(0.0, time.time() - mtime)
    minutes = seconds / 60
    if minutes < 60:
        return f"{max(1, int(minutes))}min"
    hours = minutes / 60
    if hours < 24:
        return f"{int(hours)}h"
    days = hours / 24
    if days < 30:
        return f"{int(days)}d"
    months = days / 30.44
    if months < 12:
        return f"{int(months)}mo"
    return f"{days / 365.25:.1f}y"


def suggest_category(path: str, tags: list[str] | None = None, pipeline: str = "") -> str:
    """Heuristic AI_Modells subfolder for a Hugging Face file, from its path, tags and pipeline_tag."""
    ext = Path(path).suffix.lower()
    name = Path(path).name.lower()
    tags_lower = {t.lower() for t in (tags or [])}
    pipeline = (pipeline or "").lower()

    if ext == ".gguf":
        return "gguf"
    if "vae" in name or "vae" in tags_lower:
        return "vae"
    if "lora" in name or "lora" in tags_lower:
        return "loras"
    if "controlnet" in name or "controlnet" in tags_lower:
        return "controlnet"
    if "upscal" in name or "esrgan" in name:
        return "upscale_models"
    if "clip_vision" in name or "clip-vision" in tags_lower:
        return "clip_vision"
    if "text_encoder" in name or "text-encoder" in name or pipeline in ("feature-extraction",):
        return "text_encoders"
    if pipeline in ("text-to-image", "image-to-image", "text-to-video", "image-to-video"):
        return "diffusion_models" if ext == ".safetensors" and "unet" in name else "checkpoints"
    return "checkpoints"

FALLBACK_CATALOG: dict = {
    "supportsEffort": False,
    "modelFlag": "--model",
    "models": [{"id": "default", "label": "Standard", "default": True}],
}


def catalog_path(cfg: Config) -> Path:
    return cfg.path("cli") / CATALOG_RELATIVE


def load_catalog(cfg: Config) -> dict:
    """Full models.json content, keyed by tool ('claude', 'codex', 'copilot', 'antigravity', 'happy')."""
    try:
        return json.loads(catalog_path(cfg).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def tool_catalog(cfg: Config, tool: str) -> dict:
    """Catalog entry for one tool (lower-case key, e.g. 'claude'), or a safe one-model fallback
    if models.json is missing or doesn't (yet) list this tool."""
    return load_catalog(cfg).get(tool.lower(), FALLBACK_CATALOG)


def _choice_file(cfg: Config, profile_name: str) -> Path:
    return cfg.path("cli") / "profiles" / profile_name / ".model.json"


def get_selected_model(cfg: Config, profile_name: str, tool: str) -> dict:
    """Stored {"model": <id>, "effort": <level|None>} for this profile, or the catalog default
    if nothing has been chosen yet. Same shape and same file the Node launcher writes."""
    tc = tool_catalog(cfg, tool)
    models = tc.get("models") or [{"id": "default", "default": True}]
    fallback_model = next((m["id"] for m in models if m.get("default")), models[0]["id"])
    fallback = {"model": fallback_model, "effort": tc.get("defaultEffort")}
    try:
        saved = json.loads(_choice_file(cfg, profile_name).read_text(encoding="utf-8"))
        if saved and saved.get("model"):
            return {"model": saved["model"], "effort": saved.get("effort", fallback["effort"])}
    except (OSError, ValueError):
        pass
    return fallback


def save_selected_model(cfg: Config, profile_name: str, tool: str, model_id: str, effort: str | None) -> None:
    """Write the profile's choice. Best-effort: a failed write just means the picker prompts
    again next time, same as the Node launcher's own setSelectedModel()."""
    f = _choice_file(cfg, profile_name)
    try:
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps({"model": model_id, "effort": effort}, indent=2), encoding="utf-8")
    except OSError:
        pass


def model_label(cfg: Config, tool: str, model_id: str) -> str:
    for m in tool_catalog(cfg, tool).get("models", []):
        if m["id"] == model_id:
            return m.get("label", model_id)
    return model_id


def effort_label(cfg: Config, tool: str, effort: str | None) -> str:
    if not effort:
        return "-"
    return tool_catalog(cfg, tool).get("effortLabels", {}).get(effort, effort)


def supports_effort(cfg: Config, tool: str) -> bool:
    tc = tool_catalog(cfg, tool)
    return bool(tc.get("supportsEffort")) and bool(tc.get("effortLevels"))


def effort_levels(cfg: Config, tool: str) -> list[str]:
    return tool_catalog(cfg, tool).get("effortLevels", [])


def summary(cfg: Config, profile_name: str, tool: str) -> str:
    """One-line label for buttons: 'Sonnet 4.5 (Extended)' or 'Standard' when nothing was chosen."""
    choice = get_selected_model(cfg, profile_name, tool)
    label = model_label(cfg, tool, choice["model"])
    if choice["model"] == "default" or not choice.get("effort") or not supports_effort(cfg, tool):
        return label
    return f"{label} ({effort_label(cfg, tool, choice['effort'])})"


def suggest_area(filename: str, category: str, suffix: str = ".gguf") -> str:
    """Heuristic classification of model files into areas (LLM, Image, Video, etc).

    Based on filename patterns and directory category hints. Falls back to 'LLM'
    as the default for GGUF models. Users can override in model_areas config.

    Args:
        filename: The model file name (e.g., 'mistral-7b.gguf')
        category: Directory category hint (e.g., 'llm', 'image', 'video')
        suffix: File suffix (e.g., '.gguf')

    Returns:
        Area classification string: 'LLM', 'Image', 'Video', or other
    """
    # Normalize category to lowercase for comparison
    category_lower = category.lower() if category else ""
    filename_lower = filename.lower()

    # Unambiguous image / video model families win over the folder hint - their GGUF builds sit in gguf/ as well
    if any(k in filename_lower for k in STRONG_VIDEO_KEYS):
        return "Video"
    if any(k in filename_lower for k in STRONG_IMAGE_KEYS):
        return "Image"

    # Explicit category hints take priority
    if category_lower in ("llm", "gguf", "lm", "language"):
        return "LLM"
    if category_lower in ("image", "vision", "img", "diffusion", "flux"):
        return "Image"
    if category_lower in ("video", "motion", "animate"):
        return "Video"

    # Pattern-based heuristics for LLM model names
    llm_keywords = {
        "mistral", "llama", "phi", "qwen", "deepseek", "neural", "claude",
        "gpt", "bloom", "falcon", "openchat", "dolphin", "koala", "orca",
        "nous", "solar", "zephyr", "hermes", "tinyllama", "ggml",
    }
    for keyword in llm_keywords:
        if keyword in filename_lower:
            return "LLM"

    # Image model heuristics
    image_keywords = {
        "flux", "diffusion", "sdxl", "sd", "vae", "controlnet", "lora",
        "inpaint", "upscale", "realesrgan", "esrgan", "gfpgan",
    }
    for keyword in image_keywords:
        if keyword in filename_lower:
            return "Image"

    # Video / animation heuristics
    video_keywords = {"video", "motion", "animate", "frame", "svg"}
    for keyword in video_keywords:
        if keyword in filename_lower:
            return "Video"

    # Default to LLM for .gguf files (most common in portable AI setups)
    return "LLM"


# ------------------------------------------------------------------ area / quant / status (model wiring, see MODEL_WIRING.md)
# File name fragments that identify image / video model families regardless of the folder they sit in.
STRONG_IMAGE_KEYS = ("flux", "qwen-image", "qwen_image", "sdxl", "krea", "hidream", "chroma", "z-image", "z_image")
STRONG_VIDEO_KEYS = ("wan2", "wan_2", "ltx", "hunyuanvideo", "hunyuan_video", "cogvideo", "mochi")

# Test gate of a model file: new (just downloaded) -> tested (ran in llama.cpp) -> approved (in the library, offered everywhere)
STATUSES = ("new", "tested", "approved")

_QUANT_RE = re.compile(
    r"(?<![A-Za-z0-9])("
    r"I?Q[1-8](?:_[0-9])?(?:_K)?(?:_(?:XXS|XS|S|M|L|XL|NL))?"   # Q4_K_M, Q8_0, IQ4_XS, Q3_K_XL, Q4_0, IQ2_XXS
    r"|PTQ[0-9](?:_[0-9])?|PQ[0-9](?:_[0-9])?"                 # ternary / packed variants (PTQ1_0, PQ2_0)
    r"|MXFP4|NVFP4|FP8(?:_E4M3FN)?|FP16|BF16|F16|F32"
    r")(?![A-Za-z0-9])", re.IGNORECASE)


def parse_quant(filename: str) -> str:
    """Quantisation tag taken from the file name (last match wins, e.g. '...-BF16-...-Q3_K_XL.gguf' -> 'Q3_K_XL'), '' if none."""
    stem = Path(filename).stem.replace(".", "-")
    hits = _QUANT_RE.findall(stem)
    return hits[-1].upper() if hits else ""


def base_area(area: str | None) -> str | None:
    """Normalise a stored area to one of the plain areas.

    Older versions stored mixed values such as 'LLM - QT5', 'Ollama - Tool-Calling' or 'ComfyUI - VAE'.
    Quantisation and runtime are separate fields now, so only the use case is kept."""
    if not area:
        return None
    a = area.strip()
    low = a.lower()
    if low.startswith(("llm", "ollama")):
        return "LLM"
    if low.startswith("comfyui"):
        return "Image"
    for known in AREAS:
        if low.startswith(known.lower()):
            return known
    return a.split(" - ", 1)[0].strip() or None


def effective_area(area: str | None, filename: str, category: str) -> str:
    """Stored area (normalised) or the automatic suggestion."""
    return base_area(area) or suggest_area(filename, category, Path(filename).suffix.lower())
