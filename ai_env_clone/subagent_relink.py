"""把「旧版导入留在 DSH 里的顶层子代理会话」修复成 DSH 原生子代理会话。

背景（2026-10-05 实测）
----------------------
导入器早期版本把来源工具（ZCode）的**子代理会话**当成普通会话写进了 DSH：它们因此是
``delegationDepth = 0`` 的顶层会话、登记在 ``workspace.json``、在侧边栏与大模型用户会话
平级出现——但正文只是 AI 的干活过程（提问是父代理写的任务提示词）。新版导入器已经
按 DSH 原生子代理结构写出（裸 uuid 目录、header 带 ``parentSession`` /
``origin: "subagent"`` / ``delegationDepth: 1``、seq 0 为 ``subagent/descriptor``），
本模块负责**把已经导进去的旧数据就地改造**，用户不必删除再导一遍。

为什么需要来源数据（但**不需要用户指定**）
------------------------------------------
DSH 侧的旧产物里**没有**记录「源会话 id」，父子关系在导入那一刻就丢了
（实测核查过：想只靠 DSH 自身正文互证来判定父会话不可行——同一提示词会出现在所有
同父兄弟会话里，互证只能证明「同父」，证不出父是谁）。所以判定必须有来源数据，但
**来源是自动查找的**（:func:`discover_sources`）：本机 ZCode 会话数据，以及本工具备份
目录、上次用过的包目录里的 ``zcode_backup_*.zip`` / ``dsh_backup_*.zip``；
只有「包被挪走了 / 数据不在本机」时才需要显式指定（GUI 的输入框、CLI 的
``--relink-source``）。来源彻底找不到时**只上报、不猜**：这些会话保持原样。

能产生这类问题的来源只有两个（其余工具的子代理记录本工具从未导入过，见
:data:`SUBAGENT_SOURCE_TOOLS`）：ZCode 的 ``task_type = 'subagent_child'``、
DSH 的 ``origin: "subagent"`` 会话。判定靠来源侧的 ``parent_id`` + 「标题 + 首条用户
正文」指纹把源会话与 DSH 里的旧导入会话对上。

改造动作（每一步都可回退，不删除任何内容）
------------------------------------------
1. 按源会话重写出一份**原生子代理会话**（新裸 uuid 目录，``parentSession`` 指向父会话）；
2. 旧的顶层子代理会话目录整体移到 ``sessions/.removed/<工作区>/<旧 id>/``
   （``.`` 开头的目录不是 DSH 的会话目录，不会被加载）；
3. 从 ``workspace.json`` 的 ``sessionIds`` 摘掉旧 id（否则侧边栏仍把它当顶层会话）；
4. 旧 id 的投影缓存记录改名移走（``<id>.json.bak-<ts>``）；
5. **父会话日志不用改**：DSH 加载 v3 父会话走 v3→v4 迁移时会按「子会话证据完整 ⇒ 追加
   version-0 目录事实」自动补 ``subagent/catalog``（已用官方 ``session-format-catalog``
   校验链实测：父会话 50 条事件 + 2 条子会话证据 → 迁移结果 52 条事件、目录项 2）。
   只有**父会话已发布 v4**时才要动一下：把它那份 v4 改名移走，让它回落到 v3 重新迁移
   （迁移前会核对 v4 与 v3 的用户消息数一致，即用户在 DSH 里没继续聊过，否则跳过并上报）。
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import uuid
import zipfile
from dataclasses import dataclass, field
from typing import Callable, Optional

from . import archive_source
from .dsh_repair import (
    _longpath,
    _now_iso,
    _read_log_text,
    resolve_dsh_home,
    scan_sessions,
    zstd_backend,
)
from .session_migration import SessionParser, _dsh_write_text

__all__ = [
    "RelinkTarget",
    "RelinkPlan",
    "LEGACY_MARKER_PREFIX",
    "REQUIRED_GENERATION",
    "load_source_index",
    "plan_subagent_relink",
    "apply_subagent_relink",
]

#: 旧导入器写下的系统头消息 id 前缀（``imported-system-<会话 id>``）——判定「这条会话是
#: 本工具导入的」的唯一可靠指纹，且在 DSH 迁移到 v4 后依然保留。
LEGACY_MARKER_PREFIX = "imported-system-"

#: 本模块写出的会话代际（与导入器一致：写 v3，让 DSH 自己迁到当前代际）。
REQUIRED_GENERATION = "session.v3.jsonl.zstd"

#: 移走的旧会话目录落点（``.`` 开头的项目目录不是 DSH 会话目录，不会被扫描/加载）。
REMOVED_DIR = ".removed"


def _norm(text: str) -> str:
    """指纹归一：合并空白（换行/缩进差异不算差异）。"""
    return " ".join((text or "").split())


def _fingerprint(title: str, first_user: str) -> str:
    return _norm(title) + "\x00" + _norm(first_user)


def _stamp() -> str:
    return _now_iso().replace(":", "-").replace(".", "-")


# --------------------------------------------------------------------------- #
# 来源：ZCode / DSH 的会话索引（可自动查找本机数据与备份包）
# --------------------------------------------------------------------------- #
#: 可能产生「被误当顶层会话导入的子代理记录」的来源工具：
#:
#: - ``zcode``：子代理会话是 ``session`` 表里 ``task_type = 'subagent_child'`` 的行
#:   （旧导入器把它们当普通会话写了出去）；
#: - ``dsh``：子代理会话是**裸 uuid** 的会话目录（旧导入器把 ``_scan_dsh`` 列出的
#:   每一条都当普通会话写出去）。
#:
#: 其余来源（WorkBuddy / CodeBuddy / Reasonix）的子代理记录**本工具的导入器从未列过**
#: （WorkBuddy 在 ``<会话 id>/subagents/``、Reasonix 在 ``sessions/subagents/``，
#: CodeBuddy 没有可判定的子代理标记），所以它们不可能成为这类问题的来源，无需扫描。
SUBAGENT_SOURCE_TOOLS = ("zcode", "dsh")


@dataclass
class SourceSession:
    """来源侧一条会话（只留判定所需的最小事实）。

    ``id`` / ``parent_id`` 都带**来源前缀**（``"<来源 key>|<源 id>"``）：多个来源合并成
    一份索引时 id 可能撞车，父子关系必须限定在同一来源内解析。
    """

    id: str
    parent_id: str
    title: str
    first_user: str
    is_subagent: bool
    tool: str


@dataclass
class SourceRef:
    """一个候选来源：``live`` = 本机数据根，``package`` = 备份包（zip）。"""

    tool: str
    root: str
    label: str
    kind: str


def _source_dirs() -> list:
    """备份包可能所在的目录：本工具自己的备份目录 + 上次用过的包目录。

    两份都与 GUI 同源（``<启动目录>/backup/<工具>/`` 与
    ``<用户缓存>/ai_env_clone/import_sources.json`` 里记的目录），但这里不导入
    tkinter，故自行推导（tkinter 缺失时 ``dsh_repair`` 仍要能用）。
    """
    dirs: list = []
    try:
        if getattr(sys, "frozen", False):
            base = os.path.dirname(os.path.abspath(sys.executable))
        else:
            base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        # 源码模式跑出来的包在 <仓库根>/dist/backup；exe 模式在 <exe 同级>/backup。
        # 两种都列上，跨模式切换后仍能找到自己的包。
        dirs.extend([os.path.join(base, "backup"), os.path.join(base, "dist", "backup")])
    except Exception:  # noqa: BLE001 - 取不到就算了
        pass
    try:
        from .compress_estimate import cache_dir

        with open(os.path.join(cache_dir(), "import_sources.json"),
                  encoding="utf-8") as fh:
            saved = (json.load(fh) or {}).get("backup_dir") or ""
        if saved:
            dirs.append(saved)
    except (OSError, ValueError, TypeError):
        pass
    return [d for d in dirs if d]


def discover_sources(dsh_home: str, extra: str = "",
                     live_roots: "Optional[dict]" = None) -> list:
    """自动查找可用来源：本机数据根优先，其次备份包。

    :param extra: 用户显式指定的来源（数据根 / ``db.sqlite`` / 备份包 zip），首选。
    :param live_roots: 覆盖「各工具的实时数据根」（测试注入用）。
    :return: :class:`SourceRef` 列表（不含被修复的那个 DSH 主目录本身——拿自己当来源
        只会把每条会话匹配成「父会话」，掩盖真实来源）。
    """
    from .session_migration import default_source_root

    out: list = []
    seen: set = set()

    def add(ref: SourceRef) -> None:
        key = (ref.tool, os.path.normcase(os.path.abspath(ref.root)))
        if key in seen:
            return
        seen.add(key)
        out.append(ref)

    if extra:
        src = os.path.expanduser(extra)
        if os.path.isfile(src) and src.lower().endswith((".sqlite", ".db")):
            add(SourceRef("zcode", src, "指定的 ZCode 会话库", "live"))
        elif os.path.isfile(src) and zipfile.is_zipfile(src):
            tool = _package_tool(src)
            add(SourceRef(tool or "zcode", src, "指定的备份包", "package"))
        elif os.path.isdir(src):
            db = os.path.join(src, "cli", "db", "db.sqlite")
            if os.path.isfile(db):
                add(SourceRef("zcode", src, "指定的 ZCode 数据根", "live"))
            elif _is_dsh_home(src):
                add(SourceRef("dsh", src, "指定的 DSH 主目录", "live"))
        # 显式指定的来源**独占**：用户既然点名了，就别再去翻本机其它数据与备份包
        # （既符合意图，也避免为几个 G 的包白做解包）。
        if out:
            return out

    home_norm = os.path.normcase(os.path.abspath(dsh_home))
    roots = dict(live_roots or {})
    # 实时数据只自动接受 **ZCode**：它的子代理身份写在库里，一眼可判。
    # DSH 的实时数据**不自动采用**——本机通常只有「正在修的这个主目录」，把它自己当来源
    # 只会让每条会话匹配到自己的孪生（在那边是顶层会话）而被判成「父会话、无需改造」，
    # 反而掩盖真正的来源；多装的另一个 DSH 主目录属于少数场景，用 `--relink-source` 指定即可。
    # DSH 的**备份包**仍会自动采用（那是「从另一台机器导入」的典型形态）。
    for tool in ("zcode",):
        root = roots.get(tool) or default_source_root(tool)
        if not root or not os.path.isdir(root):
            continue
        if not os.path.isfile(os.path.join(root, "cli", "db", "db.sqlite")):
            continue
        add(SourceRef(tool, root, "本机 ZCode 数据", "live"))
    for tool, root in roots.items():
        if tool != "dsh" or not root or not os.path.isdir(root):
            continue
        if os.path.normcase(os.path.abspath(root)) == home_norm or not _is_dsh_home(root):
            continue
        add(SourceRef("dsh", root, "指定的 DSH 主目录", "live"))

    # 包目录的两种常见布局：直接放 zip，或 `<目录>/<工具名>/xxx.zip`（本工具的备份目录
    # 就是后者）——两级都看。
    search_dirs: list = []
    for directory in _source_dirs() + [
            os.path.dirname(os.path.abspath(extra)) if extra else ""]:
        if not directory or not os.path.isdir(directory):
            continue
        search_dirs.append(directory)
        try:
            for sub in sorted(os.listdir(directory)):
                subp = os.path.join(directory, sub)
                if os.path.isdir(subp):
                    search_dirs.append(subp)
        except OSError:
            continue
    for directory in search_dirs:
        try:
            names = sorted(os.listdir(directory))
        except OSError:
            continue
        for name in names:
            if not name.lower().endswith(".zip") or "_rollback_" in name.lower():
                continue        # 回滚快照是**本机**还原前的现场，不是来源数据
            path = os.path.join(directory, name)
            tool = _package_tool(path)
            if tool in SUBAGENT_SOURCE_TOOLS:
                add(SourceRef(tool, path, "找到的备份包 %s" % name, "package"))
    return out


def _is_dsh_home(path: str) -> bool:
    return os.path.isdir(os.path.join(path, "sessions"))


def _package_tool(path: str) -> str:
    """按文件名判包属于哪个工具（``<tool>_backup_...zip``）；判不出返回空串。"""
    name = os.path.basename(path).lower()
    for tool in ("zcode", "dsh"):
        if name.startswith(tool + "_"):
            return tool
    return ""


def _root_of(ref: SourceRef) -> "tuple[str, Callable[[], None], str]":
    """把一个来源解析成可扫描的根目录：``(根, 清理回调, 说明)``。"""
    if ref.kind == "live":
        return ref.root, (lambda: None), ""
    try:
        arc = archive_source.extract_session_root(ref.root, ref.tool, light=False)
    except Exception as exc:  # noqa: BLE001 - 坏包只提示，不抛
        return "", (lambda: None), "备份包无法解出会话：%s" % exc
    return arc.root, arc.cleanup, ""


def _scan_source_zcode(root: str) -> list:
    """扫 ZCode 库：父会话 + ``task_type = 'subagent_child'`` 的子代理会话。"""
    db = root if root.lower().endswith((".sqlite", ".db")) else \
        os.path.join(root, "cli", "db", "db.sqlite")
    if not os.path.isfile(db):
        return []
    try:
        con = sqlite3.connect("file:%s?mode=ro" % db.replace("?", "%3f"), uri=True)
        ids = [r[0] for r in con.execute("select id from session")]
        con.close()
    except sqlite3.Error:
        return []
    out: list = []
    for sid in ids:
        try:
            sess = SessionParser.parse_zcode(db, sid)
        except (OSError, sqlite3.Error, ValueError):
            continue
        out.append(SourceSession(
            id=sid, parent_id=sess.parent_source_id, title=sess.title,
            first_user=next((m.content for m in sess.messages if m.role == "user"), ""),
            is_subagent=bool(sess.is_subagent), tool="zcode"))
    return out


def _scan_source_dsh(root: str) -> list:
    """扫 DSH 主目录：``origin: "subagent"`` 的子会话 + ``parentSession`` 即父会话 id。"""
    from .dsh_repair import _read_log_head

    decompress, _compress, _name = zstd_backend()
    out: list = []
    sroot = os.path.join(root, "sessions")
    if not os.path.isdir(sroot):
        return out
    for wsdir in sorted(os.listdir(sroot)):
        wd = os.path.join(sroot, wsdir)
        if not os.path.isdir(wd):
            continue
        for name in sorted(os.listdir(wd)):
            sd = os.path.join(wd, name)
            if not os.path.isdir(sd):
                continue
            header = None
            for gen in ("session.v4", "session.v3", "session.v2", "session"):
                fp = os.path.join(sd, gen + ".jsonl.zstd")
                if os.path.isfile(fp):
                    header, _err = _read_session_header(fp, decompress)
                    break
            if not isinstance(header, dict):
                continue
            sid = str(header.get("id") or name)
            # ★ 只认 ``origin: "subagent"``：DSH 分叉按钮产生的用户分支会话也带
            # ``parentSession``（2026-10-07 实测），但它不是子代理。
            is_sub = header.get("origin") == "subagent"
            title, first_user = _source_titles(sd, decompress)
            out.append(SourceSession(
                id=sid, parent_id=header.get("parentSession") or "", title=title,
                first_user=first_user, is_subagent=is_sub, tool="dsh"))
    return out


def _source_titles(session_dir: str, decompress) -> "tuple[str, str]":
    """从会话目录里取 ``(标题, 首条用户正文)``（取代际最高者即可）。"""
    best, gen_best = None, -1
    for name in os.listdir(session_dir):
        if not name.endswith(".jsonl.zstd"):
            continue
        stem = name[: -len(".jsonl.zstd")]
        gen = int(stem[len("session.v"):]) if stem.startswith("session.v") and \
            stem[len("session.v"):].isdigit() else 0
        if gen > gen_best:
            best, gen_best = os.path.join(session_dir, name), gen
    if best is None:
        return "", ""
    try:
        text = _read_log_text(best, decompress)
    except Exception:  # noqa: BLE001
        return "", ""
    _legacy, title, first_user, _err = _read_facts_text(text)
    return title, first_user


def _read_session_header(path: str, decompress) -> "tuple[dict | None, str]":
    from .dsh_repair import _read_session_header as read_header

    return read_header(path, decompress)


def load_source_index(refs: "Sequence[SourceRef]",
                      needed_fps: "Optional[set]" = None) -> "tuple[dict, dict, list]":
    """读候选来源，返回 ``(指纹索引, id 索引, 说明列表)``。

    指纹 = 「标题 + 首条用户正文」归一后的组合；同一指纹可能对应多条源会话
    （同父兄弟的提示词完全一样），故值是**列表**。索引里的 id 带来源前缀，
    父子关系只在同一来源内解析（见 :class:`SourceSession`）。

    :param needed_fps: 待判定会话的指纹集合。逐个来源累积，**凑齐即停**——备份包解包很贵
        （ZCode 的库近百 MB），没必要把本机所有包都翻一遍。
    """
    by_fp: dict = {}
    by_id: dict = {}
    notes: list = []
    for ref in refs:
        root, cleanup, note = _root_of(ref)
        if not root:
            notes.append("%s：%s" % (ref.label, note or "无法读取"))
            continue
        try:
            rows = _scan_source_zcode(root) if ref.tool == "zcode" \
                else _scan_source_dsh(root)
        except Exception as exc:  # noqa: BLE001 - 单个来源坏掉不影响其它来源
            notes.append("%s：读取失败（%s）" % (ref.label, exc))
            continue
        finally:
            cleanup()
        if not rows:
            notes.append("%s：没有读到会话" % ref.label)
            continue
        prefix = "%s|%s|" % (ref.tool, ref.kind)
        for row in rows:
            key_id = prefix + row.id
            item = SourceSession(
                id=key_id,
                parent_id=(prefix + row.parent_id) if row.parent_id else "",
                title=row.title, first_user=row.first_user,
                is_subagent=row.is_subagent, tool=row.tool)
            by_id[key_id] = item
            by_fp.setdefault(_fingerprint(item.title, item.first_user), []).append(item)
        notes.append("%s：%d 条会话（其中子代理 %d 条）"
                     % (ref.label, len(rows), sum(1 for r in rows if r.is_subagent)))
        if needed_fps is not None and needed_fps and needed_fps <= set(by_fp):
            remaining = [r for r in refs[refs.index(ref) + 1:]]
            if remaining:
                notes.append("已凑齐待判定会话，跳过其余 %d 个来源" % len(remaining))
            break
    return by_fp, by_id, notes


# --------------------------------------------------------------------------- #
# DSH 侧：找「旧版导入留下的」会话
# --------------------------------------------------------------------------- #
@dataclass
class DshSessionFacts:
    """DSH 侧一条会话的判定事实（只读扫描产物）。"""

    session_id: str
    project_dir: str
    dir_path: str
    cwd: str
    created_ms: int
    title: str
    first_user: str
    legacy: bool                 # 是否本工具导入的（系统头消息 id 前缀）
    is_subagent: bool            # header 已声明是原生子代理会话（已改造过，别再改一次）
    published_v4: str            # 已发布的 v4 日志路径（空 = 无）
    user_count_v3: int
    user_count_v4: int
    count_error: str = ""


def _read_facts_text(text: str) -> "tuple[bool, str, str, str]":
    """从会话日志文本里取 ``(是否旧导入, 标题, 首条用户正文, 非法行说明)``。"""
    legacy = False
    title = ""
    first_user = ""
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        etype = ev.get("type")
        data = ev.get("data") or {}
        if etype == "system/message":
            mid = str(((data.get("message") or {}).get("id")) or "")
            if mid.startswith(LEGACY_MARKER_PREFIX):
                legacy = True
        elif etype == "session/title":
            title = title or (data.get("title") or "")
        elif etype == "user/message" and not first_user:
            first_user = "".join(c.get("text", "") for c in (data.get("content") or [])
                                 if isinstance(c, dict))
    return legacy, title, first_user, ""


def _count_user_messages(text: str) -> int:
    count = 0
    for line in text.split("\n"):
        line = line.strip()
        if line and '"user/message"' in line:
            try:
                if json.loads(line).get("type") == "user/message":
                    count += 1
            except ValueError:
                continue
    return count


def scan_dsh_sessions(dsh_home: str, decompress: Optional[Callable] = None) -> list:
    """扫描 DSH 会话，返回 :class:`DshSessionFacts` 列表（不写盘）。"""
    home = resolve_dsh_home(dsh_home)
    out: list = []
    for s in scan_sessions(home):
        if not s.log_file:
            continue
        try:
            text = _read_log_text(s.log_file, decompress)
        except Exception:  # noqa: BLE001 - 读不出来的会话直接跳过（另有健康检测负责上报）
            continue
        legacy, title, first_user, _ = _read_facts_text(text)
        if not legacy:
            continue
        created = 0
        header = s.header if isinstance(s.header, dict) else {}
        if isinstance(header.get("createdAt"), int):
            created = header["createdAt"]
        # 已经是原生子代理会话的（header 声明 ``origin: "subagent"``，或是裸 uuid 目录）
        # 不再改造——否则「修复」跑第二遍会把自己写出的新会话再当成旧导入会话。
        # ★ 用户分叉会话（parentSession + delegationDepth:0、无 origin）**不是**子代理，
        # 不在此列；它也不该成为改造目标（指纹来自 ZCode 子代理记录，用户分叉撞不上）。
        already = header.get("origin") == "subagent" \
            or not s.session_id.startswith("session-")
        v4 = os.path.join(s.path, "session.v4.jsonl.zstd")
        facts = DshSessionFacts(
            session_id=s.session_id, project_dir=s.project_dir, dir_path=s.path,
            cwd=(s.cwd or ""), created_ms=created, title=title, first_user=first_user,
            legacy=True, is_subagent=already,
            published_v4=v4 if os.path.isfile(_longpath(v4)) else "",
            user_count_v3=0, user_count_v4=0,
        )
        if facts.published_v4:
            v3 = os.path.join(s.path, REQUIRED_GENERATION)
            try:
                if os.path.isfile(_longpath(v3)):
                    facts.user_count_v3 = _count_user_messages(_read_log_text(v3, decompress))
                facts.user_count_v4 = _count_user_messages(
                    _read_log_text(facts.published_v4, decompress))
            except Exception as exc:  # noqa: BLE001
                facts.count_error = str(exc)
        out.append(facts)
    return out


# --------------------------------------------------------------------------- #
# 日志改造：把旧顶层会话日志原地改成原生子代理日志
# --------------------------------------------------------------------------- #
#: 日志内**同日志序号引用**的字段名（插入一条前置事件后必须同步 +1）。
#: 旧导入器写出的日志里实际只会出现 ``session/title.data.messageSeqs``，
#: 其余几个是官方事件族里可能出现的同类引用，一并处理以免漏改。
_SEQ_LIST_KEYS = ("messageSeqs", "sourceEventSeqs", "shadowedSeqs")
_SEQ_SCALAR_KEYS = ("startSeq", "endSeq", "sourceEventSeq")


def _bump_seq_refs(value, delta: int) -> object:
    """递归把日志内引用序号整体平移（只动已知字段，其它一概不碰）。"""
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if key in _SEQ_LIST_KEYS and isinstance(item, list):
                out[key] = [x + delta if isinstance(x, int) and not isinstance(x, bool)
                            else x for x in item]
            elif key in _SEQ_SCALAR_KEYS and isinstance(item, int) \
                    and not isinstance(item, bool):
                out[key] = item + delta
            else:
                out[key] = _bump_seq_refs(item, delta)
        return out
    if isinstance(value, list):
        return [_bump_seq_refs(x, delta) for x in value]
    return value


def convert_legacy_child_log(text: str, new_id: str, parent_id: str) -> str:
    """把一份**旧导入的顶层会话日志**改造成原生子代理日志（内容一字不改）。

    为什么不用「从来源重新导出一份」：同一父会话下的兄弟子代理提示词完全相同
    （实测 8 条「你是资深 C#/.NET 代码审查员…」同提示词），源会话与 DSH 会话之间
    无法一一对应；重新导出会把 A 的内容写到 B 头上。**原地改造**则只动结构、不动内容。

    改造内容：
    1. header：``id`` 换成新裸 uuid，追加 ``parentSession`` / ``origin: "subagent"`` /
       ``delegationDepth: 1``；
    2. 事件：在最前面插入 ``subagent/descriptor``（``version`` 必须为 3），其余事件
       **整体后移一位并重新编号**，同时把日志内的序号引用（``messageSeqs`` 等）+1。

    :raise ValueError: 日志不是本工具导入的形态（缺 header / 事件非法），拒绝改造。
    """
    lines = [l for l in text.split("\n") if l.strip()]
    if len(lines) < 2:
        raise ValueError("会话日志为空或缺少事件")
    try:
        header = json.loads(lines[0])
    except ValueError as exc:
        raise ValueError("会话 header 不是合法 JSON：%s" % exc) from exc
    if not isinstance(header, dict) or header.get("type") != "session":
        raise ValueError("会话日志首行不是 session header")

    events = []
    for line in lines[1:]:
        try:
            ev = json.loads(line)
        except ValueError as exc:
            raise ValueError("会话事件行不是合法 JSON：%s" % exc) from exc
        if not isinstance(ev, dict) or not ev.get("type"):
            raise ValueError("会话事件缺少 type")
        events.append(ev)

    created = header.get("createdAt")
    if not isinstance(created, int):
        created = events[0].get("time") if events else 0
    title = ""
    for ev in events:
        if ev.get("type") == "session/title":
            title = ((ev.get("data") or {}).get("title")) or ""
            break

    new_header = dict(header)
    new_header["id"] = new_id
    new_header["parentSession"] = parent_id
    new_header["origin"] = "subagent"
    new_header["delegationDepth"] = 1

    descriptor = {
        "type": "subagent/descriptor", "seq": 0, "time": created,
        "data": {"version": 3, "mode": "one-shot", "provider": "spawn",
                 "label": _label(title)},
    }
    out_events = [descriptor]
    for old_seq, ev in enumerate(events):
        new_ev = _bump_seq_refs(ev, 1)
        new_ev["seq"] = old_seq + 1
        out_events.append(new_ev)

    out = [json.dumps(new_header, ensure_ascii=False)]
    out.extend(json.dumps(ev, ensure_ascii=False) for ev in out_events)
    return "\n".join(out) + "\n"


def _label(title: str) -> str:
    """子代理 descriptor 的 label（官方要求非空）。"""
    text = " ".join((title or "").split()).strip()
    return (text or "导入的子代理会话")[:120]


# --------------------------------------------------------------------------- #
# 计划
# --------------------------------------------------------------------------- #
@dataclass
class RelinkTarget:
    """一条「旧顶层子代理会话 → 原生子代理会话」的改造计划。"""

    child_id: str                # 旧会话 id（session-<uuid>）
    child_dir: str               # 旧会话目录绝对路径
    project_dir: str             # 旧会话所属工作区目录名
    new_id: str                  # 新会话 id（裸 uuid，与产品一致）
    parent_id: str               # 父会话（DSH 侧 id）
    parent_project_dir: str      # 父会话所属工作区目录名
    source_session_id: str       # 源会话 id（判定证据，取自 ZCode 库）
    source_parent_id: str        # 源侧父会话 id
    title: str
    cwd: str
    created_ms: int
    parent_published_v4: str = ""       # 父会话已发布的 v4（需移走，空 = 无需处理）
    parent_v4_reason: str = ""          # 移走原因 / 拒绝原因


@dataclass
class RelinkPlan:
    """子代理关系修复计划（dry-run 产物）。"""

    targets: list = field(default_factory=list)          # List[RelinkTarget]
    skipped: list = field(default_factory=list)          # List[(会话 id, 原因)]
    source_note: str = ""
    zstd_note: str = ""
    #: 用户给的来源（目录 / db.sqlite / 备份包 zip）。执行期按它**重新解析**，因为
    #: 备份包在计划阶段解出的临时目录用完即删（不能把临时路径留进计划里）。
    source: str = ""
    #: 是否真的从来源里读到了会话：决定来源说明的措辞——读到了只是「判定依据」，
    #: 没读到才是「无法确定来源」（措辞按来源是否可用，而不是按有没有改造目标）。
    source_ok: bool = False

    @property
    def empty(self) -> bool:
        return not self.targets and not self.skipped and not self.source_note \
            and not self.zstd_note

    def describe(self) -> list:
        lines = []
        if self.zstd_note:
            lines.append(self.zstd_note)
        if self.source_note:
            lines.append("%s：%s" % (
                "外部导入会话的来源（判定父子关系）" if self.source_ok
                else "未能确定外部导入会话的来源", self.source_note))
        for t in self.targets:
            lines.append(
                "把旧导入的子代理会话 %s 改造为原生子代理会话 %s（挂到父会话 %s），"
                "旧目录移到 sessions/%s/%s/，并从工作区列表摘除"
                % (t.child_id, t.new_id, t.parent_id, REMOVED_DIR, t.child_id))
            if t.parent_published_v4:
                lines.append(
                    "　└ 父会话 %s 已发布 v4（%s），把它改名移走以便 DSH 重新迁移并补上 "
                    "subagent/catalog" % (t.parent_id, t.parent_v4_reason or "无新增用户轮次"))
        for sid, reason in self.skipped:
            lines.append("跳过 %s：%s" % (sid, reason))
        return lines


def plan_subagent_relink(dsh_home: str, source: str = "",
                         live_roots: "Optional[dict]" = None) -> RelinkPlan:
    """生成「子代理关系修复」计划（只读，不写盘）。

    来源**自动查找**（见 :func:`discover_sources`）：先看本机 ZCode / DSH 的会话数据，
    再看本工具备份目录与上次用过的目录里有没有 ``zcode_backup_*.zip`` /
    ``dsh_backup_*.zip``；``source`` 只是可选的显式覆盖（指定目录 / db / 包）。

    判定链：来源侧的 ``parent_id``（ZCode 的 ``sess_subagent_*`` 行 / DSH 的
    ``origin: "subagent"`` 会话）→ 内容指纹匹配 DSH 里的旧导入会话 → 父会话也必须在
    DSH 里找得到（否则挂不上，如实跳过）。任一步不满足都进 ``skipped`` 并写明原因；
    **来源找不到时只上报，不会猜**（拿不准的会话一律不动）。
    """
    home = resolve_dsh_home(dsh_home)
    decompress, _compress, zstd_name = zstd_backend()
    plan = RelinkPlan(source=source)
    if not decompress:
        plan.zstd_note = "缺少 zstd 解压支持（%s）：无法读取会话日志，跳过子代理关系修复" \
            % (zstd_name or "未安装")
        return plan

    facts = scan_dsh_sessions(home, decompress=decompress)
    candidates = [f for f in facts if not f.is_subagent]
    relink_source = source
    if not relink_source and not candidates:
        # 没有可判定的旧导入会话：不必去找来源（省掉解包大包的代价）
        return plan
    refs = discover_sources(home, extra=source, live_roots=live_roots)
    if not refs:
        plan.source_note = ("没有找到这些外部导入会话的来源（本机数据与本工具的备份包都没找到）："
                            "无法判定父子关系，这些会话保持原样不动。如需要修复，请按它们原本"
                            "来自哪个工具，选该工具的数据目录或备份包")
        return plan
    by_fp, by_id, notes = load_source_index(
        refs, needed_fps={_fingerprint(f.title, f.first_user) for f in candidates})
    plan.source_note = "；".join(notes)
    plan.source_ok = bool(by_fp)
    if not by_fp:
        plan.source_note = ("未取到可用于判定的来源会话（%s）：**无法判定**父子关系，"
                            "这些会话保持原样不动" % (plan.source_note or "没有任何来源"))
        return plan

    # DSH 侧指纹索引：判定父会话时也要用（父会话可能不是本工具导入的）
    dsh_by_fp: dict = {}
    for f in facts:
        dsh_by_fp.setdefault(_fingerprint(f.title, f.first_user), []).append(f)

    # 源会话 id -> 对应的 DSH 旧导入会话（用于把 parent_id 落到 DSH 的会话上）
    source_to_dsh: dict = {}
    for fp, group in dsh_by_fp.items():
        src = by_fp.get(fp)
        if not src or len(src) != len(group):
            # 数量对不上时不硬配（宁可少改，也不要配错父子）
            continue
        for s, d in zip(sorted(src, key=lambda x: x.id),
                        sorted(group, key=lambda x: x.session_id)):
            source_to_dsh[s.id] = d

    for f in candidates:
        fp = _fingerprint(f.title, f.first_user)
        cands = by_fp.get(fp) or []
        if not cands:
            plan.skipped.append((f.session_id, "在来源里找不到同指纹（标题+首条用户正文）的会话"))
            continue
        parents = {c.parent_id for c in cands}
        if len(parents) > 1:
            plan.skipped.append(
                (f.session_id, "同指纹源会话分属不同父会话（%s），无法判定" % "、".join(sorted(parents))))
            continue
        src = cands[0]
        if not src.parent_id:
            plan.skipped.append((f.session_id, "源会话本身是父会话（用户会话），无需改造"))
            continue
        parent_dsh = source_to_dsh.get(src.parent_id)
        if parent_dsh is None:
            parent_src = by_id.get(src.parent_id)
            same = dsh_by_fp.get(_fingerprint(
                parent_src.title if parent_src else "",
                parent_src.first_user if parent_src else ""))
            parent_dsh = same[0] if same else None
        if parent_dsh is None:
            plan.skipped.append(
                (f.session_id, "父会话（源 %s）在本机 DSH 里找不到，无法挂接" % src.parent_id))
            continue
        if parent_dsh.session_id == f.session_id:
            plan.skipped.append((f.session_id, "父会话定位到自己，跳过"))
            continue
        target = RelinkTarget(
            child_id=f.session_id, child_dir=f.dir_path, project_dir=f.project_dir,
            new_id=str(uuid.uuid4()), parent_id=parent_dsh.session_id,
            parent_project_dir=parent_dsh.project_dir,
            source_session_id=src.id, source_parent_id=src.parent_id,
            title=src.title, cwd=f.cwd or parent_dsh.cwd, created_ms=f.created_ms,
        )
        if parent_dsh.published_v4:
            if parent_dsh.count_error:
                target.parent_v4_reason = "读取计数失败：%s" % parent_dsh.count_error
            elif parent_dsh.user_count_v4 <= parent_dsh.user_count_v3:
                target.parent_published_v4 = parent_dsh.published_v4
                target.parent_v4_reason = (
                    "v3/v4 用户消息数均为 %d，判定 DSH 里没有新增对话"
                    % parent_dsh.user_count_v4)
        plan.targets.append(target)
    return plan


# --------------------------------------------------------------------------- #
# 执行
# --------------------------------------------------------------------------- #
def _detach_session_from_index(dsh_home: str, session_id: str, backup: bool = True) -> dict:
    """从 ``workspace.json`` 的 ``sessionIds`` 摘掉会话 id（可回退，只动这一处）。"""
    path = os.path.join(dsh_home, "storages", "workspace.json")
    if not os.path.isfile(path):
        return {"ok": False, "error": "workspace.json 不存在", "removed": 0, "backup": ""}
    try:
        with open(_longpath(path), "r", encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError) as exc:
        return {"ok": False, "error": str(exc), "removed": 0, "backup": ""}
    removed = 0
    workspaces = ((doc.get("tables") or {}).get("workspaces") or {})
    for record in workspaces.values():
        ids = record.get("sessionIds") if isinstance(record, dict) else None
        if isinstance(ids, list) and session_id in ids:
            record["sessionIds"] = [x for x in ids if x != session_id]
            removed += 1
    if not removed:
        return {"ok": True, "error": "", "removed": 0, "backup": ""}
    backup_path = ""
    try:
        if backup:
            backup_path = "%s.bak.%s" % (path, _stamp())
            shutil.copy2(_longpath(path), _longpath(backup_path))
        with open(_longpath(path), "w", encoding="utf-8") as fh:
            json.dump(doc, fh, ensure_ascii=False, indent=2)
    except OSError as exc:
        return {"ok": False, "error": str(exc), "removed": 0, "backup": backup_path}
    return {"ok": True, "error": "", "removed": removed, "backup": backup_path}


def _move_aside(path: str, backup: bool = True) -> dict:
    """把一个文件/目录改名移走（``<名字>.bak-<ts>``）；``backup=False`` 时直接返回。"""
    if not os.path.exists(_longpath(path)):
        return {"ok": True, "moved": "", "error": ""}
    if not backup:
        return {"ok": True, "moved": "", "error": ""}
    dest = "%s.bak-%s" % (path, _stamp())
    try:
        os.replace(_longpath(path), _longpath(dest))
    except OSError as exc:
        return {"ok": False, "moved": "", "error": str(exc)}
    return {"ok": True, "moved": dest, "error": ""}


def apply_subagent_relink(dsh_home: str, plan: RelinkPlan, backup: bool = True) -> dict:
    """执行改造计划（每条失败只记结果，不中断其它条目）。

    :return: ``{"targets": [每条结果], "parents": [父会话 v4 处理结果]}``。
    """
    home = resolve_dsh_home(dsh_home)
    decompress, compress, _zname = zstd_backend()
    if not decompress or not compress:
        return {"targets": [], "parents": [],
                "error": "缺少 zstd 后端，无法改写会话日志"}
    sessions_root = os.path.join(home, "sessions")
    projcache = os.path.join(home, "storages", "session_projcache", "sessions")
    results: list = []
    parents: dict = {}
    for t in plan.targets:
        item = {"child_id": t.child_id, "new_id": t.new_id, "parent_id": t.parent_id,
                "ok": False, "steps": [], "error": ""}
        try:
            old_log = os.path.join(t.child_dir, REQUIRED_GENERATION)
            if not os.path.isfile(_longpath(old_log)):
                raise ValueError("找不到原始 v3 日志（%s），无法安全改造" % REQUIRED_GENERATION)
            text = _read_log_text(old_log, decompress)
            new_text = convert_legacy_child_log(text, t.new_id, t.parent_id)
            new_log = os.path.join(sessions_root, t.project_dir, t.new_id,
                                   REQUIRED_GENERATION)
            _dsh_write_text(new_log, new_text)
            item["steps"].append(("写出原生子代理会话", new_log))

            removed_dir = os.path.join(sessions_root, REMOVED_DIR, t.project_dir,
                                       t.child_id)
            os.makedirs(os.path.dirname(_longpath(removed_dir)), exist_ok=True)
            if os.path.exists(_longpath(removed_dir)):
                removed_dir = "%s.bak-%s" % (removed_dir, _stamp())
            if backup:
                os.replace(_longpath(t.child_dir), _longpath(removed_dir))
                item["steps"].append(("移走旧顶层会话目录", removed_dir))

            detach = _detach_session_from_index(home, t.child_id, backup=backup)
            item["steps"].append(("从工作区列表摘除", detach))
            if not detach.get("ok"):
                item["error"] = detach.get("error") or ""

            moved = _move_aside(os.path.join(projcache, t.child_id + ".json"),
                                backup=backup)
            if moved.get("moved"):
                item["steps"].append(("移走旧投影缓存", moved["moved"]))

            if t.parent_published_v4 and t.parent_id not in parents:
                pm = _move_aside(t.parent_published_v4, backup=backup)
                pc = _move_aside(os.path.join(projcache, t.parent_id + ".json"),
                                 backup=backup)
                parents[t.parent_id] = {"parent_id": t.parent_id, "v4": pm,
                                        "projcache": pc}
            item["ok"] = not item["error"]
        except Exception as exc:  # noqa: BLE001 - 单条失败不影响其它条目
            item["error"] = "%s: %s" % (type(exc).__name__, exc)
        results.append(item)
    return {"targets": results, "parents": list(parents.values()), "error": ""}
