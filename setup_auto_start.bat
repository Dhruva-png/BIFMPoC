@echo off
REM ============================================================
REM  ONE-TIME SETUP — run this once (double-click it).
REM  It registers the auto-processor to start silently every time
REM  this laptop is logged into, with no window ever appearing.
REM  Safe to double-click again later if needed; it just re-registers.
REM ============================================================

set TASK_NAME=BIFM_Auto_Document_Processor
set SCRIPT_DIR=%~dp0
set VBS_PATH=%SCRIPT_DIR%start_auto_processor.vbs

echo Registering scheduled task "%TASK_NAME%"...
schtasks /Create /TN "%TASK_NAME%" /TR "wscript.exe \"%VBS_PATH%\"" /SC ONLOGON /RL LIMITED /F

if %ERRORLEVEL% EQU 0 (
    echo.
    echo DONE. The auto-processor will now start automatically every time
    echo this computer is logged into, with no window or terminal appearing.
    echo.
    echo Starting it now for the first time...
    schtasks /Run /TN "%TASK_NAME%"
    echo.
    echo Drop PDF files into the "auto_intake" folder to process them.
) else (
    echo.
    echo Something went wrong registering the task. Please screenshot this
    echo window and send it back for help.
)

pause
