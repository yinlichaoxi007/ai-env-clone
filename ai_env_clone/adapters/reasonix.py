"""
Reasonix 适配器（自包含，不依赖任何遗留兼容层）。

本文件是 Reasonix 备份逻辑的唯一事实来源：
- 目录探测（``detect_root``）
- 备份条目构造（``build_items``）
- 通过统一的 :class:`~ai_env_clone.adapters.base.BaseAdapter` 接口暴露给 GUI / CLI

数据布局（用户 2026-08-10 在本机 Windows 实测确认，并对照 Reasonix 官方
``CONFIG_PATHS`` 文档的三平台约定推导 macOS / Linux 对应位置）：

- 用户配置（Roaming 级）：
    Windows : ``%APPDATA%\\reasonix\\``                (= ``~/AppData/Roaming/reasonix``)
    macOS   : ``~/Library/Application Support/reasonix``
    Linux   : ``~/.config/reasonix``
  其下子目录（实测）：
    - ``memory/global/``            全局记忆
    - ``projects/<name>/memory/``   各项目记忆（``<name>`` 形如 ``d--project-tbmhmi_alpha``，
                                    是项目路径的编码，名称层级即项目名）
    - ``projects/<name>/sessions/`` 各项目会话（实测会话即存于此，非顶层 ``sessions/``）
    - ``config.toml``               全局配置（同时含 MCP 服务器 ``[[plugins]]`` 段与
                                    ``disabled_skills`` 等用户级设置，属「设置」类）
    - ``plugins/``                  已安装扩展
    - ``skills/``                   自定义技能与子智能体（实测：如 ``skills/test-sub-agent/SKILL.md``；
                                    内置 explore 等技能的停用标记在 config.toml，不在此目录）
    - ``settings.json``             全局 Hook 配置（用户级 hook 存放处；项目级 hook 在项目文件夹内，
                                    不列入）。本机未配置 hook 时该文件可能不存在，届时标「（未找到）」
    - ``state/`` ``stats/`` ``archive/`` ``crash-fatal/`` ``repair/`` ``global-workspace/``
      ``sessions/``（仅 ``.legacy-imported`` 等迁移标记，非真实会话） ``desktop-*.json``
      ``install-id`` ``metrics-pending.json`` 等：运行态/崩溃日志/锁文件，易变、迁移价值低，
      **不列入备份项**（避免打包崩溃日志与锁）。
- 本地缓存（Local 级）：
    Windows : ``%LOCALAPPDATA%\\reasonix\\``           (= ``~/AppData/Local/reasonix``)
    macOS   : ``~/Library/Caches/reasonix``
    Linux   : ``~/.cache/reasonix``
  其下 ``environment/`` ``repair-mutation-locks/`` ``updates/``：缓存/更新，程序自身，
  **与用户数据无关，不列入备份选项**（不生成备份条目）。

说明：Reasonix 同时有 Roaming（配置/数据）与 Local（缓存）两处，二者公共根是
用户主目录 ``~``，故 ``detect_root`` / ``build_default_root`` 均返回 ``~``，各条目
path 均在 ``~`` 之下（含 ``AppData/Roaming`` 与 ``AppData/Local``），归档按相对路径
落回原位，恢复干净。这与 CodeBuddy 适配器「公共根=用户主目录」策略一致。

备份哲学（统一标准）：默认勾选无法从零重复创建的——会话、记忆；默认不勾可从零重复
创建的——扩展（plugins/）、自定义技能/子智能体（skills/）、设置（config.toml、settings.json）；
程序自身的本地缓存、运行态、日志不列入备份选项。新增其它工具时，仿照本文件新建
``ai_env_clone/adapters/<tool>.py``，用 ``@register`` 装饰类，无需改动任何入口代码。
"""

from __future__ import annotations

import os
import sys

from ..core import BackupItem
from .base import BaseAdapter, register


def _home() -> str:
    """当前用户主目录（Roaming 与 Local 两处 reasonix 数据的公共根）。"""
    return os.path.expanduser("~")


def _roaming_root() -> str:
    """Reasonix 用户配置/数据根（跨平台），即官方 CONFIG_PATHS 文档所指用户配置目录。"""
    if sys.platform.startswith("win"):
        return os.path.join(
            os.environ.get("APPDATA", os.path.join(_home(), "AppData", "Roaming")),
            "reasonix",
        )
    if sys.platform == "darwin":
        return os.path.join(_home(), "Library", "Application Support", "reasonix")
    # Linux / 其它类 Unix
    return os.path.join(_home(), ".config", "reasonix")


def _local_root() -> str:
    """Reasonix 本地缓存根（跨平台）。"""
    if sys.platform.startswith("win"):
        return os.path.join(
            os.environ.get("LOCALAPPDATA", os.path.join(_home(), "AppData", "Local")),
            "reasonix",
        )
    if sys.platform == "darwin":
        return os.path.join(_home(), "Library", "Caches", "reasonix")
    return os.path.join(_home(), ".cache", "reasonix")


def build_items(
    root: str | None = None,
    roaming_root: str | None = None,
    local_root: str | None = None,
) -> list[BackupItem]:
    """构造 Reasonix 备份条目清单。

    仅包含用户级/全局数据（见模块 docstring）。所有条目 path 均在 ``root``
    （用户主目录 ``~``）之下，归档按相对路径落回原位。

    :param root: 公共根（用户主目录）。``None`` 时自动取 ``~``。
    :param roaming_root: ``%APPDATA%/reasonix`` 级配置/数据根；``None`` 时自动探测。
    :param local_root: ``%LOCALAPPDATA%/reasonix`` 级缓存根；``None`` 时自动探测。
    """
    home = root or _home()
    roam = roaming_root or _roaming_root()
    local = local_root or _local_root()

    items: list[BackupItem] = []

    # 1) 全局记忆（memory/global/）。最核心，默认勾选。
    items.append(
        BackupItem(
            key="global_memory",
            label="全局记忆（memory/global/）",
            path=os.path.join(roam, "memory", "global"),
            uid=None,
            description="Reasonix 全局记忆（memory/global/）。最核心，默认勾选。",
            recommended=True,
        )
    )

    # 2) 项目记忆与会话（projects/ 整体）。
    #    实测：会话不在顶层 sessions/，而在 projects/<name>/sessions/；
    #    记忆在 projects/<name>/memory/。连同两者整体备份，默认勾选。
    items.append(
        BackupItem(
            key="projects",
            label="项目记忆与会话（projects/）",
            path=os.path.join(roam, "projects"),
            uid=None,
            description="Reasonix 各项目的记忆（memory/）与会话（sessions/），"
                        "按项目路径编码分目录。默认勾选。",
            recommended=True,
        )
    )

    # 3) 全局配置（config.toml）。属「设置」类，可从零重复创建，默认不勾。
    items.append(
        BackupItem(
            key="global_config",
            label="全局配置（config.toml）",
            path=os.path.join(roam, "config.toml"),
            uid=None,
            description="Reasonix 全局配置（config.toml）。可从零重新创建，默认不勾。",
            recommended=False,
        )
    )

    # 4) 扩展（plugins/）。属「扩展」类，重装可恢复，默认不勾。
    items.append(
        BackupItem(
            key="plugins",
            label="扩展（plugins/）",
            path=os.path.join(roam, "plugins"),
            uid=None,
            description="Reasonix 已安装扩展目录（plugins/）。重装可恢复，默认不勾。",
            recommended=False,
        )
    )

    # 5) 自定义技能与子智能体（skills/）。实测：用户添加的自定义技能/子智能体
    #    （如 test-sub-agent）以 skill 形式存放在 Roaming/reasonix/skills/<name>/SKILL.md。
    #    内置 explore 等技能停用状态记录在 config.toml（见 global_config 项），不在本目录。
    #    技能属「可重建」内容，默认不勾。
    items.append(
        BackupItem(
            key="skills",
            label="自定义技能与子智能体（skills/）",
            path=os.path.join(roam, "skills"),
            uid=None,
            description="Reasonix 自定义技能/子智能体目录（skills/，如 test-sub-agent）。"
                        "可重建，默认不勾。",
            recommended=False,
        )
    )

    # 6) 全局 Hook 配置（settings.json）。用户明确：全局 hook 配置存放于
    #    Roaming/reasonix/settings.json，列入备份、默认不勾；项目级 hook 在项目文件夹内，不列入。
    #    注：本机实测该文件可能尚未落盘（未配置 hook 时不存在），届时该项标「（未找到）」，
    #    待用户实际配置 hook 后生成即生效。
    items.append(
        BackupItem(
            key="global_settings",
            label="全局 Hook 配置（settings.json）",
            path=os.path.join(roam, "settings.json"),
            uid=None,
            description="Reasonix 全局 Hook 配置（settings.json）。默认不勾。",
            recommended=False,
        )
    )

    # 注意：本地缓存（Local/reasonix 下的 environment/、updates/ 等）属程序自身缓存，
    # 与用户数据无关，按统一策略**不列入备份选项**（不生成条目），此处不再添加。

    return items


@register
class ReasonixAdapter(BaseAdapter):
    name = "reasonix"
    display_name = "Reasonix"

    #: Reasonix 专属压缩经验系数（档位 -> 类别 -> 压缩后/源 占比）。
    #:
    #: 备份数据构成（据此归类）：
    #:   - text   : ``memory/*.md``、``config.toml`` 文本（高度可压，≈0.15）
    #:   - db     : 会话若为结构化/JSON，按经验 ≈0.5
    #:   - struct : 项目结构与索引，暂与 db 同档
    #:   - binary : ``plugins/`` 内含已编译/二进制（≈0.99）
    #:   - other  : 杂项 JSON 配置（≈0.2）
    #: 注：首次真实备份后会自动校准写入本工具校准文件，此处仅为回退兜底。
    COMPRESS_RATIO: dict[int, dict[str, float]] = {
        1: {  # 快速
            "text": 0.18,
            "db": 0.55,
            "struct": 0.5,
            "binary": 0.99,
            "other": 0.25,
        },
        6: {  # 正常（推荐）
            "text": 0.15,
            "db": 0.5,
            "struct": 0.46,
            "binary": 0.99,
            "other": 0.2,
        },
    }

    def detect_root(self) -> str | None:
        """探测 Reasonix 数据公共根（用户主目录 ``~``）。始终返回 ``~``。"""
        return _home()

    def build_default_root(self) -> str:
        """未探测到时的默认（建议）数据目录：用户主目录 ``~``。"""
        return _home()

    def detect_data_roots(self, root: str | None = None) -> list[dict]:
        """返回 Reasonix 在 ``root``（用户主目录）下的各数据根目录信息，供识别状态区展示。

        列出 Roaming（配置/数据）与 Local（缓存）两处，含相对 ``root`` 的完整路径与存在性。
        """
        home = root or _home()
        roam = _roaming_root()
        local = _local_root()
        candidates = [
            (os.path.relpath(roam, home), roam, ""),
            (os.path.relpath(local, home), local, "（缓存，不列入备份）"),
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
        """构造 Reasonix 备份条目（root_dir 为公共根 ``~``，可传 None 自动取）。

        ``current_uid`` 为兼容性参数（Reasonix 记忆为单用户扁平结构，无 UID 拆分），忽略。
        """
        return build_items(root_dir, _roaming_root(), _local_root())
