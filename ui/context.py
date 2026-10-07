from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from core.config import Config
from core.downloads import DownloadManager
from core.monitor import Monitor
from core.net import OllamaClient
from core.procs import ProcessManager


@dataclass
class Ctx:
    cfg: Config
    procs: ProcessManager
    downloads: DownloadManager
    monitor: Monitor
    log: Callable[[str], None]
    goto: Callable[[str], None]               # navigate to a page by key
    refresh_services: Callable[[], None]

    def ollama(self) -> OllamaClient:
        return OllamaClient(self.cfg.host(), self.cfg.port("ollama"))

    def vram_gb(self) -> float:
        return float(self.cfg.data.get("vram_gb", 12))
