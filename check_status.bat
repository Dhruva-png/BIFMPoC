@echo off
REM Double-click this any time to check whether the auto-processor is
REM currently running in the background, and see the last few log lines.

echo Checking if the auto-processor is running...
echo.
tasklist /FI "IMAGENAME eq pythonw.exe" | find /I "pythonw.exe" >nul
if %ERRORLEVEL% EQU 0 (
    echo STATUS: Running.
) else (
    echo STATUS: NOT running. Double-click setup_auto_start.bat to start it,
    echo or restart the computer ^(it starts automatically at login^).
)

echo.
echo Last 15 log lines:
echo ------------------------------------------------------------
powershell -Command "Get-Content '%~dp0logs\bifm_app.log' -Tail 15"
echo ------------------------------------------------------------
echo.
pause
