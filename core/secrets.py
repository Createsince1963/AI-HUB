"""Token storage. On Windows the values are encrypted with DPAPI (bound to the Windows user),
so API keys never sit in config.json as plain text. Elsewhere (tests) a base64 fallback is used."""
from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

from .config import APP_DIR

SECRETS_FILE = APP_DIR / "secrets.json"
_WIN = sys.platform == "win32"


def _protect(data: bytes) -> bytes:
    if not _WIN:
        return data
    import ctypes
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    buf = ctypes.create_string_buffer(data, len(data))
    inp, out = Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), Blob()
    if not ctypes.windll.crypt32.CryptProtectData(ctypes.byref(inp), None, None, None, None, 0, ctypes.byref(out)):
        raise OSError("CryptProtectData failed")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out.pbData)


def _unprotect(data: bytes) -> bytes:
    if not _WIN:
        return data
    import ctypes
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    buf = ctypes.create_string_buffer(data, len(data))
    inp, out = Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), Blob()
    if not ctypes.windll.crypt32.CryptUnprotectData(ctypes.byref(inp), None, None, None, None, 0, ctypes.byref(out)):
        raise OSError("CryptUnprotectData failed")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out.pbData)


def _load() -> dict:
    try:
        return json.loads(SECRETS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def get_secret(name: str) -> str:
    raw = _load().get(name)
    if not raw:
        return ""
    try:
        return _unprotect(base64.b64decode(raw)).decode("utf-8")
    except Exception:       # noqa: BLE001 - wrong user / corrupt file -> treat as empty
        return ""


def set_secret(name: str, value: str) -> None:
    data = _load()
    if value:
        data[name] = base64.b64encode(_protect(value.encode("utf-8"))).decode("ascii")
    else:
        data.pop(name, None)
    SECRETS_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
