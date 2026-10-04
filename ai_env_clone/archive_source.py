"""把备份包当作「只读的假数据目录」：按需解出会话子树，复用既有导入链路。

为什么单独成模块
----------------
「从备份包导入」只需要一件事：把选中的 zip 变成 ``list_source_sessions(tool, root)``
能吃的**本地目录**。其余（会话列表 / 落点判定 / ``migrate_session`` 写出）全部复用
既有代码。故包内定位与解包下沉到本模块，**不依赖 tkinter**，便于单测。

两条关键约束（均来自实测，见 ``docs/local/从备份包导入方案.md`` §5）
------------------------------------------------------------------

1. **按路径段定位，不写死绝对前缀**：归档内路径是「相对各适配器 ``detect_root()``」的；
   多数工具根是 ``~``，而 ZCode 的根是 **盘根 ``C:\\``**，包内会多一层 ``Users/<用户名>/``。
   写死前缀的判定在 ZCode 上必然失效。
2. **按需解包**：CodeBuddy 的 history 包可能几百 MB，但一次导入只需会话子树 ⇒
   只解定位到的子树 + 关键伴随文件（如 ``session-workspaces.json``），不全量解包。
   解出的层级保留归档内相对路径，这样包内的工作区映射能被既有反查逻辑自动读到。
   更进一步，``light=True`` 时**连子树都只解「列表要读的那几个文件」**（每会话的
   ``index.json`` + 标题兜底的那条消息），正文留给 :meth:`ArchiveSource.materialize`
   在真正导入该会话时补解——写盘是解包的绝对瓶颈（Windows 上每个文件约 1 ms），
   少写文件才是真正的提速手段。

定位规则（工具 -> 「会话数据根」）
--------------------------------

============== ==================================================== ==================
工具           命中判据（按路径段）                                    返回的前缀
============== ==================================================== ==================
``codebuddy``  ``<…>/history/<wid>/<sid>/index.json``                 ``<…>/history``
``reasonix``   ``<…>/projects/<scope>/sessions/*-session.jsonl``      ``projects`` 的父目录
``workbuddy``  段名 ``.workbuddy`` 且其下含 ``projects/`` / ``workbuddy.db`` ``.workbuddy``
``dsh``        段名 ``.dsh`` 且其下含 ``sessions/`` / ``storages/``    ``.dsh``
``zcode``      段名 ``.zcode`` 且其下含 ``cli/db/db.sqlite``          ``.zcode``
============== ==================================================== ==================

定位不到时退回**有界深度搜索 + 试跑该工具的扫描函数**（以「能否扫出会话」为判据），
不新增任何额外的判定规则。
"""

from __future__ import annotations

import json
import os
import posixpath
import shutil
import tempfile
import time
import zipfile
from dataclasses import dataclass, field
from typing import Optional, Sequence

from ai_env_clone.core import safe_target

__all__ = [
    "ArchiveSourceError",
    "ArchiveSource",
    "TEMP_PREFIX",
    "LOCATABLE_TOOLS",
    "LIGHT_TOOLS",
    "locate_session_root",
    "extract_session_root",
    "cleanup_stale",
]

#: 临时解包目录前缀（``cleanup_stale`` 据此识别本模块的残留）。
TEMP_PREFIX = "ai_env_clone_arc_"
#: 残留临时目录的清理阈值（秒）：超过它才删，避免误删另一个实例正在用的目录。
STALE_AFTER_S = 6 * 3600
#: 回退路径（定位失败）时，全量解包的条目 / 字节上限（防大包拖垮磁盘）。
FALLBACK_MAX_ENTRIES = 4000
FALLBACK_MAX_BYTES = 512 * 1024 * 1024
#: 回退路径下探测候选目录的最大深度。
PROBE_MAX_DEPTH = 4

#: 本模块能定位出会话数据根的工具（= ``import_matrix`` 里可作来源的工具）。
LOCATABLE_TOOLS = frozenset({"reasonix", "codebuddy", "workbuddy", "zcode", "dsh"})

#: 支持「列表阶段只解索引、正文留到导入时补解」的工具。
#: 只有 CodeBuddy 成立：它的枚举靠 ``index.json``，消息正文是独立文件；
#: 其余工具的会话正文**就是**它的枚举依据（reasonix/workbuddy 的 ``.jsonl``、
#: zcode/dsh 的库文件），收窄会直接让列表空掉。
LIGHT_TOOLS = frozenset({"codebuddy"})

#: 与「会话数据根」同级的伴随文件：扫描 / 反查逻辑会读它，解包时一并取出。
#: （CodeBuddy 的 ``session-workspaces.json`` 落在 history 的上一层。）
COMPANION_NAMES = frozenset({"session-workspaces.json"})


class ArchiveSourceError(Exception):
    """包内定位 / 解包失败（坏包、空包、无该工具的会话数据）。"""


# --------------------------------------------------------------------------- #
# 路径工具
# --------------------------------------------------------------------------- #
def _longpath(path: str) -> str:
    """为 Windows 提供 ``\\\\?\\`` 长路径前缀；其他平台原样返回。

    CodeBuddy 的会话层级很深，解包到临时目录很容易越过 260 字符
    （否则 ``open`` 报 WinError 3）。
    """
    if os.name == "nt" and not path.startswith("\\\\?\\"):
        if os.path.isabs(path):
            return "\\\\?\\" + os.path.abspath(path)
    return path


def _norm(name: str) -> str:
    """归档条目名统一成正斜杠、去首尾斜杠。"""
    return name.replace("\\", "/").strip("/")


def _join_rel(base: str, rel: str) -> str:
    """把归档内相对目录 ``rel`` 拼到本地目录 ``base`` 下（按路径段）。"""
    if not rel:
        return base
    return os.path.join(base, *[p for p in rel.split("/") if p])


# --------------------------------------------------------------------------- #
# 包内定位（只读条目名，不解包）
# --------------------------------------------------------------------------- #
def locate_session_root(entries: Sequence[str], tool: str) -> Optional[str]:
    """在归档条目名里定位 ``tool`` 的**会话数据根**（归档内相对目录）。

    :return: 归档内 posix 相对目录（可为 ``""`` = 归档根）；定位不到返回 ``None``。
    """
    if not entries or tool not in LOCATABLE_TOOLS:
        return None
    names = [_norm(n) for n in entries if n and not n.endswith("/")]

    if tool == "codebuddy":
        # ``<…>/history/<wid>/<sid>/index.json`` → ``<…>/history``
        for n in names:
            parts = n.split("/")
            if len(parts) >= 5 and parts[-1] == "index.json" and parts[-4] == "history":
                return "/".join(parts[:-3])
        return None

    if tool == "reasonix":
        # ``<…>/projects/<scope>/sessions/<id>-session.jsonl`` → ``projects`` 的父目录
        for n in names:
            parts = n.split("/")
            if (len(parts) >= 4 and parts[-1].endswith("-session.jsonl")
                    and parts[-2] == "sessions" and parts[-4] == "projects"):
                return "/".join(parts[:-4])
        return None

    if tool == "workbuddy":
        return _find_dot_dir(names, ".workbuddy", ("projects", "workbuddy.db"))
    if tool == "dsh":
        return _find_dot_dir(names, ".dsh", ("sessions", "storages"))
    if tool == "zcode":
        return _find_dot_dir(names, ".zcode", ("cli/db/db.sqlite",))
    return None


def _find_dot_dir(names: Sequence[str], seg: str, marks: Sequence[str]) -> Optional[str]:
    """找出「某段名 == ``seg`` 且其下含任一 ``marks``」的**最短**前缀。

    取最短前缀是为了拿到**外层**的 ``.xxx`` 根（如 ZCode 的 ``Users/<u>/.zcode``
    里的 ``.zcode``，而不是它下面某个同名目录）。
    """
    ordered = sorted(names, key=lambda s: (s.count("/"), len(s)))
    for n in ordered:
        parts = n.split("/")
        if seg not in parts:
            continue
        i = len(parts) - 1 - parts[::-1].index(seg)
        rest = "/".join(parts[i + 1:])
        for mark in marks:
            if rest == mark or rest.startswith(mark + "/"):
                return "/".join(parts[:i + 1])
    return None


# --------------------------------------------------------------------------- #
# 解包结果
# --------------------------------------------------------------------------- #
@dataclass
class ArchiveSource:
    """一次「从包解出会话来源」的结果（含临时目录的清理职责）。"""

    tool: str
    root: str                 # 本地解包目录，可直接喂给 list_source_sessions
    temp_dir: str             # 临时根（cleanup 时整棵删除）
    prefix: str = ""          # 归档内定位到的会话根（"" = 归档根）
    reason: str = ""          # "path"（按路径段命中）/ "fallback"（有界搜索命中）
    extracted: int = 0        # 实际解出的条目数
    total: int = 0            # 包内文件条目总数（用于说明「未全量解包」）
    zip_path: str = ""        # 源包路径（``deferred`` 时补解要再开一次）
    deferred: bool = False    # 是否「列表阶段只解了索引，正文待补解」
    _cleaned: bool = field(default=False, repr=False)

    @property
    def fully_extracted(self) -> bool:
        return self.extracted >= self.total

    def cleanup(self) -> None:
        """删除临时目录（可重复调用）。"""
        if self._cleaned:
            return
        self._cleaned = True
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def materialize(self, local_dir: str) -> bool:
        """把 ``local_dir`` 对应的归档子树**补解**出来。

        ``deferred`` 模式下列表阶段只解了每会话的 ``index.json``（+ 标题兜底的那条
        消息），正文要等到真正导入时才需要。本方法按需把该会话目录整棵解出。

        :return: 是否真的补解了。``local_dir`` 不在本来源根下（例如用户其实在做
            「从数据目录导入」）或本来源不是延迟模式时返回 ``False``，不做任何事。
        """
        if not self.deferred or self._cleaned:
            return False
        base = os.path.realpath(self.root)
        try:
            target = os.path.realpath(local_dir)
            if not os.path.isdir(target) or target == base:
                return False
            if os.path.commonpath([base, target]) != base:
                return False
        except (OSError, ValueError):
            return False
        rel = os.path.relpath(target, base).replace(os.sep, "/")
        sub = posixpath.join(self.prefix, rel) if self.prefix else rel
        with zipfile.ZipFile(self.zip_path) as zf:
            infos = [i for i in zf.infolist() if not i.is_dir()]
            names = [_norm(i.filename) for i in infos]
            _extract_subtree(zf, infos, names, sub, os.path.realpath(self.temp_dir))
        return True

    def __enter__(self) -> "ArchiveSource":
        return self

    def __exit__(self, *exc) -> bool:
        self.cleanup()
        return False


# --------------------------------------------------------------------------- #
# 解包
# --------------------------------------------------------------------------- #
def extract_session_root(zip_path: str, tool: str,
                         light: bool = False) -> ArchiveSource:
    """把 ``zip_path`` 里 ``tool`` 的会话子树按需解到临时目录。

    :param light: ``True`` 时只解**列表阶段真正会读**的成员（见
        :func:`_light_names`），其余正文留给 :meth:`ArchiveSource.materialize` 在
        真正导入该会话时补解。只对 CodeBuddy 生效（其余工具的会话正文就是它的
        枚举依据，收窄会让列表直接空掉）。

    :raise ArchiveSourceError: 坏包 / 空包 / 定位不到会话数据 / 解包失败。
        （任何失败路径都会先删掉已建的临时目录，不留垃圾。）
    """
    if not os.path.isfile(zip_path):
        raise ArchiveSourceError("备份包不存在：%s" % zip_path)
    if not zipfile.is_zipfile(zip_path):
        raise ArchiveSourceError("不是有效的 zip 备份包：%s" % zip_path)

    with zipfile.ZipFile(zip_path) as zf:
        infos = [i for i in zf.infolist() if not i.is_dir()]
        if not infos:
            raise ArchiveSourceError("备份包里没有任何文件（空包）。")
        names = [_norm(i.filename) for i in infos]
        prefix = locate_session_root(names, tool)

        cleanup_stale()
        tmp = tempfile.mkdtemp(prefix=TEMP_PREFIX)
        # ``root_real`` 与「已建目录」在循环外算好：``realpath`` / ``makedirs`` 都是
        # 逐个成员的系统调用，放进循环会让解包慢上数倍（实测 6000 个成员 +5s）。
        root_real = os.path.realpath(tmp)
        deferred = False
        try:
            if prefix is not None:
                keep = None
                if light and tool in LIGHT_TOOLS:
                    keep = _light_names(zf, infos, names, prefix, tool)
                    deferred = True
                picked = _extract_subtree(zf, infos, names, prefix, root_real, keep)
                root = _join_rel(tmp, prefix)
                reason = "path"
            else:
                picked = _extract_all(zf, infos, names, root_real)
                root = _probe_root(tmp, tool)
                reason = "fallback"
                if root is None:
                    raise ArchiveSourceError(
                        "无法在包内定位「%s」的会话数据（可能不是该工具的会话备份）。" % tool)
        except ArchiveSourceError:
            shutil.rmtree(tmp, ignore_errors=True)
            raise
        except Exception as exc:  # noqa: BLE001 - 任何解包异常都收敛为可读错误
            shutil.rmtree(tmp, ignore_errors=True)
            raise ArchiveSourceError("解包失败：%s: %s" % (type(exc).__name__, exc)) from exc

    return ArchiveSource(tool=tool, root=root, temp_dir=tmp, prefix=prefix or "",
                         reason=reason, extracted=picked, total=len(infos),
                         zip_path=zip_path, deferred=deferred)


def _first_message_id(doc) -> str:
    """会话 ``index.json`` 里「标题兜底」会读的那条消息 id。

    **必须与** ``session_migration._codebuddy_title_from_first_message`` **取同一条**，
    否则列表阶段会漏解那条消息，标题退化为空（列表质量下降）。
    """
    if not isinstance(doc, dict):
        return ""
    msgs = doc.get("messages") or []
    for m in msgs:
        if isinstance(m, dict) and m.get("role") == "user":
            return str(m.get("id") or "")
    if msgs and isinstance(msgs[0], dict):
        return str(msgs[0].get("id") or "")
    return ""


def _index_has_title(doc) -> bool:
    """``index.json`` 自身是否已给出标题。

    **必须与** ``session_migration._scan_codebuddy`` **取同一判据**：只有它为假时
    那边才会去读正文兜底，也只有那时才需要解出那条消息。
    """
    if not isinstance(doc, dict):
        return False
    if doc.get("title") or doc.get("name"):
        return True
    convs = doc.get("conversations") or []
    return bool(convs and isinstance(convs[0], dict) and convs[0].get("name"))


def _light_names(zf: zipfile.ZipFile, infos: Sequence[zipfile.ZipInfo],
                 names: Sequence[str], prefix: str, tool: str) -> set:
    """列表阶段**真正会被读**的归档成员名集合（当前只有 CodeBuddy）。

    ``list_source_sessions`` 对 CodeBuddy 只读两样东西：

    - 每会话的 ``index.json``（标题 / 消息索引）；
    - ``index.json`` 没标题时，它指向的那**一条**消息（标题兜底）。

    其余 ``messages/*.json`` 是会话正文，导入时才用得上 ⇒ 列表阶段不解，
    写盘文件数从「每会话 1 + N 条消息」降到「每会话 1~2 个」。
    """
    if tool != "codebuddy":
        return set()
    inside = (prefix + "/") if prefix else ""
    wanted: set = set()
    for info, rel in zip(infos, names):
        if not rel.startswith(inside) or posixpath.basename(rel) != "index.json":
            continue
        wanted.add(rel)
        try:
            with zf.open(info) as fp:
                doc = json.loads(fp.read().decode("utf-8"))
        except (OSError, ValueError, UnicodeDecodeError):
            continue
        if _index_has_title(doc):
            continue  # 已有标题，不会走正文兜底，那条消息不用解
        mid = _first_message_id(doc)
        if mid:
            wanted.add(posixpath.join(posixpath.dirname(rel), "messages",
                                      "%s.json" % mid))
    return wanted


def _make_dirs(parent: str, made: set) -> None:
    """建目录（已建过的直接跳过）。

    ``os.makedirs(..., exist_ok=True)`` 每个成员都要一次 ``mkdir`` 系统调用（失败后
    吞掉异常），同一目录下成百上千个成员时纯属重复劳动；``made`` 记录本次已建目录。
    """
    if not parent or parent in made:
        return
    os.makedirs(_longpath(parent), exist_ok=True)
    made.add(parent)


def _write_member(zf: zipfile.ZipFile, info: zipfile.ZipInfo, root_real: str,
                  rel: str, made: set) -> bool:
    """把一个条目写到 ``root_real`` 下（阻断 Zip Slip，长路径加前缀）。

    ``root_real`` 必须是调用方**已 realpath 过**的临时根：本函数按成员被调用，
    自己再算一次 ``realpath`` 就等于每个文件多两次系统调用。
    """
    target = safe_target(root_real, rel)
    if target is None:
        return False
    _make_dirs(os.path.dirname(target), made)
    with zf.open(info) as src, open(_longpath(target), "wb") as dst:
        shutil.copyfileobj(src, dst)
    return True


def _extract_subtree(zf: zipfile.ZipFile, infos: Sequence[zipfile.ZipInfo],
                     names: Sequence[str], prefix: str, root_real: str,
                     keep: Optional[set] = None) -> int:
    """只解 ``prefix`` 子树 + 同级伴随文件。

    ``keep`` 非空时再收窄到该成员名集合（列表阶段的「只解索引」，见
    :func:`_light_names`）。**伴随文件不受收窄影响**——CodeBuddy 的工作区映射
    就靠它，缺了会让落点判定失效。
    """
    inside = (prefix + "/") if prefix else ""
    parent = posixpath.dirname(prefix) if prefix else ""
    picked = 0
    made: set = set()
    for info, rel in zip(infos, names):
        in_tree = rel.startswith(inside) if inside else True
        if in_tree:
            if keep is not None and rel not in keep:
                continue
        elif not (parent and posixpath.dirname(rel) == parent
                  and posixpath.basename(rel) in COMPANION_NAMES):
            continue
        if _write_member(zf, info, root_real, rel, made):
            picked += 1
    return picked


def _extract_all(zf: zipfile.ZipFile, infos: Sequence[zipfile.ZipInfo],
                 names: Sequence[str], root_real: str) -> int:
    """回退路径：全量解包（有条数 / 字节双上限，防大包拖垮磁盘）。"""
    picked = 0
    total_bytes = 0
    made: set = set()
    for info, rel in zip(infos, names):
        if picked >= FALLBACK_MAX_ENTRIES or total_bytes > FALLBACK_MAX_BYTES:
            break
        if _write_member(zf, info, root_real, rel, made):
            picked += 1
            total_bytes += info.file_size
    return picked


def _probe_root(tmp: str, tool: str) -> Optional[str]:
    """回退路径：对有界深度内的每个目录**试跑该工具的扫描函数**，取能扫出会话的那个。

    不新增判定规则——「能否扫出会话」就是判据本身。
    """
    from ai_env_clone import session_migration

    base_depth = tmp.rstrip("\\/").count(os.sep)
    for cur, dirs, _files in os.walk(tmp):
        if cur.rstrip("\\/").count(os.sep) - base_depth >= PROBE_MAX_DEPTH:
            dirs[:] = []
            continue
        try:
            if session_migration.list_source_sessions(tool, cur):
                return cur
        except Exception:  # noqa: BLE001 - 探测失败继续找下一个目录
            continue
    return None


def cleanup_stale(now: Optional[float] = None) -> int:
    """清理本模块遗留的临时目录（上次异常退出留下），返回删除个数。

    只删**超过 :data:`STALE_AFTER_S` 未改动**的目录，避免误删另一个实例
    正在使用的目录。
    """
    now = time.time() if now is None else now
    root = tempfile.gettempdir()
    removed = 0
    try:
        names = os.listdir(root)
    except OSError:
        return 0
    for name in names:
        if not name.startswith(TEMP_PREFIX):
            continue
        path = os.path.join(root, name)
        if not os.path.isdir(path):
            continue
        try:
            if now - os.path.getmtime(path) < STALE_AFTER_S:
                continue
        except OSError:
            continue
        shutil.rmtree(path, ignore_errors=True)
        removed += 1
    return removed
