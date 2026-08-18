"""
CodeBuddy 适配器（自包含，不依赖任何遗留兼容层）。

本文件是 CodeBuddy 备份逻辑的唯一事实来源：
- 目录探测（``detect_root``）
- 备份条目构造（``build_items``）
- 通过统一的 :class:`~ai_env_clone.adapters.base.BaseAdapter` 接口暴露给 GUI / CLI

备份范围铁律（用户 2026-08-07 明确）：**只备份不在项目文件夹下的用户级/全局数据**。
CodeBuddy 的数据分两处，本适配器**只处理后者**：

- **项目级**（``<project>/.codebuddy``）：由各工程自己维护的记忆/配置。
  本工具自身也在项目里用 ``.codebuddy/memory/`` 存工作记忆（被 gitignore、且曾误删），
  **绝不纳入备份**，避免污染与误伤。
- **用户级 / 全局级**（跨项目、不在任何工程文件夹下），这才是本适配器备份对象：
  1. 用户级跨项目记忆：``%LOCALAPPDATA%/CodeBuddyExtension/Data/Public/.memories/``
     （即「设置→本地记忆」列表数据源，``.memory-global-config.json`` + ``*.mdc``）。
  2. 用户级规则：``~/.codebuddy/rules/``。
  3. 用户级设置 + skill 本体：``~/.codebuddy/settings.json``（用户级 skill 开关）
     + ``~/.codebuddy/skills-marketplace/``（已下载 skill 本体）。合并为一项，默认不勾。
  4. 用户级 MCP：``~/.codebuddy/mcp.json``（默认不勾）。
  5. 全局 IDE 配置：``~/.codebuddycn/``（``argv.json`` / ``extensions/``）。属「设置」类，
     可从零重复创建，默认不勾。
  6. 灵感（``~/.codebuddy/inspiration/``，按登录用户 UUID 隔离，非项目）与
     专家历史（``~/.codebuddy/expert-history.json``）——均为用户级，默认不勾。
  7. **集中会话/检查点数据**（``%LOCALAPPDATA%/CodeBuddyExtension/Data/<uuid>/CodeBuddyIDE/<uuid>/``）：
     这是 CodeBuddy **跨工程集中存储的会话历史与 AI 回答检查点**（``history/`` 真实对话消息、
     ``check-point/`` 检查点快照、``plan-task/`` 计划任务等），**不是** ``<project>/.codebuddy``
     那种「项目级」数据（后者打包工程自然带着、不进选项）。该目录外层 ``<uuid>`` = 登录用户标识，
     是「用户级」集中存储，**必须备份**。其中 ``history/`` ``check-point/`` ``plan-task/`` 默认勾选；
     工程文件索引缓存 ``file-tree/`` 属程序自身运行态、重新打开工程即重建，与用户数据无关，
     **不列入备份选项**。

所有用户级/全局数据的**公共根是用户主目录 ``~``**，故 ``detect_root`` / ``build_default_root``
均返回 ``~``，各条目 path 均在 ``~`` 之下，归档按相对路径落回原位，恢复干净。

注意区分两类「会话/项目」概念（用户 2026-08-08 纠正）：
- 「项目级」= ``<project>/.codebuddy/``（各工程自带、打包工程即带）→ 不进备份选项。
- 「集中会话」= ``CodeBuddyExtension/Data/<uuid>/CodeBuddyIDE/<uuid>/``（跨工程集中存储于
  用户 AppData，需显式备份）→ **进备份选项**，默认勾选（除工程索引 ``file-tree/``）。

备份哲学（统一标准）：默认勾选无法从零重复创建的——会话、记忆、规则；默认不勾可从零重复
创建的——插件、skill、mcp、扩展、灵感、索引、设置（含全局 argv.json）；程序自身的本地缓存、
运行态记录、日志与用户数据无关，不列入备份选项。

与 Qoder 不同，CodeBuddy 记忆是**单用户扁平结构**，无 UID 拆分（``inspiration/`` 下的
UUID 是登录用户标识，不是项目；``CodeBuddyIDE`` 外层 UUID 同理）。

新增其它工具时，仿照本文件新建 ``ai_env_clone/adapters/<tool>.py`` 即可，
用 ``@register`` 装饰类，无需改动任何入口代码。
"""

from __future__ import annotations

import os
import sys

from ..core import BackupItem
from .base import BaseAdapter, register


def _home() -> str:
    """当前用户主目录（所有用户级/全局 CodeBuddy 数据的公共根）。"""
    return os.path.expanduser("~")


def _codebuddy_extension_root() -> str:
    """返回 CodeBuddyExtension 的安装根目录（跨平台）。

    Windows : ``%LOCALAPPDATA%/CodeBuddyExtension``
    macOS   : ``~/Library/Application Support/CodeBuddyExtension``
    Linux   : ``~/.config/CodeBuddyExtension``

    其下的 ``Data/`` 子目录即为各类集中数据（跨项目记忆、集中会话/检查点等）。
    """
    if sys.platform.startswith("win"):
        base = os.environ.get(
            "LOCALAPPDATA",
            os.path.join(_home(), "AppData", "Local"),
        )
        return os.path.join(base, "CodeBuddyExtension")
    if sys.platform == "darwin":
        return os.path.join(_home(), "Library", "Application Support", "CodeBuddyExtension")
    # Linux / 其它类 Unix
    return os.path.join(_home(), ".config", "CodeBuddyExtension")


def detect_public_memories_root() -> str:
    """用户级跨项目记忆目录（「设置→本地记忆」列表数据源）。"""
    return os.path.join(
        _codebuddy_extension_data_root(), "Public", ".memories"
    )


def detect_user_rules_root() -> str:
    """用户级规则目录（``~/.codebuddy/rules/``）。"""
    return os.path.join(_home(), ".codebuddy", "rules")


def detect_global_root() -> str:
    """全局 IDE 配置根目录（``~/.codebuddycn``）。"""
    return os.path.join(_home(), ".codebuddycn")


def _codebuddy_extension_data_root() -> str:
    """CodeBuddyExtension 的 Data 根：``<extension_root>/Data``（跨平台）。

    其下结构为：``Data/Public/.memories``（跨项目记忆）、``Data/<uuid>/CodeBuddyIDE/<uuid>/``
    （集中会话/检查点，需备份）、``Data/<uuid>/genie-cache``（暂无用，忽略）。
    """
    return os.path.join(_codebuddy_extension_root(), "Data")


def _looks_like_uuid(name: str) -> bool:
    """粗略判断目录名是否像 UUID（8-4-4-4-12 十六进制）。"""
    parts = name.split("-")
    if len(parts) != 5:
        return False
    expect = (8, 4, 4, 4, 12)
    return all(len(p) == e and all(c in "0123456789abcdefABCDEF" for c in p) for p, e in zip(parts, expect))


#: UUID 在界面上截短显示的前缀长度（完整 UUID 太长难看，对齐 Qoder 思路）。
SHORT_UID_LEN = 8


def short_uid(uid: str) -> str:
    """UUID 截短显示：前 ``SHORT_UID_LEN`` 位 + 省略号（不足则原样）。"""
    if not uid:
        return uid
    return uid[:SHORT_UID_LEN] + ("…" if len(uid) > SHORT_UID_LEN else "")


def detect_user_uids() -> list[str]:
    """返回 ``Data/`` 下所有形如 UUID 的子目录名（即全部登录过的用户标识），按名称排序。

    找不到或 ``Data`` 不存在时返回空列表。
    """
    data_root = _codebuddy_extension_data_root()
    if not os.path.isdir(data_root):
        return []
    return sorted(
        name
        for name in os.listdir(data_root)
        if os.path.isdir(os.path.join(data_root, name)) and _looks_like_uuid(name)
    )


def detect_current_uid() -> str | None:
    """判定「当前用户」= ``Data/<uuid>`` 中最近被修改的那个（启发式，对齐 Qoder）。

    找不到任何用户目录时返回 ``None``。
    """
    data_root = _codebuddy_extension_data_root()
    if not os.path.isdir(data_root):
        return None
    best_uid, best_mtime = None, -1.0
    for name in os.listdir(data_root):
        full = os.path.join(data_root, name)
        if os.path.isdir(full) and _looks_like_uuid(name):
            try:
                mtime = os.path.getmtime(full)
            except OSError:
                continue
            if mtime > best_mtime:
                best_mtime, best_uid = mtime, name
    return best_uid


def detect_user_codebuddy_data_root(uid: str | None = None) -> str:
    """指定/当前用户的集中数据根：``Data/<uuid>``。

    :param uid: 指定用户 UUID；``None`` 时自动取「当前用户」（最近修改的 ``Data/<uuid>``）。
    找不到时退回 ``Data`` 本身，由调用方按存在性决定条目显隐。
    """
    if uid is None:
        uid = detect_current_uid()
    if uid:
        return os.path.join(_codebuddy_extension_data_root(), uid)
    return _codebuddy_extension_data_root()


def detect_session_root(uid: str | None = None) -> str:
    """指定/当前用户的集中会话/检查点根：``Data/<uuid>/CodeBuddyIDE/<uuid>``。"""
    user_root = detect_user_codebuddy_data_root(uid)
    return os.path.join(user_root, "CodeBuddyIDE", os.path.basename(user_root))


#: 用户级规则整体作为一个备份项（不枚举单个文件、不排除任何特定文件）。
RULES_ITEM_KEY = "user_rules"
RULES_ITEM_LABEL = "用户级规则（rules/）"


def _rule_item(rules_root: str) -> BackupItem:
    """用户级规则目录整体作为一个条目（GUI 一行，默认勾选）。"""
    return BackupItem(
        key=RULES_ITEM_KEY,
        label=RULES_ITEM_LABEL,
        path=rules_root,
        uid=None,
        description="CodeBuddy 用户级规则（rules/）。建议必选。",
        recommended=True,
    )


def build_items(
    root: str | None = None,
    global_root: str | None = None,
    current_uid: str | None = None,
    session_root: str | None = None,
) -> list[BackupItem]:
    """
    构造 CodeBuddy 备份条目清单。

    仅包含**不在项目文件夹下**的用户级/全局数据（见模块 docstring 铁律）。
    所有条目 path 均在 ``root``（用户主目录 ``~``）之下，归档按相对路径落回原位。

    :param root: 公共根（用户主目录）。``None`` 时自动取 ``~``。
    :param global_root: 全局 ``~/.codebuddycn`` 根；``None`` 时自动探测。
    :param current_uid: 当前登录用户 UUID（决定集中会话/检查点目录）。``None`` 时自动取
        「最近修改的 ``Data/<uuid>``」（启发式判定当前用户）。其他 UUID 用户会作为
        ``user_sessions_others`` 聚合项出现，供 GUI「其他用户」区块多选。
    :param session_root: 集中会话根 ``Data/<uuid>/CodeBuddyIDE/<uuid>``；``None`` 时按
        ``current_uid`` 自动探测。
    """
    home = root or _home()
    if global_root is None:
        global_root = detect_global_root()
    if current_uid is None:
        current_uid = detect_current_uid()
    if session_root is None:
        session_root = detect_session_root(current_uid)

    codebuddy_home = os.path.join(home, ".codebuddy")
    memories_root = detect_public_memories_root()
    rules_root = detect_user_rules_root()

    items: list[BackupItem] = []

    # 1) 用户级跨项目记忆（「设置→本地记忆」列表数据源）。默认勾选，最核心。
    items.append(
        BackupItem(
            key="user_memories",
            label="用户级记忆（跨项目）",
            path=memories_root,
            uid=None,
            description="CodeBuddy 跨项目用户记忆（设置→本地记忆列表数据源）。最核心，默认勾选。",
            recommended=True,
        )
    )

    # 2) 用户级规则（~/.codebuddy/rules/）。默认勾选。
    items.append(_rule_item(rules_root))

    # 3) 用户级设置 + skill 本体（合并一项，默认不勾）。
    #    settings.json 是用户级 skill 开关清单；skills-marketplace/ 是已下载 skill 本体。
    #    二者聚合前缀均为 user_skill，GUI 显示为同一行。
    items.append(
        BackupItem(
            key="user_skill:settings",
            label="用户级设置与技能(skill)本体",
            path=os.path.join(codebuddy_home, "settings.json"),
            uid=None,
            description="用户级设置（skill 开关清单 settings.json）。默认不勾。",
            recommended=False,
        )
    )
    items.append(
        BackupItem(
            key="user_skill:skills",
            label="用户级设置与技能(skill)本体",
            path=os.path.join(codebuddy_home, "skills-marketplace"),
            uid=None,
            description="用户级技能(skill)本体目录（skills-marketplace/，体积较大）。默认不勾。",
            recommended=False,
        )
    )

    # 4) 用户级 MCP（~/.codebuddy/mcp.json）。默认不勾。
    items.append(
        BackupItem(
            key="user_mcp",
            label="用户级 MCP（mcp.json）",
            path=os.path.join(codebuddy_home, "mcp.json"),
            uid=None,
            description="CodeBuddy 用户级 MCP 服务器配置（mcp.json）。默认不勾。",
            recommended=False,
        )
    )

    # 5) 全局 IDE 配置（~/.codebuddycn/argv.json）。属「设置」类，可从零重复创建，默认不勾。
    items.append(
        BackupItem(
            key="global_argv",
            label="全局 IDE 配置（argv.json）",
            path=os.path.join(global_root, "argv.json"),
            uid=None,
            description="CodeBuddy 全局 IDE 配置（%s/argv.json）。可从零重新创建，默认不勾。" % global_root,
            recommended=False,
        )
    )

    # 6) 集中会话/检查点数据（Data/<uuid>/CodeBuddyIDE/<uuid>/）。
    #    这是跨工程集中存储的会话历史与检查点，**不是**项目文件夹内的 .codebuddy，必须备份。
    #    除「工程文件索引缓存」file-tree/ 无迁移价值（默认不勾）外，其余默认勾选：
    #      - history/  真实对话消息（messages/*.json）+ index.json，核心会话历史
    #      - check-point/  AI 回答检查点快照
    #      - plan-task/   计划任务
    #    归入聚合前缀 user_sessions（GUI 合成一行），统一勾选状态。
    items.append(
        BackupItem(
            key="user_sessions:history",
            label="集中会话/检查点（CodeBuddyIDE）",
            path=os.path.join(session_root, "history"),
            uid=current_uid,
            description="CodeBuddy 集中会话历史（真实对话消息 history/）。核心，默认勾选。",
            recommended=True,
        )
    )
    items.append(
        BackupItem(
            key="user_sessions:checkpoint",
            label="集中会话/检查点（CodeBuddyIDE）",
            path=os.path.join(session_root, "check-point"),
            uid=current_uid,
            description="CodeBuddy AI 回答检查点（check-point/）。默认勾选。",
            recommended=True,
        )
    )
    items.append(
        BackupItem(
            key="user_sessions:plan_task",
            label="集中会话/检查点（CodeBuddyIDE）",
            path=os.path.join(session_root, "plan-task"),
            uid=current_uid,
            description="CodeBuddy 计划任务（plan-task/）。默认勾选。",
            recommended=True,
        )
    )
    # 注意：工程文件索引缓存（file-tree/）属程序自身的运行态/索引，重新打开工程即重建，
    # 与用户数据无关，按统一策略**不列入备份选项**（不生成条目），故此处不再添加。

    # 7) 全局扩展目录（~/.codebuddycn/extensions/）。默认不勾（体积大、重装可恢复）。
    items.append(
        BackupItem(
            key="global_extensions",
            label="全局扩展（extensions/）",
            path=os.path.join(global_root, "extensions"),
            uid=None,
            description="CodeBuddy 全局扩展安装目录（%s/extensions），体积较大，重装可恢复。可选。"
            % global_root,
            recommended=False,
        )
    )

    # 8) 灵感（~/.codebuddy/inspiration/，按登录用户 UUID 隔离，用户级）。默认不勾。
    items.append(
        BackupItem(
            key="user_inspiration",
            label="灵感（inspiration/）",
            path=os.path.join(codebuddy_home, "inspiration"),
            uid=None,
            description="CodeBuddy 灵感卡片（inspiration/，按登录用户隔离，用户级）。可选。",
            recommended=False,
        )
    )

    # 9) 专家历史（~/.codebuddy/expert-history.json，用户级）。默认不勾。
    items.append(
        BackupItem(
            key="user_expert_history",
            label="专家历史（expert-history.json）",
            path=os.path.join(codebuddy_home, "expert-history.json"),
            uid=None,
            description="CodeBuddy 专家模式会话历史（expert-history.json）。可选。",
            recommended=False,
        )
    )

    # 10) 插件（~/.codebuddy/plugins/）。默认不勾（体积大、重装可恢复）。
    items.append(
        BackupItem(
            key="user_plugins",
            label="插件（plugins/）",
            path=os.path.join(codebuddy_home, "plugins"),
            uid=None,
            description="CodeBuddy 已安装插件目录（plugins/，体积较大，重装可恢复）。可选。",
            recommended=False,
        )
    )

    # 11) 其他登录用户（Data/<uuid>，非当前用户）的集中会话/检查点。
    #     每个其他 uuid 生成一组 user_sessions_others:<uuid>:<sub> 项，聚合前缀进 GUI
    #     「其他用户」区块（默认不勾、展开后按 uid 多选）。不重复当前用户。
    other_uids = [u for u in detect_user_uids() if u != current_uid]
    for uid in other_uids:
        other_session = detect_session_root(uid)
        for sub, desc, rec in (
            ("history", "真实对话消息 history/", True),
            ("check-point", "AI 回答检查点 check-point/", True),
            ("plan-task", "计划任务 plan-task/", True),
            ("file-tree", "工程文件索引缓存 file-tree/（迁移价值低）", False),
        ):
            items.append(
                BackupItem(
                    key="user_sessions_others:%s:%s" % (uid, sub),
                    label="其他用户会话/检查点（%s）" % short_uid(uid),
                    path=os.path.join(other_session, sub),
                    uid=uid,
                    description="其他用户（%s）%s。默认不勾。" % (short_uid(uid), desc),
                    recommended=rec,
                )
            )

    return items


@register
class CodeBuddyAdapter(BaseAdapter):
    name = "codebuddy"
    display_name = "CodeBuddy"

    #: CodeBuddy 专属压缩经验系数（档位 -> 类别 -> 压缩后/源 占比）。
    #:
    #: 备份数据构成（据此归类）：
    #:   - text   : ``.memories/*.mdc``、``rules/*.mdc``、``settings.json`` 文本（高度可压，≈0.12）
    #:   - db     : 目前 CodeBuddy 本机未见本地会话 .db；若将来引入，按 SQLite 经验 ≈0.59
    #:   - struct : 暂无显著结构化索引；暂与 db 同档
    #:   - binary : ``extensions/``、``skills-marketplace/``、``plugins/`` 内含已编译/二进制（≈0.99）
    #:   - other  : ``mcp.json`` / ``expert-history.json`` 含 base64/长字符串，部分不可压，归此类（≈0.14）
    #: 注：首次真实备份后会自动校准写入本工具校准文件，此处仅为回退兜底。
    COMPRESS_RATIO: dict[int, dict[str, float]] = {
        1: {  # 快速
            "text": 0.15,
            "db": 0.61,
            "struct": 0.48,
            "binary": 0.99,
            "other": 0.18,
        },
        6: {  # 正常（推荐）
            "text": 0.12,
            "db": 0.59,
            "struct": 0.46,
            "binary": 0.99,
            "other": 0.14,
        },
    }

    def detect_root(self) -> str | None:
        """探测 CodeBuddy 用户级/全局数据的公共根（用户主目录 ``~``）。始终返回 ``~``。"""
        return _home()

    def build_default_root(self) -> str:
        """未探测到时的默认（建议）数据目录：用户主目录 ``~``。"""
        return _home()

    def detect_data_roots(self, root: str | None = None) -> list[dict]:
        """返回 CodeBuddy 在 ``root``（用户主目录）下的各数据根目录信息，供识别状态区展示。

        每个根目录显示相对 ``root`` 的完整路径（含每层），含 UUID 用户目录的根备注用户数。
        不改变单路径数据目录模型，仅做展示增强。
        """
        home = root or _home()
        uids = detect_user_uids()
        uid_note = "（含 %d 个用户会话）" % len(uids) if uids else ""

        # 各根目录：相对 home 的完整路径 + 实际绝对路径 + 备注
        ext_rel = os.path.relpath(_codebuddy_extension_root(), home)
        candidates = [
            (".codebuddy", os.path.join(home, ".codebuddy"), ""),
            (".codebuddycn", os.path.join(home, ".codebuddycn"), ""),
            (ext_rel, _codebuddy_extension_root(), uid_note),
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
        """构造 CodeBuddy 备份条目（root_dir 为公共根 ``~``，可传 None 自动取）。

        ``current_uid`` 指定当前登录用户 UUID；``None`` 时自动判定最近活跃用户。
        本方法会把实际采用的当前用户记到 ``self.last_detected_uid``，供 GUI 下拉默认选中。
        """
        if current_uid is None:
            current_uid = detect_current_uid()
        self.last_detected_uid = current_uid
        return build_items(root_dir, detect_global_root(), current_uid, detect_session_root(current_uid))

    def restore_path_rewrite(self) -> "Callable[[str], str] | None":
        """
        跨电脑还原时，把归档内相对路径里的旧登录用户 UUID 重映射到本机当前用户。

        CodeBuddy 集中会话/检查点路径含登录用户标识：
        ``.../CodeBuddyExtension/Data/<uuid>/CodeBuddyIDE/<uuid>/...``。
        备份时该 <uuid> 是源电脑的当前用户；若在新电脑直接按原相对路径落回，
        会写进一个本机当前用户读不到的「死目录」——界面能看到历史会话列表残留，
        但点开看不到具体对话内容（这正是跨电脑还原后的典型症状）。

        本方法检测 ``Data/<uuid>/CodeBuddyIDE/<uuid>`` 模式（按通用段匹配，
        不依赖 Windows ``AppData/Local`` 前缀，兼容 darwin/linux 同名结构），
        把两段 <uuid> 都替换为 ``detect_current_uid()``（本机最近活跃用户）。
        本机从未登录过（取不到 uuid）时返回 ``None``，不做重写（至少不破坏原路径）。
        """
        new_uid = detect_current_uid()
        if not new_uid:
            return None

        def _rewrite(rel_path: str) -> str:
            parts = rel_path.replace("\\", "/").split("/")
            out = []
            i = 0
            n = len(parts)
            # 模式：.../<Data>/<uuid>/CodeBuddyIDE/<uuid>/...
            # 其中第一段 <uuid> 正好紧跟在名为 'Data' 的段之后
            while i < n:
                seg = parts[i]
                if (
                    seg == "Data"
                    and i + 3 < n
                    and _looks_like_uuid(parts[i + 1])
                    and parts[i + 2] == "CodeBuddyIDE"
                    and _looks_like_uuid(parts[i + 3])
                ):
                    out.append("Data")
                    out.append(new_uid)          # 外层 uuid -> 本机当前用户
                    out.append("CodeBuddyIDE")
                    out.append(new_uid)          # 内层 uuid -> 本机当前用户
                    i += 4
                    continue
                out.append(seg)
                i += 1
            return "/".join(out)

        return _rewrite

    @staticmethod
    def _scan_source_uids(entries: "Sequence[str]") -> "list[str]":
        """从归档条目名中扫描源机器登录用户 UUID（``Data/<uuid>/CodeBuddyIDE/<uuid>``）。

        返回去重后的源 UUID 列表（可能为空）。仅用于「跨电脑还原前向用户提示」，
        不依赖 Windows 路径前缀。
        """
        found: list[str] = []
        for name in entries or []:
            parts = name.replace("\\", "/").split("/")
            n = len(parts)
            i = 0
            while i + 3 < n:
                if (
                    parts[i] == "Data"
                    and _looks_like_uuid(parts[i + 1])
                    and parts[i + 2] == "CodeBuddyIDE"
                    and _looks_like_uuid(parts[i + 3])
                ):
                    if parts[i + 1] not in found:
                        found.append(parts[i + 1])
                i += 1
        return found

    def preview_path_rewrite(self, entries: "Sequence[str]") -> "dict | None":
        """预览跨电脑还原是否会发生登录用户 UUID 重映射，供还原前向用户提示。

        返回 ``{"will_rewrite": bool, "source_uids": [...], "current_uid": str|None}``；
        本机未登录（取不到 UUID）时不返回 ``None`` 而是 ``will_rewrite=False``。
        """
        new_uid = detect_current_uid()
        src_uids = self._scan_source_uids(entries)
        will = bool(new_uid) and bool(src_uids) and any(u != new_uid for u in src_uids)
        return {
            "will_rewrite": will,
            "source_uids": src_uids,
            "current_uid": new_uid,
        }
