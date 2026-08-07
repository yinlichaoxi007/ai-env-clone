"""
把 AI 工具备份迁移工具打包成各平台可直接运行的可执行程序。

用法（在目标平台上，且已安装依赖）：
    pip install -r requirements.txt
    python build_exe.py [--name AiEnvClone-windows]

产物（PyInstaller 按平台自动加后缀）：
    Windows : dist/<name>.exe
    macOS   : dist/<name>.app          （GUI 程序，建议压缩后分发）
    Linux   : dist/<name>              （无后缀的可执行文件）

说明：
- 使用 --onefile 生成单文件；--windowed 表示无控制台窗口（纯 GUI）。
- PyInstaller 会自动收集 ai_env_clone 包（统一入口 ``__main__.py``）及其依赖，无需额外 --add-data。
- 若杀毒软件误报，可将 dist 目录加入白名单。
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ENTRY = HERE / "ai_env_clone" / "__main__.py"
DIST = HERE / "dist"
BUILD = HERE / "build"


def main() -> int:
    parser = argparse.ArgumentParser(description="打包 AiEnvClone 为各平台可执行程序")
    parser.add_argument(
        "--name",
        default="AiEnvClone",
        help="产物基础名（不含平台后缀），默认 AiEnvClone",
    )
    args = parser.parse_args()

    if not ENTRY.exists():
        print("找不到入口文件：%s" % ENTRY)
        return 1

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--onefile",                       # 单文件
        "--windowed",                      # 无控制台窗口（GUI 程序）
        "--name", args.name,
        "--clean",
        "--noconfirm",
        str(ENTRY),
    ]
    print("执行：%s" % " ".join(cmd))
    rc = subprocess.call(cmd)

    if rc == 0:
        print("\n打包完成，产物目录：%s" % DIST)
    else:
        print("\n打包失败，返回码 %d" % rc)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
