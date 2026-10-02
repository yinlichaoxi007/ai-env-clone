"""
导入会话的「目标工作区」自动判定。

背景
----

把来源软件的会话写出为目标软件的原生会话时，**必须决定它归到哪个工作区**：

- **CodeBuddy CN**：会话按 ``history/<workspaceId>/`` 索引，而 workspaceId 由
  项目路径派生（实测 ``md5(路径小写、反斜杠)``，见 :func:`codebuddy_workspace_id`）；
- **WorkBuddy**：会话按 ``projects/<工作区编码>/`` 存放，编码由工作区路径派生
  （见 :func:`~ai_env_clone.session_migration.workbuddy_project_slug`）；
- **DeepSeek Harness**：会话按 ``sessions/<工作区编码>/`` 存放，并在
  ``storages/workspace.json`` 登记；
- **Reasonix**：会话按 ``projects/<scope>/sessions/`` 存放，scope 即「项目名」。

旧版界面把这一步完全丢给用户填，既啰嗦又容易填错（填错就等于会话「写了但看不到」）。
本模块把判定收敛成一条规则：

1. **有明确工作区** -> 用足源会话自带的工作区（尽量路径同源，一次做对）；
   若源会话的工作区是「路径派生出的 id」（CodeBuddy 的 ``workspaceId`` 是 md5），
   先尽力**还原成路径**（:func:`codebuddy_recover_workspace_path`，只有哈希校验对得上才认）；
2. **没有工作区 / 还原不出来** -> 用该目标工具「无工作区会话」的默认落点
   （见 :func:`default_workspace`），并把成因（+正文里出现过的候选路径）写进
   ``WorkspacePlan.note`` / ``hints``，由界面如实交代——**绝不猜路径**；
3. **用户手动指定** -> 全部会话都落到这一个工作区（**包括自带工作区的会话**），
   由界面负责给出明确警示。

工作区不存在时**不静默创建**：由界面把「是否创建」交给用户确认
（:func:`ensure` 只负责在被允许后真正建目录）。跨机还原后本机没有那个工程目录时，
走的正是这条确认（界面会写明这个落点是「源会话还原」还是「工具默认」）。
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from datetime import datetime

# --------------------------------------------------------------------------- #
# 模式常量
# --------------------------------------------------------------------------- #
MODE_SESSION = "session"   #: 用源会话自带的工作区
MODE_DEFAULT = "default"   #: 用目标工具「无工作区会话」的默认落点
MODE_MANUAL = "manual"     #: 用户手动指定（覆盖全部会话）

MODE_LABELS = {
    MODE_SESSION: "自动 · 沿用会话自带工作区",
    MODE_DEFAULT: "自动 · 工具默认（无工作区会话）",
    MODE_MANUAL: "手动指定",
}

#: 工作区「标识形态」——决定该值是不是文件系统路径。
KIND_PATH = "path"     #: 工作区标识就是路径本身（WorkBuddy / DSH）
KIND_ID = "id"         #: 工作区标识是路径派生出的 id（CodeBuddy）
KIND_SCOPE = "scope"   #: 工作区标识是项目名（Reasonix）

#: CodeBuddy 没有「无工作区」这一原生概念（它的会话总是挂在某个项目下），
#: 因此为「来源没有任何工作区信息」的会话固定一个导入工作区，
#: 使同一批导入始终归到同一处，而不是每次随机一个新 id。
CODEBUDDY_FALLBACK_NAME = "imported-sessions"


# --------------------------------------------------------------------------- #
# 计划
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class WorkspacePlan:
    """一次导入中「某个会话 -> 目标工作区」的判定结果。"""

    tool: str          #: 目标工具
    mode: str          #: MODE_SESSION / MODE_DEFAULT / MODE_MANUAL
    value: str         #: 传给 migrate_session 的工作区标识
    path: str          #: 该标识对应的文件系统路径（``KIND_SCOPE`` 为项目目录）
    exists: bool       #: 目标机上是否已存在
    origin: str        #: 来源的人类可读说明（哪个会话带来的）
    note: str = ""     #: 落点成因的补充说明（必须展示给用户，不能只给值）
    hints: tuple = ()  #: 还原不出工作区时，正文里出现过的候选路径（仅提示，不自动采用）

    @property
    def mode_label(self) -> str:
        return MODE_LABELS.get(self.mode, self.mode)

    @property
    def is_manual(self) -> bool:
        return self.mode == MODE_MANUAL

    @property
    def is_guessed(self) -> bool:
        """落点是否「由源会话还原而来」（含哈希校验一致的还原）。"""
        return self.mode == MODE_SESSION and bool(self.note)


# --------------------------------------------------------------------------- #
# 派生规则（实测反推）
# --------------------------------------------------------------------------- #
def codebuddy_workspace_id(path: str) -> str:
    """由项目路径派生 CodeBuddy ``workspaceId``。

    实测：``md5("d:\\\\project\\\\ai-env-clone") == 059d5d31ffef54c1aab84e8c6edc485b``，
    与 ``history/`` 下真实目录名一致（本机样本 1/1 命中；其余 workspaceId
    对应的工作区当前不在本机磁盘上，无法回归）。

    规则：路径**转小写**、分隔符统一为 ``\\``，取 ``md5`` 十六进制。
    """
    p = (path or "").strip()
    if not p:
        return ""
    p = p.replace("/", "\\").lower()
    return hashlib.md5(p.encode("utf-8", "replace")).hexdigest()


#: 「备份包携带的会话工作区映射」按会话根缓存（一次导入会问很多次）。
_BACKUP_MAP_CACHE: dict = {}


def _load_backup_map(session_root: str) -> dict:
    """带进程内缓存的映射读取（空结果也缓存，避免反复 stat）。"""
    if not session_root:
        return {}
    if session_root in _BACKUP_MAP_CACHE:
        return _BACKUP_MAP_CACHE[session_root]
    try:
        from .adapters.codebuddy import load_session_workspace_map
        data = load_session_workspace_map(session_root)
    except Exception:  # noqa: BLE001 - 读映射属尽力而为
        data = {}
    if not isinstance(data, dict):
        data = {}
    _BACKUP_MAP_CACHE[session_root] = data
    return data


def session_root_of(session_dir: str) -> str:
    """由 ``<会话根>/history/<wid>/<sid>`` 反推会话根（映射文件所在的那一层）。"""
    if not session_dir:
        return ""
    d = os.path.abspath(session_dir).rstrip("\\/")
    for _ in range(3):
        d = os.path.dirname(d)
    return d


def codebuddy_backup_workspace_map(session_root: "str | None" = None) -> dict:
    """读取**备份包携带的**会话工作区映射（``{"workspaces": {wid: {...}}}``）。

    该文件由导出侧生成
    （:func:`ai_env_clone.adapters.codebuddy.build_session_workspace_map`），落在
    会话根下、**随备份包一起走**，所以跨机还原后依然可用 —— 不依赖源机器的 IDE
    「已打开文件夹」记录是否还在（那份记录躺在应用数据里，本机实测只覆盖 29% 的工作区，
    且**必须先还原、后导入**才生效，顺序错了就等于没有）。

    读不到（未导出过 / 未还原 / 文件损坏）时返回 ``{}``。调用方给的 ``session_root``
    可能只是 ``history`` 根或别的层，故读空时会**回退到默认会话根**再试一次。
    """
    data = _load_backup_map(session_root) if session_root else {}
    if data:
        return data
    try:
        from .adapters.codebuddy import detect_current_uid, detect_session_root
        alt = detect_session_root(detect_current_uid())
    except Exception:  # noqa: BLE001 - 探测属尽力而为
        alt = ""
    if alt and alt != session_root:
        return _load_backup_map(alt)
    return data


def codebuddy_workspace_candidates(wid: str,
                                   session_root: "str | None" = None) -> list:
    """映射里为该工作区记下的**正文候选路径**。

    这些只是**线索**（导出侧同样没敢自动采用），用于在推不出路径时给用户一个提示，
    可以一键填进「手动指定工作区」。
    """
    entry = (codebuddy_backup_workspace_map(session_root).get("workspaces")
             or {}).get((wid or "").strip())
    if isinstance(entry, dict):
        return [p for p in (entry.get("candidates") or []) if p]
    return []


def codebuddy_workspace_path_index(paths: "Sequence[str] | None" = None) -> dict:
    """``workspaceId -> 项目路径`` 反查表（把哈希还原成路径的唯一**结构化**来源）。

    CodeBuddy 的 ``workspaceId`` 是 ``md5(项目路径)``，**单向不可逆**；而会话的
    ``index.json`` 里也没有结构化的工作区字段（只有 ``messages`` / ``requests``）。
    因此当「来源会话自带工作区、但该工作区是 CodeBuddy 的 id 形态」时，必须先把它
    反查成路径，才能作为目标工具（WorkBuddy / DSH 都要**路径**）的落点 —— 否则只能
    退化成工具默认落点，会话就会跑到 ``~/WorkBuddy/<时间戳>`` 这类 playground 里，
    而不是它原本的工程工作区。

    反查来源是 **IDE 自己记下的「已打开文件夹」**（见
    :func:`ai_env_clone.adapters.codebuddy.iter_opened_workspace_paths`）：把每个候选路径按
    同一规则求 md5，能与 ``history/`` 目录名对上的即为该工作区（本机 1/1 命中）。

    ⚠ **只覆盖本机打开过的工程**。另一台机器备份过来的数据在还原后若没在本机打开过，
    这里就是空的 —— 那种情况走 :func:`codebuddy_recover_workspace_path`
    （从会话正文里挖候选 + 哈希校验）。

    :param paths: 候选项目路径；``None`` 时自动从 CodeBuddy IDE 记录里取。
                  探测失败（未安装 / 无记录）时返回空表，不影响其它落点判定。
    """
    if paths is None:
        try:
            from .adapters.codebuddy import iter_opened_workspace_paths
            paths = iter_opened_workspace_paths()
        except Exception:  # noqa: BLE001 - 探测属尽力而为，失败不拖垮导入
            paths = []
    index: dict = {}
    for p in paths or []:
        wid = codebuddy_workspace_id(p)
        if wid and wid not in index:
            index[wid] = p
    return index


#: 会话正文挖掘结果缓存：``session_dir -> [(候选路径, 出现次数), ...]``。
#: 预览会在每次选择变化时重算，挖掘要读文件，故做进程内记忆（同一会话只挖一次）。
_MINE_CACHE: dict = {}


def codebuddy_session_path_hints(session_dir: str, limit: int = 3) -> list:
    """会话正文里出现过的「可能的工程根」候选（带次数），供界面提示。

    只是**线索**：正文里的路径既有工程内的也有工程外的，本机实测 7 个有线索的工作区里
    有 3 个用启发式推错（过深 / 指到 IDE playground / 把换行 ``d:\\n`` 当路径），
    所以这些候选**绝不自动采用**，只摆给人看。
    """
    if not session_dir:
        return []
    key = (session_dir, limit)
    if key in _MINE_CACHE:
        return _MINE_CACHE[key]
    try:
        from .adapters.codebuddy import session_project_root_hints
        rows = session_project_root_hints(session_dir, limit=limit)
    except Exception:  # noqa: BLE001 - 挖掘属尽力而为
        rows = []
    _MINE_CACHE[key] = rows
    return rows


def codebuddy_recover_workspace_path(wid: str, session_dir: str) -> tuple[str, str]:
    """把 CodeBuddy 的 ``workspaceId`` 还原成项目路径：``(路径, 还原方式)``。

    还原不出来时返回 ``("", "")``。**只有 md5 校验对得上才认**——``md5`` 碰撞概率可忽略，
    因此这是唯一「可证明」的还原方式，绝不会把猜出来的路径当结论。

    查找顺序：
      1. **备份包携带的映射**（:func:`codebuddy_backup_workspace_map`）——跨机最可靠：
         导出时就把结果落进了包，不依赖本机 IDE 记录是否还在，也不依赖「先还原、
         后导入」的操作顺序；
      2. 本机 IDE 记录（:func:`codebuddy_workspace_path_index`，等价于结构化反查）；
      3. 会话正文里挖出的候选按同一规则求 md5 对撞。
    """
    wid = (wid or "").strip()
    if not wid:
        return "", ""
    entry = (codebuddy_backup_workspace_map(session_root_of(session_dir))
             .get("workspaces") or {}).get(wid)
    if isinstance(entry, dict):
        p = (entry.get("path") or "").strip()
        if p:
            src = entry.get("source") or ""
            if src == "ide_record":
                return p, "备份包记录（来自 IDE 记录）"
            if src == "content_hash":
                return p, "备份包记录（正文哈希校验一致）"
            return p, "备份包记录的工作区路径"
    p = codebuddy_workspace_path_index().get(wid, "")
    if p:
        return p, "IDE 已打开工程记录"
    for cand, _n in codebuddy_session_path_hints(session_dir, limit=0) or []:
        if codebuddy_workspace_id(cand) == wid:
            return cand, "会话正文（哈希校验一致）"
    return "", ""



def scope_from_path(path: str) -> str:
    """由工作区路径取 Reasonix 的「项目名」（末级目录名，非法字符压成 ``-``）。"""
    base = os.path.basename((path or "").rstrip("\\/"))
    parts = [c if (c.isalnum() or c in "-_.") else "-" for c in base]
    return "".join(parts).strip("-") or ""


def workspace_kind(tool: str) -> str:
    """该目标工具的「工作区标识」形态。"""
    if tool == "codebuddy":
        return KIND_ID
    if tool == "reasonix":
        return KIND_SCOPE
    return KIND_PATH


def workspace_kind_label(tool: str) -> str:
    """界面用：该目标工具「工作区」这一栏该叫什么。"""
    return {
        KIND_PATH: "目标工作区路径",
        KIND_ID: "目标 workspaceId",
        KIND_SCOPE: "目标项目名（scope）",
    }[workspace_kind(tool)]


def new_workbuddy_playground(stamp: str = "") -> str:
    """WorkBuddy「无工作区会话」的落点：``~/WorkBuddy/<时间戳>``。

    与产品自身行为一致——本机 ``~/.workbuddy/workbuddy.db`` 中 ``is_playground=1``
    的会话，其 cwd 均为 ``C:\\Users\\<用户>\\WorkBuddy\\<YYYY-MM-DD-HH-MM-SS>``。
    同一批导入共用同一个时间戳（由调用方传入 ``stamp``），避免每导入一条就散一个新目录。
    """
    stamp = stamp or datetime.now().strftime("%Y-%m-%d-%H-%M-%S")
    return os.path.join(os.path.expanduser("~"), "WorkBuddy", stamp)


def default_workspace(tool: str, stamp: str = "") -> str:
    """目标工具「无工作区会话」的默认落点（源会话没有任何工作区信息时使用）。

    - ``workbuddy``：``~/WorkBuddy/<时间戳>``（产品的 playground 新目录）
    - ``dsh``      ：用户主目录（DSH 没有「无工作区」形态，主目录一定存在）
    - ``reasonix`` ：``global-workspace``（产品自身的全局作用域名）
    - ``codebuddy``：固定的导入工作区 id（见 :data:`CODEBUDDY_FALLBACK_NAME`）
    """
    if tool == "workbuddy":
        return new_workbuddy_playground(stamp)
    if tool == "dsh":
        return os.path.expanduser("~")
    if tool == "reasonix":
        return "global-workspace"
    if tool == "codebuddy":
        return codebuddy_workspace_id(CODEBUDDY_FALLBACK_NAME)
    return ""


def session_workspace(tool: str, item: dict) -> str:
    """从「来源会话条目」里取出它自带的工作区标识（取不到返回空串）。

    ``item`` 即 :func:`~ai_env_clone.session_migration.list_source_sessions`
    返回的字典（``cwd`` / ``workspace_id`` / ``scope``）。
    """
    item = item or {}
    cwd = (item.get("cwd") or "").strip()
    if tool == "codebuddy":
        # 源本身就是 CodeBuddy：直接沿用它的 workspaceId（路径同源最稳）
        wid = (item.get("workspace_id") or "").strip()
        if wid:
            return wid
        return codebuddy_workspace_id(cwd) if cwd else ""
    if tool == "reasonix":
        scope = (item.get("scope") or "").strip()
        if scope:
            return scope
        return scope_from_path(cwd)
    # workbuddy / dsh：工作区就是路径
    return cwd


def workspace_path(tool: str, value: str, root: str = "") -> str:
    """工作区标识对应的文件系统路径（用于存在性判断与创建）。

    - WorkBuddy / DSH：标识本身即路径；
    - CodeBuddy：``<target_root>/<workspaceId>``；
    - Reasonix：``<projects 根>/<scope>``（``root`` 传 ``projects`` 父目录）。
    """
    if not value:
        return ""
    if tool in ("workbuddy", "dsh"):
        return value
    if not root:
        return ""
    return os.path.join(root, value)


def plan_for(tool: str, item: dict | None = None, manual: str | None = None,
             root: str = "", default_value: str = "", origin: str = "") -> WorkspacePlan:
    """算出「某个（或某一批）会话 -> 目标工作区」的落点计划。

    :param tool: 目标工具
    :param item: 来源会话条目（``manual`` 为空时用它取自带工作区）
    :param manual: 用户在界面里手动指定的工作区；传 ``None`` / ``""`` 表示走自动
    :param root: 目标根目录（CodeBuddy 的 history 根 / Reasonix 的 projects 根）
    :param default_value: 该工具「无工作区」的默认落点；留空则现算
    :param origin: 来源说明（展示用）
    """
    manual = (manual or "").strip()
    hints: tuple = ()
    if manual:
        mode, value, note = MODE_MANUAL, manual, ""
    else:
        value = session_workspace(tool, item) if item else ""
        note = ""
        if value:
            mode = MODE_SESSION
        else:
            # 源会话「有」工作区、但形态是路径派生出的 id（CodeBuddy 的 workspaceId 是
            # md5，不可逆）时：目标工具要的是**路径**，先尽力还原（只有哈希校验对得上
            # 才算，绝不用猜的）；还原不出来才退工具默认落点。
            recovered, how = "", ""
            if item and workspace_kind(tool) != KIND_ID:
                recovered, how = codebuddy_recover_workspace_path(
                    (item.get("workspace_id") or ""), (item.get("path") or ""))
            if recovered:
                mode, value = MODE_SESSION, recovered
                note = "由源会话的%s还原为路径" % how
            else:
                mode = MODE_DEFAULT
                value = default_value or default_workspace(tool)
                # 必须把成因说清楚：否则界面上那句「工作区 059d5d31ffef…」会被读成
                # 「落点就是它」，而实际落点是工具默认目录（WorkBuddy 还会现造一个
                # 时间戳新目录）。
                if item:
                    wid = (item.get("workspace_id") or "").strip()
                    if wid and not (item.get("cwd") or "").strip():
                        note = ("源会话只记了工作区 id（%s…，由项目路径哈希而来、不可逆），"
                                "备份包与本机都没能把它还原成路径 → 落到工具默认落点"
                                % wid[:8])
                        # 候选优先取「备份包里记下的」（导出时算好、跨机也在）；没有才现挖
                        # 本机会话正文（慢，但不留空）。两者都只是线索，不自动采用。
                        cands = [p for p in (item.get("workspace_candidates") or []) if p]
                        if not cands:
                            rows = codebuddy_session_path_hints(item.get("path") or "")
                            cands = [p for p, _n in rows]
                        if cands:
                            hints = tuple(cands)
                            note += ("；源会话正文里出现过：%s（仅供参考，可勾选"
                                     "「手动指定工作区」采用）" % "、".join(hints))

    path = workspace_path(tool, value, root)
    exists = os.path.isdir(path) if path else False
    return WorkspacePlan(tool=tool, mode=mode, value=value, path=path,
                         exists=exists, origin=origin, note=note, hints=hints)


def ensure(plan: WorkspacePlan) -> tuple[bool, str]:
    """按计划创建缺失的工作区目录。

    :return: ``(created, path)``；``created`` 表示本次**新建**了目录。
             已存在、无路径（标识型但无根目录）、创建失败（无权限等）都返回
             ``False``，由调用方决定要不要提示——本函数**不抛异常**，
             因为会话文件本身仍会照常写出。
    """
    path = plan.path
    if not path or os.path.isdir(path):
        return False, path
    try:
        if plan.tool == "reasonix":
            # Reasonix 的会话落在 <projects>/<scope>/sessions/
            os.makedirs(os.path.join(path, "sessions"), exist_ok=True)
        else:
            os.makedirs(path, exist_ok=True)
        return True, path
    except OSError:
        return False, path


# --------------------------------------------------------------------------- #
# 展示
# --------------------------------------------------------------------------- #
def describe_value(tool: str, value: str) -> str:
    """把工作区标识渲染为界面文案（CodeBuddy 的 id 附带派生来源）。"""
    if not value:
        return "（未确定）"
    if workspace_kind(tool) == KIND_PATH:
        return value
    if tool == "codebuddy":
        return value
    return value


def describe(plan: WorkspacePlan, max_len: int = 96) -> str:
    """把计划渲染成一行界面文案（含落点成因说明，见 ``plan.note``）。"""
    value = describe_value(plan.tool, plan.value)
    if len(value) > max_len:
        value = "…" + value[-max_len:]
    tail = "已存在" if plan.exists else "目标机不存在"
    if plan.value and not plan.path:
        tail = "无法在目标机判断是否存在"
    text = "%s    【%s，%s】" % (value or "（未确定）", plan.mode_label, tail)
    if plan.note:
        # 落到「工具默认落点」是**异常路径**（源会话没给出可用工作区），用 ⚠ 引起注意；
        # 「由源会话还原为路径」是正常结果，用 · 陈述即可。
        text += ("　⚠ " if plan.mode == MODE_DEFAULT else "　· ") + plan.note
    return text


def describe_missing(plans: list) -> list:
    """从一批计划里筛出「工作区不存在」的（按路径去重），供界面确认是否创建。"""
    seen: set = set()
    out: list = []
    for p in plans:
        if p.exists or not p.path or p.path in seen:
            continue
        seen.add(p.path)
        out.append(p)
    return out
