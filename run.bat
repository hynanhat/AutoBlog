@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
echo ========================================================
echo   Ninja Blog Automation (AutoBlog) + OCR Hiragana
echo ========================================================

REM 1. Khoi tao moi truong ao .venv neu chua ton tai
if not exist ".venv\Scripts\python.exe" (
    echo [1/3] Dang khoi tao moi truong ao Python .venv...
    python -m venv .venv
    if errorlevel 1 (
        echo [LOI] Khong the tao moi truong ao .venv. Vui long kiem tra Python da duoc cai dat.
        pause
        exit /b 1
    )
)

set "PYTHON_RUNNER=.venv\Scripts\python.exe"
set "RUNTIME_STAMP=.venv\runtime-requirements-installed.txt"

set "REINSTALL_ARG="
if /i "%~1"=="--repair" (
    set "REINSTALL_ARG=--force-reinstall"
    goto :install_runtime
)
if not exist "%RUNTIME_STAMP%" goto :install_runtime

fc.exe /b "requirements-runtime.txt" "%RUNTIME_STAMP%" >nul 2>&1
if errorlevel 1 goto :install_runtime

"%PYTHON_RUNNER%" check_runtime.py
if errorlevel 1 goto :install_runtime

goto :start_app

:install_runtime
echo Dang cai dat thu vien Python...
"%PYTHON_RUNNER%" -m pip install --disable-pip-version-check %REINSTALL_ARG% -r requirements-runtime.txt
if errorlevel 1 goto :failed

echo Dang kiem tra va cai dat Chromium...
"%PYTHON_RUNNER%" -m playwright install chromium
if errorlevel 1 goto :failed

"%PYTHON_RUNNER%" check_runtime.py
if errorlevel 1 goto :failed

copy /y "requirements-runtime.txt" "%RUNTIME_STAMP%" >nul
if errorlevel 1 goto :failed

:start_app
echo Dang mo AutoBlog...
"%PYTHON_RUNNER%" app.py
if errorlevel 1 goto :failed
exit /b 0

:failed
echo Khoi chay AutoBlog that bai.
pause
exit /b 1
