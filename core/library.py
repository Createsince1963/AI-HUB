"""Model lists for the launcher's model pickers (llama.cpp GGUF files, installed Ollama models).

Blocking file access on the external SSD - call from a worker thread, never from the UI thread.
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from .config import Config
from .models import effective_area, parse_quant

# library sub folders that hold parked / old files - never offered in the pickers
SKIP_DIRS = frozenset({"archive", "old"})
# embedding models run in llama-server only with --embedding - not useful as a chat model
EMBED_KEYS = ("embed", "minilm", "bge-", "e5-")

TEST_PREFIX = "[test] "          # label prefix of GGUF files offered straight from X_DOWNLOADS (not approved yet)


def _db_rows(cfg: Config) -> dict[str, dict]:
    """normcase(full path) -> models.db row (portable library + downloads). Read-only connection, safe in a worker
    thread while the Manager tab holds its own connection. Empty dict if the index does not exist yet."""
    from .modeldb import DB_FILE, ROOT_DOWNLOADS, ROOT_PORTABLE
    if not DB_FILE.is_file():
        return {}
    out: dict[str, dict] = {}
    try:
        con = sqlite3.connect(f"file:{DB_FILE.as_posix()}?mode=ro", uri=True, timeout=2)
        con.row_factory = sqlite3.Row
        try:
            for r in con.execute("SELECT * FROM files WHERE root IN (?, ?)", (ROOT_PORTABLE, ROOT_DOWNLOADS)):
                full = os.path.join(cfg.expand(r["base"]), *r["relpath"].split("/"))
                out[os.path.normcase(os.path.normpath(full))] = dict(r)
        finally:
            con.close()
    except sqlite3.Error:
        return {}
    return out


def _walk_gguf(base: Path, skip: frozenset[str] = frozenset()):
    """(full path, file name, category) of every model GGUF under base - mmproj vision adapters are skipped."""
    skip_lower = {s.lower() for s in skip}
    for dirpath, dirs, files in os.walk(base):
        dirs[:] = [d for d in dirs if d.lower() not in skip_lower]
        rel_dir = os.path.relpath(dirpath, base)
        category = "(root)" if rel_dir in ("", ".") else rel_dir.replace("\\", "/").split("/")[0]
        for f in files:
            if f.lower().endswith(".gguf") and "mmproj" not in f.lower():
                yield os.path.normpath(os.path.join(dirpath, f)), f, category


def _label(f: str, size_b: int, quant: str, prefix: str = "") -> str:
    size = size_b / 1024 ** 3
    size_txt = f"{size:.1f} GB" if size >= 1 else f"{size_b / 1024 ** 2:.0f} MB"
    return f"{prefix}{f}   ({size_txt}{', ' + quant if quant and quant.lower() not in f.lower() else ''})"


def list_gguf(cfg: Config, include_downloads: bool = True) -> list[tuple[str, str]]:
    """[(label, config value)] of the GGUF files whose area is LLM, approved library files first (largest first),
    then the not yet approved files in X_DOWNLOADS (prefixed "[test] ") so a download can be tried before it is moved.

    Single source of truth for the area is models.db (Model Manager). Files the index does not know yet fall back to the
    old config "model_areas" and then to the automatic suggestion. Value = path relative to the model library,
    absolute for downloads / other drives (llama.cpp module resolves both)."""
    legacy: dict = cfg.data.get("model_areas", {}) or {}
    rows = _db_rows(cfg)
    models = cfg.path("models")
    bases: list[tuple[Path, bool]] = [(models, False)]
    gguf_dir = Path(cfg.path("gguf"))
    if not _inside(gguf_dir, models):
        bases.append((gguf_dir, False))
    dl = Path(cfg.path("downloads"))
    if include_downloads and not _inside(dl, models):
        bases.append((dl, True))
    seen: set[str] = set()
    lib: list[tuple[int, str, str]] = []
    test: list[tuple[int, str, str]] = []
    for base, is_dl in bases:
        if not base.is_dir():
            continue
        for full, f, category in _walk_gguf(base, SKIP_DIRS):
            if any(k in f.lower() for k in EMBED_KEYS):
                continue
            key = os.path.normcase(full)
            if key in seen:
                continue
            seen.add(key)
            row = rows.get(key)
            try:
                raw = row["area"] if row else legacy.get(cfg.tokenize(full))
            except Exception:        # noqa: BLE001
                raw = None
            if effective_area(raw, f, category) != "LLM":     # only LLM weights (no Flux / image / video GGUFs)
                continue
            try:
                size_b = os.path.getsize(full)
            except OSError:
                continue
            quant = parse_quant(f)
            if is_dl:
                test.append((size_b, _label(f, size_b, quant, TEST_PREFIX), full))
                continue
            try:
                value = os.path.relpath(full, models).replace("\\", "/")
                if value.startswith(".."):
                    value = full
            except ValueError:          # other drive
                value = full
            lib.append((size_b, _label(f, size_b, quant), value))
    lib.sort(key=lambda t: (-t[0], t[1].lower()))         # biggest first
    test.sort(key=lambda t: (-t[0], t[1].lower()))
    return [(label, value) for _s, label, value in lib + test]


def _inside(p: Path, root: Path) -> bool:
    try:
        return os.path.normcase(str(Path(p).resolve())).startswith(os.path.normcase(str(Path(root).resolve())))
    except OSError:
        return False


def list_ollama(cfg: Config) -> list[str]:
    """Installed Ollama models read from the manifests folder - works while the Ollama server is stopped."""
    base = cfg.path("ollama_models") / "manifests"
    out: list[str] = []
    if not base.is_dir():
        return out
    for dirpath, _dirs, files in os.walk(base):
        parts = Path(os.path.relpath(dirpath, base)).parts
        for tag in files:
            p = list(parts)
            if not p or p == ["."]:
                continue
            host, rest = p[0], p[1:]
            if host == "registry.ollama.ai":
                rest = rest[1:] if rest and rest[0] == "library" else rest
                name = "/".join(rest)
            else:
                name = "/".join([host, *rest])
            out.append(f"{name}:{tag}")
    return sorted(out, key=str.lower)
