"""随包资源定位（使用说明、打赏图片等）。

只做一件事：把「相对仓库 / 相对 exe」两种模式下的资源路径统一到一处，
避免调用方各自拼一套（拼错一处就是「源码模式能看、打包后找不到」）。

- **源码模式**：资源在**仓库根**下（``docs/…``、``docs/images/…``），
  仓库根由本文件 ``__file__`` 上溯两级得到（``ai_env_clone/resources.py`` → 仓库根）。
- **打包模式**（PyInstaller onefile）：资源在 ``sys._MEIPASS`` 下。
  构建时由 ``build_exe.py`` 派生并 ``--add-data`` 进去，包内名为 ASCII 的
  ``help.md``（不用中文名，避开「中文文件名 + 路径分隔符」的跨平台边缘情况）。

本模块**不导入 tkinter**（供 ``--docs`` 这类 CLI 在 ``import tkinter`` 之前使用）。
"""

from __future__ import annotations

import os
import sys

from .doctext import strip_images

__all__ = [
    "repo_root",
    "resource_path",
    "read_help_doc",
    "HELP_DOC_NAME",
]

#: 包内（exe 内）使用说明的文件名。**刻意用 ASCII**：``--add-data`` 的两段路径
#: 还要按 ``os.pathsep`` 切分，临时目录在 Windows 上含盘符、POSIX 上是 ``/tmp/…``，
#: 全 ASCII 一次性绕开全部边缘情况。仓库里的源文件名仍是 ``docs/使用说明.md``。
HELP_DOC_NAME = "help.md"

#: 仓库里使用说明的相对路径（源码模式用；带图，构建期会被剥离）。
_HELP_REL = os.path.join("docs", "使用说明.md")


def repo_root() -> str:
    """源码模式下的仓库根目录（本文件的上溯两级）。"""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _bundle_root() -> str | None:
    """打包模式下的资源根（``sys._MEIPASS``），源码模式返回 ``None``。"""
    meipass = getattr(sys, "_MEIPASS", None)
    if getattr(sys, "frozen", False) and meipass:
        return str(meipass)
    return None


def resource_path(rel_path: str) -> str | None:
    """把相对路径解析成实际存在的文件路径；不存在返回 ``None``。

    供「关于」里的打赏二维码等**单个资产**使用。``rel_path`` 用 ``/`` 分隔
    （``"docs/images/reward_qr.png"``），内部会按平台换成 ``os.sep``。
    """
    parts = [p for p in rel_path.replace("\\", "/").split("/") if p and p != "."]
    root = _bundle_root()
    base = [root] if root else [repo_root()]
    path = os.path.join(*(base + parts))
    return path if os.path.isfile(path) else None


def read_help_doc() -> str:
    """读使用说明的**无图纯文本**（两条路径都过 :func:`strip_images`）。

    - 打包模式：读 ``sys._MEIPASS/help.md``（构建期已派生为无图版）；
    - 源码模式：读 ``docs/使用说明.md``（**带图**，运行时再剥一次）。

    ★ 两条路径都调 ``strip_images`` 且该函数**幂等** ⇒ 源码模式与打包模式
    得到的文本**逐字相同**，不会出现「开发时看到图片行、装完 exe 又不一样」。

    两种模式都找不到文件时抛 :class:`FileNotFoundError`，由调用方给出友好提示
    （打包漏 ``--add-data`` 属构建缺陷，不该静默吞掉）。
    """
    root = _bundle_root()
    if root:
        path = os.path.join(root, HELP_DOC_NAME)
    else:
        path = os.path.join(repo_root(), _HELP_REL)
    with open(path, "r", encoding="utf-8") as f:
        return strip_images(f.read())
