# Terminal-/Prozess-Tests

Simulierte Tests für die ConPTY-Terminal-Integration. Ein FakePty ersetzt `winpty.PtyProcess`,
damit die Lebenszyklus-Logik auch ohne echtes ConPTY prüfbar ist. Sie ersetzen **nicht** die
manuelle Windows-Abnahme (echter ConPTY-Spawn, `taskkill /T /F` gegen einen realen Prozessbaum,
DPI) - siehe HANDOFF.md.

Ausführen (Runtime-Python des Launchers, aus dem Projektordner):

    python tests\test_pty.py
    python tests\test_procs.py
    python tests\test_final.py

`test_final.py` startet `child_shutdown.py` als eigenen Prozess, um den finalen App-Shutdown mit
einem dauerhaft hängenden Reader zu prüfen - inklusive Kontrollfall ohne
`finalize_process_exit()`, der bewusst mit "QThread: Destroyed while thread is still running"
abbricht.

Benötigt: PySide6, pyte. Läuft plattformunabhängig (unter Linux mit `QT_QPA_PLATFORM=offscreen`).
