"""
Logger - Zentrale Logging-Konfiguration
"""

import logging
from pathlib import Path
from datetime import datetime


class LoggerConfig:
    """Konfiguriert und verwaltet Logging"""

    @staticmethod
    def setup_logger(name: str, level: str = "INFO") -> logging.Logger:
        """
        Einrichtet einen Logger mit File und Console Output

        Args:
            name: Logger-Name
            level: Log-Level (DEBUG, INFO, WARNING, ERROR, CRITICAL)

        Returns:
            Konfigurierter Logger
        """
        # Log-Verzeichnis erstellen
        log_dir = Path.home() / ".ai_launcher"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / f"launcher_{datetime.now().strftime('%Y%m%d')}.log"

        # Logger erstellen
        logger = logging.getLogger(name)
        logger.setLevel(getattr(logging, level))

        # Format
        formatter = logging.Formatter(
            '[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )

        # File Handler
        file_handler = logging.FileHandler(log_file, encoding='utf-8')
        file_handler.setLevel(getattr(logging, level))
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

        # Console Handler
        console_handler = logging.StreamHandler()
        console_handler.setLevel(getattr(logging, level))
        console_handler.setFormatter(formatter)
        logger.addHandler(console_handler)

        return logger


# Globale Logger-Instanz
logger = LoggerConfig.setup_logger("AILauncher")
