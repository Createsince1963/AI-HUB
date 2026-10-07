# ConPTY-Terminal: Architektur, Korrekturen, Abnahme

Zentrale Dokumentation der eingebetteten Terminal-Integration (`ui/pty_terminal.py`,
`ui/cli_terminal_page.py`, `core/procs.py`) und der beiden Korrekturrunden dazu.

Stand: 22.09.2026 · Status: **bedingt freigabefähig** — alle bestätigten Fehler sind korrigiert
und automatisiert abgesichert, die manuelle Windows-Abnahme (Abschnitt 6) steht noch aus.

---

## 1. Überblick

Jede CLI (Claude, Codex, Copilot, Antigravity) hat einen eigenen Tab mit einem echten
ConPTY-Terminal: `pywinpty` liefert das PTY, `pyte` hält ein korrektes Zeichenraster,
das Widget zeichnet dieses Raster in ein `QPlainTextEdit`. Dadurch funktionieren
Pfeiltasten-Menüs, Farben und Bild-Neuzeichnen wie im externen Konsolenfenster.

Drei Wege, auf denen dieselbe CLI laufen kann — alle unter **einem** Schlüssel `cli:<Name>`:

| Modus | Wer startet | Wo läuft es |
|---|---|---|
| `embedded` | Terminal-Tab, „Neu starten" | ConPTY im GUI-Widget |
| `console` | Terminal-Tab, „Externe Konsole" | eigenes Konsolenfenster |
| `captured` | Dashboard-Zeile „Start" | ohne Fenster, Ausgabe auf der Processes-Seite |

Der gemeinsame Schlüssel ist der Kern des Doppelstart-Schutzes: `ProcessManager` kennt alle
drei Modi und ist die einzige Stelle, die entscheidet, ob ein Start zulässig ist.

### Architekturregeln

1. **Das PTY-Handle gehört dem Reader-Thread.** Nur er liest daraus, nur er schließt es
   (in seinem `finally`). Der UI-Thread beendet ausschließlich den *Prozess* — das ist es,
   was ein hängendes `read()` mit EOF zurückkommen lässt. Ein `close()` aus dem UI-Thread
   während eines laufenden `read()` wäre ein Use-after-close im winpty.
2. **Kein `QThread.terminate()`.** Nirgends. Ein Thread, der in winptys `read()` parkt,
   hinterlässt bei `terminate()` Handle-, GIL- und Signalzustand undefiniert.
3. **Kein stilles Beenden laufender Sessions.** Was ein Start überschreiben würde, ist in
   der Regel ein laufender CLI-Dialog oder eine laufende Agentenaufgabe. Ein Start wird
   verweigert; der Wechsel ist ein eigener, bestätigter Befehl. Ausnahme: `stop_all()` und
   der GUI-Shutdown — dort *ist* das Beenden die Absicht.
4. **Lebenszyklus wird gesteuert, nicht erraten.** Owner-Registrierung über `weakref` plus
   Qts `destroyed`-Signal; `RuntimeError`-Fänge sind nur Absicherung für das Fenster
   zwischen C++-Löschung und Signalzustellung, nicht der reguläre Weg.

---

## 2. Bestätigte Fehler und ihre Korrektur

### 2.1 Reader-Thread wurde nie gejoint
`stop()` setzte nur ein Flag und ließ die `QThread`-Referenz fallen, während der Thread
womöglich noch in `pty.read()` steckte — Leck, und beim Schließen mehrerer Tabs Risiko eines
`QThread: Destroyed while thread is still running`.
**Korrektur:** Referenzen unter Re-Entrancy-Guard aus dem Widget nehmen → Signale trennen →
`request_stop()` (setzt auch Qts `requestInterruption()`) → Prozessbaum killen → kurzer Join.

### 2.2 Strg+L erreichte das Terminal nie
`MainWindow` registriert `Strg+L` als fensterweiten `QShortcut`; Qt löst den auf, bevor der
Tastendruck das `keyPressEvent` des fokussierten Widgets erreicht.
**Korrektur:** `event()`-Override auf `QEvent.ShortcutOverride`, der genau die Tasten
beansprucht, die das Widget ohnehin an die CLI weiterreicht — und nur bei laufender Session.
`F11` bleibt bewusst unbelegt, damit der globale Vollbild-Shortcut weiter funktioniert.

### 2.3 Kein externer Konsolenmodus im Terminal-Tab
**Korrektur:** Button „Externe Konsole" als Fallback, wenn `pywinpty`/`pyte` fehlen oder das
eingebettete Terminal Probleme macht.

### 2.4 `QThread.terminate()` als Fallback (Runde 2)
War als „Notfallpfad" eingebaut — genau die unsichere Operation, die das Design vermeiden soll.
**Korrektur:** Ersatzlos entfernt. Ein Reader, der nach `STOP_JOIN_MS` (300 ms) nicht endet,
wird in `_ORPHAN_READERS` **detached**: die `QThread`-Referenz bleibt bestehen (darum geht es
bei „Destroyed while thread is still running"), und gejoint wird einmalig mit gemeinsamem
Budget in `drain_reader_threads()`.

### 2.5 UI-Blockierung beim Schließen (Runde 2)
`wait(2000)` pro Tab — vier Tabs bis zu 8 s eingefrorene UI.
**Korrektur:** 300 ms pro Tab (der Prozess ist da bereits tot), Rest über das gemeinsame
Drain-Budget. Gemessen: 1,2 s statt 8 s bei vier hängenden Tabs.

### 2.6 Finaler Shutdown hängender Reader (Runde 3) — **kritisch**
`_ORPHAN_READERS` verhindert nur die vorzeitige Freigabe *während der Laufzeit*. Beim
Interpreter-Abbau wird das Set selbst abgebaut, und der Destruktor eines laufenden `QThread`
bricht den Prozess ab. Reproduziert: **Returncode −6 (SIGABRT)** plus
`QThread: Destroyed while thread is still running`.
**Korrektur:** `finalize_process_exit(code)` als dokumentierte Endstrategie, aufgerufen als
letzte Anweisung in `main()` beider Entry Points nach `app.exec()`. Kein hängender Reader →
`code` unverändert zurück, normaler Abbau. Hängender Reader → Streams flushen, dann
`os._exit(code)`: keine Destruktoren, kein `atexit`, kein Qt-Teardown, also kein Abort; Thread,
Stack und PTY-Handle holt sich das Betriebssystem zurück. Joinen scheidet aus (das `read()`
kommt nie zurück), `terminate()` bleibt ausgeschlossen.

### 2.7 PID-Wiederverwendung und falsche Kill-Reihenfolge (Runde 3)
Eine nackte PID ist keine stabile Identität; Windows recycelt sie. Und `terminate(force=True)`
lief *vor* `taskkill /T /F` — falsch herum, denn pywinptys `terminate()` beendet nur den am
Pseudokonsolen-Handle hängenden Prozess (`cmd.exe`) und verwaist die darunter liegende
`node.exe`, sodass `taskkill /T` anschließend keinen Baum mehr findet.
**Korrektur:** `_ProcHandle` erfasst beim Spawn PID **und** Prozess-Erzeugungszeit
(`GetProcessTimes`); `kill_tree()` feuert nur, wenn beide noch übereinstimmen. Reihenfolge jetzt:
`taskkill /T /F` zuerst (räumt den Baum, solange der Parent lebt), `terminate(force=True)`
**nur** als Fallback (nicht-Windows, verweigert, oder PTY lebt danach noch). Keine routinemäßige
Doppelung mehr. Das Handle wird in `stop()` gemeinsam mit seinem PTY geswappt — keine PID aus
einer ersetzten Session überlebt.

### 2.8 Stiller Verlust laufender Sessions (Runde 3)
`start()` stoppte den bisherigen Halter kommentarlos — ein Dashboard-Klick konnte einen
laufenden CLI-Dialog im Terminal-Tab vernichten.
**Korrektur:** `start(..., takeover=False) -> bool` verweigert und meldet über das
`blocked`-Signal, was im Weg ist (`holder_of()` → `embedded`/`console`/`captured`).
`takeover=True` erreicht nur der ausdrückliche, bestätigte Wechsel.

### 2.9 Owner-Lifecycle (Runde 3)
**Korrektur:** Owner als `weakref.ref` (kein Zyklus Manager → Page → Ctx → Manager), zusätzlich
Deregistrierung über Qts `destroyed`-Signal, Ablehnung bereits gelöschter C++-Objekte
(`shiboken6.isValid`), Deregistrierung auch in `CliTerminalPage.closeEvent()`.

---

## 3. Bedienung: was wann passiert

| Aktion | Verhalten |
|---|---|
| **Neu starten** | Startet im Tab. Verweigert, solange die CLI außerhalb des Tabs läuft. Der eigene Neustart ist erlaubt — dafür ist der Button da. |
| **Stoppen** | Beendet embedded *und* extern über `ProcessManager.stop()`. |
| **Externe Konsole** | Verweigert, solange überhaupt etwas läuft. |
| **Wechseln** | Der ausdrückliche Pfad: fragt nach, beendet die laufende Variante kontrolliert, startet die andere. Funktioniert in beide Richtungen. Vom Dashboard gestartete Prozesse werden dorthin verwiesen. |
| **Dashboard „Start"** | Bei belegtem Schlüssel Ablehnung im Log mit Nennung des Halters. Der Button ist ohnehin ausgegraut, solange der Terminal-Tab läuft. |
| **GUI schließen / „Alles stoppen"** | Unbedingt, ohne Rückfrage. |

---

## 4. Tastatur

Ans Terminal weitergereicht: Strg+A–Z (als Steuerzeichen), Pfeiltasten, Pos1/Ende, Enter,
Backspace, Entf, Einfg, Esc, Tab, Shift+Tab (Back-Tab), F1–F10, F12, Bild↑/↓.
Lokal (ohne PTY): Shift+Bild↑/↓ und Mausrad → pyte-Scrollback. Strg+V fügt die Zwischenablage
als Eingabe ein. **F11 bleibt frei** für den globalen Vollbild-Shortcut.

---

## 5. Automatisierte Tests

`tests\` — drei Suiten, **73 Checks**, PySide6 offscreen, echtes `pyte`, echter
`ProcessManager`, echte `CliTerminalPage`. `winpty.PtyProcess` ist durch ein FakePty ersetzt,
die Prozess-Identität durch eine kontrollierbare Fake-Prozesswelt.

```
python tests\test_pty.py      # Handle-Ownership, Detach statt Terminate, Idempotenz,
                              # Restart-Storm, späte Signale, Tastatur-Mapping
python tests\test_procs.py    # zentraler Doppelstart-Schutz, Shutdown-Kosten
python tests\test_final.py    # finaler Shutdown, PID-Identität, Verweigerung,
                              # Wechsel, Owner-Lifecycle
```

`test_final.py` startet `child_shutdown.py` als eigenen Prozess — inklusive Kontrollfall **ohne**
`finalize_process_exit()`, der bewusst mit SIGABRT abbricht und damit belegt, dass die Maßnahme
nicht kosmetisch ist.

**Trennung der Testarten:**

- *Statisch geprüft:* `py_compile` + `ast.parse` auf allen geänderten Dateien; alle Aufrufstellen
  von `start` / `is_running` / `running_keys` / `stop_all` gegen die neuen Signaturen geprüft.
- *Simuliert getestet:* die 73 Checks oben.
- *Nur unter Windows prüfbar (offen):* echter `pywinpty`-Spawn und das reale EOF-Verhalten von
  ConPTY nach dem Tree-Kill; `taskkill /T /F` gegen einen echten `cmd.exe → node.exe`-Baum;
  `GetProcessTimes` unter den realen Rechten des Launcher-Prozesses; dass `os._exit` unter
  Windows tatsächlich ohne QThread-Abort endet; DPI-Skalierung.

---

## 6. Manuelle Windows-Abnahme

1. `Start_AI_Launcher.bat` starten, in jeden CLI-Tab wechseln.
2. Pro Tab: Start → im laufenden Terminal `Strg+L` → die CLI muss reagieren (Redraw/Clear),
   **nicht** das Log-Dock einklappen.
3. „Neu starten" mehrfach schnell hintereinander → kein doppelter Prozess im Task-Manager,
   kein Hänger.
4. „Stoppen" → kein `cmd.exe`/`node.exe`-Rest unter diesem Tool.
5. „Externe Konsole" bei laufendem Tab → muss **abgelehnt** werden, Log nennt den Halter.
6. „Wechseln" → Rückfrage, danach sauberer Umschaltvorgang; Gegenrichtung ebenso.
7. Dashboard-„Start" gegen einen laufenden Terminal-Tab → Ablehnung im Log, laufende Sitzung
   überlebt.
8. Alle vier Tabs laufen lassen, GUI per Fenster-X schließen → Task-Manager: kein `cmd.exe`,
   kein zugehöriges `node.exe`, kein Python-Prozess des Launchers.
9. F11 bei fokussiertem Terminal → Vollbild-Toggle muss weiterhin funktionieren.
10. Profil-Dropdown und Dashboard-Start als Regressionscheck.

**Im Log gezielt suchen** (mit `Start_AI_Launcher_DEBUG.bat` starten):

- `QThread: Destroyed while thread is still running` → darf nicht auftreten.
- `RuntimeError: Internal C++ object already deleted` → darf nicht auftreten.
- `hängen in winpty read()` … `os._exit` → der pathologische Fall war real und der harte
  Abbruch hat gegriffen. Kein Fehler, aber ein Hinweis: danach im Task-Manager auf Reste prüfen.

---

## 7. Verbleibende Risiken

- **Windows-Realbetrieb ungetestet.** Die Simulation belegt die Logik, nicht winptys reales
  Timing. Insbesondere ist *nicht* behauptet, dass `finalize_process_exit()` unter Windows
  absturzfrei ist — nur, dass der ungeschützte Fall unter Linux nachweislich abbricht.
- **`os._exit` überspringt alles.** Erreicht wird der Pfad erst nach `closeEvent`, wo die Config
  bereits geschrieben ist — aber alles, was danach noch flushen wollte, tut es nicht.
- **`GetProcessTimes` kann `None` liefern** (fehlende Rechte); dann fällt die Identitätsprüfung
  auf reine Liveness zurück — der alte Zustand, aber nur in diesem Randfall.
- **Modaler Bestätigungsdialog** beim Wechsel blockiert in einem unbeaufsichtigten Kontext.
  Einen solchen Aufrufpfad gibt es derzeit nicht.
- **Dauerhaft hängender Reader** hält bis Prozessende ein PTY-Handle — begrenztes Leck, bewusst
  gegenüber `terminate()` vorgezogen.
- **Sichtbare Verhaltensänderung:** Da `_state()` im Dashboard `is_running()` nutzt, ist der
  Dashboard-Start-Button für eine CLI ausgegraut, solange deren Terminal-Tab läuft.
- **Ordner-Umbenennung `F:\@AI Tools` → `F:\AI_Tools`** weiterhin offen (separater Auftrag). Das
  `@` im Pfad ist ein plausibler Kandidat für gelegentlich fehlschlagende Mounts.
