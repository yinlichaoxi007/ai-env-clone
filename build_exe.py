"""
把 Qoder 备份迁移工具打包成单个 exe（Windows 一键启动版）。

用法（在 Windows 上，且已安装依赖）：
    pip install -r requirements.txt
    python build_exe.py

产物：
    dist/QoderBackupTool.exe   —— 双击即可运行，无需安装 Python。

说明：
- 使用 --onefile 生成单文件；-w 表示无控制台窗口（纯 GUI）。
- 通过 Importlib 机制，PyInstaller 会自动收集 qoder_backup_tool 及其依赖
  （qoder_backup_core、ai_env_clone 包等），无需额外 --add-data。
- 若杀毒软件误报，可将 dist 目录加入白名单。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ENTRY = HERE / "qoder_backup_tool.py"
DIST = HERE / "dist"
BUILD = HERE / "build"


def main() -> int:
    if not ENTRY.exists():
        print("找不到入口文件：%s" % ENTRY)
        return 1

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--onefile",                       # 单文件
        "--windowed",                      # 无控制台窗口（GUI 程序）
        "--name", "QoderBackupTool",
        "--clean",
        "--noconfirm",
        str(ENTRY),
    ]
    print("执行：%s" % " ".join(cmd))
    rc = subprocess.call(cmd)

    if rc == 0:
        exe = DIST / "QoderBackupTool.exe"
        print("\n打包完成：%s" % exe)
    else:
        print("\n打包失败，返回码 %d" % rc)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
