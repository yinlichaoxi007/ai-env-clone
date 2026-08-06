"""
Qoder 适配器（自包含，不依赖任何遗留兼容层）。

本文件是 Qoder 备份逻辑的唯一事实来源：
- 目录探测（``QoderPaths`` / ``detect_qoder_root`` / UID 探测）
- 备份条目构造（``build_items``）
- 通过统一的 :class:`~ai_env_clone.adapters.base.BaseAdapter` 接口暴露给 GUI / CLI

新增其它工具时，仿照本文件新建 ``ai_env_clone/adapters/<tool>.py`` 即可，
用 ``@register`` 装饰类，无需改动任何入口代码。
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from ..core import BackupItem
from .base import BaseAdapter, register


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


def detect_current_uid(root: str, shared: str) -> str | None:
    """
    自动判定"当前登录用户"的 UID。

    启发式：扫描两处记忆目录下各 UID 子目录，取文件修改时间最新者。
    无法从本地数据文件可靠反解明文 UID（Qoder 以加密 token 存储登录态），
    故用"最近活跃"近似。返回 UID 字符串，无数据则 ``None``。
    """
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
                label="历史会话数据库（SQLite）",
                path=db_file,
                description="全局会话/历史主库及其 -wal/-shm 配套文件，核心数据。建议必选。",
                recommended=True,
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
    if current_uid:
        cur_paths = [
            (os.path.join(root, "memories", current_uid), "root"),
            (os.path.join(shared, "memories", current_uid), "shared"),
        ]
        for mp, tag in cur_paths:
            items.append(
                BackupItem(
                    key="memories_current:" + tag,
                    label="当前用户记忆区",
                    path=mp,
                    uid=current_uid,
                    description="当前登录用户（%s）的记忆（%s 位置）。默认勾选。" % (current_uid, tag),
                    recommended=True,
                )
            )

    # 5) 记忆区：其他用户（默认不勾；GUI 勾选后按 UID 展开多选）
    #    排除当前用户，避免与"当前用户记忆区"重复备份。每个 UID 同样按两处路径拆分唯一 key。
    for uid in all_uids:
        if uid == current_uid:
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

    # 7) 设置（按用户要求默认不勾）
    settings_file = os.path.join(root, "settings.json")
    items.append(
        BackupItem(
            key="settings",
            label="设置（全局/用户级）",
            path=settings_file,
            description="用户全局设置（如主题、自动化开关）。可选。",
            recommended=False,
        )
    )

    return items


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
        """探测 Qoder 数据根目录；未找到返回 None（GUI 提示手动指定）。"""
        p = detect_qoder_root()
        return p.root if p.exists else None

    def build_default_root(self) -> str:
        """未探测到时的默认（建议）数据目录：``~/.qoder-cn``。"""
        return os.path.join(os.path.expanduser("~"), ".qoder-cn")

    def build_items(self, root_dir: str, current_uid: str | None = None) -> list[BackupItem]:
        """构造 Qoder 备份条目（root_dir 为已探测的根目录）。"""
        shared = os.path.join(root_dir, "shared_client")
        if not os.path.isdir(shared):
            shared = os.path.join(root_dir, "sharedclient")
        return build_items(QoderPaths(root=root_dir, shared=shared), current_uid)
