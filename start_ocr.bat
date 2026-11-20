@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ========================================================
echo   Khoi dong doc lap OCR Hiragana Server
echo   API: http://127.0.0.1:8000
echo ========================================================

set "PY_RUNNER="

REM 1. Kiem tra .venv o thu muc goc xem da co uvicorn chua
if exist ".venv\Scripts\python.exe" (
    .\.venv\Scripts\python.exe -c "import uvicorn, starlette" 2>nul
    if not errorlevel 1 (
        set "PY_RUNNER=.\.venv\Scripts\python.exe"
    )
)

REM 2. Neu .venv goc chua cai du, tim moi truong san co tai D:\OCR_Hiragana\.venv
if not defined PY_RUNNER if exist "D:\OCR_Hiragana\.venv\Scripts\python.exe" (
    "D:\OCR_Hiragana\.venv\Scripts\python.exe" -c "import uvicorn, starlette" 2>nul
    if not errorlevel 1 (
        echo Phat hien moi truong ao day du tai D:\OCR_Hiragana\.venv...
        set "PY_RUNNER=D:\OCR_Hiragana\.venv\Scripts\python.exe"
    )
)

REM 3. Kiem tra ocr\.venv (neu co)
if not defined PY_RUNNER if exist "ocr\.venv\Scripts\python.exe" (
    "ocr\.venv\Scripts\python.exe" -c "import uvicorn, starlette" 2>nul
    if not errorlevel 1 (
        set "PY_RUNNER=ocr\.venv\Scripts\python.exe"
    )
)

REM 4. Neu tat ca deu chua co uvicorn, tu dong cai dat vao .venv
if not defined PY_RUNNER (
    if exist ".venv\Scripts\python.exe" (
        echo [INFO] Dang cai dat cac thu vien con thieu (uvicorn, starlette, opencv...) vao .venv...
        call .venv\Scripts\activate
        pip install -r requirements-runtime.txt
        set "PY_RUNNER=.\.venv\Scripts\python.exe"
    ) else (
        set "PY_RUNNER=python"
    )
)

echo Dang khoi chay OCR server voi: %PY_RUNNER%
%PY_RUNNER% ocr/server.py

pause
