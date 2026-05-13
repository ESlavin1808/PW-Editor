@echo off
chcp 65001 >nul
title PW Editor — блочный редактор контента

:: Переходим в свою папку
cd /d "%~dp0"

:: Если venv есть — используем его
if exist "venv\Scripts\python.exe" (
    set PYTHON=venv\Scripts\python.exe
) else (
    set PYTHON=python
)

:: Проверяем PyWry
%PYTHON% -c "import pywry" 2>nul
if %errorlevel% neq 0 (
    echo Устанавливаю PyWry...
    %PYTHON% -m pip install pywry
    if %errorlevel% neq 0 (
        echo.
        echo Не удалось установить PyWry.
        pause
        exit /b 1
    )
)

:: Запускаем
%PYTHON% app.py
if %errorlevel% neq 0 (
    echo.
    pause
)
