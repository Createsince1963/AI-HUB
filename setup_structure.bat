@echo off
REM AI Launcher - Struktur-Setup Script
REM Organisiert die Dateien in die richtige Verzeichnisstruktur

setlocal enabledelayedexpansion

set "LAUNCHER_PATH=%~dp0"
if "%LAUNCHER_PATH:~-1%"=="\" set "LAUNCHER_PATH=%LAUNCHER_PATH:~0,-1%"

echo.
echo 📁 Organisiere AI Launcher Verzeichnisstruktur...
echo.

REM Erstelle Verzeichnisse
if not exist "%LAUNCHER_PATH%\ui" mkdir "%LAUNCHER_PATH%\ui" && echo  ✓ Erstellt: ui
if not exist "%LAUNCHER_PATH%\utils" mkdir "%LAUNCHER_PATH%\utils" && echo  ✓ Erstellt: utils
if not exist "%LAUNCHER_PATH%\tools" mkdir "%LAUNCHER_PATH%\tools" && echo  ✓ Erstellt: tools
if not exist "%LAUNCHER_PATH%\assets" mkdir "%LAUNCHER_PATH%\assets" && echo  ✓ Erstellt: assets
if not exist "%LAUNCHER_PATH%\assets\icons" mkdir "%LAUNCHER_PATH%\assets\icons" && echo  ✓ Erstellt: assets\icons
if not exist "%LAUNCHER_PATH%\assets\styles" mkdir "%LAUNCHER_PATH%\assets\styles" && echo  ✓ Erstellt: assets\styles

REM Verschiebe UI-Dateien
if exist "%LAUNCHER_PATH%\launcher_tab.py" (
    move /Y "%LAUNCHER_PATH%\launcher_tab.py" "%LAUNCHER_PATH%\ui\" > nul
    echo  ✓ Verschoben: launcher_tab.py
)
if exist "%LAUNCHER_PATH%\status_tab.py" (
    move /Y "%LAUNCHER_PATH%\status_tab.py" "%LAUNCHER_PATH%\ui\" > nul
    echo  ✓ Verschoben: status_tab.py
)
if exist "%LAUNCHER_PATH%\config_tab.py" (
    move /Y "%LAUNCHER_PATH%\config_tab.py" "%LAUNCHER_PATH%\ui\" > nul
    echo  ✓ Verschoben: config_tab.py
)

REM Verschiebe Utils-Dateien
if exist "%LAUNCHER_PATH%\tool_manager.py" (
    move /Y "%LAUNCHER_PATH%\tool_manager.py" "%LAUNCHER_PATH%\utils\" > nul
    echo  ✓ Verschoben: tool_manager.py
)
if exist "%LAUNCHER_PATH%\config.py" (
    move /Y "%LAUNCHER_PATH%\config.py" "%LAUNCHER_PATH%\utils\" > nul
    echo  ✓ Verschoben: config.py
)

REM Erstelle __init__.py Dateien wenn nicht vorhanden
if not exist "%LAUNCHER_PATH%\ui\__init__.py" (
    echo. > "%LAUNCHER_PATH%\ui\__init__.py"
    echo  ✓ Erstellt: ui\__init__.py
)
if not exist "%LAUNCHER_PATH%\utils\__init__.py" (
    echo. > "%LAUNCHER_PATH%\utils\__init__.py"
    echo  ✓ Erstellt: utils\__init__.py
)
if not exist "%LAUNCHER_PATH%\tools\__init__.py" (
    echo. > "%LAUNCHER_PATH%\tools\__init__.py"
    echo  ✓ Erstellt: tools\__init__.py
)

echo.
echo ✓ Verzeichnisstruktur eingerichtet!
echo.
echo Nächste Schritte:
echo   1. Öffne CMD im AI_Launcher Verzeichnis
echo   2. Führe aus: run.bat
echo.
pause
