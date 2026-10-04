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

    def __enter__(self) -> "ArchiveSource":
        return self

    def __exit__(self, *exc) -> bool:
        self.cleanup()
        return False


# --------------------------------------------------------------------------- #
# 解包
# --------------------------------------------------------------------------- #
def extract_session_root(zip_path: str, tool: str) -> ArchiveSource:
    """把 ``zip_path`` 里 ``tool`` 的会话子树按需解到临时目录。

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
        try:
            if prefix is not None:
                picked = _extract_subtree(zf, infos, prefix, tmp)
                root = _join_rel(tmp, prefix)
                reason = "path"
            else:
                picked = _extract_all(zf, infos, tmp)
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
                         reason=reason, extracted=picked, total=len(infos))


def _write_member(zf: zipfile.ZipFile, info: zipfile.ZipInfo, tmp: str,
                  rel: str) -> bool:
    """把一个条目写到 ``tmp`` 下（阻断 Zip Slip，长路径加前缀）。"""
    target = safe_target(os.path.realpath(tmp), rel)
    if target is None:
        return False
    parent = os.path.dirname(target)
    if parent:
        os.makedirs(_longpath(parent), exist_ok=True)
    with zf.open(info) as src, open(_longpath(target), "wb") as dst:
        shutil.copyfileobj(src, dst)
    return True


def _extract_subtree(zf: zipfile.ZipFile, infos: Sequence[zipfile.ZipInfo],
                     prefix: str, tmp: str) -> int:
    """只解 ``prefix`` 子树 + 同级伴随文件。"""
    inside = (prefix + "/") if prefix else ""
    parent = posixpath.dirname(prefix) if prefix else ""
    picked = 0
    for info in infos:
        rel = _norm(info.filename)
        keep = rel.startswith(inside) if inside else True
        if not keep and parent:
            # 伴随文件（如 codebuddy 的 session-workspaces.json）与会话根同级
            keep = (posixpath.dirname(rel) == parent
                    and posixpath.basename(rel) in COMPANION_NAMES)
        if not keep:
            continue
        if _write_member(zf, info, tmp, rel):
            picked += 1
    return picked


def _extract_all(zf: zipfile.ZipFile, infos: Sequence[zipfile.ZipInfo],
                 tmp: str) -> int:
    """回退路径：全量解包（有条数 / 字节双上限，防大包拖垮磁盘）。"""
    picked = 0
    total_bytes = 0
    for info in infos:
        if picked >= FALLBACK_MAX_ENTRIES or total_bytes > FALLBACK_MAX_BYTES:
            break
        if _write_member(zf, info, tmp, _norm(info.filename)):
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
