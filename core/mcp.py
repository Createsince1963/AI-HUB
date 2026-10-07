"""MCP wiring for the CLI tools (Claude, Codex, ...).

The launcher owns the MCP configuration, so nothing is written to the host PC
(no C:\\Users\\...\\.claude.json) - the setup moves with the SSD.

  config.json -> "mcp": {
      "servers": {<id>: {"name", "url" ({HOST}/{PORT} placeholders), "port": <ports key>, "module": <launcher module id>}},
      "enabled": {<cli name>: [<server id>, ...]}
  }

Per CLI an adapter turns the enabled servers into extra command-line arguments.
A CLI without an adapter simply gets no MCP button in the dashboard.
"""
from __future__ import annotations

import json
from typing import Callable

from .config import APP_DIR, Config

MCP_DIR = APP_DIR / "mcp"          # generated client configs (rewritten on every start)
LOOPBACK = ("127.0.0.1", "localhost", "::1")


def servers(cfg: Config) -> dict[str, dict]:
    return cfg.data.get("mcp", {}).get("servers", {})


def enabled(cfg: Config, cli: str) -> list[str]:
    ids = cfg.data.get("mcp", {}).get("enabled", {}).get(cli, [])
    return [i for i in ids if i in servers(cfg)]


def is_off(cfg: Config, cli: str) -> bool:
    """MCP switched off for this CLI: the ticked servers stay remembered, but nothing is passed / started."""
    return bool(cfg.data.get("mcp", {}).get("off", {}).get(cli, False))


def set_off(cfg: Config, cli: str, off: bool) -> None:
    cfg.data.setdefault("mcp", {}).setdefault("off", {})[cli] = bool(off)


def active(cfg: Config, cli: str) -> list[str]:
    """Servers that are really used at the next start."""
    return [] if is_off(cfg, cli) else enabled(cfg, cli)


def set_enabled(cfg: Config, cli: str, ids: list[str]) -> None:
    cfg.data.setdefault("mcp", {}).setdefault("enabled", {})[cli] = list(ids)


def server_url(cfg: Config, sid: str) -> str:
    s = servers(cfg)[sid]
    host = cfg.host()
    if host in ("0.0.0.0", ""):
        host = "127.0.0.1"                      # the server listens everywhere, the client talks to itself
    port = cfg.port(s["port"]) if s.get("port") else ""
    return s["url"].replace("{HOST}", host).replace("{PORT}", str(port))


def server_headers(cfg: Config, sid: str) -> dict[str, str]:
    """LAN mode (host is not loopback): the Scrapling MCP server requires its bearer token."""
    if cfg.host() in LOOPBACK or sid != "scrapling":
        return {}
    tok = cfg.path("scrapling") / "mcp_token.txt"
    try:
        return {"Authorization": f"Bearer {tok.read_text(encoding='utf-8').strip()}"}
    except OSError:
        return {}


def required_modules(cfg: Config, cli: str) -> list[str]:
    """Launcher module ids that must be running before the CLI starts (e.g. 'scrapling')."""
    return [servers(cfg)[i]["module"] for i in active(cfg, cli) if servers(cfg)[i].get("module")]


# ---------------------------------------------------------------- adapters
def _claude(cfg: Config, ids: list[str]) -> list[str]:
    """Claude Code: --mcp-config <json file>. Other MCP configs of the user stay active (no --strict-mcp-config)."""
    entries = {}
    for sid in ids:
        e = {"type": "http", "url": server_url(cfg, sid)}
        h = server_headers(cfg, sid)
        if h:
            e["headers"] = h
        entries[sid] = e
    MCP_DIR.mkdir(parents=True, exist_ok=True)
    f = MCP_DIR / "claude.mcp.json"
    f.write_text(json.dumps({"mcpServers": entries}, indent=2), encoding="utf-8")
    return ["--mcp-config", str(f)]


ADAPTERS: dict[str, Callable[[Config, list[str]], list[str]]] = {
    "Claude": _claude,
    # "Codex": ...   next step
}


def supported(cli: str) -> bool:
    return cli in ADAPTERS


def cli_args(cfg: Config, cli: str) -> list[str]:
    ids = active(cfg, cli)
    adapter = ADAPTERS.get(cli)
    return adapter(cfg, ids) if adapter and ids else []
