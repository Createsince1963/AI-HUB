"""
Config Tab - Konfigurationsmanagement für Tools
"""

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QFormLayout, QGroupBox,
    QLineEdit, QPushButton, QLabel, QSpinBox, QCheckBox,
    QComboBox, QFileDialog, QMessageBox
)
from PySide6.QtCore import Qt
from PySide6.QtGui import QFont


class ConfigTab(QWidget):
    """Tab zur Konfiguration der Tools"""

    def __init__(self, config_manager):
        super().__init__()
        self.config_manager = config_manager

        self.init_ui()

    def init_ui(self):
        """UI-Komponenten initialisieren"""
        main_layout = QVBoxLayout()
        self.setLayout(main_layout)

        # Titel
        title = QLabel("⚙️ Einstellungen")
        title_font = QFont()
        title_font.setPointSize(14)
        title_font.setBold(True)
        title.setFont(title_font)
        main_layout.addWidget(title)

        # Claude Code Config
        claude_group = self.create_tool_config(
            "Claude Code",
            [
                ("Claude Code Pfad:", "claude_path"),
                ("API Key:", "claude_api_key"),
                ("Model:", "claude_model"),
            ]
        )
        main_layout.addWidget(claude_group)

        # Ollama Config
        ollama_group = self.create_tool_config(
            "Ollama",
            [
                ("Ollama Pfad:", "ollama_path"),
                ("Port:", "ollama_port"),
                ("Standard-Modell:", "ollama_model"),
            ]
        )
        main_layout.addWidget(ollama_group)

        # ComfyUI Config
        comfyui_group = self.create_tool_config(
            "ComfyUI",
            [
                ("ComfyUI Pfad:", "comfyui_path"),
                ("Port:", "comfyui_port"),
                ("GPU-Typ:", "comfyui_gpu"),
            ]
        )
        main_layout.addWidget(comfyui_group)

        # OpenWebUI Config
        webui_group = self.create_tool_config(
            "OpenWebUI",
            [
                ("OpenWebUI Pfad:", "webui_path"),
                ("Port:", "webui_port"),
                ("Ollama URL:", "ollama_url"),
            ]
        )
        main_layout.addWidget(webui_group)

        # Speichern-Button
        save_layout = QHBoxLayout()
        self.save_btn = QPushButton("💾 Speichern")
        self.save_btn.clicked.connect(self.save_config)
        self.reset_btn = QPushButton("↶ Zurücksetzen")
        self.reset_btn.clicked.connect(self.reset_config)

        save_layout.addWidget(self.save_btn)
        save_layout.addWidget(self.reset_btn)
        save_layout.addStretch()

        main_layout.addStretch()
        main_layout.addLayout(save_layout)

    def create_tool_config(self, tool_name, fields):
        """Erstellt eine Config-Gruppe für ein Tool"""
        group = QGroupBox(tool_name)
        layout = QFormLayout()

        self.config_fields = getattr(self, 'config_fields', {})
        self.config_fields[tool_name] = {}

        for label, key in fields:
            if "port" in key.lower():
                widget = QSpinBox()
                widget.setRange(1024, 65535)
                widget.setValue(8000)
            elif "gpu" in key.lower() or "model" in key.lower():
                widget = QComboBox()
                widget.addItems(["NVIDIA", "AMD", "Intel", "CPU"])
            else:
                widget = QLineEdit()

            layout.addRow(label, widget)
            self.config_fields[tool_name][key] = widget

        group.setLayout(layout)
        return group

    def save_config(self):
        """Speichert die Konfiguration"""
        print("Speichere Konfiguration...")
        # Implementierung folgt

    def reset_config(self):
        """Setzt die Konfiguration zurück"""
        print("Setze Konfiguration zurück...")
        # Implementierung folgt
