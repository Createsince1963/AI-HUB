@echo off
REM Starts the AI Launcher with the shared portable runtime  <ROOT>\@Runtime\python
REM (no host Python, no venv - works after a drive-letter change).
REM Always uses python.exe (not pythonw.exe) in a minimized window, output logged to
REM logs\launcher_start.log - pythonw.exe on this portable runtime swallows startup
REM errors silently (no window, no log, no way to diagnose), which is what the
REM console-based Start_AI_Launcher_DEBUG.bat proved was happening.
setlocal
set "HERE=%~dp0"
for %%I in ("%HERE%..") do set "ROOT=%%~fI"
set "PY=%ROOT%\@Runtime\python\python.exe"

if not exist "%PY%" (
    echo Shared runtime not found: %PY%
    echo Running Setup_Runtime.bat first...
    call "%HERE%Setup_Runtime.bat"
    if not exist "%PY%" exit /b 1
)
"%PY%" -c "import PySide6, psutil" >nul 2>&1 || (
    echo PySide6 missing - running Setup_Runtime.bat...
    call "%HERE%Setup_Runtime.bat"
)
if not exist "%HERE%logs" mkdir "%HERE%logs"
set "BLOG=%HERE%logs\bat_start.log"
echo [%date% %time%] bat start ^| PY=%PY% ^| HERE=%HERE% >> "%BLOG%"
set PYTHONFAULTHANDLER=1
echo [%date% %time%] starting python (minimized, logged) >> "%BLOG%"
start "AI Launcher" /min cmd /c ""%PY%" "%HERE%launcher.py" %* >> "%HERE%logs\launcher_start.log" 2>&1"
echo [%date% %time%] bat done >> "%BLOG%"
endlocal
