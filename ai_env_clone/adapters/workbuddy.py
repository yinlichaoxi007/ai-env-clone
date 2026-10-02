"""
WorkBuddy 适配器（自包含，不依赖任何遗留兼容层）。

本文件是 WorkBuddy 备份逻辑的唯一事实来源：
- 目录探测（``detect_root`` / ``detect_data_roots``）
- 备份条目构造（``build_items``）
- 通过统一的 :class:`~ai_env_clone.adapters.base.BaseAdapter` 接口暴露给 GUI / CLI

数据布局（2026-09-22 在本机 Windows 实测确认；WorkBuddy 为 Electron 桌面包，
用户数据统一落在 ``~/.workbuddy``，而非 ``%APPDATA%``）：

- 用户数据根 ``~/.workbuddy/``：
  - ``workbuddy.db``（SQLite，含 ``-wal``/``-shm``）
    核心索引库。表：``sessions``（会话元数据：id/cwd/user_id/title/status/
    created_at/updated_at/model/mode/…）、``workspaces``（工作区路径 → 最近打开时间）、
    ``session_usage``（用量）、``automations`` / ``automation_runs``（自动化）、
    ``buddy_snapshots``。**核心数据**（不含它，界面里看不到会话列表），默认勾选。
  - ``projects/<工作区编码>/<会话 id>.jsonl`` + ``<会话 id>.meta.json``
    会话**完整事件流**（明文 JSONL，每行一条事件：``message`` / ``reasoning`` /
    ``function_call`` / ``function_call_result`` / ``file-history-snapshot`` /
    ``ai-title``）。这是真正不可重建的对话内容，**核心数据**，默认勾选。
  - ``workspace/sessions/<会话 id>/``
    会话级工作区文件快照（会话运行期落盘的中间文件）。默认勾选。
  - ``memory/<用户 id>_memory.md``
    用户级记忆（跨会话）。**核心数据**，默认勾选。
  - ``MEMORY.md``
    用户级**本地**长期记忆（用户可编辑，工具每会话自动加载注入）。与
    ``memory/<用户 id>_memory.md`` 是**两套不同机制**：后者是服务端管理的记忆画像，
    前者是本地落盘的硬性规则/约定。**核心数据**，默认勾选。
    ⚠️ 该文件**并非每台机器都有**（只有被显式写入过才有）⇒ 按存在性探测，
    缺失只显示「未找到」，不报错、不推断数据丢失。
  - ``SOUL.md`` / ``IDENTITY.md`` / ``USER.md``
    用户级人格 / 身份 / 画像文件（用户可编辑、工具自动加载）。默认勾选。
  - ``storage/user-<uid>[-personal]/``
    用户级键值存储（``global`` / ``scoped`` 两级，含自动化、偏好等）。默认勾选。
  - ``file-history/<会话 id>/``、``changes-index`` + ``changes-detail/``
    文件历史快照与「会话改动了哪些文件」的索引/明细，与会话强关联。默认勾选。
  - ``tasks/``、``teams/``、``plans/``
    任务、团队与计划定义。默认不勾（可重建的编排记录）。
  - ``artifact-index/``、``assistant-display/``
    产物索引与助手展示态。属索引类，默认不勾。
  - ``format_presets/``
    用户自定义格式预设（如「单词表批量打印.json」）。用户手工创建、不可重建，默认勾选。
  - ``skills/``
    用户自定义技能。默认勾选（「还原后立刻能开工」类）。
  - ``connectors/``
    连接器账号/配置。默认不勾。
  - ``settings.json``
    用户设置。默认勾选（还原后偏好立即生效）。
  - ``mcp.json`` / ``mcp-approvals.json``
    MCP 配置与工具授权记录。默认不勾。
  - ``models.json``
    自定义模型配置。默认勾选（还原后模型清单立即可用）；
    **可能含明文 apiKey 等凭证**，导出时脱敏、恢复后需手动补填。
  - ``plugins/``、``connectors-marketplace/``
    已安装插件与市场缓存，体积大、可重装，默认不勾。
  - ``local_storage/``
    前端本地存储。默认不勾。
  - ``binaries/``（自带 Python/Node/PortableGit，≈390MB）、``logs/``（≈190MB）、
    ``cache/``、``traces/``、``app/``（Electron 运行态）、``security/``、
    ``audit-log/``、``pending-telemetry/``、``shell-snapshots/``、``device-id``、
    ``user-state.json``、``workspace-state.json``、``last-launch.json``、
    ``edge-sync-mapping*.db``、``*.port``、``*.done`` 标记等：
    程序自身的运行时/缓存/日志/遥测，**与用户数据无关，不列入备份选项**。

- 桌面端附属目录（运行态，不列入备份）：``%APPDATA%/WorkBuddy``（Electron userData，
  本机实测为空壳）、``%LOCALAPPDATA%/WorkBuddy/logs``。
- ``~/WorkBuddy/<时间戳>/`` 是 WorkBuddy 为「playground / 无工程会话」自动创建的工作
  目录（会话 ``sessions`` 表里的 ``cwd`` 就指向这里）。它**属于用户工程内容**，不是工具
  元数据，本适配器不打包，仅在识别状态区提示。

所有条目 path 均在公共根 ``~`` 之下，归档按相对路径落回原位，恢复干净。
这与 Qoder / CodeBuddy / Reasonix / DSH 适配器「公共根 = 用户主目录」的约定一致。

备份哲学（统一标准，用户 2026-10-01 定策后更新）：默认勾选「无法从零重复创建、或缺失后
需重新逐项配置」的两类——① 会话、记忆、规则；② 设置、skill、灵感、自定义模型配置
（后四者是为了「还原后立刻能开工」）。默认不勾**重新获取成本低**的——插件、扩展、MCP、
索引、编排记录、其他用户数据；程序自身的本地缓存、运行态记录、日志不列入备份选项。

新增其它工具时，仿照本文件新建 ``ai_env_clone/adapters/<tool>.py`` 即可，
用 ``@register`` 装饰类，无需改动任何入口代码。
"""

from __future__ import annotations

import json
import os
import sys

from ..core import ORIGIN_NOTE_CONVERSATION, ORIGIN_NOTE_SESSION_DB, BackupItem
from .base import BaseAdapter, register


def _home() -> str:
    """当前用户主目录（WorkBuddy 用户数据的公共根）。"""
    return os.path.expanduser("~")


def _workbuddy_home() -> str:
    """WorkBuddy 用户数据根目录：``~/.workbuddy``。

    （WorkBuddy 不提供环境变量覆盖，路径固定为用户主目录下的 ``.workbuddy``；
    如需改变，可在 GUI「数据目录」中手动指定公共根，条目路径会随之偏移。）
    """
    return os.path.join(_home(), ".workbuddy")


def _appdata(name: str) -> str:
    """``%APPDATA%/<name>``（Windows）/ 对应平台的应用配置目录。"""
    if sys.platform.startswith("win"):
        base = os.environ.get("APPDATA", os.path.join(_home(), "AppData", "Roaming"))
    elif sys.platform == "darwin":
        base = os.path.join(_home(), "Library", "Application Support")
    else:
        base = os.path.join(_home(), ".config")
    return os.path.join(base, name)


def _localappdata(name: str) -> str:
    """``%LOCALAPPDATA%/<name>``（Windows）/ 对应平台的本地数据目录。"""
    if sys.platform.startswith("win"):
        base = os.environ.get("LOCALAPPDATA", os.path.join(_home(), "AppData", "Local"))
    elif sys.platform == "darwin":
        base = os.path.join(_home(), "Library", "Application Support")
    else:
        base = os.path.join(_home(), ".local", "share")
    return os.path.join(base, name)


def _db_with_companions(db_path: str) -> list[str]:
    """返回 SQLite 主库及其 -wal/-shm 配套（存在的才返回）。"""
    return [p for p in (db_path, db_path + "-wal", db_path + "-shm") if os.path.exists(p)]


def build_items(root: str | None = None, wb_home: str | None = None) -> list[BackupItem]:
    """构造 WorkBuddy 备份条目清单。

    :param root: 公共根（用户主目录）。``None`` 时自动取 ``~``。
    :param wb_home: WorkBuddy 数据根 ``~/.workbuddy``；``None`` 时自动探测。
    """
    home = root or _home()
    wb = wb_home or _workbuddy_home()

    items: list[BackupItem] = []

    # 1) 核心索引库 workbuddy.db（会话列表 / 工作区 / 自动化等）。
    #    主库 + -wal/-shm 各生成唯一 key，GUI 按前缀聚合为一行。
    db_main = os.path.join(wb, "workbuddy.db")
    for db_file in _db_with_companions(db_main):
        suffix = ""
        if db_file.endswith("-wal"):
            suffix = ":wal"
        elif db_file.endswith("-shm"):
            suffix = ":shm"
        items.append(
            BackupItem(
                key="session_db" + suffix,
                label="会话索引数据库（workbuddy.db）",
                path=db_file,
                uid=None,
                description="WorkBuddy 会话/工作区索引库（sessions / workspaces / automations 等表）。"
                            "缺它界面里看不到会话列表，核心数据，默认勾选。",
                recommended=True,
                carries_origin=ORIGIN_NOTE_SESSION_DB,
            )
        )

    # 2) 会话完整事件流 projects/（明文 JSONL + meta）。
    items.append(
        BackupItem(
            key="projects",
            label="会话事件流（projects/）",
            path=os.path.join(wb, "projects"),
            uid=None,
            description="各工作区会话的完整事件流（<会话 id>.jsonl：message / reasoning / "
                        "function_call / function_call_result 等）。真正的对话内容，核心数据，默认勾选。",
            recommended=True,
            carries_origin=ORIGIN_NOTE_CONVERSATION,
        )
    )

    # 3) 会话级工作区文件快照。
    items.append(
        BackupItem(
            key="workspace_sessions",
            label="会话工作区快照（workspace/sessions/）",
            path=os.path.join(wb, "workspace", "sessions"),
            uid=None,
            description="会话运行期在会话工作区内落盘的文件快照（用于会话内文件回溯）。默认勾选。",
            recommended=True,
            carries_origin=ORIGIN_NOTE_CONVERSATION,
        )
    )

    # 4) 用户级记忆。
    items.append(
        BackupItem(
            key="memory",
            label="用户级记忆（memory/）",
            path=os.path.join(wb, "memory"),
            uid=None,
            description="WorkBuddy 用户级记忆（memory/<用户 id>_memory.md，跨会话累积）。核心，默认勾选。",
            recommended=True,
        )
    )

    # 4b) 用户级「本地」长期记忆 MEMORY.md。
    #     与 memory/ 不是同一机制：memory/<uid>_memory.md 是服务端管理的记忆画像，
    #     本文件是本地落盘的硬性规则/约定，且被工具每会话自动加载注入上下文。
    #     无法从零重建 ⇒ 默认勾选。并非每台机器都有（未被写入过则不存在）。
    items.append(
        BackupItem(
            key="memory_md",
            label="用户级本地记忆（MEMORY.md）",
            path=os.path.join(wb, "MEMORY.md"),
            uid=None,
            description="用户级本地长期记忆（MEMORY.md，工具每会话自动加载）。"
            "与 memory/ 是两套机制，非每台机器都有。核心，默认勾选。",
            recommended=True,
        )
    )

    # 5) 用户级人格/身份/画像（SOUL.md / IDENTITY.md / USER.md）。
    #    三个文件各一条（同前缀 user_profile，GUI 聚合成一行），任一存在即视为找到。
    for fname in ("SOUL.md", "IDENTITY.md", "USER.md"):
        items.append(
            BackupItem(
                key="user_profile:%s" % fname,
                label="用户级人格与画像（SOUL/IDENTITY/USER.md）",
                path=os.path.join(wb, fname),
                uid=None,
                description="用户级人格/身份/画像文件（%s），工具启动时自动加载。默认勾选。" % fname,
                recommended=True,
            )
        )

    # 6) 用户级键值存储。
    items.append(
        BackupItem(
            key="storage",
            label="用户级存储（storage/）",
            path=os.path.join(wb, "storage"),
            uid=None,
            description="用户级键值存储（storage/user-<uid>[-personal]/ 下的 global 与 scoped）。默认勾选。",
            recommended=True,
        )
    )

    # 7) 文件历史与会话改动索引/明细。
    items.append(
        BackupItem(
            key="file_history",
            label="文件历史（file-history/）",
            path=os.path.join(wb, "file-history"),
            uid=None,
            description="会话内被修改文件的历史快照（可按会话回溯）。默认勾选。",
            recommended=True,
        )
    )
    items.append(
        BackupItem(
            key="changes_index",
            label="会话改动索引与明细（changes-*）",
            path=os.path.join(wb, "changes-index"),
            uid=None,
            description="会话改动了哪些文件的索引（changes-index/）。默认勾选。",
            recommended=True,
        )
    )
    items.append(
        BackupItem(
            key="changes_detail",
            label="会话改动索引与明细（changes-*）",
            path=os.path.join(wb, "changes-detail"),
            uid=None,
            description="会话改动明细内容（changes-detail/）。默认勾选。",
            recommended=True,
        )
    )

    # 8) 用户自定义格式预设（用户手工创建，不可重建）。
    items.append(
        BackupItem(
            key="format_presets",
            label="自定义格式预设（format_presets/）",
            path=os.path.join(wb, "format_presets"),
            uid=None,
            description="用户自定义的格式预设（如「单词表批量打印.json」）。用户手工创建，默认勾选。",
            recommended=True,
        )
    )

    # 9) 任务 / 团队（编排记录，可重建）。
    items.append(
        BackupItem(
            key="tasks",
            label="任务与团队（tasks/ teams/）",
            path=os.path.join(wb, "tasks"),
            uid=None,
            description="代理任务与团队/专家编排记录（tasks/）。可从零重建，默认不勾。",
            recommended=False,
        )
    )
    items.append(
        BackupItem(
            key="teams",
            label="任务与团队（tasks/ teams/）",
            path=os.path.join(wb, "teams"),
            uid=None,
            description="团队/专家编排定义（teams/）。可从零重建，默认不勾。",
            recommended=False,
        )
    )

    # 10) 索引类。
    items.append(
        BackupItem(
            key="artifact_index",
            label="产物索引（artifact-index/）",
            path=os.path.join(wb, "artifact-index"),
            uid=None,
            description="会话产物索引（artifact-index/）。属索引类，可重建，默认不勾。",
            recommended=False,
        )
    )
    items.append(
        BackupItem(
            key="assistant_display",
            label="助手展示态（assistant-display/）",
            path=os.path.join(wb, "assistant-display"),
            uid=None,
            description="助手展示态缓存（assistant-display/）。属运行态，默认不勾。",
            recommended=False,
        )
    )

    # 11) 用户自定义技能。默认勾选（用户 2026-10-01 定策：skill 属「还原后立刻能开工」类）。
    items.append(
        BackupItem(
            key="skills",
            label="自定义技能（skills/）",
            path=os.path.join(wb, "skills"),
            uid=None,
            description="用户自定义技能目录（skills/）。默认勾选，避免还原后再逐个重建。",
            recommended=True,
        )
    )

    # 12) 连接器。
    items.append(
        BackupItem(
            key="connectors",
            label="连接器（connectors/）",
            path=os.path.join(wb, "connectors"),
            uid=None,
            description="已配置的连接器账号与状态（connectors/）。可重新授权，默认不勾。",
            recommended=False,
        )
    )

    # 13) 设置与 MCP 配置。
    #     ⚠️ 三条**必须给可区分的标签**：它们按 key 前缀（settings / mcp / mcp_approvals）
    #     各占一行，而不是聚合到同一行。原本三条共用「设置与 MCP 配置」这个标签，
    #     在「设置默认勾选、MCP 默认不勾」之后会出现「三行同样文字、勾选态却不同」的
    #     误导性界面，故拆开写清各自是什么文件（用户 2026-10-01 定策后调整）。
    #     设置属「还原后立刻能开工」类 ⇒ 默认勾选；MCP 与授权记录属「重配成本低」类
    #     ⇒ 默认不勾。
    items.append(
        BackupItem(
            key="settings",
            label="用户设置（settings.json）",
            path=os.path.join(wb, "settings.json"),
            uid=None,
            description="用户设置（settings.json）。默认勾选，还原后偏好立即生效、无需重新配置。",
            recommended=True,
        )
    )
    items.append(
        BackupItem(
            key="mcp",
            label="MCP 配置（mcp.json）",
            path=os.path.join(wb, "mcp.json"),
            uid=None,
            description="MCP 服务器配置（mcp.json）。按需勾选：服务器需在本机重新可用后才有意义。",
            recommended=False,
        )
    )
    items.append(
        BackupItem(
            key="mcp_approvals",
            label="MCP 授权记录（mcp-approvals.json）",
            path=os.path.join(wb, "mcp-approvals.json"),
            uid=None,
            description="MCP 工具授权记录（mcp-approvals.json）。可重建，按需勾选。",
            recommended=False,
        )
    )

    # 14) 自定义模型配置（可能含明文 apiKey，导出时脱敏）。
    #     默认勾选（用户 2026-10-01 定策：自定义模型配置属「还原后立刻能开工」类）。
    items.append(
        BackupItem(
            key="models",
            label="自定义模型配置（models.json）",
            path=os.path.join(wb, "models.json"),
            uid=None,
            description="WorkBuddy 自定义模型配置（models.json，可能含 apiKey/令牌等敏感凭证）。"
                        "默认勾选，还原后模型清单立即可用；但备份时已脱敏（敏感凭证替换为占位符），"
                        "恢复后需在目标机手动补填。",
            recommended=True,
            sensitive=True,
        )
    )

    # 15) 插件与市场缓存（体积大、可重装）。
    items.append(
        BackupItem(
            key="plugins",
            label="插件与市场缓存（plugins/）",
            path=os.path.join(wb, "plugins"),
            uid=None,
            description="已安装插件（plugins/，体积较大，重装可恢复）。默认不勾。",
            recommended=False,
        )
    )
    items.append(
        BackupItem(
            key="connectors_marketplace",
            label="插件与市场缓存（connectors-marketplace/）",
            path=os.path.join(wb, "connectors-marketplace"),
            uid=None,
            description="连接器市场清单缓存（connectors-marketplace/，体积较大）。默认不勾。",
            recommended=False,
        )
    )

    # 16) 前端本地存储。
    items.append(
        BackupItem(
            key="local_storage",
            label="前端本地存储（local_storage/）",
            path=os.path.join(wb, "local_storage"),
            uid=None,
            description="渲染进程的本地存储条目（local_storage/）。默认不勾。",
            recommended=False,
        )
    )

    return items


@register
class WorkBuddyAdapter(BaseAdapter):
    name = "workbuddy"
    display_name = "WorkBuddy"

    #: WorkBuddy 专属压缩经验系数（档位 -> 类别 -> 压缩后/源 占比）。
    #:
    #: 备份数据构成（据此归类）：
    #:   - db     : ``workbuddy.db``（SQLite；DEFLATE 约压掉 40%，≈0.6）
    #:   - text   : ``projects/*.jsonl``、``memory/*.md``、``SOUL.md`` 等明文文本
    #:              （JSONL 含大量重复结构与中英文本，实测可压到 ≈0.12）
    #:   - struct : ``storage/*``、``changes-index/*.json`` 结构化 JSON（≈0.35）
    #:   - binary : ``plugins/`` 内含已编译/二进制（≈0.99）
    #:   - other  : ``file-history/`` 快照与杂项（≈0.3）
    #: 注：首次真实备份后会自动校准写入本工具校准文件，此处仅为回退兜底。
    COMPRESS_RATIO: dict[int, dict[str, float]] = {
        1: {  # 快速
            "text": 0.15,
            "db": 0.62,
            "struct": 0.4,
            "binary": 0.99,
            "other": 0.35,
        },
        6: {  # 正常（推荐）
            "text": 0.12,
            "db": 0.6,
            "struct": 0.35,
            "binary": 0.99,
            "other": 0.3,
        },
    }

    def detect_root(self) -> str | None:
        """探测 WorkBuddy 数据公共根（用户主目录 ``~``）。始终返回 ``~``。"""
        return _home()

    def build_default_root(self) -> str:
        """未探测到时的默认（建议）数据目录：用户主目录 ``~``。"""
        return _home()

    def detect_data_roots(self, root: str | None = None) -> list[dict]:
        """返回 WorkBuddy 在 ``root``（用户主目录）下的各数据根目录信息。

        与其他适配器粒度一致：每个「数据根」一行。WorkBuddy 的用户数据根是
        ``.workbuddy``，其中 ``projects/`` 单独再报一行并带上「N 个工程 / M 个会话」
        计数（会话正文都在这里，缺它界面列表就是空的）；另有桌面端 ``AppData`` 下的
        两个运行态目录（标注不列入备份）。
        """
        home = root or _home()
        wb = _workbuddy_home()
        projects = os.path.join(wb, "projects")

        # 会话计数：工程目录数 + *.jsonl 会话数（只读统计，失败则留 0）
        n_ws = n_sess = 0
        if os.path.isdir(projects):
            try:
                for name in os.listdir(projects):
                    if os.path.isdir(os.path.join(projects, name)):
                        n_ws += 1
                for _dp, _dns, fns in os.walk(projects):
                    n_sess += sum(1 for f in fns if f.endswith(".jsonl"))
            except OSError:
                pass

        candidates = [
            (os.path.relpath(wb, home), wb, ""),
            (os.path.relpath(projects, home), projects,
             "（含 %d 个工程 / %d 个会话）" % (n_ws, n_sess) if (n_ws or n_sess) else ""),
            (os.path.relpath(_appdata("WorkBuddy"), home), _appdata("WorkBuddy"), "（桌面运行态，不列入备份）"),
            (os.path.relpath(_localappdata("WorkBuddy"), home), _localappdata("WorkBuddy"), "（日志，不列入备份）"),
        ]
        roots = []
        for rel, abs_path, rc_note in candidates:
            roots.append(
                {
                    "rel": rel,
                    "exists": os.path.isdir(abs_path),
                    "note": rc_note,
                }
            )
        # 会话工作区目录（用户工程内容，不打包，仅提示）
        if os.path.isdir(os.path.join(home, "WorkBuddy")):
            roots.append(
                {
                    "rel": "WorkBuddy",
                    "exists": True,
                    "note": "（会话工作区，属工程内容，不列入备份）",
                }
            )
        return roots

    def build_items(self, root_dir: str | None = None, current_uid: str | None = None) -> list[BackupItem]:
        """构造 WorkBuddy 备份条目（``root_dir`` 为公共根 ``~``，可传 None 自动取）。

        ``current_uid`` 为兼容性参数（WorkBuddy 用户级数据为单用户扁平结构，无 UID 拆分），忽略。
        """
        return build_items(root_dir, _workbuddy_home())

    # ------------------------------------------------------------------ #
    # 导出脱敏：models.json 可能含明文敏感凭证
    # ------------------------------------------------------------------ #
    def export_transform_paths(self) -> "Sequence[str] | None":
        """导出前需脱敏的条目（core 按归档内相对路径**后缀**匹配）。

        覆盖实测可能内嵌明文凭证的配置类文件：``models.json``（自定义模型的 apiKey）、
        ``settings.json``、``mcp.json``（MCP 服务器 header 里的 ``X-Api-Key``）、
        ``mcp-approvals.json``、``.connectors-marketplace.meta.json``。

        ⚠️ ``projects/**/*.jsonl`` 是**明文逐条对话**，用户可能在对话里粘贴过密钥、
        口令、内网地址、客户数据 —— 这类内容**无法靠字段名脱敏**（键名不固定，且就
        藏在普通正文里），故不在本清单内。导出前应让用户明确知情：会话转录包含全部
        对话原文。
        """
        return [
            "models.json",
            "settings.json",
            "mcp.json",
            "mcp-approvals.json",
            ".connectors-marketplace.meta.json",
        ]

    #: 字段名出现这些片段即视为「敏感凭证」，导出时脱敏
    #: 注意 ``api-key``：MCP 配置的 HTTP header 常写作 ``X-Api-Key``（连字符写法），
    #: 只列 ``apikey`` / ``api_key`` 会漏掉它。
    SENSITIVE_HINTS = (
        "apikey", "api_key", "api-key", "token", "secret", "password", "passwd",
        "accesskey", "access_key", "privatekey", "private_key",
        "credential", "auth",
    )

    @staticmethod
    def _looks_sensitive(key: str) -> bool:
        k = key.lower()
        return any(hint in k for hint in WorkBuddyAdapter.SENSITIVE_HINTS)

    def export_transform(self) -> "Callable[[str, bytes], bytes] | None":
        """把上述配置文件中名称像敏感凭证的字段值替换为占位符 ``***REDACTED***``。

        额外规则：**``headers`` 对象内的所有字符串值一律脱敏** —— MCP 配置把密钥放在
        ``headers: {"X-Api-Key": "…"}`` 里，键名形态不固定，逐名匹配不可靠。

        环境变量引用（``${ENV_VAR}``）本身不含明文，原样保留。非 JSON 内容原样保留。
        """
        REDACTED = "***REDACTED***"

        def _transform(rel_path: str, source: bytes) -> bytes:
            text = source.decode("utf-8", "replace")
            try:
                data = json.loads(text)
            except (ValueError, UnicodeDecodeError):
                return source
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
                        if WorkBuddyAdapter._looks_sensitive(str(k)) and isinstance(v, str) and v:
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
        """结构指纹：归档内出现 ``.workbuddy/workbuddy.db`` 或 ``.workbuddy/projects/`` 即认作本工具。"""
        joined = [n.replace("\\", "/") for n in names or []]
        markers = (".workbuddy/workbuddy.db", "/.workbuddy/workbuddy.db")
        hits = [j for j in joined if j.endswith(markers[0]) or markers[1] in j]
        if hits:
            return True, []
        has_projects = any("/.workbuddy/projects/" in j or j.startswith(".workbuddy/projects/") for j in joined)
        if has_projects:
            return True, []
        return False, ["未找到 .workbuddy/workbuddy.db 或 .workbuddy/projects/"]
