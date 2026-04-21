@echo off
chcp 65001 >nul 2>&1
set "SCRIPT_DIR=%~dp0"
python "%SCRIPT_DIR%usb_sync.py" %*
