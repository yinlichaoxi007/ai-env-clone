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
   一堆 ``**``、`` ` ``。同理表格不再输出原始的 ``|---|`` 分隔行，而是按
   **调用方传入的可用宽度**铺成网格：装不下自然宽就**自己折行**（列仍对齐），
   只有**极窄**时才退成分条。★ 表格与分条之间只有**一个**切换阈值
   （见 :func:`_table_min_total`），变宽、变窄两个方向走的是同一个算式。
   ⇒ 用户实测反馈过两次相关的坑：先是「拉到最大还是逐项列出」（可用宽度被写成了
   常量），后是「宽度变小变分条的阈值远小于变大才变回表格的阈值，应该一样」——
   后者是因为网格曾按「装得下自然宽 / 折行」分成两条判据（64 列变分条、169 列才
   不再折行），中间那档又没有表格的视觉标识，被读成了「逐项列出」。
   ★ 可用宽度必须**实测传入**，写成常量会让「把窗口拉宽」对渲染毫无影响。
7. ★ **一律不处理下划线强调**（``_斜体_`` / ``__粗__``）：本文档里 ``_`` 几乎
   都是标识符（``ai_env_clone``、``OPENAI_API_KEY``、``import_migration``），
   按 markdown 规则它们要满足「两侧是词边界」才算强调，而宽松匹配会把标识符
   劈成两半、把命令改坏（实测真文档里 12 处 ``_x_`` 命中**全是**误判）。
"""

from __future__ import annotations

import shutil
import re
import sys
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

#: 表格网格的**默认**显示列上限——注意这**不是**「超过就换形态」的硬阈值。
#:
#: ★ 真实可用宽度由**调用方实测后传入**：界面量查看器正文区、CLI 取终端列数
#: （见 :func:`md_to_paragraphs` 的 ``table_max_width``）。本常量只在调用方
#: **拿不到**宽度时兜底，数值取常见 100 列终端与 880 px 查看器（等宽 7 px/列
#: ⇒ 约 117 列）的较小者。
#:
#: 历史教训：这里曾是写死的阈值，于是**窗口拉到最大、表格也仍是分条**——
#: 因为渲染结果与窗口宽度根本无关。别再把可用宽度做成常量。
_TABLE_MAX_WIDTH = 96

#: 网格里每列的**最小**显示列数（14 列 ≈ 7 个汉字）。低于它，单元格会被切成
#: 「一行两三个字」的窄条，不如改成分条（见 :func:`_reflow_table`）。
#: 它同时是两件事的门槛：① :func:`_fit_widths` 的压缩下限；② 判定「还能不能铺表格」
#: 的算式（见 :func:`_table_min_total`）。
_TABLE_FIT_MIN_COL = 14

#: 列与列之间的分隔串（**含竖线**）。这条竖线是网格形态唯一的视觉标识：纯文本
#: 没有表格线，不画它，折行后的续行就会与相邻列挤成「一坨左对齐文字」，被读成
#: 「逐项列出」（用户实测反馈）。画上之后，无论单元格折成几行，列边界始终可见。
_TABLE_SEP = " | "

#: 列间距的显示宽度（= ``_TABLE_SEP`` 的宽度；竖线是 ASCII，算 1 列）。
_TABLE_GAP = 3

#: **任何**表格形态的最小可用宽度：68 = 4 列最小版式的宽度（``4 × 14 + 3 × 3 = 65``）
#: 再留 3 列余量。
#:
#: ★ 这条「统一下限」是**用户实测反馈**的直接产物。原先阈值随列数变化（2 列 40、
#: 4 列 64），于是同一个窗口宽度下一张表还是表格、另一张已退成分条 —— 看起来就是
#: 「阈值错开」。抬到统一的 68 后，真文档里 2 列与 4 列两表的切换点**完全一致**，
#: 变宽 / 变窄两个方向的阈值也自然一致（判据只有一个算式）。
_TABLE_MIN_WIDTH = 68

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


def md_to_paragraphs(md: str,
                     table_max_width: int | None = None) -> list[tuple[str, str]]:
    """markdown → ``[(文本, 标签), …]``；标签供界面决定字体，CLI 忽略。

    标签取值：``h1`` / ``h2`` / ``h3`` / ``h4`` / ``p`` / ``li`` / ``quote``
    / ``code`` / ``table`` / ``records`` / ``hr``。

    - **``table_max_width`` 是「表格能有多少显示列可用」**，由调用方**实测**后传入
      （界面量查看器正文区、CLI 取终端列数）；``None`` 时用 :data:`_TABLE_MAX_WIDTH`
      兜底。★ 这个参数的存在本身就是为了让「窗口拉宽 → 表格变宽」成立：写成常量
      会让渲染结果与窗口宽度脱钩（见 :func:`_render_table`）。
    - 调用前会自动过一遍 :func:`strip_images`（幂等，故两次调用等价），
      所以即使传入的是带图的仓库原文，输出也**不含任何图片行**；
    - **空行不产出段落**（段落间距由界面统一控制，否则会叠加出双倍空白）；
      引用块里的空续行（单独一个 ``>``）同样不产出段落；
    - 链接渲染为可读文本 ``[文字](url)`` → ``文字``；不显示 URL；
    - 行内标记（``**粗**`` / ``*斜*`` / ``~~删~~`` / `` `码` ``）**只删标记、
      保留内容**，因为 Tk ``Text`` 与终端都不渲染 markdown；
    - 表格整块交给 :func:`_render_table`，标签只有两种：``table``（等宽对齐的网格，
      列间画竖线；放不下自然宽的列**自己折行**但保持列对齐）、``records``（极窄时的
      分条）。★ 两者之间只有**一个**切换阈值（:func:`_table_min_total`）。两种标签的
      内容都可能含换行，界面需按等宽 / 折行缩进分别配置。
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
            content, tag = _render_table(block, table_max_width)
            out.append((content, tag))
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


#: 折行时的「可断字符」：断点取在它**之后**（该字符留在上一行）。
#: 用途是让 ``com.qodercn.app.stable``、``~/.qoder-cn``、``a/b/c`` 这类
#: **没有空格的长串**在分隔符后断开，而不是被从单词中间劈成两半。
#: ★ 左括号类也必须收进来：``Qoder CN（JetBrains 插件）`` 里除了那个空格，唯一
#: 的自然断点就是 ``（`` 之后 —— 不收它，括号里那一段就成了「无断点的长串」，
#: ``JetBrains`` 会被硬断劈成 ``JetB`` / ``rains``（实测发生于 14 列的窄列）。
_WRAP_BREAK_AFTER = " \t/\\-_.+=,;:)]}>，。、；：！？）》」』】([{（《〈「『【〔"


def _last_break(s: str) -> int:
    """``s`` 里最后一个可断位置（返回「上一行结束处」的索引，0 表示没有）。"""
    best = 0
    for ch in set(_WRAP_BREAK_AFTER):
        idx = s.rfind(ch)
        if idx >= 0 and idx + 1 > best:
            best = idx + 1
    return best


def _wrap_disp(text: str, width: int) -> list[str]:
    """按**显示宽度**折行（东亚宽字符算 2 列）。

    断点优先取行内最后一个**可断字符之后**（空格 / 路径分隔符 / 括号 / 标点，
    见 :data:`_WRAP_BREAK_AFTER`），这样英文单词与路径不会被劈开；**只有整行
    一个可断点都没有**时才硬断——中文没有词间空格，长中文句只能逐字断。

    ★ 曾经多加过一条「断点不足半行就硬断」的判据，理由是「怕一行只落两三个字」。
    实测证明它帮了倒忙：``Qoder CN（JetBrains 插件）`` 在 14 列宽下唯一可用的
    断点 ``Qoder CN（`` 只占 10/14 列 ⇒ 判据拒绝 ⇒ 硬断 ⇒ ``JetBrains`` 被劈成
    ``JetB`` / ``rains``。断得早只是多占一行，劈开却是内容变形：两害相权取其轻。

    为什么表格要自己折行而不是交给控件：控件折行后**续行从最左边起排**，每一列的
    错位都对不上表头；自己折完再并排，列位置就仍然对齐（见 :func:`_layout_grid`）。
    """
    if width <= 0 or _disp_width(text) <= width:
        return [text]
    out: list[str] = []
    cur = ""
    cur_w = 0
    for ch in text:
        cw = 2 if unicodedata.east_asian_width(ch) in _WIDE_EAW else 1
        if cur_w + cw > width and cur:
            cut = _last_break(cur)
            if cut > 0:
                out.append(cur[:cut].rstrip())
                cur = cur[cut:].lstrip()
            else:
                out.append(cur.rstrip())
                cur = ""
            cur_w = _disp_width(cur)
        cur += ch
        cur_w += cw
    if cur.strip():
        out.append(cur.rstrip())
    return out or [""]


def _fit_widths(widths: list[int], avail: int) -> list[int]:
    """把自然列宽压进 ``avail`` 显示列（每列不低于 :data:`_TABLE_FIT_MIN_COL`）。

    做法是**反复削当前最宽的那一列**，直到总宽装得下或所有列都到底线下限为止。
    这比「一律等比缩放」好：长列（本仓库是备注 / 数据目录列）让得多，短列几乎不动，
    版式更接近自然宽度。循环次数 = 需要削掉的总列数（实测 169→117 也就 50 余轮），
    对 4 列小表完全无感。
    """
    n = len(widths)
    if not n:
        return []
    room = max(n * _TABLE_FIT_MIN_COL, avail - _TABLE_GAP * (n - 1))
    w = list(widths)
    for _ in range(sum(w) + 1):
        if sum(w) <= room:
            break
        i = max(range(n), key=lambda k: w[k])
        if w[i] <= _TABLE_FIT_MIN_COL:
            break
        w[i] -= 1
    return w


def _layout_grid(body: list[list[str]], widths: list[int], has_sep: bool) -> str:
    """把表格铺成等宽网格文本：单元格先按列宽折行，同一行的折行块**按行号并排**
    （整行高度取该行最高的那个单元格），所以列边界始终竖直对齐，不会出现
    「折行后列全错位」。

    列间用 :data:`_TABLE_SEP`（``" | "``）分隔 —— **这是网格形态唯一的视觉标识**。
    纯文本没有表格线，不画竖线时折出来的续行会与相邻列挤成一坨，整块被读成
    「一堆左对齐的文字」（用户实测就把它当成了「逐项列出」）；画上之后无论单元格
    折成几行，「这是一张表」都一眼可辨。

    单元格放得下就原样输出、放不下才折行（:func:`_wrap_disp`）⇒「不折行的网格」
    只是本函数在可用宽度足够时的自然结果，**不是另一条代码路径**，因此也不存在
    「表格 ↔ 折行 ↔ 分条」三档之间的两套阈值。
    """
    ncol = len(widths)

    def _join(parts: list[str]) -> str:
        return _TABLE_SEP.join(
            p + " " * max(0, widths[i] - _disp_width(p))
            for i, p in enumerate(parts)
        ).rstrip()

    lines: list[str] = []
    for ridx, row in enumerate(body):
        cells = [_wrap_disp(row[i], widths[i]) for i in range(ncol)]
        for k in range(max(len(c) for c in cells)):
            lines.append(_join([c[k] if k < len(c) else "" for c in cells]))
        if ridx == 0 and has_sep:
            lines.append(_join(["-" * w for w in widths]))
    return "\n".join(lines)


def _table_min_total(ncol: int) -> int:
    """表格形态所需的**最小可用宽度**——表格与分条之间**唯一**的切换阈值。

    ``max(_TABLE_MIN_WIDTH, ncol * _TABLE_FIT_MIN_COL + _TABLE_GAP * (ncol - 1))``

    两部分各有用处：按列数算的「每列至少 :data:`_TABLE_FIT_MIN_COL` 列内容 + 列间
    分隔」是**硬约束**（低于它 :func:`_fit_widths` 就压不下去、行会被撑得比可用宽度
    还宽，破坏 ``test_grid_never_exceeds_the_available_width``）；:data:`_TABLE_MIN_WIDTH`
    是**统一下限**，避免列数少的表（2 列天然只要 31 列）过早退成分条——否则同一个窗口
    宽度下一张表是表格、另一张是分条，看起来就是「阈值错开」。

    ★ 刻意只有一个阈值：变宽与变窄走的是同一个算式 ⇒ 同一宽度下形态唯一，不会出现
    「缩小时先分条、放大时要更宽才回到表格」（用户实测反馈）。
    """
    return max(_TABLE_MIN_WIDTH,
               ncol * _TABLE_FIT_MIN_COL + _TABLE_GAP * (ncol - 1))


def _render_table(rows: list[list[str]],
                  max_width: int | None = None) -> tuple[str, str]:
    """表格块 → ``(文本, 标签)``：**能铺成表格就铺成表格，只有极窄才分条**。

    只有两档（``max_width`` 为 ``None`` 时取 :data:`_TABLE_MAX_WIDTH`）：

    ====================================== ==================== ====================
    可用宽度                                形态                 标签
    ====================================== ==================== ====================
    ≥ :func:`_table_min_total`              网格（放不下自然宽的  ``table``
                                            列**自己折行**）
    低于它                                  分条                  ``records``
    ====================================== ==================== ====================

    为什么把原先「装得下自然宽 / 折行」两条判据合掉：

    - 两者**输出本来就完全等价**——可用宽度 ≥ 自然宽时 :func:`_fit_widths` 返回原
      列宽、:func:`_wrap_disp` 对每个单元格都原样返回，折行网格自然退化成不折行的
      网格；留着只是让阈值多一个、语义上像「三档」。
    - 多出来的那个阈值正是用户实测的痛点：「宽度变小变回逐项列出的阈值（64）远小于
      宽度变大后变回表格的阈值（169），应该一样」。合掉之后**表格 ↔ 分条只剩一个
      切换点**，两个方向一致。

    两档各自的理由：

    - **网格**丢掉 ``|---|`` 分隔行、按显示宽度对齐各列、列间画 :data:`_TABLE_SEP`
      竖线、表头下补一条等宽横线（保住「哪行是表头」）。真文档的表恰恰是「备注 /
      数据目录」列特别长（``docs/使用说明.md`` 实测自然宽 169 与 339 显示列），任何
      屏幕都装不下自然宽 ⇒ 主路径就是「自己折行 + 按行号并排」，列边界仍竖直对齐。
      **交给控件折行**才是灾难：续行从最左边起排，列与表头全错位。
    - **分条**（:func:`_reflow_table`）只留给极窄的情况——那时每列不足
      :data:`_TABLE_FIT_MIN_COL` 列，网格里的字会被切得七零八落，不如「标题 +
      ``列名：值``」那样把长句整段交给控件。

    各单元格在此**之前**已过 :func:`_plain_inline`（调用方负责），故判断宽度算的是
    最终要显示的文本。
    """
    limit = _TABLE_MAX_WIDTH if max_width is None else max(1, int(max_width))
    body = [r for r in rows if not _is_sep_row(r)]
    if not body:
        return "", "table"
    has_sep = len(body) != len(rows)

    ncol = max(len(r) for r in body)
    body = [r + [""] * (ncol - len(r)) for r in body]
    widths = [max(_disp_width(r[i]) for r in body) for i in range(ncol)]

    if limit >= _table_min_total(ncol):
        return _layout_grid(body, _fit_widths(widths, limit), has_sep), "table"
    return _reflow_table(body), "records"


def _reflow_table(body: list[list[str]]) -> str:
    """宽表 → 「分条」文本：首列当**条目名**，其余列按 ``列名：值`` 逐行列出。

    形如::

        Qoder CN（JetBrains 插件）
          显示名：Qoder CN
          数据目录：~/.qoder-cn

    两个决定及其理由：

    - **首列当标题**（而不是也写成 ``工具：xxx``）：表格的首列几乎总是这一行的「名字」，
      重复 9 遍列名纯属噪音；当标题既省行数又便于扫读。首列为空的行就不写标题行。
    - **值再长也不折成多行**：窄列硬折会把长句切成十几列宽的窄条，比不对齐更难读；
      折行交给控件（换行处由 tag 的缩进对齐）。

    条目之间空一行——纯文本没有表格线，空白是唯一的分组手段。值为空的列直接跳过，
    否则会留下一串 ``备注：`` 这样的空标签。
    """
    header, rows = body[0], body[1:]
    out: list[str] = []
    for r in rows:
        fields = ["  %s：%s" % (header[i], r[i]) if header[i] else "  " + r[i]
                  for i in range(1, len(header)) if r[i]]
        if not r[0] and not fields:
            continue
        if out:
            out.append("")
        if r[0]:
            out.append(r[0])
        out.extend(fields)
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


def plain_text(md: str, table_max_width: int | None = None) -> str:
    """markdown → 纯文本（供 CLI ``--docs`` 直接打印）。

    与 :func:`md_to_paragraphs` **同源**：段落序列一致，只是输出端换成终端——
    终端渲染能力与 Tk ``Text`` 一样都是「纯文本 + 缩进」，所以行内标记、表格、
    代码栅栏这几件事在两边口径统一，不会一个剥了标记、另一个没剥。

    差异只有两处**表现**（不是内容）：列表项保留 ``- `` 前缀、代码块按 4 空格缩进。

    ``table_max_width`` 透传给 :func:`md_to_paragraphs`；CLI 侧传终端列数，
    这样输出重定向到文件时也不会出现「按终端宽度折好的窄表」——
    终端宽度取不到时的兜底见 :func:`handle_docs_flag`。
    """
    lines: list[str] = []
    for content, tag in md_to_paragraphs(md, table_max_width):
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

    表格宽度取**终端列数**（与界面「实测正文区宽度」同一个口径）：打印到终端时
    表格就跟终端一样宽；输出被重定向到文件 / 管道（不是 tty）时取不到有意义的值，
    退回 :data:`_TABLE_MAX_WIDTH`，免得写进文件的表被 80 列硬折。
    """
    if "--docs" not in argv:
        return False
    from .resources import read_help_doc
    from .version import emit_console

    cols = None
    try:
        if sys.stdout is not None and sys.stdout.isatty():
            cols = shutil.get_terminal_size().columns
    except (AttributeError, OSError, ValueError):
        cols = None
    try:
        text = plain_text(read_help_doc(), cols)
    except OSError as exc:
        text = "（无法读取使用说明：%s）\n" % exc
    emit_console(text)
    return True
