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
echo [2/3] Dang kiem tra thu vien trong requirements.txt...
"%PYTHON_RUNNER%" -m pip install --disable-pip-version-check -r requirements.txt
if errorlevel 1 (
    echo [LOI] Khong the cai dat cac thu vien.
    goto :failed
)

echo [3/3] Dang kiem tra trinh duyet Playwright Chromium...
"%PYTHON_RUNNER%" -m playwright install chromium
if errorlevel 1 (
    echo [LOI] Khong the cai dat Chromium.
    goto :failed
)

echo Dang khoi chay ung dung AutoBlog...
"%PYTHON_RUNNER%" app.py
if errorlevel 1 goto :failed
exit /b 0

:failed
echo Khoi chay AutoBlog that bai.
pause
exit /b 1
