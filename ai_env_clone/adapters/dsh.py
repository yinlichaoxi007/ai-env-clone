"""
DeepSeek Harness（DSH）适配器。

本文件是 DSH 备份逻辑的唯一事实来源：
- 目录探测（``detect_dsh_root``）
- 备份条目构造（``build_items``）
- 通过统一的 :class:`~ai_env_clone.adapters.base.BaseAdapter` 接口暴露给 GUI / CLI

数据布局（本机 Windows 实测确认，对照 DSH 的 ``docs/subsystems/persistence.md``
文档约定推导 macOS / Linux 对应位置）：

DSH 数据全部位于 ``~/.dsh/`` 目录下（``$DSH_HOME`` 环境变量可覆盖）：

- 用户数据根（``~/.dsh``）：
  - ``sessions/<workspace_dir>/session-<uuid>/session.jsonl.zstd``
    会话事件日志（Zstandard 压缩的 JSONL），每会话独立文件。
    这是 DSH 的**核心会话历史**，不可从零重建，默认勾选。
  - ``storages/workspace.json``
    工作区与关联会话的索引（会话 ID → 所属工作区）。
    与 ``sessions/`` 配套，核心数据，默认勾选。
  - ``storages/session_projcache.json``
    会话统计缓存（turn 数、token 用量等摘要）。
    与 ``sessions/`` 配套，核心数据，默认勾选。
  - ``AGENTS.md``
    用户全局指令（AI 助手工作准则，跨项目、跨会话自动加载）。
    等同于「用户级规则」，默认勾选。
  - ``settings.yaml``
    用户设置（LLM 提供商、locale、auto-detect 等）。
    属「设置」类，可从零重新创建，默认不勾。
  - ``.credentials.yaml``
    API 密钥等敏感凭证。
    属「设置」类，默认不勾（用户需谨慎）。
  - ``profiles/``
    配置文件（插件配置、cordis.yml、package.json 等）。
    属「插件/扩展」类，可从零重建，默认不勾。
  - ``.anonymous-user-id``
    匿名用户标识文件，运行态，**不列入备份选项**。

备份哲学（统一标准）：默认勾选无法从零重复创建的——会话、存储索引、用户规则；
默认不勾可从零重复创建的——设置、凭证、配置文件；
程序自身的本地缓存、运行态标识与用户数据无关，不列入备份选项。
"""

from __future__ import annotations

import os
import sys

from ..core import BackupItem
from .base import BaseAdapter, register


def _home() -> str:
    """当前用户主目录（DSH 数据的公共根）。"""
    return os.path.expanduser("~")


def _dsh_home() -> str:
    """DSH 数据根目录（``$DSH_HOME`` 或默认 ``~/.dsh``）。"""
    env_home = os.environ.get("DSH_HOME")
    if env_home:
        return os.path.expanduser(env_home)
    return os.path.join(_home(), ".dsh")


def _dsh_sessions_root() -> str:
    """DSH 会话目录：``<dsh_home>/sessions``。"""
    return os.path.join(_dsh_home(), "sessions")


def _dsh_storages_root() -> str:
    """DSH 存储目录：``<dsh_home>/storages``。"""
    return os.path.join(_dsh_home(), "storages")


def _dsh_profiles_root() -> str:
    """DSH 配置文件目录：``<dsh_home>/profiles``。"""
    return os.path.join(_dsh_home(), "profiles")


def _detect_workspace_session_dirs() -> list[str]:
    """
    探测会话目录下各工作区子目录，返回绝对路径列表。

    DSH 的会话按工作区组织：``sessions/<workspace_dir>/session-<uuid>/``，
    每个会话目录下含 ``session.jsonl.zstd`` 文件。

    找不到或 ``sessions`` 不存在时返回空列表。
    """
    sessions_root = _dsh_sessions_root()
    if not os.path.isdir(sessions_root):
        return []

    # 遍历工作区级别目录（如 ``--D-project-ai-env-clone--``）
    workspace_dirs: list[str] = []
    for name in os.listdir(sessions_root):
        full = os.path.join(sessions_root, name)
        if os.path.isdir(full) and not name.startswith("."):
            workspace_dirs.append(full)
    return workspace_dirs


def _count_session_files(sessions_root: str) -> int:
    """
    统计指定工作区目录下的会话文件数。

    每个会话子目录含 ``session.jsonl.zstd`` 文件，以此计数。
    """
    count = 0
    if not os.path.isdir(sessions_root):
        return 0
    for name in os.listdir(sessions_root):
        full = os.path.join(sessions_root, name)
        if os.path.isdir(full) and name.startswith("session-"):
            session_file = os.path.join(full, "session.jsonl.zstd")
            if os.path.isfile(session_file):
                count += 1
    return count


def build_items(
    root: str | None = None,
    dsh_home: str | None = None,
) -> list[BackupItem]:
    """
    构造 DeepSeek Harness 备份条目清单。

    所有条目 path 均在 ``root``（用户主目录 ``~``）之下，归档按相对路径落回原位。

    :param root: 公共根（用户主目录）。``None`` 时自动取 ``~``。
    :param dsh_home: DSH 数据根目录 ``~/.dsh``；``None`` 时自动探测。
    """
    home = root or _home()
    dsh = dsh_home or _dsh_home()
    sessions_root = os.path.join(dsh, "sessions")
    storages_root = os.path.join(dsh, "storages")
    profiles_root = os.path.join(dsh, "profiles")

    items: list[BackupItem] = []

    # 1) 会话历史（sessions/<workspace>/session-<uuid>/session.jsonl.zstd 文件）。
    #    核心数据，默认勾选。每个工作区按目录整体备份，GUI 聚合为一行。
    workspace_dirs = _detect_workspace_session_dirs()
    if workspace_dirs:
        total_sessions = sum(
            _count_session_files(wd) for wd in workspace_dirs
        )
        # 会话目录整体作为一个条目（core 的 os.walk 会递归扫描所有文件）
        items.append(
            BackupItem(
                key="sessions",
                label="会话历史（sessions/）",
                path=sessions_root,
                uid=None,
                description="DSH 会话事件日志（sessions/）。共 %d 个工作区、%d 个会话。核心，默认勾选。"
                % (len(workspace_dirs), total_sessions),
                recommended=True,
            )
        )
    else:
        # 无会话目录时生成占位项，保持选项结构稳定
        items.append(
            BackupItem(
                key="sessions",
                label="会话历史（sessions/）",
                path=sessions_root,
                uid=None,
                description="DSH 会话事件日志（sessions/）。未找到会话数据。",
                recommended=True,
            )
        )

    # 2) 存储数据（storages/workspace.json + session_projcache.json）。
    #    工作区索引与会话统计缓存，与会话配套，核心数据，默认勾选。
    #    两文件用不同 key 后缀区分，GUI 按前缀聚合。
    ws_file = os.path.join(storages_root, "workspace.json")
    items.append(
        BackupItem(
            key="storages:workspace",
            label="存储数据（storages/）",
            path=ws_file,
            uid=None,
            description="工作区与会话关联索引（workspace.json）。核心，默认勾选。",
            recommended=True,
        )
    )
    sp_file = os.path.join(storages_root, "session_projcache.json")
    items.append(
        BackupItem(
            key="storages:session_projcache",
            label="存储数据（storages/）",
            path=sp_file,
            uid=None,
            description="会话统计缓存（session_projcache.json）。核心，默认勾选。",
            recommended=True,
        )
    )

    # 3) 用户全局指令（AGENTS.md）。
    #    等同于「用户级规则」，默认勾选。
    agents_file = os.path.join(dsh, "AGENTS.md")
    items.append(
        BackupItem(
            key="user_agents",
            label="用户全局指令（AGENTS.md）",
            path=agents_file,
            uid=None,
            description="DSH 用户全局指令（AGENTS.md，跨项目自动加载）。核心，默认勾选。",
            recommended=True,
        )
    )

    # 4) 用户设置（settings.yaml）。属「设置」类，可从零重新创建，默认不勾。
    settings_file = os.path.join(dsh, "settings.yaml")
    items.append(
        BackupItem(
            key="settings",
            label="用户设置（settings.yaml）",
            path=settings_file,
            uid=None,
            description="DSH 用户设置（LLM 提供商、locale 等）。可从零重新创建，默认不勾。",
            recommended=False,
        )
    )

    # 5) 凭证（.credentials.yaml）。属「设置」类，敏感信息，默认不勾。
    creds_file = os.path.join(dsh, ".credentials.yaml")
    items.append(
        BackupItem(
            key="credentials",
            label="凭证（.credentials.yaml）",
            path=creds_file,
            uid=None,
            description="DSH API 密钥等凭证。敏感信息，默认不勾。",
            recommended=False,
        )
    )

    # 6) 配置文件（profiles/）。属「插件/扩展」类，可从零重建，默认不勾。
    items.append(
        BackupItem(
            key="profiles",
            label="配置文件（profiles/）",
            path=profiles_root,
            uid=None,
            description="DSH 配置文件（profiles/，含插件配置、cordis.yml 等）。"
                        "可从零重建，默认不勾。",
            recommended=False,
        )
    )

    # 注意：.anonymous-user-id 属运行态标识，与用户数据无关，按统一策略
    # **不列入备份选项**（不生成条目），此处不再添加。

    return items


@register
class DSHAdapter(BaseAdapter):
    name = "dsh"
    display_name = "DeepSeek Harness"

    #: DSH 专属压缩经验系数（档位 -> 类别 -> 压缩后/源 占比）。
    #:
    #: 备份数据构成（据此归类）：
    #:   - text   : ``AGENTS.md``、``settings.yaml``、``.credentials.yaml`` 文本（高度可压，≈0.12）
    #:   - db     : 暂无本地 SQLite，按通用经验 ≈0.5
    #:   - struct : ``storages/*.json`` 结构化数据（≈0.4）
    #:   - binary : 通用已压缩/二进制（≈0.99，几乎压不动）
    #:   - other  : ``session.jsonl.zstd``（Zstandard 已压缩，DEFLATE 几乎压不动，但
    #:              ``compress_estimate.category_of`` 未收录 ``.zstd`` 扩展名、会落入 other 类）
    #:             与 ``storages/*.json`` 等杂项。折中取 ≈0.2，首次估算可能偏低，
    #:              真实备份后按扩展名自动校准（.zstd 实测 ≈0.99 在 other 合理区间内会被采纳）。
    #: 注：首次真实备份后会自动校准写入本工具校准文件，此处仅为回退兜底。
    COMPRESS_RATIO: dict[int, dict[str, float]] = {
        1: {  # 快速
            "text": 0.15,
            "db": 0.55,
            "struct": 0.45,
            "binary": 0.99,
            "other": 0.25,
        },
        6: {  # 正常（推荐）
            "text": 0.12,
            "db": 0.5,
            "struct": 0.4,
            "binary": 0.99,
            "other": 0.2,
        },
    }

    #: DSH 的 session.jsonl.zstd 已是 Zstandard 压缩格式，DEFLATE 几乎压不动，
    #: 校准数据可能不准确，首次估算有偏差属正常。
    supports_calibration: bool = True

    def detect_root(self) -> str | None:
        """探测 DSH 数据公共根（用户主目录 ``~``）。始终返回 ``~``。"""
        return _home()

    def build_default_root(self) -> str:
        """未探测到时的默认（建议）数据目录：用户主目录 ``~``。"""
        return _home()

    def detect_data_roots(self, root: str | None = None) -> list[dict]:
        """返回 DSH 在 ``root``（用户主目录）下的各数据根目录信息，供识别状态区展示。

        与其他适配器粒度一致：**同一根目录只显示一行**。DSH 各类数据
        （会话 sessions/、存储 storages/、规则 AGENTS.md 等）全部位于
        ``~/.dsh`` 这一个根目录之下，故只返回这一行，子目录不单独列出；
        根存在时备注会话数概览。
        """
        home = root or _home()
        dsh = _dsh_home()

        # 统计会话数（备注用，子目录本身不单独显示）
        workspace_dirs = _detect_workspace_session_dirs()
        total_sessions = sum(
            _count_session_files(wd) for wd in workspace_dirs
        )
        note = (
            "（含 %d 个工作区、%d 个会话）" % (len(workspace_dirs), total_sessions)
            if workspace_dirs
            else ""
        )

        return [
            {
                "rel": os.path.relpath(dsh, home),
                "exists": os.path.isdir(dsh),
                "note": note,
            }
        ]

    def build_items(self, root_dir: str | None = None, current_uid: str | None = None) -> list[BackupItem]:
        """构造 DSH 备份条目（root_dir 为公共根 ``~``，可传 None 自动取）。

        ``current_uid`` 为兼容性参数（DSH 单用户扁平结构，无 UID 拆分），忽略。
        """
        return build_items(root_dir, _dsh_home())