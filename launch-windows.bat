@echo off
setlocal
set "QAR_DIR=%~dp0"

where pyw.exe >nul 2>&1
if %errorlevel% equ 0 (
    start "" pyw.exe -3 "%QAR_DIR%quest_apk_renamer.py" %*
    exit /b 0
)

where pythonw.exe >nul 2>&1
if %errorlevel% equ 0 (
    start "" pythonw.exe "%QAR_DIR%quest_apk_renamer.py" %*
    exit /b 0
)

echo Python 3 was not found. Use the packaged Quest APK Renamer installer.
pause
exit /b 1
