@echo off
REM One-click launcher for the AI tool backup/migration utility.
REM Prefer the packaged dist\AiEnvClone.exe; fall back to python source mode.
setlocal
cd /d "%~dp0"

if exist "%~dp0dist\AiEnvClone.exe" (
    start "" "%~dp0dist\AiEnvClone.exe"
    goto :eof
)

where pythonw >nul 2>&1
if errorlevel 1 (
    python --version >nul 2>&1
    if errorlevel 1 (
        echo Python not found. Please install Python 3.10+ and add it to PATH.
        pause
        exit /b 1
    )
    set PYLAUNCH=python
) else (
    set PYLAUNCH=pythonw
)

REM pythonw is the windowless interpreter: no console window, and the GUI
REM process is detached so closing it won't leave a black box behind.
start "" %PYLAUNCH% -m ai_env_clone
endlocal
