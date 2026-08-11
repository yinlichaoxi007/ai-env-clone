@echo off
REM Build script: package AiEnvClone into a single-file executable (dist\AiEnvClone.exe)
REM so it can run on a Windows PC without Python installed.
REM Usage: double-click build.bat, or run it from a command prompt in this folder.
setlocal
cd /d "%~dp0."

REM --- 1. Check Python is available ---
where python >nul 2>&1
if errorlevel 1 (
    echo Python not found. Please install Python 3.10+ and add it to PATH.
    echo Download: https://www.python.org/downloads/
    pause
    exit /b 1
)

REM --- 2. Install build dependencies (PyInstaller) ---
echo Installing build dependencies from requirements.txt ...
python -m pip install -r "%~dp0requirements.txt"
if errorlevel 1 (
    echo Failed to install build dependencies.
    pause
    exit /b 1
)

REM --- 3. Run the PyInstaller packaging script ---
echo Building single-file executable (this may take a while) ...
python "%~dp0build_exe.py"
if errorlevel 1 (
    echo Build failed. See output above for details.
    pause
    exit /b 1
)

echo.
echo Build succeeded. Executable: %~dp0dist\AiEnvClone.exe
echo You can copy dist\AiEnvClone.exe to any Windows PC (no Python needed).
pause
endlocal
