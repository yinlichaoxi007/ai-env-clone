"""
Qoder 适配器（自包含，不依赖任何遗留兼容层）。

本文件是 Qoder 备份逻辑的唯一事实来源：
- 目录探测（``QoderPaths`` / ``detect_qoder_root`` / UID 探测）
- 备份条目构造（``build_items``）
- 通过统一的 :class:`~ai_env_clone.adapters.base.BaseAdapter` 接口暴露给 GUI / CLI

Qoder CN 在本机存在**三处互不相同的用户数据面**，本适配器**同时覆盖**，这也是
「新版导入数据后仍看不到历史会话」的关键背景：

1. 旧版 IDE / 插件（含前 Lingma 世代）→ ``~/.qoder-cn/`` 与 ``~/.lingma/``
   - ``shared_client/cache/db/local.db``：会话主库。⚠️ 口径修正（实测）：
     **容器本身是标准 SQLite（未整库加密，``sqlite3`` 可直接打开）**，密文只出现在
     **会话正文列**——``chat_message.content``（含 ``summary`` / ``tool_result``）与
     ``chat_record.question|answer`` 为不可读密文；
     而 ``chat_session``（标题 / 项目绝对路径 / 时间 / 模型）、``agent_memory``、
     ``lingma_memory`` 均为**明文**（``agent_memory.content`` 就是 Markdown）。
     → 故**整库搬运可行**（走 ``snapshot_sqlite`` 在线备份），但**按条融合 / 跨工具
     复刻不可行**（正文取不出）。曾经的「加密库」表述不准确，勿再沿用。
   - ``cache/projects/<项目>/conversation-history/<id>/<id>.jsonl``：会话的
     **明文纯文字副本**（``{"role","message":{"content":[{"type":"text",...}]}}``）。
     实测本机 16 个文件，最大者 1.65 MB / 1974 行——但 content 类型**只有 ``text``**
     （无 ``tool_use`` / ``tool_result`` / ``thinking``），且不覆盖全部会话
     → 只够做**有损导入**（仅文字、无工具过程），不能当无损迁移。
   - ``memories/<uid>/{global,projects}``、``shared_client/memories/<uid>/``、
     ``rules/``、``settings.json``、``plugins/``、``shared_client/index/``、
     ``qoder-knowledge/``（含 ``legacy-migration/state.v1.json`` 完成标记）。
   - ``shared_client/.cn_migration/{critical,noncritical}/*.done``：**向新版迁移的
     文件级交接标记**（``local.db.done``、``id.done``、``user.done`` …）。已实测存在。
     ⚠️ 一次性语义：它一旦落盘，产品就把该批文件视为「已迁移」而不再搬运，所以
     **必须与旧版数据同进同出**，单独带标记到新机会让产品跳过必要初始化。
2. 新版 Qoder CN 桌面端（Electron）→ ``%APPDATA%/com.qodercn.app.stable/``
   - ``main.sqlite``：桌面端自己的**会话库**（``chat_sessions`` /
     ``chat_session_messages``，消息体为 ``payload_json``）+ 设置、市场、技能等
     （同为标准 SQLite）。实测本机该库会话两张表**为空**。
   - ``sessionMigration.sqlite``：**应用内「导入旧版数据」功能的账本**
     （``import_job`` / ``import_job_item`` / ``import_history`` /
     ``migration_ledger`` / ``candidate_snapshot``）。本机全表为空 → 导入从未成功落库。
     ⚠️ 跨账号硬约束：旧库数据全挂在**旧账号 ID** 下，任何「按当前登录账号过滤」的
     导入逻辑用新账号查旧库都会得到 0 条可导入，点下去自然没反应——这不是本工具能修的。
   - ``memoryMigration.sqlite``：记忆导入账本（``memory_migration_ledger``）。
   - ``main.sqlite.pre-migration-backup``：迁移前的库快照（改坏时的回退依据）。
   - ``qoder-data.v1.json``、``auth.v1.dat``、``auth.machine-id``、
     ``chat-session-turn-payload-buffer.sqlite`` 等运行态/凭证文件。
3. Qoder CN CLI / Agent 运行时（与旧版**共用** ``~/.qoder-cn`` 根，不是独立根）
   - ``projects/<项目 key>/``（新版按项目组织的数据）、``memory/``（用户级
     auto-memory）、``plans/`` / ``tasks/`` / ``file-history/`` / ``canvas/`` /
     ``mcp.json`` 等新版产物族。
   - ``logs/runs/<run>/manifest.json``：运行时清单（``cli_version`` / ``argv``）。
     ⚠️ **该文件的 argv 里明文写着 MCP 的 ``X-Api-Key``**，故 ``*/logs/*`` 被
     ``DEFAULT_EXCLUDES`` 整目录排除是**必要**的，不要为「保留日志」而放开。
   - 新版桌面端在 ``%LOCALAPPDATA%/.qoder-cn/shared_client/`` 下另有一份
     「工作区临时文件 + 追踪」目录（``workingSpace/``、``ai_tracker/``）。

> ⚠️ 结论：**三处面各存一部分数据，缺一处就会出现「导入成功却看不到历史」。**
> 旧版历史在 ``local.db``（正文列密文，只能整库搬），桌面端读自己的 ``main.sqlite``，
> CLI/Agent 新族数据在 ``~/.qoder-cn`` 根下与旧代并存（**根没变 ≠ 存储通用**）。
> 本适配器三处都覆盖，并提供
> :func:`diagnose_history` 诊断历史不可见的成因（GUI 上有「检测历史会话」入口）。
>
> ⚠️ **目录存在性随机器/版本而变，这不是探测失败**：上面「第 3 类」的新版产物族
> （``projects/<key>/*.jsonl``、``plans/``、``tasks/``、``file-history/``、``canvas/``、
> ``mcp.json``、``mcp-router.json``、``extensions/``、``%APPDATA%/QoderCN``）
> **并非每台机器都有**——取决于该机的 Qoder 版本、以及是否真的用过 CLI/Agent。
> 同一份设计文档里「存在且带体积」的那批实测采自**另一台工作电脑**；本机是
> 「旧代 + 新版空壳」，``projects/`` 下只有空的 ``<key>/memory/``、无任何 ``.jsonl``，
> 上述目录一个都没有。故条目策略统一为**存在才勾选、缺失只显示「未找到」**：
> 任何一处缺失都不代表适配器探测失败，更不代表用户数据丢失。诊断输出里
> 「会话 0 个」之类的结论，务必结合该机版本理解。
>
> ⚠️ **项目 key 不稳定**：``projects/<key>`` 的命名规则在版本间变过（盘符大小写、
> 分隔符与空格的处理、单/双连字符），因此适配器**绝不自己拼 key、也不解析 key 反推
> 路径**——备份按目录整族入包、不重命名不合并；还原重映射以包内 jsonl 首行
> ``workspace-directories`` 的 ``directories`` 为源路径真值。

新增其它工具时，仿照本文件新建 ``ai_env_clone/adapters/<tool>.py`` 即可，
用 ``@register`` 装饰类，无需改动任何入口代码。
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
from dataclasses import dataclass

from ..core import ORIGIN_NOTE_CONVERSATION, ORIGIN_NOTE_SESSION_DB, BackupItem
from .base import BaseAdapter, register

#: 新版 Qoder CN 桌面端（Electron）的 userData 目录名
QODER_CN_ELECTRON_DIR = "com.qodercn.app.stable"
#: 国际版 Qoder 桌面端目录名（本适配器不备份，仅在识别状态区提示）
QODER_ELECTRON_DIR = "com.qoder.app.stable"
#: Qoder CN **独立 IDE**（非 com.qodercn.app.stable 那套）在 %APPDATA% 下的配置目录名。
#: 只在部分机器/版本上存在（另一台工作电脑实测有、本机无），仅用于识别状态区提示。
QODER_CN_IDE_DIR = "QoderCN"
#: 前 Lingma 世代的数据根（旧版 Qoder CN 的前身）。同样随机器/版本出现或消失。
QODER_LINGMA_DIR = ".lingma"


@dataclass
class QoderPaths:
    """Qoder 数据目录布局。``shared`` 指向 shared_client（新版）或 sharedclient（旧版）。"""

    root: str
    shared: str

    @property
    def exists(self) -> bool:
        return os.path.isdir(self.root) and os.path.isdir(self.shared)


def detect_qoder_root(explicit: str | None = None) -> "QoderPaths":
    """
    探测 Qoder 数据根目录。

    优先使用 ``explicit``；否则探测 ``~/.qoder-cn/shared_client``（新版）或
    ``~/.qoder-cn/sharedclient``（旧版）；都不存在则给出默认建议路径。
    """
    if explicit:
        root = os.path.expanduser(explicit)
        for name in ("shared_client", "sharedclient"):
            shared = os.path.join(root, name)
            if os.path.isdir(shared):
                return QoderPaths(root, shared)
        # 显式指定但子目录未匹配，仍以 .qoder-cn/shared_client 为约定
        return QoderPaths(root, os.path.join(root, "shared_client"))

    base = os.path.expanduser("~")
    root = os.path.join(base, ".qoder-cn")
    for name in ("shared_client", "sharedclient"):
        shared = os.path.join(root, name)
        if os.path.isdir(shared):
            return QoderPaths(root, shared)
    return QoderPaths(root, os.path.join(root, "shared_client"))


def detect_memory_uids(root: str, shared: str) -> list[str]:
    """
    探测记忆区下出现过的全部用户 ID（目录名即 UID）。

    Qoder CN 的记忆分散在两个位置，且都按 UID 分子目录：
        - ``<root>/memories/<uid>/{global,projects}``
        - ``<shared>/memories/<uid>/{global,projects}``

    返回去重后的 UID 列表；若都未出现则空列表。
    """
    uids: set[str] = set()
    for base in (os.path.join(root, "memories"), os.path.join(shared, "memories")):
        if os.path.isdir(base):
            for name in os.listdir(base):
                full = os.path.join(base, name)
                if os.path.isdir(full) and not name.startswith("."):
                    uids.add(name)
    return sorted(uids)


def _uid_from_app_status(root: str) -> str | None:
    """从 ``<root>/.qoder-app-status.json`` 的 ``avatar_url`` 解析完整 UID。

    该文件是产品自己写的登录态快照（``logged_in`` / ``name`` / ``avatar_url`` /
    ``version`` / ``product``），``avatar_url`` 形如
    ``https://qoder.com.cn/users/<uid>/default/avatars``，其中的 ``<uid>`` 是
    **完整 UUIDv7**。这是最可靠的 UID 来源（确定式解析，非启发式）。
    """
    path = os.path.join(root, ".qoder-app-status.json")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    url = data.get("avatar_url") if isinstance(data, dict) else None
    if not isinstance(url, str):
        return None
    m = re.search(r"/users/([0-9A-Za-z._-]{4,})/", url)
    return m.group(1) if m else None


def _uid_from_models(root: str) -> str | None:
    """从 ``<root>/.models/<uid>/`` 目录名解析 UID（目录名即完整 UUIDv7）。

    模型目录缓存顺带是 UID 的可靠来源；``default`` 是公共档位，需跳过。
    """
    base = os.path.join(root, ".models")
    if not os.path.isdir(base):
        return None
    for name in sorted(os.listdir(base)):
        if name.startswith(".") or name == "default":
            continue
        if os.path.isdir(os.path.join(base, name)):
            return name
    return None


def detect_current_uid(root: str, shared: str) -> str | None:
    """
    自动判定"当前登录用户"的 UID（**确定式解析**，2026-09-27 实测校准）。

    按可靠性依次尝试，任一级命中即返回：

    1. ``<root>/.qoder-app-status.json`` 的 ``avatar_url``（完整 UUIDv7）；
    2. ``<root>/.models/<uid>/`` 目录名（完整 UUIDv7）；
    3. **兜底**：扫描两处记忆目录下各 UID 子目录，取文件修改时间最新者
       （旧版数据里可能只剩旧账号目录，此时"最近活跃"是合理近似）。

    ⚠️ 必须支持 UID 的**三种写法**（实测并存）：完整 UUIDv7
    （``019eb095-47ef-70d2-8a41-ea6278c176b1``）、截断前 8 位目录名
    （``019eb095`` / ``13166325``）、以及库内的 16 位数字账号 ID
    （``1316632530577119``）。历史实现误以为"无法从本地数据文件可靠反解明文 UID"，
    故只能用 mtime 启发式——该说法已不成立（第 1、2 级即为明文确定式来源）。

    返回 UID 字符串，三级都取不到则 ``None``。
    """
    for candidate in (_uid_from_app_status(root), _uid_from_models(root)):
        if candidate:
            return candidate

    best_uid: str | None = None
    best_time = 0.0
    for base in (os.path.join(root, "memories"), os.path.join(shared, "memories")):
        if not os.path.isdir(base):
            continue
        for name in os.listdir(base):
            full = os.path.join(base, name)
            if not os.path.isdir(full) or name.startswith("."):
                continue
            # 该 UID 目录下最近修改时间
            latest = 0.0
            for dp, _, fns in os.walk(full):
                for fn in fns:
                    try:
                        t = os.path.getmtime(os.path.join(dp, fn))
                    except OSError:
                        continue
                    if t > latest:
                        latest = t
            if latest > best_time:
                best_time = latest
                best_uid = name
    return best_uid


def _uid_prefix(uid: str) -> str:
    """UID 的**比对前缀**（前 8 位十六进制，小写）。

    Qoder 的 UID 实测有三种写法并存：完整 UUIDv7（``019eb095-47ef-…``）、
    截断为前 8 位的目录名（``019eb095``）、以及库内的 16 位数字账号 ID
    （``1316632530577119`` → 目录 ``13166325``）。三者的前 8 位都一致，故用
    前 8 位做「是否同一账号」的判定，并与 ``-`` 无关。
    """
    return uid.split("-")[0][:8].lower()


def _same_user(uid: str, other: str) -> bool:
    """两个 UID 写法是否指向同一账号（按前 8 位前缀判定）。"""
    return bool(uid) and bool(other) and _uid_prefix(uid) == _uid_prefix(other)


def _resolve_uid_dir(base: str, uid: str) -> str | None:
    """在 ``base`` 下找出属于 ``uid`` 的**实际目录名**。

    目录名可能是完整 UUIDv7，也可能只是前 8 位截断（``shared_client/memories/019eb095``）。
    若按完整 UID 找不到，则退化为「前缀命中」的目录名，保证升级为确定式解析后
    「当前用户记忆区」仍能落到真实目录上（否则会因目录名是截断形式而全部落空）。
    找不到返回 ``None``。
    """
    if not uid or not os.path.isdir(base):
        return None
    if os.path.isdir(os.path.join(base, uid)):
        return uid
    for name in sorted(os.listdir(base)):
        if name.startswith(".") or not os.path.isdir(os.path.join(base, name)):
            continue
        if _same_user(name, uid):
            return name
    return None


def _db_with_companions(db_path: str) -> list[str]:
    """返回 SQLite 主库及其 -wal/-shm 配套（存在的才返回）。"""
    out = []
    for p in (db_path, db_path + "-wal", db_path + "-shm"):
        if os.path.exists(p):
            out.append(p)
    return out


def build_items(paths: "QoderPaths", current_uid: str | None = None) -> list[BackupItem]:
    """
    根据 Qoder CN 目录布局构造可备份条目清单。

    覆盖同一 ``~/.qoder-cn`` 根目录下的全部 Qoder CN 产品（原 Lingma 插件、
    Qoder CN IDE 等），用户无需区分具体产品。

    记忆区按 UID 拆分：当前用户（聚合前缀 ``memories_current``，默认勾选）与
    其他用户（聚合前缀 ``memories_others``，默认不勾、按 UID 拆分，GUI 勾选后展开多选）。

    同名逻辑项（如会话库主库/-wal/-shm、记忆区 root/shared 两处）各自使用**唯一 key**
    （格式 ``<聚合前缀>:<区分后缀>``，如 ``session_db:wal``、``memories_current:shared``），
    路径信息完全不失真；GUI 按聚合前缀去重渲染为一个勾选项，导出时仍扫描全部物理路径。

    :param current_uid: 若提供则覆盖自动检测到的"当前用户"，用于 GUI 手动指定。
    """
    root, shared = paths.root, paths.shared

    # 自动判定当前用户 UID（最近修改最新）；GUI 可让用户改
    if not current_uid:
        current_uid = detect_current_uid(root, shared)
    all_uids = detect_memory_uids(root, shared)

    items: list[BackupItem] = []

    # 1) 全局会话数据库（SQLite 主库 + 配套）
    #    主库 / -wal / -shm 三项使用唯一 key（聚合前缀 "session_db" + 区分后缀），
    #    路径信息不失真，GUI 按前缀聚合成一个勾选项。
    db_main = os.path.join(shared, "cache", "db", "local.db")
    for db_file in _db_with_companions(db_main):
        # 主库 key="session_db"；配套的 -wal/-shm 用 "session_db:wal"/"session_db:shm"
        if db_file == db_main:
            key = "session_db"
        elif db_file.endswith("-wal"):
            key = "session_db:wal"
        elif db_file.endswith("-shm"):
            key = "session_db:shm"
        else:
            key = "session_db"
        items.append(
            BackupItem(
                key=key,
                label="历史会话数据库",
                path=db_file,
                description="全局会话/历史主库及其 -wal/-shm 配套文件，核心数据。建议必选。",
                recommended=True,
                carries_origin=ORIGIN_NOTE_SESSION_DB,
            )
        )

    # 2) 项目级会话历史（IDE 写入的 conversation-history/*.jsonl）
    proj_sessions = os.path.join(root, "cache", "projects")
    items.append(
        BackupItem(
            key="project_sessions",
            label="项目级会话历史",
            path=proj_sessions,
            description="各项目的对话历史（conversation-history/*.jsonl）。建议必选。",
            recommended=True,
            carries_origin=ORIGIN_NOTE_CONVERSATION,
        )
    )

    # 2.1) 新版按项目组织的数据（根目录 projects/<项目 key>/）
    #
    # 与上面 cache/projects 不是同一处：新版 Agent 在**数据根直属**的 projects/ 下按
    # 项目建目录（本机实测形如 C--Users-<用户>-Documents-Qoder-<日期>-<8位hash>/）。
    # 新版会话正文（<uuid>.jsonl）+ 项目级 memory 都落在这里，旧版本工具链没有该目录。
    # ⚠️ 项目 key 命名规则在版本间变过（盘符大小写、分隔符/空格处理、单双连字符），
    #    故按目录**整族入包**，不重命名、不合并。
    new_projects = os.path.join(root, "projects")
    items.append(
        BackupItem(
            key="new_projects",
            label="新版会话与项目记忆（projects/）",
            path=new_projects,
            description="新版 Agent 按项目组织的数据根（projects/<项目 key>/：会话 <uuid>.jsonl、"
                        "state.json、项目级 memory/）。会话正文不可从零重建，默认勾选。",
            recommended=True,
        )
    )

    # 3) 用户级规则（根目录 rules/，实测由 Qoder CN IDE 写入）
    rules_dir = os.path.join(root, "rules")
    items.append(
        BackupItem(
            key="rules",
            label="用户级规则",
            path=rules_dir,
            description="用户级规则库（~/.qoder-cn/rules）。建议必选。",
            recommended=True,
        )
    )

    # 4) 记忆区：当前用户（默认勾选，可在数据目录区切换 UID）
    #    两处路径（~/.qoder-cn/memories/<uid> 与 ~/.qoder-cn/shared_client/memories/<uid>）
    #    各生成唯一 key（聚合前缀 "memories_current" + :root/:shared 后缀），
    #    GUI 按前缀聚合成一个勾选项；任一存在即视为"找到"。
    #    注意：无论是否探测到当前用户，都必须生成"当前用户记忆区"这一标准选项，
    #    否则数据目录识别失败（current_uid 为空）时该行会从清单消失，导致备份内容区
    #    选项与勾选状态随路径识别结果忽有忽无。探测失败时以占位 uid 生成，路径无效、
    #    exists 自动为 False，GUI 仅显示"（未找到）"且不勾选，选项结构保持稳定。
    #
    #    ⚠️ 目录名可能是 UID 的**截断写法**（本机实测 shared_client/memories/019eb095，
    #    而当前 UID 是完整 UUIDv7），故用 _resolve_uid_dir() 先解析出真实目录名，
    #    否则「当前用户记忆区」会因目录名不匹配而全部落空、把本人数据误判成"其他用户"。
    cur_uids = [current_uid] if current_uid else [None]
    for uid in cur_uids:
        cur_paths = []
        for base, tag in (
            (os.path.join(root, "memories"), "root"),
            (os.path.join(shared, "memories"), "shared"),
        ):
            if uid:
                actual = _resolve_uid_dir(base, uid) or uid
                cur_paths.append((os.path.join(base, actual), tag, actual))
            else:
                cur_paths.append((base, tag, None))
        for mp, tag, actual in cur_paths:
            if uid:
                shown = actual if actual == uid else "%s（目录名截断写法）" % actual
                desc = "当前登录用户（%s）的记忆（%s 位置）。默认勾选。" % (shown, tag)
            else:
                desc = "当前登录用户的记忆（%s 位置）。未检测到当前用户，请确认数据目录正确。默认勾选。" % tag
            items.append(
                BackupItem(
                    key="memories_current:" + tag,
                    label="当前用户记忆区",
                    path=mp,
                    uid=uid,
                    description=desc,
                    recommended=True,
                )
            )

    # 5) 记忆区：其他用户（默认不勾；GUI 勾选后按 UID 展开多选）
    #    排除当前用户，避免与"当前用户记忆区"重复备份。每个 UID 同样按两处路径拆分唯一 key。
    for uid in all_uids:
        # 与当前用户同一账号（含"完整 UUID vs 前 8 位截断"两种写法）的目录不算"其他用户"
        if current_uid and _same_user(uid, current_uid):
            continue
        cur_paths = [
            (os.path.join(root, "memories", uid), "root"),
            (os.path.join(shared, "memories", uid), "shared"),
        ]
        for mp, tag in cur_paths:
            items.append(
                BackupItem(
                    key="memories_others:%s:%s" % (uid, tag),
                    label="其他用户记忆区",
                    path=mp,
                    uid=uid,
                    description="用户 %s 的记忆（%s 位置）。默认不勾，勾选后在列表内选择要备份的 UID。" % (uid, tag),
                    recommended=False,
                )
            )

    # 6) 代码索引（体积大，默认不勾）
    index_dir = os.path.join(shared, "index")
    items.append(
        BackupItem(
            key="code_index",
            label="代码索引",
            path=index_dir,
            description="代码语义索引数据库，体积较大。可选。",
            recommended=False,
        )
    )

    # 7) 设置（默认勾选，用户 2026-10-01 定策：设置属「还原后立刻能开工」类）
    settings_file = os.path.join(root, "settings.json")
    items.append(
        BackupItem(
            key="settings",
            label="设置（全局/用户级）",
            path=settings_file,
            description="用户全局设置（如主题、自动化开关）。默认勾选，还原后无需重新配置。",
            recommended=True,
        )
    )

    # 8) 新版 Agent 用户级记忆（``memory/``，跨项目 auto-memory）。
    #    与按 UID 分层的旧版 ``memories/`` 是**两套结构**（新版无 UID 层），故单独成条，
    #    不并入 ``memories_current``，避免还原时把两种结构混在一处。
    items.append(
        BackupItem(
            key="agent_memory",
            label="新版用户级记忆（memory/）",
            path=os.path.join(root, "memory"),
            description="新版 Agent 的跨项目 auto-memory（一条记忆一个 .md + MEMORY.md 索引）。"
                        "用户数据、不可从零重建，默认勾选。",
            recommended=True,
        )
    )

    # 9) 新版 Agent 产物族：计划 / 待办 / 文件检查点 / Canvas。
    #    均为「数据根直属」目录，与旧版无交集；新增条目只做加法，旧 key 一律不变。
    for key, dirname, label, desc, rec in (
        ("agent_plans", "plans", "计划（plans/）",
         "Plan 模式的计划产物（plans/*.md）。默认勾选。", True),
        ("agent_tasks", "tasks", "待办列表（tasks/）",
         "按会话 UUID 分目录的待办清单（tasks/<uuid>/）。默认勾选。", True),
        ("agent_file_history", "file-history", "文件检查点（file-history/）",
         "会话内被修改文件的检查点历史（file-history/<uuid>/），体积随用量增长。默认不勾。", False),
    ):
        items.append(
            BackupItem(
                key=key,
                label=label,
                path=os.path.join(root, dirname),
                description=desc,
                recommended=rec,
            )
        )
    for suffix in ("canvases", "kanban"):
        items.append(
            BackupItem(
                key="agent_canvas:" + suffix,
                label="Canvas 内容（canvas/）",
                path=os.path.join(root, "canvas", suffix),
                description="用户 Canvas 内容与看板（canvas/%s）。随包资源 recipes/、sdk/ "
                            "不单独备份。默认不勾。" % suffix,
                recommended=False,
            )
        )

    # 10) MCP 配置（含明文密钥，导出脱敏；见 QoderAdapter.export_transform）。
    for suffix, fname in (("json", "mcp.json"), ("router", "mcp-router.json")):
        items.append(
            BackupItem(
                key="mcp_config:" + suffix,
                label="MCP 配置（mcp.json / mcp-router.json）",
                path=os.path.join(root, fname),
                description="用户级 MCP 服务器配置（%s）。**可能含明文密钥**，导出时已脱敏、"
                            "恢复后需在目标机手动补填。默认不勾。" % fname,
                recommended=False,
                sensitive=True,
            )
        )

    # 11) 已装插件登记表（可重建；还原整体覆盖会抹掉本机已登记项，故与数据分开）。
    for suffix, fname in (("v2", "installed_plugins_v2.json"), ("v1", "installed_plugins.json")):
        items.append(
            BackupItem(
                key="plugins_manifest:" + suffix,
                label="已装插件登记（plugins/installed_plugins*.json）",
                path=os.path.join(root, "plugins", fname),
                description="已安装插件登记表（plugins/%s）。可重新安装，默认不勾。" % fname,
                recommended=False,
            )
        )

    # 12) 旧版向新版迁移的交接标记（``.cn_migration/``）。
    #    ⚠ 这是**一次性完成标记**：单独把它带到新机器，产品会认为「已迁移过」而跳过
    #      必要的初始化/搬运，表现为「数据在但界面不认」。必须与旧版数据同进同出。
    items.append(
        BackupItem(
            key="legacy_migration_marks",
            label="旧版迁移交接标记（shared_client/.cn_migration/）",
            path=os.path.join(shared, ".cn_migration"),
            description="旧版向新版迁移的文件级完成标记（critical/noncritical/*.done）。"
                        "仅在需要「旧代在新机仍能认这个库」时勾选，且必须与旧版数据"
                        "（local.db / memories）同进同出；单独带过去会让产品跳过必要初始化。"
                        "默认不勾。",
            recommended=False,
        )
    )

    # 13) 旧版身份 / 登录态文件（``shared_client/cache/`` 下的身份族）。
    #    与 local.db 的 ``sync_metadata`` 同属一套身份体系；只搬库不搬身份，旧代在新机
    #    可能因身份/配置缺失而不认这个库（或重跑一次搬迁）。含凭证，导出脱敏。
    #
    #    ⚠️ 该目录下**到底有哪些文件随机器/版本而变**：本机实测只剩
    #    ``machine_token.json`` / ``policy`` / ``user`` / ``quota``（``id`` /
    #    ``status.json`` / ``app-config.json`` 已被产品搬走，只在
    #    ``.cn_migration/critical/*.done`` 里留下标记）；而设计文档在**另一台工作电脑**
    #    上实测这三个文件仍在。故一并建为**默认不勾**的可选条目：本机显示「未找到」，
    #    工作机则能勾上——不猜、不推断，交给存在性判断。
    for suffix, fname, what in (
        ("machine_token", "machine_token.json", "机器令牌"),
        ("policy", "policy", "策略文件"),
        ("user", "user", "账号信息"),
        ("id", "id", "设备/账号标识"),
        ("status", "status.json", "状态快照"),
        ("app_config", "app-config.json", "应用配置"),
    ):
        items.append(
            BackupItem(
                key="legacy_identity:" + suffix,
                label="旧版身份与登录态（shared_client/cache/）",
                path=os.path.join(shared, "cache", fname),
                description="旧版身份/登录态文件（shared_client/cache/%s，%s）。含凭证、"
                            "跨机须重新登录，默认不勾。" % (fname, what),
                recommended=False,
                sensitive=True,
            )
        )

    return items


# --------------------------------------------------------------------------- #
# 新版 Qoder CN（Electron 桌面端）数据根与条目
# --------------------------------------------------------------------------- #
def _platform_appdata_under(root: str) -> str:
    """返回 ``root`` 主目录下的「漫游应用数据」目录（Windows 为 ``AppData/Roaming``）。

    ``root`` 与真实用户主目录一致时优先采用环境变量给出的权威位置
    （``%APPDATA%``，可被重定向），否则按平台约定在 ``root`` 之下推导——
    这样在 GUI 中把「数据目录」改到别处（或单元测试用临时目录）时，
    新增条目会随之落在该目录下、显示「（未找到）」，而不会读写真实用户数据。
    """
    is_real_home = False
    try:
        is_real_home = os.path.abspath(root) == os.path.abspath(os.path.expanduser("~"))
    except (ValueError, OSError):
        is_real_home = False

    if sys.platform.startswith("win"):
        if is_real_home:
            return os.environ.get("APPDATA") or os.path.join(root, "AppData", "Roaming")
        return os.path.join(root, "AppData", "Roaming")
    if sys.platform == "darwin":
        return os.path.join(root, "Library", "Application Support")
    return os.path.join(root, ".config")


def _platform_localappdata_under(root: str) -> str:
    """返回 ``root`` 主目录下的「本地应用数据」目录（Windows 为 ``AppData/Local``）。"""
    is_real_home = False
    try:
        is_real_home = os.path.abspath(root) == os.path.abspath(os.path.expanduser("~"))
    except (ValueError, OSError):
        is_real_home = False

    if sys.platform.startswith("win"):
        if is_real_home:
            return os.environ.get("LOCALAPPDATA") or os.path.join(root, "AppData", "Local")
        return os.path.join(root, "AppData", "Local")
    if sys.platform == "darwin":
        return os.path.join(root, "Library", "Application Support")
    return os.path.join(root, ".local", "share")


def detect_electron_root(root: str | None = None) -> str:
    """新版 Qoder CN 桌面端（Electron）userData 根：``%APPDATA%/com.qodercn.app.stable``。"""
    base = root or os.path.expanduser("~")
    return os.path.join(_platform_appdata_under(base), QODER_CN_ELECTRON_DIR)


def detect_intl_electron_root(root: str | None = None) -> str:
    """国际版 Qoder 桌面端 userData 根（仅用于识别状态区提示，不备份）。"""
    base = root or os.path.expanduser("~")
    return os.path.join(_platform_appdata_under(base), QODER_ELECTRON_DIR)


def detect_legacy_local_root(root: str | None = None) -> str:
    """新版桌面端在本地应用数据下的工作区目录根：``%LOCALAPPDATA%/.qoder-cn``。"""
    base = root or os.path.expanduser("~")
    return os.path.join(_platform_localappdata_under(base), ".qoder-cn")


def detect_cn_ide_root(root: str | None = None) -> str:
    """Qoder CN 独立 IDE 的配置根：``%APPDATA%/QoderCN``（仅识别状态区提示，不备份）。

    只在部分机器/版本上存在：一台工作电脑实测有，本机没有。列为数据根是为了让
    「识别状态区」在任何机器上都如实反映，缺失即显示未找到，不作任何推断。
    """
    base = root or os.path.expanduser("~")
    return os.path.join(_platform_appdata_under(base), QODER_CN_IDE_DIR)


def detect_lingma_root(root: str | None = None) -> str:
    """前 Lingma 世代数据根：``~/.lingma``（仅识别状态区提示，不备份）。"""
    base = root or os.path.expanduser("~")
    return os.path.join(base, QODER_LINGMA_DIR)


def _sqlite_companions(db_path: str) -> list[tuple[str, str]]:
    """``(路径, key 后缀)``：主库 + 存在的 ``-wal``/``-shm``。"""
    out = [(db_path, "")]
    for suffix in ("-wal", "-shm"):
        p = db_path + suffix
        if os.path.exists(p):
            out.append((p, ":" + suffix.lstrip("-")))
    return out


def build_electron_items(
    root: str | None = None,
    electron_root: str | None = None,
    legacy_local_root: str | None = None,
) -> list[BackupItem]:
    """构造**新版 Qoder CN 桌面端**（Electron）的备份条目。

    新版把会话历史放在自己的 ``main.sqlite`` 里，与旧版 ``~/.qoder-cn`` 完全分离；
    再把两代数据混在一起会得到「导入成功却看不到历史」的错觉，故单独成组。
    """
    base = root or os.path.expanduser("~")
    ed = electron_root or detect_electron_root(base)
    ll = legacy_local_root or detect_legacy_local_root(base)

    items: list[BackupItem] = []

    # 1) 新版会话库（核心）。
    for path, suffix in _sqlite_companions(os.path.join(ed, "main.sqlite")):
        items.append(
            BackupItem(
                key="electron_main_db" + suffix,
                label="新版会话库（main.sqlite）",
                path=path,
                uid=None,
                description="新版 Qoder CN 桌面端会话库（chat_sessions / chat_session_messages）、"
                            "设置、市场与技能清单。新版界面读取的就是这里，核心数据，默认勾选。",
                recommended=True,
            )
        )

    # 2) 迁移前库快照（改坏时的回退依据）。
    items.append(
        BackupItem(
            key="electron_main_backup",
            label="迁移前库快照（main.sqlite.pre-migration-backup）",
            path=os.path.join(ed, "main.sqlite.pre-migration-backup"),
            uid=None,
            description="新版在改库前自动留下的快照。一旦迁移/导入出问题可据此回退，默认勾选。",
            recommended=True,
        )
    )

    # 3) 数据导入账本（应用内「导入旧版数据」的进度与结果）。
    items.append(
        BackupItem(
            key="electron_session_migration",
            label="会话导入账本（sessionMigration.sqlite）",
            path=os.path.join(ed, "sessionMigration.sqlite"),
            uid=None,
            description="应用内「导入 QoderWork CN / Qoder CN IDE 数据」的账本（导入任务与结果记录）。"
                        "诊断「导入后看不到历史」时必查，默认勾选。",
            recommended=True,
        )
    )
    items.append(
        BackupItem(
            key="electron_memory_migration",
            label="记忆导入账本（memoryMigration.sqlite）",
            path=os.path.join(ed, "memoryMigration.sqlite"),
            uid=None,
            description="应用内记忆导入账本（memory_migration_ledger）。默认勾选。",
            recommended=True,
        )
    )

    # 4) 桌面端状态/设置。状态属运行态（默认不勾）；偏好属「设置」类（默认勾选）。
    items.append(
        BackupItem(
            key="electron_data_json",
            label="桌面端状态（qoder-data.v1.json）",
            path=os.path.join(ed, "qoder-data.v1.json"),
            uid=None,
            description="新版桌面端的运行器/代理/回合状态（qoder-data.v1.json）。属运行态，默认不勾。",
            recommended=False,
        )
    )
    items.append(
        BackupItem(
            key="electron_settings_json",
            label="桌面端偏好（notification/tray/updates 等）",
            path=os.path.join(ed, "notification-preferences.v1.json"),
            uid=None,
            description="桌面端通知偏好（notification-preferences.v1.json）。"
                        "属「设置」类，默认勾选（还原后无需重新勾一遍偏好）。",
            recommended=True,
        )
    )

    # 5) 凭证（敏感）。
    items.append(
        BackupItem(
            key="electron_auth",
            label="登录凭证（auth.*）",
            path=os.path.join(ed, "auth.v1.dat"),
            uid=None,
            description="新版桌面端登录凭证（auth.v1.dat，机器绑定）。敏感，目标机须重新登录，默认不勾。",
            recommended=False,
            sensitive=True,
        )
    )

    # 6) 运行态（回合载荷缓冲）。
    items.append(
        BackupItem(
            key="electron_turn_buffer",
            label="回合载荷缓冲（chat-session-turn-payload-buffer.sqlite）",
            path=os.path.join(ed, "chat-session-turn-payload-buffer.sqlite"),
            uid=None,
            description="进行中回合的临时载荷缓冲（chat-session-turn-payload-buffer.sqlite）。属运行态，默认不勾。",
            recommended=False,
        )
    )

    # 7) 本地应用数据下的工作区/追踪目录（体积小、含工作区文件，默认不勾）。
    items.append(
        BackupItem(
            key="legacy_local_workspace",
            label="桌面端工作区临时文件（Local/.qoder-cn/shared_client/workingSpace）",
            path=os.path.join(ll, "shared_client", "workingSpace"),
            uid=None,
            description="新版桌面端在本地应用数据下缓存的工作区文件（workingSpace/）。默认不勾。",
            recommended=False,
        )
    )
    items.append(
        BackupItem(
            key="legacy_local_tracker",
            label="桌面端使用追踪（Local/.qoder-cn/shared_client/ai_tracker）",
            path=os.path.join(ll, "shared_client", "ai_tracker"),
            uid=None,
            description="新版桌面端的使用追踪数据（ai_tracker/）。默认不勾。",
            recommended=False,
        )
    )

    return items


# --------------------------------------------------------------------------- #
# 历史会话不可见诊断
# --------------------------------------------------------------------------- #
def _count_rows(db_path: str, table: str) -> int | None:
    """只读打开 SQLite 并统计行数；库不存在或打不开返回 ``None``。"""
    if not os.path.isfile(db_path):
        return None
    try:
        con = sqlite3.connect("file:%s?mode=ro" % db_path.replace("?", "%3f"), uri=True)
    except sqlite3.Error:
        return None
    try:
        return int(con.execute('SELECT COUNT(*) FROM "%s"' % table).fetchone()[0])
    except sqlite3.Error:
        return None
    finally:
        con.close()


def diagnose_history(root: str | None = None) -> dict:
    """诊断「新版 Qoder CN 导入数据后仍看不到历史会话」。

    返回结构化结果，供 GUI 展示：

    - ``electron_root`` / ``electron_exists``：新版桌面端 userData 根及其是否存在；
    - ``new_sessions`` / ``new_messages``：新版 ``main.sqlite`` 的会话/消息条数
      （``None`` 表示库不存在或无法读取）；
    - ``import_jobs`` / ``import_history``：应用内导入账本记录数（``None`` 同上）；
    - ``legacy_db_exists`` / ``legacy_sessions`` / ``legacy_messages``：旧版
      ``~/.qoder-cn/shared_client/cache/db/local.db`` 的存在性与会话/消息条数；
    - ``cn_migration_done``：旧版侧 ``.cn_migration`` 交接标记是否已落盘
      （落盘表示该批文件「已迁移过」，重复导入不再生效）；
    - ``current_uid`` / ``legacy_uids``：当前登录 UID（确定式解析）与旧版记忆目录下
      出现过的账号 UID 列表，用于检出「旧数据挂在旧账号下」这一硬约束；
    - ``findings``：可直接展示给人看的结论列表。
    """
    base = root or os.path.expanduser("~")
    ed = detect_electron_root(base)
    qoder_paths = detect_qoder_root(os.path.join(base, ".qoder-cn"))
    legacy_db = os.path.join(qoder_paths.shared, "cache", "db", "local.db")
    cn_mig = os.path.join(qoder_paths.shared, ".cn_migration")

    new_sessions = _count_rows(os.path.join(ed, "main.sqlite"), "chat_sessions")
    new_messages = _count_rows(os.path.join(ed, "main.sqlite"), "chat_session_messages")
    import_jobs = _count_rows(os.path.join(ed, "sessionMigration.sqlite"), "import_job")
    import_history = _count_rows(os.path.join(ed, "sessionMigration.sqlite"), "import_history")
    legacy_sessions = _count_rows(legacy_db, "chat_session")
    legacy_messages = _count_rows(legacy_db, "chat_message")

    cn_migration_done = False
    if os.path.isdir(cn_mig):
        for _dp, _dns, _fns in os.walk(cn_mig):
            if any(f.endswith(".done") for f in _fns):
                cn_migration_done = True
                break

    findings: list[str] = []
    electron_exists = os.path.isdir(ed)
    if not electron_exists:
        findings.append(
            "未发现新版 Qoder CN 桌面端数据目录（%s）：本机可能只装了旧版 IDE/插件。" % ed
        )
    else:
        if new_sessions == 0:
            findings.append(
                "新版会话库 main.sqlite 的 chat_sessions 表为空 —— 这正是新版界面里"
                "看不到历史会话的直接原因。"
            )
        elif new_sessions:
            findings.append("新版会话库 main.sqlite 已有 %d 个会话。" % new_sessions)
        if import_jobs is not None and import_history is not None and import_jobs == 0 and import_history == 0:
            findings.append(
                "应用内「导入旧版数据」账本（sessionMigration.sqlite）为空 —— 导入流程"
                "从未成功落库，因此重复点击导入也不会出现历史会话。"
            )
        if cn_migration_done:
            findings.append(
                "旧版侧 .cn_migration 交接标记已落盘 —— 产品把该批文件视为「已迁移」，"
                "重复触发导入时不再重新搬运；需要迁移账本与交接标记一并重置才可能重跑。"
            )
    if legacy_sessions:
        findings.append(
            "旧版会话库 local.db 中存有 %d 个会话 / %d 条消息。⚠️ 其**容器是标准 SQLite"
            "（未整库加密）**，但 ``chat_message.content`` / ``chat_record.question``"
            " 为**列级密文**——会话正文取不出，故本工具只能整库搬运，无法转成其它软件"
            "的原生格式。" % (legacy_sessions, legacy_messages or 0)
        )
    elif legacy_sessions == 0:
        findings.append("旧版会话库 local.db 的 chat_session 表为空（无历史会话可迁移）。")
    else:
        findings.append("未找到旧版会话库 local.db（%s）。" % legacy_db)

    if not findings:
        findings.append("未发现异常。")

    # 跨账号硬约束：旧库数据全挂在**旧账号 ID** 下。任何「按当前登录账号过滤」的官方
    # 导入入口（设置里的「导入旧版数据」）用新账号查旧库都会得到 0 条可导入，
    # 点下去自然没反应 —— 这不是 bug，是账号体系变更的必然结果。
    cur_uid = detect_current_uid(qoder_paths.root, qoder_paths.shared)
    legacy_uids = detect_memory_uids(qoder_paths.root, qoder_paths.shared)
    if cur_uid and legacy_uids:
        head = cur_uid.split("-")[0][:8].lower()
        known = {u.split("-")[0][:8].lower() for u in legacy_uids}
        if head not in known:
            findings.append(
                "旧版数据挂在**其它账号**目录下（%s），与当前登录账号（%s）不同 —— "
                "任何「按当前登录账号过滤」的官方导入入口都查不到可导入项，"
                "这正是「设置里点导入旧版数据没反应」的直接原因。本工具的按路径整库"
                "搬运不受账号体系影响（包里就是原路径原字节）。"
                % ("、".join(legacy_uids), cur_uid)
            )

    return {
        "electron_root": ed,
        "electron_exists": electron_exists,
        "new_sessions": new_sessions,
        "new_messages": new_messages,
        "import_jobs": import_jobs,
        "import_history": import_history,
        "legacy_db": legacy_db,
        "legacy_sessions": legacy_sessions,
        "legacy_messages": legacy_messages,
        "cn_migration_done": cn_migration_done,
        "current_uid": cur_uid,
        "legacy_uids": legacy_uids,
        "findings": findings,
    }



@register
class QoderAdapter(BaseAdapter):
    name = "qoder"
    display_name = "Qoder"

    #: Qoder 专属压缩经验系数（档位 -> 类别 -> 压缩后/源 占比），按本机真实备份
    #: 反推并校准，单独维护、不与其他工具混用。
    #:
    #: 分类归属（与 ``compress_estimate`` 的扩展名集合对应）：
    #:   - db     : ``.db`` / ``.sqlite`` / ``.sqlite3``（SQLite；含向量等已编码 blob，
    #:              DEFLATE 仅再压掉约 40%，实测 ≈0.59）
    #:   - struct : ``.zap`` / ``.bolt``（向量索引；非通用压缩格式，DEFLATE 仍可压约一半，
    #:              实测 ≈0.46）
    #:   - text   : ``.txt``/``.py``/``.jsonl``/``.log``/``.yaml`` 等高度可压源码文本
    #:              （实测 .txt≈0.10、.jsonl≈0.21）
    #:   - binary : 图片/音视频/压缩包/可执行等通用已压缩或二进制（≈0.99，几乎压不动）
    #:   - other  : 未归类的其余（如 ``.json``/``.md`` 实为含 base64 的向量/附件快照，
    #:              不可压，归此类；实测 .json≈0.83、.md≈0.67）
    #: 注意：db 系数随 SQLite 内部存储内容浮动，仅代表当前 Qoder 数据；其他工具若存明文
    #: 为主可压到剩 ~0.27，届时在此单独调整即可。
    COMPRESS_RATIO: dict[int, dict[str, float]] = {
        1: {  # 快速
            "text": 0.17,
            "db": 0.61,
            "struct": 0.48,
            "binary": 0.99,
            "other": 0.17,
        },
        6: {  # 正常（推荐）
            "text": 0.15,
            "db": 0.59,
            "struct": 0.46,
            "binary": 0.99,
            "other": 0.13,
        },
    }

    def detect_root(self) -> str | None:
        """探测 Qoder 数据根的公共根（用户主目录 ``~``）。与 CodeBuddy 统一：数据目录取 ``~``，``.qoder-cn`` 是其下的一个根。"""
        return os.path.expanduser("~")

    def build_default_root(self) -> str:
        """未探测到时的默认（建议）数据目录：用户主目录 ``~``。"""
        return os.path.expanduser("~")

    def detect_data_roots(self, root: str | None = None) -> list[dict]:
        """返回 Qoder 在 ``root`` 下的各数据根目录信息。

        覆盖：旧版 IDE(``~/.qoder-cn``) / 新版桌面端 userData / 本地工作区临时目录 /
        国际版 Qoder / **前 Lingma 世代(``~/.lingma``)** / **独立 IDE 配置
        (``%APPDATA%/QoderCN``)**。后两者的存在性随机器与版本变化（工作电脑上有、
        本机没有），一律**如实探测、缺失即显示未找到**，不做任何推断。
        供 GUI 识别状态区展示，不改变单路径数据目录模型。
        """
        base = root or os.path.expanduser("~")
        qoder_root = os.path.join(base, ".qoder-cn")
        ed = detect_electron_root(base)
        ll = detect_legacy_local_root(base)

        # 旧版侧备注：会话数（只读统计，失败则不显示）
        legacy_note = ""
        paths = detect_qoder_root(qoder_root)
        n_sessions = _count_rows(os.path.join(paths.shared, "cache", "db", "local.db"), "chat_session")
        if n_sessions:
            legacy_note = "（旧版会话库 %d 个会话）" % n_sessions

        new_note = ""
        new_sessions = _count_rows(os.path.join(ed, "main.sqlite"), "chat_sessions")
        if new_sessions is not None:
            new_note = "（新版会话库 %d 个会话）" % new_sessions

        candidates = [
            (".qoder-cn", qoder_root, legacy_note),
            (os.path.relpath(ed, base), ed, new_note),
            (os.path.relpath(ll, base), ll, "（本地工作区临时文件）"),
            (os.path.relpath(detect_cn_ide_root(base), base),
             detect_cn_ide_root(base), "（独立 IDE 配置，不列入备份）"),
            (os.path.relpath(detect_lingma_root(base), base),
             detect_lingma_root(base), "（前 Lingma 世代，不列入备份）"),
            (os.path.relpath(detect_intl_electron_root(base), base),
             detect_intl_electron_root(base), "（国际版 Qoder，不列入备份）"),
        ]
        roots = []
        for rel, abs_path, note in candidates:
            roots.append(
                {
                    "rel": rel,
                    "exists": os.path.isdir(abs_path),
                    "note": note,
                }
            )
        return roots

    def build_items(self, root_dir: str | None = None, current_uid: str | None = None) -> list[BackupItem]:
        """构造 Qoder 备份条目（旧版 IDE 数据 + 新版桌面端数据）。

        ``root_dir`` 为公共根（用户主目录 ``~``，与其它工具统一）；Qoder 实际数据在
        ``<root_dir>/.qoder-cn`` 与 ``<root_dir>`` 下的 ``%APPDATA%`` 中，路径信息不失真。
        """
        base = root_dir or os.path.expanduser("~")
        paths = detect_qoder_root(os.path.join(base, ".qoder-cn"))
        items = build_items(paths, current_uid)
        items.extend(
            build_electron_items(
                base,
                electron_root=detect_electron_root(base),
                legacy_local_root=detect_legacy_local_root(base),
            )
        )
        return items

    def export_transform_paths(self) -> "Sequence[str] | None":
        """导出时需脱敏的条目：MCP 配置与旧版身份/令牌文件（均可能含明文凭证）。

        采用**后缀匹配**（core 按 ``rel.endswith("/" + 片段)`` 判定）。
        身份族（``cache/`` 下）**一律带目录前缀**写，避免 ``user`` / ``id`` 这类
        通用文件名误命中归档里同名的其它文件。
        新版 ``.qoder-app-status.json``、``installation_id`` 属个人信息/机器标识，
        本适配器**不建条目**、不会进包，此处列出仅作兜底（条目若日后新增即自动生效）。
        """
        return [
            "mcp.json",
            "mcp-router.json",
            "cache/machine_token.json",
            "cache/policy",
            "cache/user",
            "cache/id",
            "cache/status.json",
            "cache/app-config.json",
            ".qoder-app-status.json",
            "installation_id",
        ]

    #: 字段名出现这些片段即视为「敏感凭证」，导出时脱敏
    #: 注意 ``api-key``：MCP 配置里 HTTP header 常写作 ``X-Api-Key``（连字符而非下划线），
    #: 只列 ``apikey`` / ``api_key`` 会漏掉它 —— 这是实测踩到过的漏点。
    SENSITIVE_HINTS = (
        "apikey", "api_key", "api-key", "token", "secret", "password", "passwd",
        "accesskey", "access_key", "privatekey", "private_key",
        "credential", "auth",
    )

    @staticmethod
    def _looks_sensitive(key: str) -> bool:
        k = key.lower()
        return any(hint in k for hint in QoderAdapter.SENSITIVE_HINTS)

    def export_transform(self) -> "Callable[[str, bytes], bytes] | None":
        """把配置文件里「名称像敏感凭证」的字段值替换为占位符 ``***REDACTED***``。

        与 CodeBuddy 同型，另加两条 Qoder 实测必要的规则：

        - **``headers`` 对象内的所有字符串值一律脱敏** —— MCP 配置把密钥放在
          ``headers: {"X-Api-Key": "…"}`` 里，键名形态不固定，逐名匹配不可靠；
        - 键名片段匹配补 ``api-key``（``X-Api-Key`` 是连字符写法）。

        环境变量引用（``${ENV_VAR}``）不含明文、原样保留；非 JSON 内容原样返回
        （``policy`` 是 ``{"timestamp":…}``、``installation_id`` 是裸字符串，
        都不会被改坏）；无改动时返回原字节，保证恢复后配置结构完整。
        """
        REDACTED = "***REDACTED***"

        def _transform(rel_path: str, source: bytes) -> bytes:
            text = source.decode("utf-8", "replace")
            try:
                data = json.loads(text)
            except (ValueError, UnicodeDecodeError):
                return source  # 非 JSON，原样保留
            changed = False

            def _is_env_ref(v: str) -> bool:
                return v.startswith("${") and v.endswith("}")

            def _scrub(obj):
                nonlocal changed
                if isinstance(obj, dict):
                    for k, v in obj.items():
                        if str(k).lower() == "headers" and isinstance(v, dict):
                            # header 内的值全是凭证（键名形态不固定），一律脱敏
                            for hk, hv in v.items():
                                if isinstance(hv, str) and hv and not _is_env_ref(hv):
                                    v[hk] = REDACTED
                                    changed = True
                                else:
                                    _scrub(hv)
                            continue
                        if QoderAdapter._looks_sensitive(str(k)) and isinstance(v, str) and v:
                            if not _is_env_ref(v):
                                obj[k] = REDACTED
                                changed = True
                        else:
                            _scrub(v)
                elif isinstance(obj, list):
                    for item in obj:
                        _scrub(item)

            _scrub(data)
            if not changed:
                return source
            return json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")

        return _transform

    def match_structure(self, names: "Sequence[str]") -> "tuple[bool, list[str]]":
        """结构指纹：归档内出现 ``.qoder-cn/`` 或新版 ``com.qodercn.app.stable/main.sqlite``。"""
        for n in names or []:
            j = n.replace("\\", "/")
            if j.startswith(".qoder-cn/") or "/.qoder-cn/" in j:
                return True, []
            if j.endswith("com.qodercn.app.stable/main.sqlite"):
                return True, []
        return False, ["未找到 .qoder-cn/ 或 com.qodercn.app.stable/main.sqlite"]
