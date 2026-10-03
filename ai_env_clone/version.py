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
    "LEGAL_COPYRIGHT",
    "FINAL",
    "app_title",
    "emit_console",
    "handle_version_flag",
    "is_newer",
    "is_prerelease",
    "parse_version",
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

#: 版权串。**「关于」对话框与 exe 版本资源共用这一个常量**，
#: 避免两处写法漂移（改年份只改这里）。
LEGAL_COPYRIGHT = "Copyright (c) 2026 yinlichaoxi007"

#: 可识别的预发布标识，顺序即**优先级**：同一版本号下 ``dev`` < ``alpha`` <
#: ``beta`` < ``rc`` < 正式版。未列出的标识按 :func:`_tag_rank` 排在
#: 已列出的所有标识之后、正式版之前。
_PRERELEASE_TAGS = ("dev", "alpha", "beta", "rc")

#: :func:`parse_version` 里代表「正式版」的哨兵。**它必须大于任何预发布元组**。
#: ⚠️ **不能用空元组**：Python 里 ``() < (0, "rc", 1)``，空元组会排到**最前**，
#: 于是「同号正式版比预发布旧」这种反向排序会静默发生、且不报错。
#: 故取一个**优先级高于所有已知与未知标识**的数值作首位。
FINAL = (len(_PRERELEASE_TAGS) + 1, "", 0)

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


def _tag_rank(tag: str) -> int:
    try:
        return _PRERELEASE_TAGS.index(tag)
    except ValueError:
        # 未知标识（如 ``preview``）排在已列出的所有标识之后，仍小于正式版
        return len(_PRERELEASE_TAGS)


def parse_version(text: str) -> tuple[tuple[int, int, int], tuple] | None:
    """把版本串解析成**可保序比较**的元组；无法解析返回 ``None``。

    返回 ``(数字段, 预发布段)``，其中预发布段为 :data:`FINAL` 表示正式版。
    预发布段形如 ``(标识优先级, 标识小写, 序号)`` —— 带上**优先级**是为了让未知标识
    也能稳定参与比较而不抛异常。数字段会补齐到 3 位（``1.2`` == ``1.2.0``）。
    ``+`` 之后的构建元数据一律丢弃（不参与版本序）。

    两种预发布写法都认（历史 tag 用过后者）::

        "0.2.0-rc.1"   →  ((0, 2, 0), (3, 'rc', 1))
        "0.1.0rc"      →  ((0, 1, 0), (3, 'rc', 0))

    ★ **不要拿 :func:`release_tuple` 做版本比较** —— 它为了写 Windows VERSIONINFO
    而丢弃预发布段（``0.2.0-rc.1`` → ``(0, 2, 0, 0)``），会把 ``0.2.0-rc.1``
    与同号正式版判成相等。更新比较必须用本函数。
    """
    if not isinstance(text, str):
        return None
    s = text.strip()
    if s[:1] in ("v", "V"):
        s = s[1:]
    s = s.split("+", 1)[0]
    if not s:
        return None

    head, sep, tail = s.partition("-")
    segs = head.split(".")
    if len(segs) < 2 or not segs[0]:
        return None

    # 历史 tag 用过「数字段直接粘标识」的写法（``0.1.0rc``、``0.1.0-rc`` 同义）。
    # 先把最后一段的数字前缀与字母后缀拆开。
    glued_tag = ""
    last = segs[-1]
    if not last.isdigit():
        cut = 0
        while cut < len(last) and last[cut].isdigit():
            cut += 1
        if cut == 0:  # 整段都不是数字 ⇒ 非法
            return None
        glued_tag = last[cut:].lower()
        segs[-1] = last[:cut]

    nums: list[int] = []
    for seg in segs:
        if not seg.isdigit():
            return None
        nums.append(int(seg))
    # 数字段补齐到 3 位，便于直接逐位比较（1.2 与 1.2.0 等价）
    while len(nums) < 3:
        nums.append(0)
    numbers = tuple(nums[:3])

    if not sep:
        if glued_tag:
            return (numbers, (_tag_rank(glued_tag), glued_tag, 0))
        return (numbers, FINAL)

    # 预发布段：标识 + 可选序号（rc.1 / rc.2 / beta）
    if not tail:
        return None
    parts = [p for p in tail.split(".") if p != ""]
    if not parts:
        return None
    tag = parts[0].strip().lower()
    if not tag:
        return None
    num = 0
    if len(parts) > 1:
        num_text = "".join(parts[1:]).strip()
        if num_text:
            if not num_text.isdigit():
                return None
            num = int(num_text)
    return (numbers, (_tag_rank(tag), tag, num))


def is_newer(candidate: str, current: str | None = None) -> bool:
    """``candidate`` 是否比 ``current``（缺省取本程序版本）更新。

    - 数字段逐位比；
    - 数字段相同时**预发布 < 正式**（``0.2.0-rc.1 < 0.2.0``）——
      这正是「rc 用户会被提示升级到同号正式版」，是对的；
    - 两个都是预发布时按「标识优先级 → 序号」比（``rc.1 < rc.2``）。

    ★ 任一侧无法解析即返回 ``False``（宁可漏报更新，也不给用户一个坏包）。
    """
    cur = current if current is not None else __version__
    a = parse_version(candidate)
    b = parse_version(cur)
    if a is None or b is None:
        return False
    if a[0] != b[0]:
        return a[0] > b[0]
    return a[1] > b[1]


def is_prerelease(text: str | None = None) -> bool:
    """给定版本（缺省本程序版本）是否为预发布版。

    用于更新通道判定：**预发布用户默认接收预发布更新，正式版用户默认只接正式版**
    （界面另给勾选框可改）。字符串里带 ``-`` 段的即视为预发布。
    """
    raw = text if text is not None else __version__
    s = raw.strip()
    if s[:1] in ("v", "V"):
        s = s[1:]
    if "-" in s:
        return True
    parsed = parse_version(s)
    if parsed is None:
        return False
    return parsed[1] != FINAL


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


def emit_console(text: str) -> None:
    """输出一段文本；无控制台时退化为系统消息框。

    PyInstaller ``--windowed`` 产物的 ``sys.stdout`` 是 ``None``（没有控制台），
    此时 ``print`` 会直接失败，表现为「执行了却什么都没发生」。故用 ``ctypes``
    调 ``MessageBoxW`` 兜底——标准库、不需要 tkinter，也就不破坏本模块
    「可在导入 tkinter 之前使用」的前提。

    ⚠️ 消息框能显示的长度有限（超长会被截断）。``--docs`` 的完整内容应以
    终端 / 控制台为准；``--windowed`` 产物本来就没有控制台，此时只作兜底提示。
    """
    if sys.stdout is not None:
        sys.stdout.write(text)
        if not text.endswith("\n"):
            sys.stdout.write("\n")
        try:
            sys.stdout.flush()
        except (OSError, ValueError):  # pragma: no cover - 管道已关闭
            pass
        return
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, text, APP_NAME, 0x40)
    except Exception:  # pragma: no cover - 仅在异常环境（无 user32/非 Windows）触发
        pass


def _emit(text: str) -> None:
    """输出一行文本（:func:`handle_version_flag` 用）。"""
    emit_console(text)


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
