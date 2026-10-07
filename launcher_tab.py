"""
Launcher Tab - Haupt-Interface für das Starten der Tools
"""

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGroupBox,
    QPushButton, QLabel, QGridLayout, QCheckBox
)
from PySide6.QtCore import Qt
from PySide6.QtGui import QFont, QColor


class LauncherTab(QWidget):
    """Tab zum Starten und Verwalten der KI-Tools"""

    def __init__(self, tool_manager, config_manager):
        super().__init__()
        self.tool_manager = tool_manager
        self.config_manager = config_manager

        self.init_ui()

    def init_ui(self):
        """UI-Komponenten initialisieren"""
        main_layout = QVBoxLayout()
        self.setLayout(main_layout)

        # Titel
        title = QLabel("🚀 AI-Tools Launcher")
        title_font = QFont()
        title_font.setPointSize(16)
        title_font.setBold(True)
        title.setFont(title_font)
        main_layout.addWidget(title)

        # Tools-Grid
        tools_group = QGroupBox("Verfügbare Tools")
        tools_layout = QGridLayout()

        # Tool-Definitionen
        self.tools = [
            {"name": "Claude Code", "icon": "🤖", "description": "Claude Code CLI", "cmd": "claude"},
            {"name": "Ollama", "icon": "🦙", "description": "Lokale LLM-Server", "cmd": "ollama"},
            {"name": "ComfyUI", "icon": "🎨", "description": "Stable Diffusion UI", "cmd": "comfyui"},
            {"name": "OpenWebUI", "icon": "🌐", "description": "Web-Interface für Ollama", "cmd": "openwebui"},
            {"name": "Node.js", "icon": "📦", "description": "JavaScript Runtime", "cmd": "node"},
            {"name": "MCP Server", "icon": "🔗", "description": "Model Context Protocol", "cmd": "mcp"},
        ]

        row, col = 0, 0
        for tool in self.tools:
            tool_widget = self.create_tool_button(tool)
            tools_layout.addWidget(tool_widget, row, col)
            col += 1
            if col > 2:
                col = 0
                row += 1

        tools_group.setLayout(tools_layout)
        main_layout.addWidget(tools_group)

        # Optionen
        options_group = QGroupBox("Optionen")
        options_layout = QVBoxLayout()

        self.auto_start = QCheckBox("Auto-Start beim Hochfahren")
        self.auto_start.setChecked(False)
        self.verbose = QCheckBox("Verbose Output")
        self.verbose.setChecked(False)

        options_layout.addWidget(self.auto_start)
        options_layout.addWidget(self.verbose)
        options_group.setLayout(options_layout)
        main_layout.addWidget(options_group)

        # Bottom Spacer
        main_layout.addStretch()

        # Button-Reihe
        button_layout = QHBoxLayout()

        self.start_all_btn = QPushButton("✓ Alle starten")
        self.start_all_btn.clicked.connect(self.start_all)
        self.stop_all_btn = QPushButton("⊗ Alle stoppen")
        self.stop_all_btn.clicked.connect(self.stop_all)
        self.refresh_btn = QPushButton("🔄 Aktualisieren")
        self.refresh_btn.clicked.connect(self.refresh)

        button_layout.addWidget(self.start_all_btn)
        button_layout.addWidget(self.stop_all_btn)
        button_layout.addWidget(self.refresh_btn)

        main_layout.addLayout(button_layout)

    def create_tool_button(self, tool):
        """Erstellt einen Button für ein Tool"""
        widget = QWidget()
        layout = QVBoxLayout()

        btn = QPushButton(f"{tool['icon']} {tool['name']}")
        btn.setMinimumHeight(80)
        btn.clicked.connect(lambda: self.start_tool(tool['cmd']))

        desc = QLabel(tool['description'])
        desc.setStyleSheet("color: #666666; font-size: 10px;")

        layout.addWidget(btn)
        layout.addWidget(desc)
        layout.addStretch()

        widget.setLayout(layout)
        return widget

    def start_tool(self, tool_name):
        """Startet ein einzelnes Tool"""
        print(f"Starte Tool: {tool_name}")
        # Implementierung folgt im ToolManager

    def start_all(self):
        """Startet alle konfigurieren Tools"""
        print("Starte alle Tools...")

    def stop_all(self):
        """Stoppt alle laufenden Tools"""
        print("Stoppe alle Tools...")

    def refresh(self):
        """Aktualisiert die Tool-Liste"""
        print("Aktualisiere Tool-Liste...")
