@echo off
REM Scan a PyQt5 project for Qt6/PySide6 porting work.
REM   Qt5_Scan.bat "F:\path\to\project"                 -> report in the console
REM   Qt5_Scan.bat "F:\path\to\project" --report r.md    -> Markdown report
REM   Qt5_Scan.bat "F:\path\to\project" --apply-safe     -> mechanical fixes (creates .bak files)
setlocal
set "HERE=%~dp0"
for %%I in ("%HERE%..") do set "ROOT=%%~fI"
"%ROOT%\@Runtime\python\python.exe" "%HERE%tools\qt5_scan.py" %*
endlocal
