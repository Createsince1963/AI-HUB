# OctoAlly-Integration in den AI Portable HUB

**Projekt:** AI Workspace Control Center / AI Portable HUB  
**Zielplattform:** Windows, portable Installation  
**Workspace-Root:** `F:\@AI Tools`  
**Integrationsrolle:** Claude-Code- und OpenAI-Codex-Orchestrierung  
**Status:** Integrationskonzept und Coding-Auftrag

---

## 1. Ziel

OctoAlly wird nicht als Ersatz für den bestehenden AI Launcher verwendet. Es wird als eigenständiges Modul für Claude Code, OpenAI Codex, Agenten, Projekt-Sessions, Terminals und Git-Funktionen eingebunden.

Der AI Launcher bleibt die zentrale Steuerungsinstanz für:

- portable Pfade und gemeinsame Einstellungen
- Ollama
- llama.cpp
- lokalen Modellpool
- ComfyUI
- MCP-Server
- Hardware- und Prozessüberwachung
- Launcher, Runtime und Logs
- Start, Stopp und Status aller Module

OctoAlly ergänzt:

- Claude-Code-Sessions
- OpenAI-Codex-Sessions
- Agenten-Sessions
- Multi-Projekt-Verwaltung
- Live-Terminalausgabe
- Session-Persistenz
- Git-Diffs und Git-Historie
- Datei- und Projektansicht

## 2. Zielarchitektur

```text
F:\@AI Tools
|
+-- AI Workspace Control Center
|   +-- Home
|   +-- Projects
|   +-- Claude / Codex        -> OctoAlly WebView
|   +-- Ollama
|   +-- llama.cpp
|   +-- ComfyUI
|   +-- MCP
|   +-- Models
|   +-- Hardware
|   +-- Logs
|   +-- Settings
|
+-- @Runtime
|   +-- Node
|   +-- Git
|   +-- optional Unix/tmux compatibility layer
|
+-- AI_Modells
+-- AI_Ollama_CCP
+-- ComfyUI
+-- OctoAlly
|   +-- app
|   +-- data
|   +-- logs
|   +-- config
|   +-- updates
|
+-- Projects
```

## 3. Klare Systemgrenzen

### AI Launcher ist verantwortlich für

1. Ermittlung von `AI_TOOLS_ROOT` aus dem eigenen Startpfad.
2. Aufbau aller portablen Pfade relativ zum Root.
3. Bereitstellung der gemeinsamen Runtime in `PATH`.
4. Start und Stopp des OctoAlly-Prozesses.
5. Portprüfung und Statusüberwachung.
6. Einbettung des lokalen Dashboards.
7. zentrale Protokollierung.
8. optionale Sicherung von Konfiguration und Sitzungsdaten.
9. Anzeige verständlicher Fehlerzustände.

### OctoAlly ist verantwortlich für

1. Verwaltung von Claude-Code- und Codex-Sessions.
2. projektbezogene Agenten und Instruktionsdateien.
3. Live-Ausgabe und interaktive Terminals.
4. Git-Ansicht, Diffs und Commits.
5. eigene Sitzungs- und Projektdaten.

### Nicht in OctoAlly integrieren

- Ollama-Modellverwaltung
- llama.cpp-Serververwaltung
- ComfyUI-Prozesssteuerung
- zentralen Modellpool
- Hardware-Monitoring
- allgemeine MCP-Verwaltung

Diese Funktionen bleiben im AI Launcher. Dadurch werden unnötige Änderungen am OctoAlly-Fork vermieden.

## 4. Wichtige technische Einschränkung

OctoAlly dokumentiert Linux und macOS als vollständig unterstützte Plattformen. Die Sitzungs-Persistenz basiert auf `tmux`. Eine native portable Windows-Integration darf deshalb nicht ungeprüft vorausgesetzt werden.

Vor der produktiven Einbindung ist ein Windows-Kompatibilitätstest erforderlich für:

- Start von Server und Dashboard
- Claude-Code-Aufruf
- Codex-Aufruf
- Terminal-Backend
- `tmux`-abhängige Funktionen
- Session-Wiederaufnahme
- Git-Operationen
- Electron-App, falls verwendet
- Pfade mit Leerzeichen und `@` im Verzeichnisnamen

### Empfohlene Windows-Strategie

**Stufe 1:** Dashboard und Server nativ unter portablem Node.js testen.  
**Stufe 2:** Terminal- und Persistenzfunktionen separat validieren.  
**Stufe 3:** Falls `tmux` nativ blockiert, OctoAlly gekapselt über WSL2 betreiben und nur das lokale Web-Dashboard im AI Launcher einbetten.  
**Stufe 4:** Erst nach erfolgreichem Test Änderungen am Fork vornehmen.

WSL2 ist eine mögliche Kompatibilitätslösung, aber keine vollständig portable Lösung. Die native Windows-Variante bleibt das bevorzugte Ziel.

## 5. Verzeichnis- und Datenkonzept

```text
F:\@AI Tools\OctoAlly\
+-- app\                 # Repository oder installierte Anwendung
+-- config\              # Launcher-seitige Einstellungen
+-- data\                # persistente OctoAlly-Daten
+-- logs\                # stdout, stderr und Launcher-Logs
+-- updates\             # temporäre Update-Dateien
+-- backup\              # optionale Konfigurationssicherungen
```

Keine persistenten Daten dürfen ausschließlich in einem Benutzerprofil oder in einem temporären Verzeichnis liegen, sofern OctoAlly eine konfigurierbare Alternative anbietet.

## 6. Zentrale Launcher-Konfiguration

Beispiel für die zentrale Settings-Struktur:

```json
{
  "modules": {
    "octoally": {
      "enabled": true,
      "installDir": "${AI_TOOLS_ROOT}\\OctoAlly\\app",
      "dataDir": "${AI_TOOLS_ROOT}\\OctoAlly\\data",
      "logDir": "${AI_TOOLS_ROOT}\\OctoAlly\\logs",
      "host": "127.0.0.1",
      "port": 42010,
      "autoStart": false,
      "autoOpen": false,
      "embedWebView": true,
      "startMode": "native",
      "healthCheckUrl": "http://127.0.0.1:42010",
      "startupCommand": "octoally start",
      "shutdownCommand": "octoally stop"
    }
  }
}
```

Hinweis: Befehle, Datenpfade und Umgebungsvariablen sind beim Proof of Concept gegen die tatsächlich installierte OctoAlly-Version zu validieren. Nicht dokumentierte OctoAlly-Parameter dürfen nicht erfunden oder fest verdrahtet werden.

## 7. Launcher-Oberfläche

### Neuer Hauptbereich

```text
Claude / Codex
```

### Ansichten

1. **Dashboard**  
   Eingebettete OctoAlly-Weboberfläche.

2. **Status**  
   Prozessstatus, Portstatus, URL, PID, Startmodus und letzte Meldung.

3. **Steuerung**  
   Start, Stopp, Neustart, Browser öffnen und Log öffnen.

4. **Konfiguration**  
   Installationspfad, Port, Startmodus, Autostart und WebView-Verhalten.

5. **Diagnose**  
   Node.js, Git, Claude Code, Codex, Terminal-Backend sowie Schreibrechte prüfen.

### Statuszustände

- Nicht installiert
- Installiert, gestoppt
- Startet
- Läuft
- Port belegt
- Fehler
- Update verfügbar
- Abhängigkeit fehlt
- Kompatibilitätsmodus aktiv

## 8. Startlogik

```text
Benutzer öffnet Claude/Codex
        |
        v
Prüfe OctoAlly-Installation
        |
        +-- fehlt -> Installationshinweis anzeigen
        |
        v
Prüfe portable Node-Runtime und Git
        |
        +-- Fehler -> Diagnose anzeigen
        |
        v
Prüfe konfigurierten Port
        |
        +-- belegt durch OctoAlly -> vorhandene Instanz verwenden
        +-- belegt durch Fremdprozess -> Konflikt melden
        |
        v
Starte OctoAlly mit vollständiger Umgebung
        |
        v
Wiederhole lokalen Health Check mit begrenzten Versuchen
        |
        +-- erfolgreich -> WebView laden
        +-- fehlgeschlagen -> Prozess- und Fehlerlog anzeigen
```

## 9. Prozess- und Portmanagement

Der Launcher muss:

- vor dem Start auf bestehende Instanzen prüfen
- Doppelstarts verhindern
- PID und Startzeit erfassen
- stdout und stderr in getrennte Dateien schreiben
- den Port nicht durch hartes Prozessbeenden freigeben
- nur Prozesse beenden, die eindeutig zur gestarteten OctoAlly-Instanz gehören
- nach Absturz einen klaren Fehlerstatus anzeigen
- bei Launcher-Ende konfigurierbar entscheiden, ob OctoAlly weiterläuft

Empfohlene Logs:

```text
F:\@AI Tools\OctoAlly\logs\octoally-launcher.log
F:\@AI Tools\OctoAlly\logs\octoally-stdout.log
F:\@AI Tools\OctoAlly\logs\octoally-stderr.log
```

## 10. Umgebungsvariablen

Der Starter erzeugt eine isolierte Prozessumgebung. Beispiel:

```text
AI_TOOLS_ROOT=F:\@AI Tools
OCTOALLY_INSTALL_DIR=F:\@AI Tools\OctoAlly\app
PATH=F:\@AI Tools\@Runtime\Node;F:\@AI Tools\@Runtime\Git\cmd;<bestehender PATH>
```

Nur von OctoAlly offiziell unterstützte Variablen dürfen an OctoAlly übergeben werden. Launcher-interne Variablen können frei verwendet werden, dürfen aber keine falsche OctoAlly-Funktion suggerieren.

Vor dem Start sind veraltete oder unerwünschte globale Variablen wie `NODE_OPTIONS` kontrolliert zu behandeln, damit die portable Runtime nicht durch eine fremde Systemkonfiguration beeinflusst wird.

## 11. Sicherheit

1. Dashboard nur an `127.0.0.1` beziehungsweise `localhost` binden.
2. Keine automatische Freigabe im LAN.
3. Keine Zugangsdaten in Logs schreiben.
4. Claude-, Codex- und API-Anmeldedaten nicht in die zentrale Launcher-Konfiguration kopieren.
5. Projektzugriff nur auf bewusst hinzugefügte Ordner erlauben.
6. Vor gefährlichen Agentenmodi eine sichtbare Warnung anzeigen.
7. `--dangerously-skip-permissions` niemals ohne transparente Nutzerentscheidung erzwingen.
8. WebView-Navigation auf lokale OctoAlly-Ziele begrenzen oder externe Links kontrolliert im Standardbrowser öffnen.
9. Updates nicht ungeprüft bei jedem Start erzwingen.
10. Vor Updates Konfiguration und persistente Daten sichern.

## 12. Integration vorhandener Agentendateien

Der AI Launcher soll bestehende Projektdateien respektieren:

```text
Projekt\CLAUDE.md
Projekt\AGENTS.md
Projekt\.claude\agents\*.md
Projekt\.agents\
Projekt\PROJECT_CHARTER.md
Projekt\ROADMAP.md
```

Regeln:

- keine vorhandenen Dateien ungefragt überschreiben
- vor einer automatischen Agenteninstallation eine Dateiliste anzeigen
- Konflikte als `vorhanden`, `neu`, `geändert` oder `übersprungen` protokollieren
- Backups vor Änderungen anlegen
- Agentendateien mit maximal 4.000 Zeichen behandeln, wenn sie für Copilot-Agenten bestimmt sind
- die 4.000-Zeichen-Grenze nicht auf normale Projektdateien oder Projektdokumentationen anwenden

## 13. Updatekonzept

### Standardmodus

- Updates werden erkannt und im Launcher angezeigt.
- Installation erfolgt nur nach bewusster Aktion.
- Vorher wird ein Backup erstellt.
- Nachher erfolgen Start- und Health-Check.
- Bei Fehler bleibt die vorherige Version wiederherstellbar.

### Fork-Modus

Wenn OctoAlly angepasst wird:

```text
upstream/main
    |
    v
Integrationsbranch
    |
    v
AI-Launcher-spezifische Patches
```

Launcher-spezifische Änderungen klein und isoliert halten. Vorrangig WebView, Startlogik und Konfigurationsadapter außerhalb des OctoAlly-Kerns implementieren.

## 14. Proof of Concept

### Phase 1: Technische Prüfung

- Repository in `F:\@AI Tools\OctoAlly\app` klonen
- Node.js-Version prüfen
- Abhängigkeiten installieren
- Server und Dashboard bauen
- Start auf `127.0.0.1:42010` prüfen
- Claude Code erkennen und starten
- Codex erkennen und starten
- Projekt mit Leerzeichen im Pfad öffnen
- Git-Diff und Dateioperationen testen
- Terminal- und Session-Persistenz testen

### Phase 2: Launcher-Adapter

- Moduldefinition `octoally` ergänzen
- Start/Stopp/Neustart implementieren
- Port- und Prozessprüfung implementieren
- Logs umleiten
- Fehlerzustände abbilden
- zentralen Settings-Dialog ergänzen

### Phase 3: WebView

- lokalen Endpunkt einbetten
- Ladezustand darstellen
- Reload und Browser-Öffnen anbieten
- externe Navigation behandeln
- WebView-Fehler sauber auffangen

### Phase 4: Produktivhärtung

- Backup/Restore
- Update/Rollback
- saubere Deinstallation
- Diagnosebericht
- Portkonflikttest
- Absturztest
- USB-/Laufwerksbuchstabenwechsel prüfen

## 15. Akzeptanzkriterien

Die Integration gilt als erfolgreich, wenn:

- der AI Launcher OctoAlly starten, stoppen und neu starten kann
- kein fest codierter Laufwerksbuchstabe benötigt wird
- der Status innerhalb des Launchers korrekt angezeigt wird
- das Dashboard lokal in der Launcher-WebView lädt
- Claude Code und Codex aus OctoAlly gestartet werden können
- bestehende Projektdateien nicht ungefragt überschrieben werden
- Logs zentral unter `F:\@AI Tools\OctoAlly\logs` verfügbar sind
- Doppelstarts verhindert werden
- Portkonflikte verständlich gemeldet werden
- der Launcher nach einem OctoAlly-Fehler funktionsfähig bleibt
- Ollama, llama.cpp, ComfyUI und MCP weiterhin unabhängig funktionieren
- ein fehlgeschlagenes Update rückgängig gemacht werden kann

## 16. Nicht-Ziele der ersten Version

- Umbau von OctoAlly zu einem Ollama-Frontend
- Ersatz des vorhandenen Modellmanagers
- direkte Kopplung aller ComfyUI-Funktionen
- gemeinsame Speicherung von Claude-/Codex-Zugangsdaten
- tiefgreifendes Rebranding des OctoAlly-Kerns
- automatische Veränderung bestehender Projekt-Repositories

## 17. Coding-Auftrag für Coworker oder Codex

```text
Integriere OctoAlly als optionales Modul in den bestehenden AI Workspace Control Center unter F:\@AI Tools.

Rahmenbedingungen:
1. Der AI Launcher bleibt Hauptanwendung und zentrale Prozesssteuerung.
2. Ollama, llama.cpp, ComfyUI, MCP, Hardwaremonitoring und Modellverwaltung bleiben unverändert im Launcher.
3. OctoAlly wird ausschließlich für Claude Code, OpenAI Codex, Agenten, Projekt-Sessions, Terminals und Git-Funktionen eingebunden.
4. Verwende keine fest codierten Laufwerksbuchstaben. Ermittle AI_TOOLS_ROOT dynamisch aus dem Launcher-Startpfad.
5. Nutze die zentrale portable Runtime unter F:\@AI Tools\@Runtime.
6. Ergänze Start, Stopp, Neustart, Status, Portprüfung, Health Check und zentrale Logs.
7. Binde das lokale OctoAlly-Dashboard als neuen Tab „Claude / Codex“ per WebView ein.
8. Erzeuge klare Zustände für nicht installiert, gestoppt, startet, läuft, Port belegt, Abhängigkeit fehlt und Fehler.
9. Verhindere Doppelstarts und beende nur eindeutig zugeordnete Prozesse.
10. Schreibe stdout und stderr nach F:\@AI Tools\OctoAlly\logs.
11. Binde den Dienst ausschließlich an localhost beziehungsweise 127.0.0.1.
12. Überschreibe keine vorhandenen CLAUDE.md-, AGENTS.md- oder Agentendateien.
13. Behandle Windows- und tmux-Kompatibilität als Prüfpunkt. Implementiere keine unbestätigten Annahmen.
14. Nutze nur dokumentierte OctoAlly-Befehle und Parameter.
15. Liefere Diagnose, Backup, Update und Rollback modular, ohne bestehende Launcher-Funktionen zu beschädigen.

Ergebnis:
- vollständige Integration im vorhandenen Launcher-Stil
- zentrale Settings
- robuste Prozesskontrolle
- eingebettetes Dashboard
- verständliche Fehlerausgaben
- Dokumentation aller geänderten Dateien
- Testprotokoll gegen die definierten Akzeptanzkriterien
```

## 18. Quelle

- [OctoAlly auf GitHub](https://github.com/ai-genius-automations/octoally)

---

## 19. Entscheidung

**Empfehlung:** OctoAlly zunächst unverändert als externes, optionales Claude-/Codex-Modul integrieren. Erst nach erfolgreicher Windows- und Persistenzprüfung einen Fork oder tiefere Anpassungen vornehmen. Damit bleibt der AI Launcher stabil, portable und für Ollama, llama.cpp, ComfyUI und MCP zuständig, während OctoAlly seine spezialisierte Orchestrierungsrolle übernimmt.
