@echo off
REM Creates / repairs the shared runtime  <ROOT>\@Runtime  (fresh download, fully portable).
REM The real work is done by Setup_Runtime.ps1; -ExecutionPolicy Bypass so it always runs.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0Setup_Runtime.ps1" %*
if errorlevel 1 pause
