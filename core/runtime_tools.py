"""Portable command-line tools in the shared runtime: ExifTool and FFmpeg.

Layout (drive-letter independent, resolved from the "tools" path, default {ROOT}/@Runtime/Tools):
    Tools/exiftool/exiftool.exe
    Tools/ffmpeg/bin/ffmpeg.exe, ffprobe.exe, ffplay.exe

tool_env() returns the environment every child process needs to find these tools without any
global install: the tool folders are prepended to PATH (shutil.which("ffmpeg") works), and the
explicit variables known libraries / ComfyUI custom nodes look at are set:
    FFMPEG, FFMPEG_BINARY, FFPROBE_BINARY   generic (moviepy, pydub, majoor thumbnail cache)
    IMAGEIO_FFMPEG_EXE                       imageio-ffmpeg (ComfyUI video nodes)
    EXIFTOOL_PATH                            generic
    MJR_AM_EXIFTOOL_PATH, MJR_AM_FFPROBE_PATH  majoor-assetsmanager
Missing tools are simply skipped - nothing breaks if the folder is absent.
apply_to_process() puts the same values into this GUI process, so every child (captured,
own console, embedded terminal) inherits them.
"""
from __future__ import annotations

import os
from pathlib import Path

from .config import Config

EXE = ".exe" if os.name == "nt" else ""


def tools_dir(cfg: Config) -> Path:
    return cfg.path("tools")


def binaries(cfg: Config) -> dict[str, Path]:
    """name -> expected executable path (may not exist)."""
    t = tools_dir(cfg)
    fb = t / "ffmpeg" / "bin"
    return {
        "exiftool": t / "exiftool" / f"exiftool{EXE}",
        "ffmpeg": fb / f"ffmpeg{EXE}",
        "ffprobe": fb / f"ffprobe{EXE}",
        "ffplay": fb / f"ffplay{EXE}",
    }


def status(cfg: Config) -> dict[str, bool]:
    return {name: p.is_file() for name, p in binaries(cfg).items()}


def path_dirs(cfg: Config) -> list[Path]:
    """Folders to prepend to PATH (only those holding a tool that exists)."""
    seen: list[Path] = []
    for p in binaries(cfg).values():
        if p.is_file() and p.parent not in seen:
            seen.append(p.parent)
    return seen


def tool_env(cfg: Config, base_path: str | None = None) -> dict[str, str]:
    b = binaries(cfg)
    env: dict[str, str] = {}
    if b["ffmpeg"].is_file():
        ff = str(b["ffmpeg"])
        env.update({"FFMPEG": ff, "FFMPEG_BINARY": ff, "IMAGEIO_FFMPEG_EXE": ff})
    if b["ffprobe"].is_file():
        fp = str(b["ffprobe"])
        env.update({"FFPROBE_BINARY": fp, "MJR_AM_FFPROBE_PATH": fp})
    if b["exiftool"].is_file():
        ex = str(b["exiftool"])
        env.update({"EXIFTOOL_PATH": ex, "MJR_AM_EXIFTOOL_PATH": ex})
    dirs = [str(d) for d in path_dirs(cfg)]
    if dirs:
        current = os.environ.get("PATH", "") if base_path is None else base_path
        parts = [p for p in current.split(os.pathsep) if p and os.path.normcase(p) not in
                 {os.path.normcase(d) for d in dirs}]
        env["PATH"] = os.pathsep.join(dirs + parts)
    return env


def apply_to_process(cfg: Config) -> dict[str, str]:
    """Export tool_env() into os.environ of this process; returns what was set."""
    env = tool_env(cfg)
    os.environ.update(env)
    return env
