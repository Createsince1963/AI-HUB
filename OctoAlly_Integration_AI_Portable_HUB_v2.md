# OctoAlly-Integration in den AI Portable HUB (v2)

**Projekt:** AI Workspace Control Center / AI Portable HUB
**Zielplattform:** Windows, portable Installation
**Workspace-Root:** `<AI_TOOLS_ROOT>` (wird zur Laufzeit aus dem Launcher-Startpfad ermittelt; aktuell `D:\@AI Tools`, früher `F:\@AI Tools`)
**Integrationsrolle:** Claude-Code- und OpenAI-Codex-Orchestrierung
**Status:** Integrationskonzept und Coding-Auftrag, **v2 – gegen den Quellcode OctoAlly v1.0.87 geprüft**

**Änderungen gegenüber v1:** Abschnitte 4, 5, 6, 9, 10, 11, 14 und 15 wurden korrigiert, Abschnitt 4a (Prüfergebnis Quellcode), 4b (Voraussetzungen) und 4c (Lizenz) sind neu. Feste Laufwerksbuchstaben sind entfernt. Die 4.000-Zeichen-Regel für Copilot-Agenten ist gestrichen, da sie hier nicht zum Thema gehört.

---

## 1. Ziel

OctoAlly wird nicht als Ersatz für den bestehenden AI Launcher verwendet. Es wird als eigenständiges, optionales Modul für Claude Code, OpenAI Codex, Agenten, Projekt-Sessions, Terminals und Git-Funktionen eingebunden.

Der AI Launcher bleibt die zentrale Steuerungsinstanz für:

- portable Pfade und gemeinsame Einstellungen
- Ollama, llama.cpp, lokalen Modellpool
- ComfyUI
- MCP-Server
- Hardware- und Prozessüberwachung
- Launcher, Runtime und Logs
- Start, Stopp und Status aller Module

OctoAlly ergänzt:

- Claude-Code- und OpenAI-Codex-Sessions
- Agenten-Sessions
- Multi-Projekt-Verwaltung
- Live-Terminalausgabe
- Git-Diffs und Git-Historie
- Datei- und Projektansicht
- Session-Persistenz (nur wenn das Windows-Terminal-Backend es zulässt, siehe 4a)

## 2. Zielarchitektur

```text
<AI_TOOLS_ROOT>
|
+-- AI Workspace Control Center (Launcher)
|   +-- Home / Projects / Claude-Codex (WebView) / Ollama / llama.cpp
|   +-- ComfyUI / MCP / Models / Hardware / Logs / Settings
|
+-- @Runtime
|   +-- Node        (muss ergänzt werden, fehlt aktuell)
|   +-- Git         (muss ergänzt werden, fehlt aktuell)
|   +-- optional Unix/tmux-Kompatibilitätsschicht (nur falls PoC sie erfordert)
|
+-- AI_Modells, AI_Ollama_CCP, COMFYUI
+-- AI_OctoAlly   (portables Unterverzeichnis, alles OctoAlly-bezogene liegt hier)
|   +-- app          (Repository / Build)
|   +-- home         (umgeleitetes Benutzerprofil für OctoAlly, siehe 5)
|   +-- data         (octoally.db via DB_PATH)
|   +-- logs
|   +-- config
|   +-- updates
|   +-- backup
|
+-- Projects
```

## 3. Klare Systemgrenzen

### AI Launcher ist verantwortlich für

1. Ermittlung von `AI_TOOLS_ROOT` aus dem eigenen Startpfad.
2. Aufbau aller portablen Pfade relativ zum Root.
3. Bereitstellung der gemeinsamen Runtime in `PATH`.
4. Start und Stopp des OctoAlly-Prozesses (direkt über Node, siehe 9).
5. Portprüfung und Statusüberwachung.
6. Einbettung des lokalen Dashboards.
7. zentrale Protokollierung.
8. Sicherung von Konfiguration und Sitzungsdaten.
9. Erzwingen der Loopback-Bindung und verständliche Fehlerzustände.

### OctoAlly ist verantwortlich für

1. Claude-Code- und Codex-Sessions.
2. projektbezogene Agenten und Instruktionsdateien.
3. Live-Ausgabe und interaktive Terminals.
4. Git-Ansicht, Diffs und Commits.
5. eigene Sitzungs- und Projektdaten.

### Nicht in OctoAlly integrieren

Ollama-Modellverwaltung, llama.cpp-Serververwaltung, ComfyUI-Prozesssteuerung, zentraler Modellpool, Hardware-Monitoring, allgemeine MCP-Verwaltung. Diese Funktionen bleiben im AI Launcher.

## 4. Technische Einschränkungen (Windows)

OctoAlly ist für Linux und macOS gebaut. Eine native portable Windows-Integration darf nicht ungeprüft vorausgesetzt werden.

### 4a. Prüfergebnis Quellcode v1.0.87

| Punkt | Befund im Code | Folge |
|---|---|---|
| Plattformzweige | `process.platform` kennt nur `linux` und `darwin` (`cli.mjs`, `server/src/index.ts`) | Kein Windows-Zweig |
| CLI `bin/octoally` | Bash-Skript mit `readlink -f`, `fuser`, `lsof`, `kill` | Unter Windows nicht nutzbar; Launcher startet Node direkt |
| `install.sh` | apt/brew, `~/.local/bin`, systemd/launchd | Nicht verwenden |
| Session-Persistenz | `tmux` und `dtach` per Default aktiv, Erkennung über `which` und `fuser` | Auf Windows fehlen beide; Server fällt auf „direct mode" zurück, also **keine Session-Persistenz** |
| Shell | `process.env.SHELL \|\| '/bin/bash'` in `pty-worker.ts` | Muss unter Windows getestet werden |
| PTY-Modul | `node-pty-prebuilt-multiarch` (nativ) | ConPTY-Prebuild muss für die Node-Version vorhanden sein |
| SQLite | `better-sqlite3` (nativ) | Prebuild oder Build-Toolchain nötig |
| Netzwerk | Default `HOST` ist `::` (alle Interfaces) | Muss auf `127.0.0.1` gesetzt werden |
| Authentifizierung | `OCTOALLY_TOKEN` wird nur in `config.ts` eingelesen und **nirgends geprüft** | Es gibt keine Auth; Schutz nur über Loopback-Bindung |
| Codex | Start mit `--no-alt-screen` (`pty-worker.ts`) | Erwartetes Verhalten, nicht ändern |
| Desktop-App | Electron-Paket nur für Linux/macOS vorgesehen | Nicht verwenden |

Hinweis: Der Befund beruht auf Quellcode-Lektüre. Es wurde nichts ausgeführt; die Windows-Lauffähigkeit ist weiterhin **ungeprüft**.

### 4b. Voraussetzungen

- Portable Node-LTS-Version in `@Runtime\Node` (aktuell nicht vorhanden).
- Portable Git in `@Runtime\Git` (aktuell nicht vorhanden).
- Passende Prebuilds für `better-sqlite3` und `node-pty-prebuilt-multiarch`, andernfalls Build-Tools (Python, MSVC).
- Claude Code und Codex als erreichbare Befehle (`claude`, `codex`); die Befehle sind in OctoAlly über die Settings `session_claude_command` und `session_codex_command` konfigurierbar.

### 4c. Lizenz

Apache-2.0 **mit Commons Clause**. Private, interne Nutzung und kostenlose Weitergabe sind erlaubt. Verkauf eines Produkts oder Dienstes, dessen Wert im Wesentlichen aus OctoAlly stammt, ist ausgeschlossen. Für das geplante Fachbuch und eine mögliche Produktisierung des Launchers gilt: OctoAlly nicht in ein verkauftes Produkt bündeln und keinen Code kopieren. Konzepte dürfen nachgebaut werden. Vor jeder Weitergabe die Klausel erneut prüfen.

### Windows-Strategie

**Stufe 0:** Quellcode-Prüfung (erledigt, siehe 4a).
**Stufe 1:** Server nativ unter portablem Node starten, mit `OCTOALLY_USE_TMUX=false` und `OCTOALLY_USE_DTACH=false`. Prüfen, ob Dashboard und Direktmodus laufen.
**Stufe 2:** Claude-Code- und Codex-Sessions im Direktmodus testen (ConPTY, Shell-Auswahl, Pfade mit Leerzeichen und `@`).
**Stufe 3:** Falls der Direktmodus unbrauchbar ist, OctoAlly gekapselt über WSL2 betreiben und nur das lokale Dashboard einbetten. WSL2 ist **nicht** vollständig portabel.
**Stufe 4:** Erst nach erfolgreichem Test Änderungen am Fork vornehmen (z. B. Windows-Shell-Auswahl).

## 5. Verzeichnis- und Datenkonzept

OctoAlly hat **keine** Variable für ein Datenverzeichnis. Tatsächlich gilt:

- Datenbank: `DB_PATH` (Env), sonst `~/.octoally/octoally.db`.
- Projektliste, Agenten und Statusline liegen fest unter `~/.octoally/…` und `~/.claude/…` (`~` = `homedir()`, unter Windows `USERPROFILE`).
- OctoAlly schreibt in `~/.claude/settings.json` (Statusline), `~/.claude/agents/` (Default-Agents) und kann in seinem Cleanup-Endpunkt `~/CLAUDE.md` und `~/.claude/CLAUDE.md` löschen, wenn sie „ruflo" enthalten.

Damit das globale Claude-Setup des Benutzers unberührt bleibt, startet der Launcher OctoAlly mit **umgeleitetem Profil**:

```text
USERPROFILE=<AI_TOOLS_ROOT>\AI_OctoAlly\home
HOME=<AI_TOOLS_ROOT>\AI_OctoAlly\home
DB_PATH=<AI_TOOLS_ROOT>\AI_OctoAlly\data\octoally.db
```

Folge: `~/.claude` und `~/.octoally` landen unter `AI_OctoAlly\home`. Die Claude-Anmeldung muss dann einmal in diesem Profil erfolgen. Soll stattdessen das portable Claude-Profil des Launchers genutzt werden, `CLAUDE_CONFIG_DIR` explizit setzen und im PoC prüfen, welche Dateien OctoAlly trotzdem unter `home` anlegt. **Beide Varianten sind im PoC zu verifizieren.**

```text
<AI_TOOLS_ROOT>\AI_OctoAlly\
+-- app\        # Repository und Build
+-- home\       # umgeleitetes Profil (.octoally, .claude)
+-- data\       # octoally.db
+-- config\     # Launcher-seitige Einstellungen
+-- logs\       # stdout, stderr, Launcher-Log
+-- updates\
+-- backup\
```

## 6. Zentrale Launcher-Konfiguration

```json
{
  "modules": {
    "octoally": {
      "enabled": true,
      "installDir": "${AI_TOOLS_ROOT}\\AI_OctoAlly\\app",
      "profileDir": "${AI_TOOLS_ROOT}\\AI_OctoAlly\\home",
      "dbPath": "${AI_TOOLS_ROOT}\\AI_OctoAlly\\data\\octoally.db",
      "logDir": "${AI_TOOLS_ROOT}\\AI_OctoAlly\\logs",
      "host": "127.0.0.1",
      "port": 42010,
      "autoStart": false,
      "autoOpen": false,
      "embedWebView": true,
      "startMode": "native-direct",
      "useTmux": false,
      "useDtach": false,
      "healthCheckUrl": "http://127.0.0.1:42010/api",
      "startupCommand": "node server/dist/index.js",
      "startupCwd": "${AI_TOOLS_ROOT}\\AI_OctoAlly\\app\\server"
    }
  }
}
```

Hinweise:

- `startupCommand` ersetzt `octoally start`. Das Bash-Skript wird nicht verwendet.
- `healthCheckUrl` und der Pfad `/api` sind im PoC gegen einen tatsächlichen Endpunkt zu bestätigen.
- Nicht dokumentierte Parameter nicht erfinden. Gesichert aus dem Code sind: `PORT`, `HOST`, `DB_PATH`, `NODE_ENV`, `LOG_LEVEL`, `OCTOALLY_USE_TMUX`, `OCTOALLY_USE_DTACH`, `OCTOALLY_TOKEN` (ohne Wirkung, siehe 4a).

## 7. Launcher-Oberfläche

### Neuer Hauptbereich: `Claude / Codex`

1. **Dashboard** – eingebettete OctoAlly-Weboberfläche.
2. **Status** – Prozess, Port, URL, PID, Startmodus, letzte Meldung.
3. **Steuerung** – Start, Stopp, Neustart, Browser öffnen, Log öffnen.
4. **Konfiguration** – Pfade, Port, Startmodus, Autostart, WebView-Verhalten.
5. **Diagnose** – Node, Git, Claude Code, Codex, Terminal-Backend, Schreibrechte, native Module, Loopback-Bindung.

### Statuszustände

Nicht installiert, installiert/gestoppt, startet, läuft, Port belegt, Fehler, Update verfügbar, Abhängigkeit fehlt, Kompatibilitätsmodus aktiv (Direktmodus ohne Persistenz).

## 8. Startlogik

```text
Benutzer öffnet Claude/Codex
   -> Installation vorhanden?        nein: Installationshinweis
   -> Node, Git, native Module OK?   nein: Diagnose anzeigen
   -> Port frei?
        belegt durch eigene Instanz (PID-Datei des Launchers): wiederverwenden
        belegt durch Fremdprozess: Konflikt melden
   -> Start: node server/dist/index.js mit isolierter Umgebung (siehe 10)
   -> Health Check mit begrenzten Versuchen
        ok: WebView laden
        Fehler: stdout/stderr-Log anzeigen
```

## 9. Prozess- und Portmanagement

Der Launcher muss:

- Doppelstarts über **eigene** PID-Datei des Launchers verhindern (nicht `.octoally.pid` des Bash-Skripts).
- PID und Startzeit erfassen.
- stdout und stderr in getrennte Dateien schreiben.
- Beenden nur über die eigene PID und den zugehörigen Prozessbaum (Windows: Job Object oder `taskkill /T` auf die eigene PID). **Kein** Port-basiertes Beenden wie im OctoAlly-Skript (`fuser -k`, `lsof | kill`).
- nach Absturz einen klaren Fehlerstatus zeigen.
- beim Launcher-Ende konfigurierbar entscheiden, ob OctoAlly weiterläuft.
- vor dem Stopp prüfen, ob noch Sessions laufen, und warnen (ohne Persistenz gehen sie verloren).

Logs:

```text
<AI_TOOLS_ROOT>\AI_OctoAlly\logs\octoally-launcher.log
<AI_TOOLS_ROOT>\AI_OctoAlly\logs\octoally-stdout.log
<AI_TOOLS_ROOT>\AI_OctoAlly\logs\octoally-stderr.log
```

## 10. Umgebungsvariablen

```text
AI_TOOLS_ROOT=<ermittelt>
HOST=127.0.0.1
PORT=42010
NODE_ENV=production
DB_PATH=<AI_TOOLS_ROOT>\AI_OctoAlly\data\octoally.db
USERPROFILE=<AI_TOOLS_ROOT>\AI_OctoAlly\home
HOME=<AI_TOOLS_ROOT>\AI_OctoAlly\home
OCTOALLY_USE_TMUX=false
OCTOALLY_USE_DTACH=false
PATH=<AI_TOOLS_ROOT>\@Runtime\Node;<AI_TOOLS_ROOT>\@Runtime\Git\cmd;<bestehender PATH>
```

- `HOST=127.0.0.1` ist **Pflicht**. Der OctoAlly-Default `::` lauscht auf allen Interfaces und es gibt keine Authentifizierung.
- `USERPROFILE`/`HOME` sind keine OctoAlly-Variablen, sondern Betriebssystem-Variablen, mit denen das Profil umgeleitet wird (siehe 5).
- `NODE_OPTIONS` und andere globale Variablen vor dem Start kontrolliert bereinigen.
- `AI_TOOLS_ROOT` ist eine Launcher-Variable ohne Bedeutung für OctoAlly.

## 11. Sicherheit

1. **Loopback erzwingen:** `HOST=127.0.0.1` setzen und nach dem Start prüfen, dass der Port nur auf Loopback lauscht. Andernfalls Start abbrechen.
2. Keine Freigabe im LAN; Windows-Firewall-Regel nicht automatisch öffnen. Es existiert keine OctoAlly-Authentifizierung, jeder lokale Prozess und (bei Fehlbindung) jedes LAN-Gerät könnte Sessions starten.
3. Keine Zugangsdaten in Logs.
4. Claude-, Codex- und API-Anmeldedaten nicht in die Launcher-Konfiguration kopieren.
5. Projektzugriff nur auf bewusst hinzugefügte Ordner.
6. Der Dateibrowser von OctoAlly zeigt standardmäßig das Home-Verzeichnis; durch die Profilumleitung (5) ist das nicht mehr das echte Benutzerprofil.
7. `--dangerously-skip-permissions` ist in OctoAlly ein Projekt-Flag (auch „für alle Projekte" setzbar). Der Launcher setzt es nie selbst, zeigt eine Warnung und prüft den Zustand im Diagnose-Tab.
8. WebView-Navigation auf lokale Ziele begrenzen, externe Links im Standardbrowser öffnen.
9. Updates nicht automatisch erzwingen; vorher Backup.
10. Den OctoAlly-Cleanup-Endpunkt („ruflo entfernen") nicht aufrufen.

## 12. Integration vorhandener Agentendateien

Der AI Launcher respektiert:

```text
Projekt\CLAUDE.md
Projekt\AGENTS.md
Projekt\.claude\agents\*.md
Projekt\.agents\
Projekt\PROJECT_CHARTER.md
Projekt\ROADMAP.md
```

- Keine vorhandenen Dateien ungefragt überschreiben.
- Vor automatischer Agenteninstallation Dateiliste anzeigen; Zustände `vorhanden`, `neu`, `geändert`, `übersprungen` protokollieren.
- Backups vor Änderungen.
- OctoAlly installiert Default-Agents nach `~/.claude/agents/`; durch die Profilumleitung betrifft das nur `AI_OctoAlly\home`.

## 13. Updatekonzept

- Updates werden angezeigt, Installation nur nach bewusster Aktion mit Backup, danach Start- und Health-Check, bei Fehler Rollback.
- Das mitgelieferte `update.sh` und der Update-Pfad in `cli.mjs` werden nicht verwendet. Update erfolgt über `git pull` bzw. neues Release im `app`-Ordner, danach `npm install` und Build.

### Fork-Modus

```text
upstream/main -> Integrationsbranch -> AI-Launcher-spezifische Patches
```

Änderungen klein und isoliert halten. Mögliche Fork-Kandidaten (nur nach bestandenem PoC): Windows-Shell-Auswahl statt `SHELL`/`/bin/bash`, Auth-Prüfung für `OCTOALLY_TOKEN`, konfigurierbares Datenverzeichnis. Lizenz beachten (4c).

## 14. Proof of Concept

### Phase 1: Technische Prüfung

- Repository nach `<AI_TOOLS_ROOT>\AI_OctoAlly\app` klonen; Node-Version prüfen.
- `npm run install:all`, danach `npm run build`; prüfen, ob `better-sqlite3` und `node-pty-prebuilt-multiarch` laden.
- Start mit der Umgebung aus 10 und `netstat` prüfen: nur `127.0.0.1:42010`?
- Dashboard laden und `/api` testen.
- Claude-Code-Session im Direktmodus starten (ConPTY, Shell, Eingabe, Resize).
- Codex-Session starten.
- Projekt mit Leerzeichen und `@` im Pfad öffnen.
- Git-Diff und Dateioperationen.
- Session-Verhalten bei Server-Neustart dokumentieren (Verlust erwartet).
- Prüfen, welche Dateien außerhalb von `AI_OctoAlly\home` und `AI_OctoAlly\data` angelegt werden.

### Phase 2: Launcher-Adapter

Moduldefinition, Start/Stopp/Neustart über eigene PID, Port- und Prozessprüfung, Loopback-Prüfung, Log-Umleitung, Fehlerzustände, Settings-Dialog.

### Phase 3: WebView

Lokalen Endpunkt einbetten, Ladezustand, Reload, Browser öffnen, externe Navigation, WebView-Fehler abfangen.

### Phase 4: Produktivhärtung

Backup/Restore, Update/Rollback, Deinstallation, Diagnosebericht, Portkonflikttest, Absturztest, **Laufwerksbuchstabenwechsel** (D: ↔ F: ↔ andere), Test mit verlorener Persistenz.

## 15. Akzeptanzkriterien

Die Integration gilt als erfolgreich, wenn:

- der Launcher OctoAlly starten, stoppen und neu starten kann (nur eigene Prozesse betroffen).
- kein fest codierter Laufwerksbuchstabe benötigt wird (Test mit geändertem Buchstaben).
- der Server nachweislich nur an `127.0.0.1` lauscht.
- der Status im Launcher korrekt angezeigt wird.
- das Dashboard in der Launcher-WebView lädt.
- Claude Code und Codex aus OctoAlly gestartet werden können.
- das globale Benutzerprofil (`%USERPROFILE%\.claude`) unverändert bleibt.
- bestehende Projektdateien nicht ungefragt überschrieben werden.
- Logs zentral unter `AI_OctoAlly\logs` liegen.
- Doppelstarts verhindert und Portkonflikte verständlich gemeldet werden.
- der Launcher nach einem OctoAlly-Fehler funktionsfähig bleibt.
- Ollama, llama.cpp, ComfyUI und MCP unabhängig weiterlaufen.
- ein fehlgeschlagenes Update rückgängig gemacht werden kann.
- die Einschränkung „keine Session-Persistenz im Direktmodus" in der Oberfläche sichtbar ist (oder per WSL2-Variante gelöst wurde).

## 16. Nicht-Ziele der ersten Version

Umbau zu einem Ollama-Frontend, Ersatz des Modellmanagers, Kopplung der ComfyUI-Funktionen, gemeinsame Speicherung von Zugangsdaten, Rebranding des OctoAlly-Kerns, automatische Änderung bestehender Repositories, Nutzung der Electron-Desktop-App, Nutzung von `bin/octoally`, `install.sh` und `update.sh`.

## 17. Coding-Auftrag für Coworker oder Codex

```text
Integriere OctoAlly (v1.0.87, Quellcode unter <AI_TOOLS_ROOT>\X_DOWNLOADS\octoally-main bzw. geklont nach AI_OctoAlly\app)
als optionales Modul in den AI Workspace Control Center.

Rahmenbedingungen:
1. Der AI Launcher bleibt Hauptanwendung und zentrale Prozesssteuerung.
2. Ollama, llama.cpp, ComfyUI, MCP, Hardwaremonitoring und Modellverwaltung bleiben unverändert.
3. OctoAlly nur für Claude Code, Codex, Agenten, Projekt-Sessions, Terminals und Git.
4. Keine festen Laufwerksbuchstaben; AI_TOOLS_ROOT dynamisch aus dem Launcher-Startpfad.
5. Portable Runtime unter <AI_TOOLS_ROOT>\@Runtime (Node und Git müssen dort ergänzt werden).
6. Start NICHT über bin/octoally oder install.sh (Bash/Linux), sondern direkt:
   node server/dist/index.js im Ordner AI_OctoAlly\app\server, mit der Umgebung aus Abschnitt 10.
7. HOST=127.0.0.1 erzwingen und nach dem Start prüfen; sonst Abbruch. OctoAlly hat keine Auth.
8. USERPROFILE und HOME auf AI_OctoAlly\home umleiten; DB_PATH auf AI_OctoAlly\data setzen.
   Verifiziere im Test, dass %USERPROFILE%\.claude des Benutzers nicht verändert wird.
9. OCTOALLY_USE_TMUX=false und OCTOALLY_USE_DTACH=false im ersten Durchlauf; Direktmodus testen.
10. Start, Stopp, Neustart, Status, Portprüfung, Health Check, zentrale Logs (stdout/stderr getrennt).
11. Beenden nur über eigene PID/Prozessbaum, nie über Port-Kill.
12. WebView-Tab „Claude / Codex"; Zustände: nicht installiert, gestoppt, startet, läuft, Port belegt,
    Abhängigkeit fehlt, Fehler, Kompatibilitätsmodus.
13. Keine vorhandenen CLAUDE.md-, AGENTS.md- oder Agentendateien überschreiben.
14. Nur Variablen und Befehle verwenden, die im Quellcode nachweisbar sind (Abschnitt 6).
15. Diagnose, Backup, Update, Rollback modular.
16. Keinen OctoAlly-Code in verkaufte Produkte übernehmen (Commons Clause); Änderungen am Fork nur nach PoC.

Ergebnis:
- Integration im vorhandenen Launcher-Stil, zentrale Settings
- robuste Prozesskontrolle, eingebettetes Dashboard, verständliche Fehlerausgaben
- Dokumentation aller geänderten Dateien
- Testprotokoll gegen Abschnitt 15, inkl. Liste der offenen, ungetesteten Annahmen
```

## 18. Quellen

- [OctoAlly auf GitHub](https://github.com/ai-genius-automations/octoally)
- Lokale Prüfung: `X_DOWNLOADS\octoally-main` (v1.0.87): `cli.mjs`, `bin/octoally`, `server/src/config.ts`, `server/src/index.ts`, `server/src/services/pty-worker.ts`, `server/src/routes/projects.ts`, `server/src/routes/settings.ts`, `LICENSE`

## 19. Entscheidung

**Empfehlung:** OctoAlly zunächst unverändert als externes, optionales Modul testen, gestartet direkt über Node, strikt auf Loopback und mit umgeleitetem Profil. Zuerst den Direktmodus ohne tmux/dtach auf Windows prüfen. Fehlt die Session-Persistenz oder läuft der Terminal-Backend nicht, entscheiden zwischen WSL2-Kapselung und dem Verzicht auf OctoAlly zugunsten des eigenen ConPTY-Terminals (siehe `OCTOALLY_ANALYSE.md`). Fork und tiefere Anpassungen erst nach bestandenem PoC.
