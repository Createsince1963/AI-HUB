"""
Enhanced Tool Manager - Verwaltet Starten/Stoppen von Tools über .bat Launcher
und Node.js Launcher-Module
"""

import os
import subprocess
import json
import time
from pathlib import Path
from datetime import datetime
from typing import Dict, Optional, List
import requests
import logging

logger = logging.getLogger(__name__)


class ToolManager:
    """Verwaltet KI-Tools mit Portable Launcher-System"""

    def __init__(self, config_manager):
        self.config_manager = config_manager
        self.running_processes: Dict[str, subprocess.Popen] = {}
        self.tool_status: Dict[str, str] = {
            "claude": "ready",
            "ollama": "ready",
            "comfyui": "ready",
            "openwebui": "ready",
            "node": "ready",
            "mcp": "ready",
        }
        self.tool_ports = {
            "ollama": 11434,
            "comfyui": 8188,
            "openwebui": 3000,
        }
        self.ai_tools_root = Path(os.environ.get("AI_ROOT") or Path(__file__).resolve().parents[1])  # drive-independent: AI_ROOT env or parent of AI_Launcher

    def start_tool(self, tool_name: str, wait_for_ready: bool = True) -> bool:
        """
        Startet ein Tool über den Portable Launcher

        Args:
            tool_name: Name des zu startenden Tools
            wait_for_ready: Warte bis Tool bereit ist (prüft Ports)

        Returns:
            True wenn erfolgreich, False sonst
        """
        try:
            if tool_name in self.running_processes:
                logger.warning(f"{tool_name} läuft bereits")
                return False

            # Tool-spezifische Start-Logik
            if tool_name == "ollama":
                success = self._start_ollama()
            elif tool_name == "comfyui":
                success = self._start_comfyui()
            elif tool_name == "claude":
                success = self._start_claude()
            elif tool_name == "openwebui":
                success = self._start_openwebui()
            else:
                logger.error(f"Unbekanntes Tool: {tool_name}")
                return False

            if success:
                self.tool_status[tool_name] = "running"

                # Warte bis Tool ready ist
                if wait_for_ready:
                    self._wait_for_tool(tool_name)

                return True
            else:
                self.tool_status[tool_name] = "error"
                return False

        except Exception as e:
            logger.error(f"Fehler beim Starten von {tool_name}: {e}")
            self.tool_status[tool_name] = "error"
            return False

    def stop_tool(self, tool_name: str) -> bool:
        """Stoppt ein Tool"""
        try:
            if tool_name not in self.running_processes:
                logger.warning(f"{tool_name} läuft nicht")
                return False

            proc = self.running_processes[tool_name]
            proc.terminate()

            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()

            del self.running_processes[tool_name]
            self.tool_status[tool_name] = "ready"
            logger.info(f"✓ {tool_name} gestoppt")
            return True

        except Exception as e:
            logger.error(f"Fehler beim Stoppen von {tool_name}: {e}")
            return False

    def get_tool_status(self, tool_name: str) -> str:
        """Gibt den Status eines Tools zurück"""
        return self.tool_status.get(tool_name, "unknown")

    def get_all_status(self) -> Dict[str, str]:
        """Gibt den Status aller Tools zurück"""
        return self.tool_status.copy()

    # =========================================================================
    # Launcher Implementierungen
    # =========================================================================

    def _start_ollama(self) -> bool:
        """Startet Ollama über Ollama.bat Portable Launcher"""
        ollama_dir = self.ai_tools_root / "Ollama_Portable"
        bat_file = ollama_dir / "Ollama.bat"

        if not bat_file.exists():
            logger.error(f"Ollama.bat nicht gefunden: {bat_file}")
            return False

        try:
            # Starte Ollama.bat (non-blocking)
            proc = subprocess.Popen(
                str(bat_file),
                cwd=str(ollama_dir),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                creationflags=subprocess.CREATE_NEW_CONSOLE,  # Separates Fenster
            )
            self.running_processes["ollama"] = proc
            logger.info(f"✓ Ollama gestartet (PID: {proc.pid})")
            return True
        except Exception as e:
            logger.error(f"Fehler beim Starten von Ollama: {e}")
            return False

    def _start_comfyui(self) -> bool:
        """Startet ComfyUI über ComfyUI.bat Portable Launcher"""
        comfyui_dir = self.ai_tools_root / "ComfyUI_Portable"
        bat_file = comfyui_dir / "ComfyUI.bat"

        if not bat_file.exists():
            logger.error(f"ComfyUI.bat nicht gefunden: {bat_file}")
            return False

        try:
            # Starte ComfyUI.bat
            proc = subprocess.Popen(
                str(bat_file),
                cwd=str(comfyui_dir),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                creationflags=subprocess.CREATE_NEW_CONSOLE,
            )
            self.running_processes["comfyui"] = proc
            logger.info(f"✓ ComfyUI gestartet (PID: {proc.pid})")
            return True
        except Exception as e:
            logger.error(f"Fehler beim Starten von ComfyUI: {e}")
            return False

    def _start_claude(self) -> bool:
        """Startet Claude Code über Claude.bat Portable Launcher"""
        claude_dir = self.ai_tools_root / "AI_CLI_Portable"
        bat_file = claude_dir / "Claude.bat"

        if not bat_file.exists():
            logger.error(f"Claude.bat nicht gefunden: {bat_file}")
            return False

        try:
            proc = subprocess.Popen(
                str(bat_file),
                cwd=str(claude_dir),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                creationflags=subprocess.CREATE_NEW_CONSOLE,
            )
            self.running_processes["claude"] = proc
            logger.info(f"✓ Claude Code gestartet (PID: {proc.pid})")
            return True
        except Exception as e:
            logger.error(f"Fehler beim Starten von Claude: {e}")
            return False

    def _start_openwebui(self) -> bool:
        """Startet Open WebUI (für Ollama Web-Interface)"""
        # Open WebUI kann über Docker oder Pip installiert werden
        # Für portable Version könnte es auch ein separater Launcher sein
        config = self.config_manager.get_tool_config("openwebui")
        port = config.get("port", 3000)

        try:
            cmd = f"python -m openwebui --port {port}"
            proc = subprocess.Popen(
                cmd,
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                creationflags=subprocess.CREATE_NEW_CONSOLE,
            )
            self.running_processes["openwebui"] = proc
            logger.info(f"✓ Open WebUI gestartet (PID: {proc.pid})")
            return True
        except Exception as e:
            logger.error(f"Fehler beim Starten von Open WebUI: {e}")
            return False

    # =========================================================================
    # Helpers
    # =========================================================================

    def _wait_for_tool(self, tool_name: str, timeout: int = 30) -> bool:
        """
        Wartet bis ein Tool bereit ist (prüft Port)

        Args:
            tool_name: Name des Tools
            timeout: Max. Sekunden zu warten

        Returns:
            True wenn Tool ready, False bei Timeout
        """
        if tool_name not in self.tool_ports:
            return True  # Nicht testbar, annahme ready

        port = self.tool_ports[tool_name]
        start = time.time()

        while time.time() - start < timeout:
            try:
                response = requests.get(f"http://localhost:{port}", timeout=1)
                logger.info(f"✓ {tool_name} ist bereit")
                return True
            except:
                time.sleep(1)

        logger.warning(f"⚠ {tool_name} ready-Check abgelaufen (Port {port} nicht erreichbar)")
        return False

    def start_all(self) -> Dict[str, bool]:
        """Startet alle konfigurierten Tools"""
        results = {}
        enabled_tools = [
            tool for tool, cfg in self.config_manager.config.get("tools", {}).items()
            if cfg.get("enabled", True)
        ]

        for tool in enabled_tools:
            results[tool] = self.start_tool(tool)

        return results

    def stop_all(self) -> Dict[str, bool]:
        """Stoppt alle laufenden Tools"""
        results = {}
        for tool in list(self.running_processes.keys()):
            results[tool] = self.stop_tool(tool)

        return results

    def set_ai_tools_root(self, path: str) -> None:
        """Setzt den Root-Pfad für AI Tools"""
        self.ai_tools_root = Path(path)
        logger.info(f"AI Tools Root gesetzt auf: {self.ai_tools_root}")

    def get_tool_info(self, tool_name: str) -> Dict:
        """Gibt Informationen über ein Tool zurück"""
        config = self.config_manager.get_tool_config(tool_name)
        status = self.get_tool_status(tool_name)

        return {
            "name": tool_name,
            "status": status,
            "enabled": config.get("enabled", False),
            "port": self.tool_ports.get(tool_name),
            "config": config,
        }

    def get_all_tools_info(self) -> List[Dict]:
        """Gibt Informationen über alle Tools zurück"""
        tools = []
        for tool_name in self.tool_status.keys():
            tools.append(self.get_tool_info(tool_name))
        return tools
