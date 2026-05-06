@echo off
setlocal EnableExtensions
chcp 65001 >nul 2>&1

set "SCRIPT_DIR=%~dp0gen_section\"
set "WORK_DIR=%CD%"
set "OUTPUT_FILE=%WORK_DIR%\output.ini"

type nul > "%OUTPUT_FILE%"

for %%F in ("%WORK_DIR%\*.txt") do (
  if exist "%%~fF" call :ProcessOne "%%~fF" || exit /b 1
)

echo Wrote "%OUTPUT_FILE%"
exit /b 0

:ProcessOne
set "INPUT_FILE=%~1"

where awk >nul 2>nul
if not errorlevel 1 (
  awk -f "%SCRIPT_DIR%gen_section.awk" "%INPUT_FILE%" >> "%OUTPUT_FILE%" 2>nul
  if not errorlevel 1 (
    echo.>> "%OUTPUT_FILE%"
    exit /b 0
  )
)

where python >nul 2>nul
if not errorlevel 1 (
  python "%SCRIPT_DIR%gen_section.py" "%INPUT_FILE%" >> "%OUTPUT_FILE%" 2>nul
  if not errorlevel 1 (
    echo.>> "%OUTPUT_FILE%"
    exit /b 0
  )
)

where perl >nul 2>nul
if not errorlevel 1 (
  perl "%SCRIPT_DIR%gen_section.pl" "%INPUT_FILE%" >> "%OUTPUT_FILE%" 2>nul
  if not errorlevel 1 (
    echo.>> "%OUTPUT_FILE%"
    exit /b 0
  )
)

echo Failed to process "%INPUT_FILE%".
exit /b 1
