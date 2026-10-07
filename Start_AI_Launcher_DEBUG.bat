@echo off
REM Visible console for troubleshooting - errors stay visible after a crash.
setlocal
set "HERE=%~dp0"
for %%I in ("%HERE%..") do set "ROOT=%%~fI"
set "PY=%ROOT%\@Runtime\python\python.exe"
echo Python: %PY%
set PYTHONFAULTHANDLER=1
"%PY%" "%HERE%launcher.py" %*
echo.
echo [Exit code %ERRORLEVEL%]
pause
endlocal
