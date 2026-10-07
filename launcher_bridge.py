"""
Launcher Bridge - Python ↔ Node.js Communication Layer

Bridges between Qt6 GUI (Python) and portable Launcher modules (Node.js)
for seamless tool management across AI_CLI, ComfyUI, Ollama
"""

import subprocess
import json
import shutil
import time
from pathlib import Path
from typing import Dict, Optional, List, Callable
from dataclasses import dataclass
import logging

logger = logging.getLogger(__name__)


@dataclass
class LauncherConfig:
    """Configuration for a launcher"""
    tool_name: str
    root_dir: Path
    launcher_module: str  # e.g., "launcher.mjs", "comfyui-launcher.mjs"
    node_exe: Path
    bootstrap_bat: Optional[str] = None  # e.g., "ComfyUI.bat"
    args: List[str] = None  # Extra arguments


class LauncherBridge:
    """
    Bridges Python (Qt6 GUI) to Node.js Launcher modules

    Handles:
    - Process spawning with proper environment
    - Event/status callbacks
    - Logging integration
    - Process lifecycle management
    """

    def __init__(self, config: LauncherConfig):
        self.config = config
        self.process: Optional[subprocess.Popen] = None
        self.status_callbacks: List[Callable] = []
        self.log_callbacks: List[Callable] = []

    def start(self, args: Optional[List[str]] = None) -> bool:
        """
        Start the launcher module

        Args:
            args: Additional arguments to pass to launcher

        Returns:
            True if started successfully
        """
        if self.process is not None:
            logger.warning(f"{self.config.tool_name} already running")
            self._call_status("already_running")
            return False

        try:
            # Prepare arguments
            launcher_path = self.config.root_dir / "launcher" / self.config.launcher_module

            if not launcher_path.exists():
                logger.error(f"Launcher module not found: {launcher_path}")
                self._call_status("launcher_not_found")
                return False

            if not self.config.node_exe.exists():
                logger.error(f"Node.js not found: {self.config.node_exe}")
                self._call_status("node_not_found")
                return False

            cmd = [
                str(self.config.node_exe),
                str(launcher_path),
            ]

            # Add base args
            if self.config.args:
                cmd.extend(self.config.args)

            # Add call-specific args
            if args:
                cmd.extend(args)

            # Set environment
            env = self._prepare_environment()

            # Spawn process
            logger.info(f"Starting {self.config.tool_name}: {' '.join(cmd)}")
            self._call_status("starting")

            self.process = subprocess.Popen(
                cmd,
                cwd=str(self.config.root_dir),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                text=True,
                bufsize=1,  # Line buffered
            )

            logger.info(f"✓ {self.config.tool_name} started (PID: {self.process.pid})")
            self._call_status("running")
            self._call_log(f"✓ {self.config.tool_name} started (PID: {self.process.pid})")

            return True

        except Exception as e:
            logger.error(f"Failed to start {self.config.tool_name}: {e}")
            self._call_status("error")
            self._call_log(f"✗ Error starting {self.config.tool_name}: {e}")
            return False

    def stop(self, timeout: int = 5) -> bool:
        """
        Stop the launcher process

        Args:
            timeout: Seconds to wait before force-killing

        Returns:
            True if stopped successfully
        """
        if self.process is None:
            logger.warning(f"{self.config.tool_name} not running")
            return False

        try:
            self._call_status("stopping")
            self._call_log(f"Stopping {self.config.tool_name}...")

            self.process.terminate()

            try:
                self.process.wait(timeout=timeout)
                logger.info(f"✓ {self.config.tool_name} stopped gracefully")
            except subprocess.TimeoutExpired:
                logger.warning(f"Timeout, force-killing {self.config.tool_name}")
                self.process.kill()
                self.process.wait()

            self.process = None
            self._call_status("stopped")
            self._call_log(f"✓ {self.config.tool_name} stopped")
            return True

        except Exception as e:
            logger.error(f"Error stopping {self.config.tool_name}: {e}")
            self._call_status("error")
            return False

    def is_running(self) -> bool:
        """Check if process is running"""
        if self.process is None:
            return False

        return self.process.poll() is None

    def get_status(self) -> str:
        """Get current status"""
        if self.process is None:
            return "stopped"
        elif self.is_running():
            return "running"
        else:
            return "crashed"

    def on_status_change(self, callback: Callable[[str], None]) -> None:
        """Register callback for status changes"""
        self.status_callbacks.append(callback)

    def on_log(self, callback: Callable[[str], None]) -> None:
        """Register callback for log messages"""
        self.log_callbacks.append(callback)

    # =========================================================================
    # Launcher-Specific Methods
    # =========================================================================

    @staticmethod
    def for_ai_cli(root_dir: Path, node_exe: Path) -> "LauncherBridge":
        """Create bridge for AI_CLI launcher"""
        config = LauncherConfig(
            tool_name="AI_CLI",
            root_dir=root_dir,
            launcher_module="launcher.mjs",
            node_exe=node_exe,
            bootstrap_bat="Claude.bat",
        )
        return LauncherBridge(config)

    @staticmethod
    def for_comfyui(root_dir: Path, node_exe: Path) -> "LauncherBridge":
        """Create bridge for ComfyUI launcher"""
        config = LauncherConfig(
            tool_name="ComfyUI",
            root_dir=root_dir,
            launcher_module="comfyui-launcher.mjs",
            node_exe=node_exe,
            bootstrap_bat="ComfyUI.bat",
        )
        return LauncherBridge(config)

    @staticmethod
    def for_ollama(root_dir: Path, node_exe: Path) -> "LauncherBridge":
        """Create bridge for Ollama launcher"""
        config = LauncherConfig(
            tool_name="Ollama",
            root_dir=root_dir,
            launcher_module="ollama-launcher.mjs",
            node_exe=node_exe,
            bootstrap_bat="Ollama.bat",
        )
        return LauncherBridge(config)

    # =========================================================================
    # Private Helpers
    # =========================================================================

    def _prepare_environment(self) -> Dict[str, str]:
        """Prepare environment variables for launcher"""
        import os
        env = os.environ.copy()

        # Add tool-specific paths to PATH
        node_bin = self.config.node_exe.parent
        env["PATH"] = f"{node_bin};{env.get('PATH', '')}"

        # Log level for launcher
        env["LAUNCHER_DEBUG"] = "1"

        return env

    def _call_status(self, status: str) -> None:
        """Call all status callbacks"""
        for callback in self.status_callbacks:
            try:
                callback(status)
            except Exception as e:
                logger.error(f"Error in status callback: {e}")

    def _call_log(self, message: str) -> None:
        """Call all log callbacks"""
        for callback in self.log_callbacks:
            try:
                callback(message)
            except Exception as e:
                logger.error(f"Error in log callback: {e}")


class LauncherFactory:
    """Factory for creating appropriate launcher bridges"""

    def __init__(self, ai_tools_root: Path, node_exe: Path):
        self.ai_tools_root = ai_tools_root
        self.node_exe = node_exe

    def create_launcher(self, tool_name: str) -> Optional[LauncherBridge]:
        """Create launcher for specific tool"""

        if tool_name == "claude":
            return LauncherBridge.for_ai_cli(
                self.ai_tools_root / "AI_CLI",
                self.node_exe,
            )
        elif tool_name == "comfyui":
            return LauncherBridge.for_comfyui(
                self.ai_tools_root / "ComfyUI_Portable",
                self.node_exe,
            )
        elif tool_name == "ollama":
            return LauncherBridge.for_ollama(
                self.ai_tools_root / "Ollama_Portable",
                self.node_exe,
            )
        else:
            logger.error(f"Unknown tool: {tool_name}")
            return None

    def get_node_exe(self) -> Path:
        """Get path to node.exe"""
        return self.node_exe


# Example Usage:
"""
# In Qt6 main window:
from launcher_bridge import LauncherFactory

class MyLauncherApp:
    def __init__(self):
        ai_tools_root = Path(__file__).resolve().parents[1]  # drive-independent
        node_exe = ai_tools_root / "AI_CLI/app/node/node-v22.16.0-win-x64/node.exe"

        self.factory = LauncherFactory(ai_tools_root, node_exe)

    def start_comfyui(self):
        launcher = self.factory.create_launcher("comfyui")

        # Register callbacks
        launcher.on_status_change(self.on_status_change)
        launcher.on_log(self.on_log_message)

        # Start
        launcher.start()

    def on_status_change(self, status: str):
        print(f"Status: {status}")
        # Update GUI

    def on_log_message(self, message: str):
        print(f"Log: {message}")
        # Add to GUI log widget
"""
