"""Ollama helpers for the Model Manager: activate a GGUF file as an Ollama model, list installed models.

Blocking calls - run them from a worker thread (ui.widgets.run_async), never from the GUI thread.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from .config import Config
from .library import list_ollama


def normalize(name: str) -> str:
    """Ollama model name with explicit tag ("qwen3-8b" -> "qwen3-8b:latest"), lower case."""
    n = name.strip().lower()
    return n if ":" in n else f"{n}:latest"


def sanitize_name(stem: str) -> str:
    """Suggest an Ollama model name from a GGUF file name: lower case, only [a-z0-9._-]."""
    n = re.sub(r"[^a-z0-9._-]+", "-", stem.lower()).strip("-.")
    return n or "model"


def installed(cfg: Config) -> set[str]:
    """Names of the Ollama models installed on disk (manifests folder) - works while the server is stopped."""
    return {normalize(n) for n in list_ollama(cfg)}


def find_exe(cfg: Config) -> Path | None:
    base = cfg.path("ollama")
    for cand in (base / "ollama.exe", base / "bin" / "ollama.exe", base / "ollama"):
        if cand.is_file():
            return cand
    found = shutil.which("ollama")
    return Path(found) if found else None


def create_from_gguf(cfg: Config, name: str, gguf: str) -> str:
    """`ollama create <name>` from a GGUF file. Ollama copies the weights into its own store, so the GGUF file is
    not needed afterwards. Needs the Ollama server to be running. Returns the CLI output, raises RuntimeError on failure."""
    exe = find_exe(cfg)
    if exe is None:
        raise RuntimeError("ollama executable not found (Settings > Ollama path)")
    src = Path(gguf)
    if not src.is_file():
        raise RuntimeError(f"GGUF file not found: {src}")
    fname = src.name
    ref = f'"./{fname}"' if " " in fname else f"./{fname}"
    # Modelfile next to the GGUF so that the relative FROM path resolves (paths with spaces such as "@AI Tools" are a
    # problem otherwise); removed again in any case.
    for stale in src.parent.glob(".Modelfile_*"):         # leftovers of imports that were killed before the cleanup below
        try:
            if stale.is_file() and stale.stat().st_size < 512:
                stale.unlink()
        except OSError:
            pass
    fd, tmp = tempfile.mkstemp(prefix=".Modelfile_", dir=str(src.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(f"FROM {ref}\n")
        env = dict(os.environ)
        env["OLLAMA_MODELS"] = str(cfg.path("ollama_models"))
        host = "127.0.0.1" if cfg.host() in ("0.0.0.0", "") else cfg.host()
        env["OLLAMA_HOST"] = f"{host}:{cfg.port('ollama')}"
        flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        p = subprocess.run([str(exe), "create", name, "-f", tmp], cwd=str(src.parent), env=env, capture_output=True,
                           text=True, timeout=1800, creationflags=flags)
        out = (p.stdout + "\n" + p.stderr).strip()
        if p.returncode != 0:
            raise RuntimeError(out or f"ollama create failed (exit {p.returncode})")
        return out
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


# ====================================================================== store inspection / zero-copy import
# Ollama keeps weights as content-addressed blobs (models\blobs\sha256-<hex>) and names in models\manifests.
# For a GGUF import the model blob is the GGUF file byte for byte, so a HARD LINK to the library file can serve as
# the blob: `ollama create` finds the digest already present and copies nothing. Needs NTFS and the same volume;
# otherwise everything falls back to Ollama's normal copy. `ollama rm` then only removes the link - the GGUF stays.
import hashlib
import json

MODEL_MEDIA = "application/vnd.ollama.image.model"


def blobs_dir(cfg: Config) -> Path:
    return cfg.path("ollama_models") / "blobs"


def blob_path(cfg: Config, sha_hex: str) -> Path:
    return blobs_dir(cfg) / f"sha256-{sha_hex.lower()}"


def sha256_of(path: str, chunk: int = 8 * 1024 * 1024) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            b = fh.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def manifests(cfg: Config) -> dict[str, list[str]]:
    """Installed model name (normalized) -> digests (hex) of its model-weight layers. Offline (reads the files)."""
    base = cfg.path("ollama_models") / "manifests"
    out: dict[str, list[str]] = {}
    if not base.is_dir():
        return out
    for dirpath, _d, files in os.walk(base):
        parts = Path(os.path.relpath(dirpath, base)).parts
        if not parts or parts == (".",):
            continue
        host, rest = parts[0], list(parts[1:])
        if host == "registry.ollama.ai" and rest and rest[0] == "library":
            rest = rest[1:]
        prefix = "/".join(rest) if host == "registry.ollama.ai" else "/".join([host, *rest])
        for tag in files:
            try:
                data = json.loads(Path(dirpath, tag).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            digests = [l.get("digest", "").split(":", 1)[-1].lower() for l in data.get("layers", [])
                       if l.get("mediaType") == MODEL_MEDIA]
            out[normalize(f"{prefix}:{tag}")] = digests
    return out


def is_linked(a: str | Path, b: str | Path) -> bool:
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def link_blob(cfg: Config, gguf: str, sha_hex: str) -> str:
    """Make models\\blobs\\sha256-<hex> a hard link to `gguf`. Returns 'linked', 'exists-linked', 'exists-copy' or
    'failed: <reason>'. Never deletes or overwrites anything."""
    dst = blob_path(cfg, sha_hex)
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        return "exists-linked" if is_linked(dst, gguf) else "exists-copy"
    try:
        os.link(gguf, dst)
        return "linked"
    except OSError as exc:
        return f"failed: {exc}"


def create_linked(cfg: Config, name: str, gguf: str, sha_hex: str | None = None, log=None) -> tuple[str, str]:
    """Zero-copy activation: hash -> hard link as blob -> ollama create. Returns (sha_hex, link_state).
    Falls back to a normal copying `ollama create` when the link is not possible."""
    say = log or (lambda _m: None)
    if not sha_hex:
        say(f"SHA256 of {os.path.basename(gguf)} ...")
        sha_hex = sha256_of(gguf)
    state = link_blob(cfg, gguf, sha_hex)
    say(f"blob {state}" + ("" if state.startswith(("linked", "exists-linked")) else " - Ollama will store a copy"))
    create_from_gguf(cfg, name, gguf)
    return sha_hex.lower(), state


def dedupe_blob(cfg: Config, gguf: str, sha_hex: str | None = None) -> tuple[bool, str]:
    """Replace an existing Ollama blob COPY by a hard link to the identical library file (frees its size).
    Safety: the library file's SHA256 must equal the blob digest; the blob is renamed first and only removed after
    the link exists. Ollama should not be generating with that model meanwhile."""
    sha_hex = (sha_hex or sha256_of(gguf)).lower()
    dst = blob_path(cfg, sha_hex)
    if not dst.is_file():
        return False, "no Ollama blob with this hash (model not installed from this file)"
    if is_linked(dst, gguf):
        return True, "already linked"
    if os.path.getsize(dst) != os.path.getsize(gguf):
        return False, "size differs"
    tmp = dst.with_name(dst.name + ".dedupe-old")
    os.replace(dst, tmp)
    try:
        os.link(gguf, dst)
    except OSError as exc:
        os.replace(tmp, dst)                    # restore
        return False, f"hard link not possible: {exc}"
    os.remove(tmp)
    return True, "deduplicated"


def storage(cfg: Config, library_files: list[dict]) -> dict:
    """Sizes for the overview. library_files: rows with 'path', 'size', 'hash' (hex or None).
    duplicate = Ollama blobs that are a separate copy of a library file (same hash, or same size when unhashed);
    shared = blobs that are hard links to a library file (cost nothing)."""
    lib_total = sum(f["size"] for f in library_files)
    by_hash = {f["hash"].lower(): f for f in library_files if f.get("hash")}
    by_size: dict[int, list[dict]] = {}
    for f in library_files:
        by_size.setdefault(f["size"], []).append(f)
    oll = dup = shared = 0
    dup_items: list[tuple[str, int, str]] = []
    bd = blobs_dir(cfg)
    if bd.is_dir():
        for b in bd.iterdir():
            if not b.name.startswith("sha256-") or "-partial" in b.name or not b.is_file():
                continue
            st = b.stat()
            oll += st.st_size
            digest = b.name[7:]
            f = by_hash.get(digest)
            cands = [f] if f else [c for c in by_size.get(st.st_size, []) if st.st_size > 50_000_000]
            if not cands:
                continue
            if any(is_linked(b, c["path"]) for c in cands):
                shared += st.st_size
            else:
                dup += st.st_size
                dup_items.append((cands[0]["path"], st.st_size, "hash" if f else "size"))
    return {"library": lib_total, "ollama": oll, "duplicate": dup, "shared": shared, "dup_items": dup_items}


def remove_model(cfg: Config, name: str) -> str:
    """`ollama rm <name>` (server must run). A hard-linked blob only loses its link - the library file stays."""
    exe = find_exe(cfg)
    if exe is None:
        raise RuntimeError("ollama executable not found")
    env = dict(os.environ)
    env["OLLAMA_MODELS"] = str(cfg.path("ollama_models"))
    host = "127.0.0.1" if cfg.host() in ("0.0.0.0", "") else cfg.host()
    env["OLLAMA_HOST"] = f"{host}:{cfg.port('ollama')}"
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    p = subprocess.run([str(exe), "rm", name], env=env, capture_output=True, text=True, timeout=120, creationflags=flags)
    out = (p.stdout + "\n" + p.stderr).strip()
    if p.returncode != 0:
        raise RuntimeError(out or f"ollama rm failed (exit {p.returncode})")
    return out


def link_capability(cfg: Config, library_dir: Path) -> tuple[bool, str]:
    """Can the library and the Ollama store share files via hard links? Tries a tiny real link and removes it."""
    store = blobs_dir(cfg)
    try:
        store.mkdir(parents=True, exist_ok=True)
        library_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return False, f"folder not writable: {exc}"
    src = library_dir / ".linktest.tmp"
    dst = store / ".linktest.tmp"
    try:
        src.write_bytes(b"x")
        if dst.exists():
            dst.unlink()
        os.link(src, dst)
        ok = is_linked(src, dst)
        return ok, "hard links work - no duplicate storage" if ok else "link created but not identical"
    except OSError as exc:
        return False, f"hard links NOT possible ({exc}) - Ollama will copy (different drive or not NTFS)"
    finally:
        for p in (dst, src):
            try:
                p.unlink()
            except OSError:
                pass


def export_blob(cfg: Config, digest: str, dest: Path) -> str:
    """Make a pulled Ollama model usable by llama.cpp / the library: hard link its weight blob as <dest>.gguf.
    Returns 'linked', 'exists' or 'copy-needed: <reason>'. Never copies 10+ GB silently."""
    src = blob_path(cfg, digest)
    if not src.is_file():
        return "copy-needed: blob not found"
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        return "exists"
    with open(src, "rb") as fh:
        if fh.read(4) != b"GGUF":
            return "copy-needed: blob is not a GGUF file"
    try:
        os.link(src, dest)
        return "linked"
    except OSError as exc:
        return f"copy-needed: {exc}"
