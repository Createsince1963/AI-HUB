# AI Launcher 🚀

Eine moderne Qt6/PySide6-basierte GUI zur zentralisierten Administration und zum Starten von KI-Tools auf lokalen Systemen.

## Features

✨ **Zentrale Verwaltung**
- Intuitive Qt6/PySide6 Benutzeroberfläche
- Multi-Tool Support: Claude Code, Ollama, ComfyUI, OpenWebUI, Node.js, MCP

📊 **Status-Monitoring**
- Echtzeit-Status aller Tools
- CPU/RAM-Auslastung
- Process-Information (PID, Logs)

⚙️ **Flexible Konfiguration**
- Tool-spezifische Einstellungen
- Pfad-Management für verschiedene Installationen
- Automatischer Start beim Hochfahren (optional)

🔧 **Entwickler-freundlich**
- Modulare Architektur
- Clean Code Principles
- Einfache Erweiterbarkeit für neue Tools

## Projektstruktur

```
AI_Launcher/
├── main.py                 # Hauptanwendung & Einstiegspunkt
├── requirements.txt        # Python Dependencies
├── README.md              # Diese Datei
├── config.example.json    # Beispiel-Konfiguration
│
├── ui/
│   ├── __init__.py
│   ├── launcher_tab.py    # Haupt-Launcher Interface
│   ├── status_tab.py      # Status & Monitoring
│   └── config_tab.py      # Konfiguration & Einstellungen
│
├── utils/
│   ├── __init__.py
│   ├── tool_manager.py    # Tool-Lebenzyklus Management
│   ├── config.py          # Konfigurationsmanagement
│   └── logger.py          # Logging & Debugging
│
├── tools/                 # Tool-spezifische Module
│   ├── __init__.py
│   ├── claude_handler.py
│   ├── ollama_handler.py
│   ├── comfyui_handler.py
│   ├── openwebui_handler.py
│   ├── node_handler.py
│   └── mcp_handler.py
│
└── assets/                # Ressourcen
    ├── icons/
    └── styles/
```

## Installation

### Voraussetzungen

- Python 3.9+
- Virtual Environment (optional aber empfohlen)
- Die eigentlichen KI-Tools (Claude Code, Ollama, etc.)

### Setup

1. **Repository klonen/Verzeichnis vorbereiten**
   ```bash
   cd F:\@AI Tools\AI_Launcher
   ```

2. **Virtual Environment erstellen**
   ```bash
   python -m venv venv
   venv\Scripts\activate
   ```

3. **Dependencies installieren**
   ```bash
   pip install -r requirements.txt
   ```

4. **Anwendung starten**
   ```bash
   python main.py
   ```

## Verwendung

### Grundlegende Bedienung

1. **Launcher Tab** 🚀
   - Tools durch Buttons starten
   - "Alle starten" / "Alle stoppen" für Batch-Operationen
   - Auto-Start Option für häufig genutzte Tools

2. **Status Tab** 📊
   - Live-Überwachung aller laufenden Tools
   - CPU/RAM-Verbrauch pro Tool
   - Log-Anzeige für Debugging

3. **Config Tab** ⚙️
   - Tool-Pfade einstellen
   - API-Keys konfigurieren
   - Port-Einstellungen anpassen

### Beispiel-Workflows

**Ollama + OpenWebUI starten:**
1. Gehe zum Launcher Tab
2. Klicke "🦙 Ollama" → Warte bis Status "Läuft" zeigt
3. Klicke "🌐 OpenWebUI" → öffnet sich automatisch auf http://localhost:3000

**ComfyUI für Image Generation:**
1. Stelle in Config Tab GPU-Typ und ComfyUI-Pfad ein
2. Klicke "🎨 ComfyUI" im Launcher
3. Öffnet sich automatisch auf http://localhost:8188

## Konfiguration

### Automatische Konfiguration
Die Anwendung speichert Einstellungen in: `~/.ai_launcher/config.json`

### Manuelle Anpassung (config.json)
```json
{
  "tools": {
    "ollama": {
      "enabled": true,
      "path": "C:\\Programs\\ollama",
      "port": 11434,
      "model": "mistral"
    },
    "comfyui": {
      "enabled": true,
      "path": "F:\\ComfyUI",
      "gpu": "NVIDIA"
    }
  }
}
```

## Entwicklung

### Neues Tool hinzufügen

1. **Tool-Handler erstellen** (`tools/my_tool_handler.py`):
```python
class MyToolHandler:
    def __init__(self, config_manager):
        self.config = config_manager
    
    def start(self) -> bool:
        # Implementierung
        pass
    
    def stop(self) -> bool:
        # Implementierung
        pass
```

2. **In ToolManager registrieren**:
```python
def start_tool(self, tool_name: str) -> bool:
    if tool_name == "my_tool":
        return self._start_my_tool()
```

3. **UI-Button hinzufügen** (launcher_tab.py):
```python
self.tools.append({
    "name": "My Tool",
    "icon": "🔧",
    "description": "Beschreibung",
    "cmd": "my_tool"
})
```

### Debugging

- Logs befinden sich in: `~/.ai_launcher/launcher.log`
- Starten mit `--verbose` für erweiterte Output
- Checks in Status Tab für Echtzeit-Diagnostik

## Roadmap

- [ ] Erweiterte Process-Überwachung (Memory-Limits)
- [ ] Web-Dashboard für Remote-Zugriff
- [ ] Docker-Integration für isolierte Umgebungen
- [ ] Automatische Tool-Updates
- [ ] Custom Plugins für spezielle Tools
- [ ] Performance-Optimierung für RTX 5070
- [ ] Integration mit ExifTool für Metadaten

## Troubleshooting

### Tool startet nicht
1. Prüfe Config Tab - ist der Pfad korrekt?
2. Öffne Terminal und teste Befehl manuell
3. Prüfe Logs: `~/.ai_launcher/launcher.log`

### Port-Konflikte
1. Ändere Port in Config Tab
2. Oder stoppe andere Anwendungen die den Port nutzen
3. `netstat -ano | findstr :PORT` (Windows) zur Diagnose

### Memory-Probleme
1. Reduziere Anzahl gleichzeitig laufender Tools
2. Prüfe GPU-Speicher Einstellungen in ComfyUI
3. Nutze Status Tab zum Monitoring

## Lizenz

MIT License - Siehe LICENSE Datei

## Support

Bei Fragen/Problemen:
1. Prüfe README & Dokumentation
2. Schau in den Logs
3. Teste manuell im Terminal
4. Kontaktiere Support

---

**Version:** 0.1.0 | **Letztes Update:** 2026-09-19
