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
6. ★ **行内标记只删标记、保留内容**（``**粗**`` → ``粗``）。原因：Tk ``Text`` 与
   终端都是**纯文本**，markdown 标记不会被渲染成样式，原样留下用户看到的就是
   一堆 ``**``、`` ` ``。同理表格不再输出原始 ``|`` 与 ``|---|`` 分隔行，而是
   渲染成**按显示宽度对齐的纯文本**（见 :func:`_render_table`）。
7. ★ **一律不处理下划线强调**（``_斜体_`` / ``__粗__``）：本文档里 ``_`` 几乎
   都是标识符（``ai_env_clone``、``OPENAI_API_KEY``、``import_migration``），
   按 markdown 规则它们要满足「两侧是词边界」才算强调，而宽松匹配会把标识符
   劈成两半、把命令改坏（实测真文档里 12 处 ``_x_`` 命中**全是**误判）。
"""

from __future__ import annotations

import re
import unicodedata

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

#: 有序列表项：``1. `` / ``12) ``。★ 必须**连编号一起抓**：编号是内容的一部分
#: （「按第 3 步做」这类指引靠它），只抓正文会把编号丢掉、有序列表退化成无序。
_OL_RE = re.compile(r"^\s*(\d+[.)])\s+(.*)$")

#: 引用行：``> xxx``。
_QUOTE_RE = re.compile(r"^\s*>\s?(.*)$")

#: 表格行：以 ``|`` 开头或结尾的整行。
_TABLE_RE = re.compile(r"^\s*\|.*$")

#: 表格的「表头分隔行」单元格：``---`` / ``:--`` / ``--:`` / ``:-:``。
_SEP_CELL_RE = re.compile(r"^:?-+:?$")

#: 行内粗体：``**粗体**``。
_BOLD_RE = re.compile(r"\*\*([^*\n]+)\*\*")

#: 行内斜体：``*斜体*``（不跨行，且不吞掉 ``**`` 里的星号）。
#: ★ 只认星号，**不认下划线**——理由见模块 docstring 第 7 条。
_ITALIC_RE = re.compile(r"(?<!\*)\*([^*\n]+)\*(?!\*)")

#: 删除线：``~~x~~``。
_STRIKE_RE = re.compile(r"~~([^~\n]+)~~")

#: 行内代码：`` `x` ``（可能为空串，如 `` `` ``）。
_CODE_RE = re.compile(r"`([^`\n]*)`")

#: 表格列间的显示宽度：东亚宽字符（W/F）算 2 列，其余算 1 列。
_WIDE_EAW = frozenset(("W", "F"))

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
      引用块里的空续行（单独一个 ``>``）同样不产出段落；
    - 链接渲染为可读文本 ``[文字](url)`` → ``文字``；不显示 URL；
    - 行内标记（``**粗**`` / ``*斜*`` / ``~~删~~`` / `` `码` ``）**只删标记、
      保留内容**，因为 Tk ``Text`` 与终端都不渲染 markdown；
    - 表格整块交给 :func:`_render_table` 渲染成对齐纯文本（``table`` 标签的
      内容可能含换行，界面需用等宽字体绘制该标签）。
    """
    text = strip_images(md)
    out: list[tuple[str, str]] = []
    in_fence = False
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        raw = lines[i]
        if _FENCE_RE.match(raw):
            # 栅栏行本身**不产出段落**：它只是「代码块开始 / 结束」的标记，
            # 输出 `` ```powershell `` 这种行在纯文本查看器里纯属噪音。
            # 代码块的身份由内容行的 ``code`` 标签承担。
            in_fence = not in_fence
            i += 1
            continue
        if in_fence:
            out.append((raw, "code"))
            i += 1
            continue
        stripped = raw.strip()
        if not stripped:
            i += 1
            continue

        if _TABLE_RE.match(raw):
            # 表格必须**整块**处理：列宽要看完所有行才知道。
            block: list[list[str]] = []
            while i < len(lines) and _TABLE_RE.match(lines[i]):
                block.append([_plain_inline(c) for c in _split_row(lines[i])])
                i += 1
            out.append((_render_table(block), "table"))
            continue

        m = _HR_RE.match(raw)
        if m:
            out.append(("", "hr"))
            i += 1
            continue

        m = _HEADING_RE.match(raw)
        if m:
            out.append((_plain_inline(m.group(2).strip()), "h%d" % min(len(m.group(1)), 4)))
            i += 1
            continue

        m = _QUOTE_RE.match(raw)
        if m:
            content = _plain_inline(m.group(1).strip())
            if content:  # 单独一个 ``>`` 只是空续行，不该产出一个空段落
                out.append((content, "quote"))
            i += 1
            continue

        m = _UL_RE.match(raw)
        if m:
            out.append(("- " + _plain_inline(m.group(1).strip()), "li"))
            i += 1
            continue

        m = _OL_RE.match(raw)
        if m:
            # 编号必须保留：有序列表的序号是内容的一部分
            out.append((m.group(1) + " " + _plain_inline(m.group(2).strip()), "li"))
            i += 1
            continue

        out.append((_plain_inline(stripped), "p"))
        i += 1

    return out


def _disp_width(s: str) -> int:
    """字符串在**等宽字体**下占的列数（东亚宽字符按 2 列算）。

    对齐表格必须按「显示宽度」而不是 ``len()`` 算：``工具`` 是 2 个字符、4 列宽，
    按 ``len()`` 补空格会让含中文的列整体左偏。
    """
    return sum(2 if unicodedata.east_asian_width(ch) in _WIDE_EAW else 1
               for ch in s)


def _split_row(line: str) -> list[str]:
    """``| a | b |`` → ``['a', 'b']``（去掉首尾竖线与单元格两侧空白）。"""
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip() for c in s.split("|")]


def _is_sep_row(cells: list[str]) -> bool:
    """是否为表头分隔行（``|---|:--:|``）。"""
    return bool(cells) and all(_SEP_CELL_RE.match(c) for c in cells)


def _render_table(rows: list[list[str]]) -> str:
    """表格块 → **按显示宽度对齐的纯文本**（等宽字体下即可读）。

    做三件事：

    1. **丢掉 ``|---|`` 分隔行**。纯文本画不出表格线，留着它只会让每行多出一串
       横杠；分隔行原本的「表头 / 数据分界」语义改由下面第 3 点的横线承担。
    2. **按显示宽度对齐各列**（东亚字符算 2 列，见 :func:`_disp_width`），列间留
       2 个空格。不做「超出宽度就折行」这类处理——查看器带横向滚动条，宁可宽一点
       也不要让单元格内容被截断。
    3. 若原表**有**分隔行，就在表头下补一条等宽横线，保住「哪行是表头」这个信息；
       原表没有分隔行（非标准写法）则原样对齐、不加横线。

    各单元格在此**之前**已过 :func:`_plain_inline`（调用方负责），故对齐宽度算的是
    最终要显示的文本。
    """
    body = [r for r in rows if not _is_sep_row(r)]
    if not body:
        return ""
    has_sep = len(body) != len(rows)

    ncol = max(len(r) for r in body)
    body = [r + [""] * (ncol - len(r)) for r in body]
    widths = [max(_disp_width(r[i]) for r in body) for i in range(ncol)]

    def _line(cells: list[str]) -> str:
        padded = [cells[i] + " " * (widths[i] - _disp_width(cells[i]))
                  for i in range(ncol)]
        return "  ".join(padded).rstrip()

    out = [_line(body[0])]
    if has_sep:
        out.append("  ".join("-" * w for w in widths).rstrip())
    out.extend(_line(r) for r in body[1:])
    return "\n".join(out)


def _plain_inline(s: str) -> str:
    """行内标记 → 纯文本：**只删标记、保留内容**。

    Tk ``Text`` 控件与终端都是**纯文本**，markdown 标记不会被渲染成样式——不处理
    的话用户看到的就是字面的 ``**``、`` ` ``、``~~``。故这里把标记本身删掉：

    ========================================== ==================
    原文                                        输出
    ========================================== ==================
    ``[文字](url)``                            ``文字``（URL 不外显）
    ``**粗体**`` / ``*斜体*`` / ``~~删除~~``     ``粗体`` / ``斜体`` / ``删除``
    `` `代码` ``                                ``代码``（反引号去掉）
    ========================================== ==================

    ★ 关键实现细节：**代码片段必须「先占位、后还原」，不能简单地先剥掉反引号**。
    真文档里有 `` `***REDACTED***` ``、`` `messages/*.json` ``、
    `` `...\\workspaceStorage\\*\\workspace.json` `` 这类内容，反引号一剥，里面的
    ``*`` 就暴露给强调规则，会把 ``***REDACTED***`` 吃成 ``REDACTED``、把 glob
    模式 ``messages/*.json`` 吃成 ``messages/.json``。占位符自身不含 ``*``/``~``，
    故强调规则看不见它们；等强调处理完再还原，代码内容一个字符都不会变。

    ★ 见模块 docstring 第 7 条：**下划线强调一律不处理**（``ai_env_clone``、
    ``OPENAI_API_KEY`` 这类标识符远多于真正的 ``_斜体_``）。
    """
    codes: list[str] = []

    def _stash(m: re.Match) -> str:
        codes.append(m.group(1))
        return "\x01%d\x01" % (len(codes) - 1)

    s = _LINK_RE.sub(lambda m: m.group(1), s)
    s = _CODE_RE.sub(_stash, s)
    s = _STRIKE_RE.sub(lambda m: m.group(1), s)
    s = _BOLD_RE.sub(lambda m: m.group(1), s)
    s = _ITALIC_RE.sub(lambda m: m.group(1), s)
    for idx, code in enumerate(codes):
        s = s.replace("\x01%d\x01" % idx, code)
    return s


def plain_text(md: str) -> str:
    """markdown → 纯文本（供 CLI ``--docs`` 直接打印）。

    与 :func:`md_to_paragraphs` **同源**：段落序列一致，只是输出端换成终端——
    终端渲染能力与 Tk ``Text`` 一样都是「纯文本 + 缩进」，所以行内标记、表格、
    代码栅栏这几件事在两边口径统一，不会一个剥了标记、另一个没剥。

    差异只有两处**表现**（不是内容）：列表项保留 ``- `` 前缀、代码块按 4 空格缩进。
    """
    lines: list[str] = []
    for content, tag in md_to_paragraphs(md):
        if tag == "hr":
            lines.append("─" * 32)
        elif tag == "code":
            # 栅栏行已由 md_to_paragraphs 丢弃，这里只会拿到真正的代码内容。
            text = content.strip()
            if not text:
                continue
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
