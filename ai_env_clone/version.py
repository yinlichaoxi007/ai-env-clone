"""产品标识与版本号的唯一定义处。

为什么单独成模块，而不是写在 ``__init__.py`` / ``__main__.py`` 里：

- ``__main__.py`` 顶部就 ``import tkinter``，而**版本查询不该依赖 GUI 库**——CI、
  最小化容器、只做版本核对的脚本可能没装 tkinter（本项目的受管 Python 就没有）。
  本模块不导入 tkinter，也不导入本包内其它模块，因此可以在 ``import tkinter``
  之前安全使用。
- 窗口标题模板放这里，是为了让「标题里必须带版本号」这条不变式能被**不装
  tkinter 的测试**直接断言（见 ``tests/test_version.py``）。

发布约定：发布 tag = ``v`` + :data:`__version__`（例如 ``v0.2.0-rc.1``）。
改版本号只改这一处——窗口标题、``--version`` 输出、打包产物的 exe 版本资源
全部由它派生；``tests/test_version.py`` 会在「当前 HEAD 正好落在一个 ``v*``
tag 上」时校验两者一致，防止 tag 与代码版本漂移。
"""

from __future__ import annotations

import sys

__all__ = [
    "APP_NAME",
    "APP_TITLE_TPL",
    "app_title",
    "handle_version_flag",
    "release_tuple",
    "version_line",
    "windows_version_info",
    "__version__",
]

#: 版本号。发布时打 tag ``v`` + 本值（见模块 docstring）。
__version__ = "0.2.0-rc.1"

#: 产物基础名：与 ``build_exe.py --name`` 的默认值、打包产物名一致。
APP_NAME = "AiEnvClone"

#: 主窗口标题模板：``<工具显示名> 备份迁移工具 v<版本>``。
#: 标题带版本号是刻意的——用户反馈问题截的是标题，版本号在里面就不必再追问。
APP_TITLE_TPL = "%s 备份迁移工具 v%s"

#: Windows 版本资源的字符串表语言：简体中文 + Unicode（codepage 1200）。
#: 表键 ``080404B0`` = langID 0x0804 与 codepage 0xB0（1200）的十六进制拼写。
_WIN_LANGID = 0x0804
_WIN_CODEPAGE = 1200
_WIN_TABLE_KEY = "080404B0"


def app_title(tool_display_name: str) -> str:
    """主窗口标题：``<工具显示名> 备份迁移工具 v<版本>``。"""
    return APP_TITLE_TPL % (tool_display_name, __version__)


def version_line() -> str:
    """``--version`` / ``-V`` 的单行输出。"""
    return "%s %s" % (APP_NAME, __version__)


def release_tuple() -> tuple[int, int, int, int]:
    """版本号 → Windows VERSIONINFO 的四段数字（``filevers`` / ``prodvers``）。

    版本资源的数字段**只能放整数**，预发布标识（``rc`` / ``beta`` …）无法表达，
    因此这里只取发布段并补零到四位：``0.2.0-rc.1`` → ``(0, 2, 0, 0)``。
    完整版本号仍由字符串字段携带（见 :func:`windows_version_info`），
    所以预发布版不会与同号正式版在界面文案上混淆。
    """
    head = __version__.split("-", 1)[0].split("+", 1)[0]
    parts: list[int] = []
    for seg in head.split("."):
        try:
            parts.append(int(seg))
        except ValueError:
            break
    while len(parts) < 4:
        parts.append(0)
    return (parts[0], parts[1], parts[2], parts[3])


def windows_version_info(exe_name: str = "") -> str:
    """生成 PyInstaller ``--version-file`` 的内容（exe 属性页 → 详细信息）。

    PyInstaller 用 ``eval()`` 反序列化该文件（``PyInstaller/utils/win32/
    versioninfo.py::load_version_info_from_text_file``），所以内容必须是
    ``VSVersionInfo(...)`` 的合法 Python 字面量。文件按 UTF-8 写出并附 encoding
    声明，PyInstaller 的 ``misc.decode`` 会遵从它 ⇒ 中文字段可正常显示。

    ``exe_name`` 用于 ``OriginalFilename``；CI 各平台产物名不同（Windows 为
    ``AiEnvClone-windows.exe``），由调用方传入，缺省用 :data:`APP_NAME`。
    """
    return _VERSION_FILE_TPL % {
        "filevers": repr(release_tuple()),
        "version": __version__,
        "app": APP_NAME,
        "original": exe_name or (APP_NAME + ".exe"),
        "tablekey": _WIN_TABLE_KEY,
        "langid": "0x%04X" % _WIN_LANGID,
        "codepage": repr(_WIN_CODEPAGE),
    }


def _emit(text: str) -> None:
    """输出一行文本；无控制台时退化为系统消息框。

    PyInstaller ``--windowed`` 产物的 ``sys.stdout`` 是 ``None``（没有控制台），
    此时 ``print`` 会直接失败，表现为「执行了却什么都没发生」。故用 ``ctypes``
    调 ``MessageBoxW`` 兜底——标准库、不需要 tkinter，也就不破坏本模块
    「可在导入 tkinter 之前使用」的前提。
    """
    if sys.stdout is not None:
        print(text)
        return
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, text, APP_NAME, 0x40)
    except Exception:  # pragma: no cover - 仅在异常环境（无 user32/非 Windows）触发
        pass


def handle_version_flag(argv: list[str]) -> bool:
    """``argv`` 里出现 ``--version`` / ``-V`` 时输出并返回 ``True``（调用方应退出）。

    刻意只「输出 + 报告已处理」，不自己 ``sys.exit``：退出码由调用方决定，
    测试也能直接断言返回值。``argv`` 传 ``sys.argv[1:]``，即不含程序名。
    """
    if not ("--version" in argv or "-V" in argv):
        return False
    _emit(version_line())
    return True


# ---------------------------------------------------------------------------
# Windows 版本资源模板。数字段由 release_tuple() 填入，其余为固定文案。
# 占位符用 %(name)s 形式；模板中不含其它百分号，故 % 格式化是安全的。
# ---------------------------------------------------------------------------
_VERSION_FILE_TPL = """# -*- coding: utf-8 -*-
# 本文件由 ai_env_clone/version.py 生成（build_exe.py 调用），请勿手工维护。
# 版本号的唯一来源是 ai_env_clone/version.py，改版本只改那里。
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=%(filevers)s,
    prodvers=%(filevers)s,
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo(
      [
        StringTable(
          '%(tablekey)s',
          [StringStruct('FileDescription', 'AI 工具环境备份迁移工具'),
           StringStruct('FileVersion', '%(version)s'),
           StringStruct('InternalName', '%(app)s'),
           StringStruct('LegalCopyright', 'Copyright (c) 2026 yinlichaoxi007'),
           StringStruct('OriginalFilename', '%(original)s'),
           StringStruct('ProductName', '%(app)s'),
           StringStruct('ProductVersion', '%(version)s')]
        )
      ]
    ),
    VarFileInfo([VarStruct('Translation', [%(langid)s, %(codepage)s])])
  ]
)
"""
