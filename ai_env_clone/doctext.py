"""使用说明的文本处理：剥离图片 + markdown 轻量转纯文本段落。

为什么单独成模块，且**不导入 tkinter**：

- 「图形界面查看器」与「命令行 ``--docs``」必须给出**逐字相同**的说明文本。
  若两边各写一套渲染，口径迟早漂移（改一处漏一处）。做法是把
  「markdown → 文本段落」做成纯函数，界面只负责把段落画出来。
- ``build_exe.py`` 在**构建期**就调 :func:`strip_images` 派生一份「无图版」
  打进 exe（``help.md``），运行时界面再调一次纯属**幂等兜底**——
  靠的就是「同一份实现 + 幂等」这两条。

设计约束（``tests/test_doc_text.py`` 逐条钉住）：

1. **不渲染图片、连占位都不写**。用户已定：使用说明菜单与 CLI 都只显示文字。
   遇到图片整行跳过或只删片段，不输出 ``[图片:xxx]`` 之类占位。
2. :func:`strip_images` **必须幂等**（``strip(strip(x)) == strip(x)``）——
   「构建期派生 + 运行期兜底都调它」这条设计正是靠它成立。
3. 删除整行图片后**必须折叠空行**，否则会留下连续空行、段落间距翻倍。
4. 行内删除片段**不得残留双空格**（中文会被视觉上粘在一起）。
5. 被链接包裹的图片 ``[![alt](img)](url)`` **连外层链接一并删**，
   否则留下 ``[](url)`` 坏链接。
"""

from __future__ import annotations

import re

__all__ = [
    "strip_images",
    "md_to_paragraphs",
    "plain_text",
    "handle_docs_flag",
]

#: 行内图片：``![alt](path)``。alt 与 path 都允许为空、允许含空格。
_IMG_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")

#: 被链接包裹的图片：``[![alt](img)](url)``。必须**先**处理它，
#: 否则 ``_IMG_RE`` 只删掉内层图片、外层链接变成 ``[](url)`` 这种坏链接。
_LINKED_IMG_RE = re.compile(r"\[\s*!\[[^\]]*\]\([^)]*\)\s*\]\([^)]*\)")

#: 整行就是一张图片（行内/被链接包裹的两种形态都算）。判「整行」要先 strip。
_ONLY_IMG_RE = re.compile(r"(?:!\[[^\]]*\]\([^)]*\)|\[\s*!\[[^\]]*\]\([^)]*\)\s*\]\([^)]*\))$")

#: 行内链接：``[text](url)`` → 渲染成 ``text``（URL 不在界面里显示）。
_LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")

#: 代码栅栏（``` 或 ~~~，可带语言标注）。
_FENCE_RE = re.compile(r"^\s*(?:```|~~~)")

#: 分隔线：``---`` / ``***`` / ``___``（3 个以上）。
_HR_RE = re.compile(r"^\s*[-*_]{3,}\s*$")

#: 标题：1~6 个 #。
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")

#: 无序列表项：``- `` / ``* `` / ``+ ``。
_UL_RE = re.compile(r"^\s*[-*+]\s+(.*)$")

#: 有序列表项：``1. `` / ``12) ``。
_OL_RE = re.compile(r"^\s*\d+[.)]\s+(.*)$")

#: 引用行：``> xxx``。
_QUOTE_RE = re.compile(r"^\s*>\s?(.*)$")

#: 表格行：以 ``|`` 开头或结尾的整行。
_TABLE_RE = re.compile(r"^\s*\|.*$")

#: 图片删除后用于统一空白的哨兵（避免直接拼接造成双空格）。
_SENTINEL = "\x00"


def _strip_inline(line: str) -> str:
    """删掉一行里的全部图片写法，**不留双空格**。

    做法是先把图片换成哨兵，再用 ``[ \\t]*哨兵[ \\t]*`` 整体替换成**一个空格**，
    这样「图片两侧原本各有一个空格」就归一为一个，不会出现 ``文字  文字``。
    """
    line = _LINKED_IMG_RE.sub(_SENTINEL, line)
    line = _IMG_RE.sub(_SENTINEL, line)
    if _SENTINEL not in line:
        return line
    return re.sub(r"[ \t]*" + _SENTINEL + r"[ \t]*", " ", line)


def strip_images(md: str) -> str:
    """剥离 markdown 中的图片（**整行跳过、连占位都不写**），返回纯文本。

    规则（按「图片是否独立成行」分两种）：

    ==================================== ==========================================
    形态                                处理
    ==================================== ==========================================
    **独立成行的图片**                    **整行删除**，并折叠随之产生的连续空行
    **行内图片**（夹在文字中间）          只删片段，且**不残留双空格**
    **被链接包裹的图片** ``[![alt](i)]`` 连同外层链接一并删除（否则留坏链接）
    ==================================== ==========================================

    ★ **必须幂等**：``strip_images(strip_images(x)) == strip_images(x)``。
    构建期与运行期都调它，两条路径行为一致全靠这条。

    已知实测（对仓库 ``docs/使用说明.md``）：**284 行 → 282 行**，
    即少 2 行而非 1 行 —— 删掉 1 行图片 + 折叠掉 1 个空行。
    """
    if not md:
        return md

    out: list[str] = []
    in_fence = False  # 代码块内不做任何 markdown 处理（含缩进清理）
    pending_gap = False  # 刚删掉一张独立成行的图片：需要吃掉其后紧邻的冗余空行

    for raw in md.split("\n"):
        if _FENCE_RE.match(raw):
            in_fence = not in_fence
            out.append(raw)
            pending_gap = False
            continue

        if in_fence:
            # 代码块原样保留：里面的缩进与空行是有语义的，不能折叠。
            out.append(raw)
            continue

        stripped = raw.strip()
        if stripped and _ONLY_IMG_RE.fullmatch(stripped):
            pending_gap = True
            continue

        is_blank = not stripped
        if pending_gap and is_blank and (not out or not out[-1].strip()):
            # 删掉图片行后紧跟的冗余空行：与上一个空行重复，跳过（达到折叠效果）
            pending_gap = False
            continue
        pending_gap = False

        if is_blank:
            out.append("")
            continue

        line = _strip_inline(raw)
        if _SENTINEL in line:  # 行内图片被删成了整行
            line = ""
        # 非代码块行去掉首尾空白：图片在行首/行尾时不留下悬空空格。
        out.append(line.strip())

    text = "\n".join(out)
    # 结尾多余空行收成一个（连续空行一律压成一个），
    # 避免删除图片后尾部出现「空行 + 空行」造成下方渲染多出一段空白。
    return text.rstrip("\n") + ("\n" if text.strip() else "")


def md_to_paragraphs(md: str) -> list[tuple[str, str]]:
    """markdown → ``[(文本, 标签), …]``；标签供界面决定字体，CLI 忽略。

    标签取值：``h1`` / ``h2`` / ``h3`` / ``h4`` / ``p`` / ``li`` / ``quote``
    / ``code`` / ``table`` / ``hr``。

    - 调用前会自动过一遍 :func:`strip_images`（幂等，故两次调用等价），
      所以即使传入的是带图的仓库原文，输出也**不含任何图片行**；
    - **空行不产出段落**（段落间距由界面统一控制，否则会叠加出双倍空白）；
    - 链接渲染为可读文本 ``[文字](url)`` → ``文字``；不显示 URL。
    """
    text = strip_images(md)
    out: list[tuple[str, str]] = []
    in_fence = False
    for raw in text.split("\n"):
        if _FENCE_RE.match(raw):
            in_fence = not in_fence
            out.append((raw.strip(), "code"))
            continue
        if in_fence:
            out.append((raw, "code"))
            continue
        stripped = raw.strip()
        if not stripped:
            continue

        m = _HR_RE.match(raw)
        if m:
            out.append(("", "hr"))
            continue

        m = _HEADING_RE.match(raw)
        if m:
            out.append((_plain_inline(m.group(2).strip()), "h%d" % min(len(m.group(1)), 4)))
            continue

        m = _QUOTE_RE.match(raw)
        if m:
            out.append((_plain_inline(m.group(1).strip()), "quote"))
            continue

        m = _UL_RE.match(raw)
        if m:
            out.append(("- " + _plain_inline(m.group(1).strip()), "li"))
            continue

        m = _OL_RE.match(raw)
        if m:
            out.append((_plain_inline(m.group(1).strip()), "li"))
            continue

        if _TABLE_RE.match(raw):
            # 表格按等宽原文保留（不引第三方渲染器做真表格）
            out.append((_plain_inline(stripped), "table"))
            continue

        out.append((_plain_inline(stripped), "p"))

    return out


def _plain_inline(s: str) -> str:
    """行内标记 → 纯文本：链接只留文字，其余标记原样。"""
    return _LINK_RE.sub(lambda m: m.group(1), s)


def plain_text(md: str) -> str:
    """markdown → 纯文本（供 CLI ``--docs`` 直接打印）。

    与 :func:`md_to_paragraphs` **同源**：段落序列一致，只是输出端换成终端。
    列表项保留 ``- `` 前缀以便阅读；代码栅栏标记本身不打印。
    """
    lines: list[str] = []
    for content, tag in md_to_paragraphs(md):
        if tag == "hr":
            lines.append("─" * 32)
        elif tag == "code":
            text = content.strip()
            if not text or text.startswith("```") or text.startswith("~~~"):
                continue  # 跳过栅栏本身
            lines.append("    " + text)
        else:
            lines.append(content)
    return "\n".join(lines) + "\n"


def handle_docs_flag(argv: list[str]) -> bool:
    """``argv`` 里出现 ``--docs`` 时把使用说明按纯文本打印，返回 ``True``。

    刻意与 :func:`version.handle_version_flag` 同构：**不自己** ``sys.exit``，
    退出码交由调用方决定，测试才能直接断言返回值。

    之所以放在本模块（而不是 ``__main__.py``）：``__main__.py`` 一 import 就
    ``import tkinter``，而本分支必须能在**没装 tkinter** 的环境（CI、受管 Python）
    里工作，因此它的实现不能依赖 GUI 库。

    输出与「帮助 → 使用说明」查看器**共用** ``resources.read_help_doc()`` +
    :func:`plain_text`，所以两边口径结构上一致，不靠人工同步。
    """
    if "--docs" not in argv:
        return False
    from .resources import read_help_doc
    from .version import emit_console

    try:
        text = plain_text(read_help_doc())
    except OSError as exc:
        text = "（无法读取使用说明：%s）\n" % exc
    emit_console(text)
    return True
