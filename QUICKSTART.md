# 🚀 AI Launcher Quick-Start Guide

## Installation in 3 Schritten

### 1️⃣ Struktur aufbauen
```bash
# Windows CMD/PowerShell:
cd F:\@AI Tools\AI_Launcher
setup_structure.bat
```

Oder manuell:
- Erstelle Ordner: `ui`, `utils`, `tools`, `assets`
- Verschiebe Dateien gemäß README.md Struktur

### 2️⃣ Virtual Environment & Dependencies
```bash
# Automatisch (über run.bat/run.ps1):
run.bat
# oder
.\run.ps1
```

### 3️⃣ Konfigurieren (Optional)
- Öffne `F:\@AI Tools\AI_Launcher\config.example.json`
- Kopiere zu `~/.ai_launcher/config.json`
- Passe Pfade zu deinen Tools an (Claude, Ollama, ComfyUI, etc.)

## Starten der Anwendung

### Windows Batch (einfach):
```bash
run.bat
```

### PowerShell (mit Optionen):
```powershell
.\run.ps1
# oder mit Verbose Mode:
.\run.ps1 -VerboseMode
```

### Python direkt:
```bash
python main.py
```

## Interface Übersicht

### 🚀 Launcher Tab
- **Verfügbare Tools**: Claude, Ollama, ComfyUI, OpenWebUI, Node.js, MCP
- **Buttons**: Schnellstart für jedes Tool
- **Batch-Operationen**: Alle starten/Alle stoppen

### 📊 Status Tab
- **Live-Monitoring**: CPU, RAM pro Tool
- **PID-Anzeige**: Prozess-Information
- **Log-Viewer**: Debug-Ausgaben

### ⚙️ Config Tab
- **Tool-Pfade**: Wo sind deine Tools installiert
- **Ports**: Anpassung für Konflikte
- **GPU-Einstellungen**: NVIDIA, AMD, Intel, CPU

## Häufige Workflows

### Ollama + OpenWebUI (Web-Interface für LLMs)
```
1. Gehe zu Launcher Tab
2. Klick "🦙 Ollama" → Status wird "Läuft"
3. Klick "🌐 OpenWebUI" → öffnet http://localhost:3000
```

### ComfyUI (Stable Diffusion)
```
1. In Config Tab: GPU-Typ und ComfyUI-Pfad setzen
2. Gehe zu Launcher Tab
3. Klick "🎨 ComfyUI" → öffnet http://localhost:8188
```

### Multiple Tools parallel
```
1. "✓ Alle starten" klicken
2. Status Tab prüfen bis alle "Läuft" zeigen
3. In Browser öffnen was du brauchst
```

## Konfiguration anpassen

### Automatisch (via GUI)
1. Config Tab öffnen
2. Pfade/Ports anpassen
3. "💾 Speichern" klicken

### Manuell (config.json)
```json
{
  "tools": {
    "ollama": {
      "path": "F:\\@AI Tools\\ollama_portable",
      "port": 11434
    },
    "comfyui": {
      "path": "F:\\ComfyUI",
      "gpu": "NVIDIA"
    }
  }
}
```

Speichern unter: `C:\Users\<USERNAME>\.ai_launcher\config.json`

## Troubleshooting

### ❌ "Python not found"
```bash
# Python installieren:
https://www.python.org/downloads/
```

### ❌ Port already in use
```bash
# Windows - finde Prozess auf Port 11434:
netstat -ano | findstr :11434

# In Config Tab auf anderen Port ändern (z.B. 11435)
```

### ❌ Tool startet nicht
1. Config Tab - Pfad korrekt?
2. Terminal - Befehl manuell testen
3. Logs anschauen: `Status Tab → 📋 Logs anzeigen`

### ❌ GPU nicht erkannt
- NVIDIA: CUDA Toolkit installiert?
- AMD: ROCm installiert?
- In Config Tab GPU-Typ anpassen

## Erste Schritte nach Installation

✅ Virtual Environment aktiviert (run.bat macht das automatisch)
✅ Dependencies installiert (requirements.txt)
✅ Main.py lädt ohne Fehler
✅ GUI öffnet sich
✅ Mindestens ein Tool in Config Tab konfiguriert
✅ Tool lässt sich starten

## Nächste Entwicklungsschritte

- [ ] Tool-Handler für jedes Tool implementieren
- [ ] Process-Monitoring erweitern
- [ ] Log-Viewer vollständig machen
- [ ] Automatische Tool-Updates
- [ ] Web-Dashboard
- [ ] Plugin-System
- [ ] Docker-Integration

## Support & Debugging

**Log-Datei:**
```
C:\Users\<USERNAME>\.ai_launcher\launcher.log
```

**Verbose Mode:**
```powershell
.\run.ps1 -VerboseMode
```

**Manueller Test:**
```bash
# Terminal
python main.py
# Schaue auf Console Output
```

---

**Version:** 0.1.0 | **Status:** Beta | **Feedback:** Willkommen! 🎉
