@echo off
chcp 65001 > nul 2>&1
set PYTHONIOENCODING=utf-8
set PYTHONUNBUFFERED=1
cd /d "%~dp0"

if not exist ".venv\Scripts\activate.bat" (
    echo [ERROR] Virtual environment .venv not found.
    echo.
    echo Please run the following commands first:
    echo   python -m venv .venv
    echo   .venv\Scripts\pip install -e ".[dev]"
    echo.
    pause
    exit /b 1
)

call ".venv\Scripts\activate.bat"

if /i "%~1"=="--check-deps" (
    echo ========================================
    echo   MoeGuard - Dependency Check
    echo ========================================
    echo.
    python -m moeguard.check_deps
    goto :end
)

echo ========================================
echo   MoeGuard
echo ========================================
echo.
python -m moeguard.role_main

:end
pause
