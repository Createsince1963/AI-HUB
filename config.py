"""
Config Manager - Verwaltet Konfigurationsdatei
"""

import json
from pathlib import Path
from typing import Any, Dict, Optional


class ConfigManager:
    """Verwaltet die Konfiguration des AI Launcher"""

    def __init__(self, config_path: Optional[Path] = None):
        if config_path is None:
            config_path = Path.home() / ".ai_launcher" / "config.json"

        self.config_path = Path(config_path)
        self.config_path.parent.mkdir(parents=True, exist_ok=True)

        self.config = self._load_config()

    def _load_config(self) -> Dict[str, Any]:
        """Lädt die Konfigurationsdatei"""
        if self.config_path.exists():
            try:
                with open(self.config_path, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception as e:
                print(f"Fehler beim Laden der Config: {e}")
                return self._default_config()
        else:
            return self._default_config()

    def _default_config(self) -> Dict[str, Any]:
        """Gibt die Standard-Konfiguration zurück"""
        return {
            "version": "0.1.0",
            "theme": "light",
            "auto_start": False,
            "verbose": False,
            "tools": {
                "claude": {
                    "enabled": True,
                    "path": "",
                    "api_key": "",
                    "model": "claude-opus",
                },
                "ollama": {
                    "enabled": True,
                    "path": "",
                    "port": 11434,
                    "model": "mistral",
                    "url": "http://localhost:11434",
                },
                "comfyui": {
                    "enabled": True,
                    "path": "",
                    "port": 8188,
                    "gpu": "NVIDIA",
                },
                "openwebui": {
                    "enabled": True,
                    "path": "",
                    "port": 3000,
                    "ollama_url": "http://localhost:11434",
                },
                "node": {
                    "enabled": True,
                    "path": "",
                },
                "mcp": {
                    "enabled": True,
                    "path": "",
                    "port": 9000,
                },
            },
            "logging": {
                "level": "INFO",
                "file": str(self.config_path.parent / "launcher.log"),
            },
        }

    def save(self) -> bool:
        """Speichert die Konfiguration"""
        try:
            with open(self.config_path, 'w', encoding='utf-8') as f:
                json.dump(self.config, f, indent=2, ensure_ascii=False)
            print(f"✓ Konfiguration gespeichert: {self.config_path}")
            return True
        except Exception as e:
            print(f"✗ Fehler beim Speichern der Config: {e}")
            return False

    def get(self, key: str, default: Any = None) -> Any:
        """Gibt einen Konfigurationswert zurück"""
        keys = key.split(".")
        value = self.config

        for k in keys:
            if isinstance(value, dict):
                value = value.get(k, default)
            else:
                return default

        return value

    def set(self, key: str, value: Any) -> bool:
        """Setzt einen Konfigurationswert"""
        try:
            keys = key.split(".")
            config = self.config

            for k in keys[:-1]:
                if k not in config:
                    config[k] = {}
                config = config[k]

            config[keys[-1]] = value
            return self.save()
        except Exception as e:
            print(f"Fehler beim Setzen von {key}: {e}")
            return False

    def get_tool_config(self, tool_name: str) -> Dict[str, Any]:
        """Gibt die Konfiguration eines Tools zurück"""
        return self.config.get("tools", {}).get(tool_name, {})

    def set_tool_config(self, tool_name: str, config: Dict[str, Any]) -> bool:
        """Setzt die Konfiguration eines Tools"""
        if "tools" not in self.config:
            self.config["tools"] = {}

        self.config["tools"][tool_name] = config
        return self.save()

    def reset_to_defaults(self) -> bool:
        """Setzt die Konfiguration auf Standard zurück"""
        self.config = self._default_config()
        return self.save()
