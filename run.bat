@echo off
chcp 65001 >nul 2>&1
REM 一键启动 Qoder 备份迁移工具（免输命令）
REM 双击本文件即可运行；若已打包成 QoderBackupTool.exe 则优先用它。
setlocal
cd /d "%~dp0"

if exist "dist\QoderBackupTool.exe" (
    start "" "dist\QoderBackupTool.exe"
    goto :eof
)

python --version >nul 2>&1
if errorlevel 1 (
    echo 未检测到 Python，请先安装 Python 3.10+ 并勾选“Add to PATH”。
    pause
    exit /b 1
)

start "" pythonw qoder_backup_tool.py
if errorlevel 1 (
    REM 回退到带控制台的方式，便于查看报错
    python qoder_backup_tool.py
)
endlocal
