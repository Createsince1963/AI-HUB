"""
Status Tab - Zeigt den Status aller laufenden Tools
"""

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QTableWidget,
    QTableWidgetItem, QLabel, QPushButton
)
from PySide6.QtCore import Qt
from PySide6.QtGui import QFont, QColor


class StatusTab(QWidget):
    """Tab zur Anzeige des Status aller Tools"""

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
        title = QLabel("📊 Tool Status")
        title_font = QFont()
        title_font.setPointSize(14)
        title_font.setBold(True)
        title.setFont(title_font)
        main_layout.addWidget(title)

        # Status-Tabelle
        self.status_table = QTableWidget()
        self.status_table.setColumnCount(5)
        self.status_table.setHorizontalHeaderLabels([
            "Tool", "Status", "PID", "CPU", "Speicher"
        ])
        self.status_table.setColumnWidth(0, 150)
        self.status_table.setColumnWidth(1, 100)
        self.status_table.setColumnWidth(2, 80)
        self.status_table.setColumnWidth(3, 80)
        self.status_table.setColumnWidth(4, 100)

        main_layout.addWidget(self.status_table)

        # Control-Buttons
        button_layout = QHBoxLayout()

        self.refresh_btn = QPushButton("🔄 Aktualisieren")
        self.refresh_btn.clicked.connect(self.update_status)

        self.logs_btn = QPushButton("📋 Logs anzeigen")
        self.logs_btn.clicked.connect(self.show_logs)

        button_layout.addWidget(self.refresh_btn)
        button_layout.addWidget(self.logs_btn)
        button_layout.addStretch()

        main_layout.addLayout(button_layout)

        # Initial laden
        self.update_status()

    def update_status(self):
        """Aktualisiert die Status-Tabelle"""
        # Dummy-Daten für Template
        tools = [
            ("Claude Code", "Bereit", "-", "0%", "0 MB"),
            ("Ollama", "Läuft", "1234", "2%", "512 MB"),
            ("ComfyUI", "Bereit", "-", "0%", "0 MB"),
            ("OpenWebUI", "Läuft", "5678", "1%", "256 MB"),
            ("Node.js", "Bereit", "-", "0%", "0 MB"),
            ("MCP Server", "Bereit", "-", "0%", "0 MB"),
        ]

        self.status_table.setRowCount(len(tools))

        for row, (tool, status, pid, cpu, mem) in enumerate(tools):
            self.status_table.setItem(row, 0, QTableWidgetItem(tool))

            status_item = QTableWidgetItem(status)
            if status == "Läuft":
                status_item.setBackground(QColor("#4CAF50"))
                status_item.setForeground(QColor("white"))
            else:
                status_item.setBackground(QColor("#FFC107"))
            self.status_table.setItem(row, 1, status_item)

            self.status_table.setItem(row, 2, QTableWidgetItem(pid))
            self.status_table.setItem(row, 3, QTableWidgetItem(cpu))
            self.status_table.setItem(row, 4, QTableWidgetItem(mem))

    def show_logs(self):
        """Zeigt die Logs an"""
        print("Zeige Logs...")
