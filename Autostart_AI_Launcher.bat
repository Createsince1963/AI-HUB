@echo off
REM Copy THIS file into the Windows startup folder (Win+R -> shell:startup).
REM It finds the launcher on any drive letter, so it keeps working when the
REM portable drive is F:, G:, K: ... If the drive is not plugged in, it exits silently.
setlocal
set "REL=@AI Tools\AI_Launcher\Start_AI_Launcher.bat"
set "REL2=AI_Tools\AI_Launcher\Start_AI_Launcher.bat"

REM 1) same folder as this file (when run from the launcher folder itself)
if exist "%~dp0Start_AI_Launcher.bat" (
    call "%~dp0Start_AI_Launcher.bat"
    exit /b 0
)

REM 2) scan all drive letters
for %%D in (F G H I J K L M N O P Q R S T U V W X Y Z E D C) do (
    if exist "%%D:\%REL%" (
        call "%%D:\%REL%"
        exit /b 0
    )
    if exist "%%D:\%REL2%" (
        call "%%D:\%REL2%"
        exit /b 0
    )
)
exit /b 0
