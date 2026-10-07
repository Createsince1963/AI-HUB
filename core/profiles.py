"""Profiles of the AI_CLI Node launcher (AI_CLI\\profiles\\<name>).

Each profile is a folder with its own login, sessions and settings. The Node launcher owns the layout; this module only
lists / creates the folders the same way `profiles.mjs` does, so the dashboard can pick a profile and hand it over as
`--profile <name>` - the Node profile menu is skipped.

config.json -> "cli_profiles": {"Claude": "default"}  = the profile chosen last (remembered per CLI tool).
"""
from __future__ import annotations

import re
from pathlib import Path

from .config import Config

TOOLS = {"Claude": "claude", "Codex": "codex", "Copilot": "copilot", "Antigravity": "antigravity"}
# CLI name (BAT stem) -> owner marker in the profile's .tool file (same names launcher/profiles.mjs uses)
_BAD_CHARS = re.compile(r'[\\/:*?"<>|]')


def supported(cli: str) -> bool:
    return cli in TOOLS


def root(cfg: Config) -> Path:
    return cfg.path("cli") / "profiles"


def valid_name(name: str) -> bool:
    """Same rules as isValidProfileName() in launcher/profiles.mjs."""
    return bool(name) and not _BAD_CHARS.search(name) and not name.startswith((".", "-")) and name.strip() == name


def _tool_of(folder: Path) -> str:
    try:
        return (folder / ".tool").read_text(encoding="utf-8").strip() or "claude"     # no marker = legacy Claude profile
    except (OSError, UnicodeDecodeError):
        # a corrupt/non-UTF-8 .tool file used to raise UnicodeDecodeError uncaught here, which
        # propagated all the way up through list_profiles()/backend_kind() into Dashboard.refresh()
        # and crashed the 1s UI timer - fall back the same way a missing file already does.
        return "claude"


def list_profiles(cfg: Config, cli: str) -> list[str]:
    """Profiles owned by this CLI tool: 'default' first, then alphabetical."""
    tool = TOOLS[cli]
    try:
        names = [d.name for d in root(cfg).iterdir() if d.is_dir() and valid_name(d.name) and _tool_of(d) == tool]
    except OSError:
        names = []
    return sorted(names, key=lambda n: (n != "default", n.casefold()))


def selected(cfg: Config, cli: str) -> str:
    """Last chosen profile; falls back to 'default', then to the first existing one."""
    names = list_profiles(cfg, cli)
    saved = cfg.data.get("cli_profiles", {}).get(cli, "")
    if saved in names or not names:
        return saved or "default"
    return "default" if "default" in names else names[0]


def select(cfg: Config, cli: str, name: str) -> None:
    cfg.data.setdefault("cli_profiles", {})[cli] = name


def create(cfg: Config, cli: str, name: str) -> Path:
    """New empty profile (same folders as createProfile() in profiles.mjs). Raises ValueError / FileExistsError."""
    name = name.strip()
    if not valid_name(name):
        raise ValueError('invalid name - not allowed: \\ / : * ? " < > |  and no leading "." or "-"')
    if name.casefold() in [n.casefold() for n in list_profiles(cfg, cli)] or (root(cfg) / name).exists():
        raise FileExistsError(f'profile "{name}" already exists')
    d = root(cfg) / name
    if TOOLS[cli] == "claude":
        (d / "claude-config").mkdir(parents=True)
    (d / "npm-cache").mkdir(parents=True, exist_ok=True)
    (d / "npm-global").mkdir(parents=True, exist_ok=True)
    (d / ".tool").write_text(TOOLS[cli] + "\n", encoding="utf-8")
    return d


def cli_args(cfg: Config, cli: str) -> list[str]:
    return ["--profile", selected(cfg, cli)] if supported(cli) else []
