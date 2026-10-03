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
- PyInstaller 会自动收集 ai_env_clone 包（统一入口 ``__main__.py``）及其依赖。
- Windows 上会把**版本资源**写进 exe（文件属性 → 详细信息），内容由
  ``ai_env_clone/version.py`` 生成 ⇒ 与窗口标题、``--version`` 输出同源，不会漂移。
- **使用说明**（帮助 → 使用说明 / ``--docs``）的纯文本由本脚本在**构建期派生**：
  读 ``docs/使用说明.md`` → 剥离图片（``ai_env_clone.doctext.strip_images``）→ 写临时目录
  → ``--add-data`` 打进 exe（包内根下 ``help.md``）。
  仓库源文档**保持带图**（给开发者与仓库网页看），两者同源派生、不会漂移。
  ★ 只带这一个**文件**、**绝不整目录加 ``docs``**：``docs/local/`` 是 gitignored 的
  内部设计文档（本机 388 KB，CI 检出后根本不存在）⇒ 整目录会把它们误打进 exe 发给用户，
  且造成「本机产物比 CI 大」的不一致。
  ★ **只改本脚本一处即四平台自动生效**（CI 四平台跑的都是 ``python build_exe.py``）。
- 若杀毒软件误报，可将 dist 目录加入白名单。
"""
from __future__ import annotations

import argparse
import contextlib
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
# 允许从任意工作目录执行（例如 CI 用绝对路径调用本脚本）。
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from ai_env_clone.doctext import strip_images  # noqa: E402
from ai_env_clone.resources import HELP_DOC_NAME  # noqa: E402
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
#: 仓库里的使用说明源文档（**带图**，给开发者与仓库网页看；打包时剥离图片后另存）
HELP_SRC = HERE / "docs" / "使用说明.md"


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


@contextlib.contextmanager
def doc_text_file():
    """临时写出「无图版使用说明」，供 ``--add-data`` 打进 exe。

    流程：读仓库源文档（**带图**）→ :func:`ai_env_clone.doctext.strip_images`
    （与运行时查看器**同一份实现**）→ 写临时目录 → 随 ``with`` 块内的
    ``--add-data`` 进包。包内根下即 :data:`HELP_DOC_NAME`（ASCII 名）。

    三个刻意的选择：

    1. **构建期派生，而不是运行时剥离**：运行时只剩「幂等兜底」，
       两模式行为必然一致；仓库源文档得以保持带图。
    2. **ASCII 包内名**：``--add-data`` 的两段路径还要按 ``os.pathsep`` 切分，
       临时目录在 Windows 上含盘符、POSIX 上是 ``/tmp/…`` ⇒ 全 ASCII 一次性
       绕开「中文文件名 + 路径分隔符」的全部跨平台边缘情况。
    3. **只带这一个文件**：见模块 docstring —— 整目录加 ``docs`` 会把
       gitignored 的 ``docs/local/`` 内部设计文档误打进 exe。
    """
    if not HELP_SRC.exists():
        print("找不到使用说明源文档：%s（将不随包分发，界面「使用说明」会提示缺失）"
              % HELP_SRC, file=sys.stderr)
        yield None
        return
    text = strip_images(HELP_SRC.read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory(prefix="aienvclone-doc-") as tmp:
        path = Path(tmp) / HELP_DOC_NAME
        # 显式保留源文档的行尾（默认换行），避免 Windows 上写出 CRLF、
        # 在 Linux/macOS 上写出与仓库不一致的行尾。
        path.write_text(text, encoding="utf-8", newline="")
        print("使用说明（无图）：%s ← %s（%d 字符）"
              % (HELP_DOC_NAME, HELP_SRC.name, len(text)))
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
        # 使用说明（无图版）：四平台都带。目标目录写 "." ⇒ exe 内即根下 help.md。
        # ★ 用 os.pathsep 拼两段路径（Windows ";" / POSIX ":"），别手拼字符串。
        doc = stack.enter_context(doc_text_file())
        if doc is not None:
            cmd += ["--add-data", str(doc) + os.pathsep + "."]
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
