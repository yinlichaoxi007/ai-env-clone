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
- Windows 上会把**版本资源**写进 exe（文件属性 → 详细信息），内容由
  ``ai_env_clone/version.py`` 生成 ⇒ 与窗口标题、``--version`` 输出同源，不会漂移。
- 若杀毒软件误报，可将 dist 目录加入白名单。
"""
from __future__ import annotations

import argparse
import contextlib
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
# 允许从任意工作目录执行（例如 CI 用绝对路径调用本脚本）。
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from ai_env_clone.version import APP_NAME, __version__, windows_version_info  # noqa: E402

# Windows 控制台默认编码可能是 cp1252，无法打印中文。优先切 UTF-8；
# 若控制台不允许改编码，退而求其次只把 errors 设为 replace，避免中文打印直接抛异常。
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (ValueError, OSError):
        try:
            sys.stdout.reconfigure(errors="replace")
            sys.stderr.reconfigure(errors="replace")
        except (ValueError, OSError):
            pass

ENTRY = HERE / "ai_env_clone" / "__main__.py"
DIST = HERE / "dist"
BUILD = HERE / "build"


def cleanup_build_dir() -> None:
    """清理 PyInstaller 打包产生的中间产物目录 build/（纯缓存，下次打包自动重建）。

    打包成功后删除，避免把临时构建文件留在项目根。删除失败不影响产物，
    仅提示，可稍后手动清理。
    """
    if not BUILD.exists():
        return
    try:
        shutil.rmtree(BUILD)
        print("已清理打包中间产物目录：%s" % BUILD)
    except OSError as exc:
        print("清理 build 目录失败（不影响产物，可稍后手动删除）：%s" % exc, file=sys.stderr)


@contextlib.contextmanager
def version_file(exe_name: str):
    """临时写出 PyInstaller 的版本资源文件，供 ``--version-file`` 使用。

    内容来自 ``ai_env_clone.version.windows_version_info``，也就是版本号的唯一来源；
    因此 exe 属性页里的版本不会与窗口标题、``--version`` 输出漂移。
    放在临时目录而不是项目根：属一次构建的中间产物，不该留在工作区。
    """
    with tempfile.TemporaryDirectory(prefix="aienvclone-ver-") as tmp:
        path = Path(tmp) / "version_info.txt"
        path.write_text(windows_version_info(exe_name), encoding="utf-8")
        print("版本资源：%s → %s（%s）" % (path.name, exe_name, __version__))
        yield path


def main() -> int:
    parser = argparse.ArgumentParser(description="打包 AiEnvClone 为各平台可执行程序")
    parser.add_argument(
        "--name",
        default=APP_NAME,
        help="产物基础名（不含平台后缀），默认 %s" % APP_NAME,
    )
    args = parser.parse_args()

    if not ENTRY.exists():
        print("找不到入口文件：%s" % ENTRY)
        return 1

    print("打包 %s %s（%s）" % (APP_NAME, __version__, sys.platform))

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--onefile",                       # 单文件
        "--windowed",                      # 无控制台窗口（GUI 程序）
        "--name", args.name,
        "--clean",
        "--noconfirm",
    ]

    with contextlib.ExitStack() as stack:
        # --version-file 只在 Windows 生效（写的是 PE 的 VS_VERSIONINFO 资源），
        # 其它平台不传，避免 PyInstaller 报未知/无效果参数。
        if sys.platform == "win32":
            vf = stack.enter_context(version_file(args.name + ".exe"))
            cmd += ["--version-file", str(vf)]
        cmd.append(str(ENTRY))

        print("执行：%s" % " ".join(cmd))
        rc = subprocess.call(cmd)

    if rc == 0:
        print("\n打包完成，产物目录：%s" % DIST)
        cleanup_build_dir()
    else:
        print("\n打包失败，返回码 %d" % rc)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
