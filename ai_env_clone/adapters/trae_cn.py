"""
TraeCode CN（VSCode 系 IDE，安装目录 ``Trae CN``）适配器。

> **命名口径**：安装目录叫 ``Trae CN``，产品自身向用户展示的名字是
> ``TraeCode CN``（``product.json`` 的 ``win32NameVersion``，注册表
> 「应用和功能」里也是 ``TraeCode CN (User)``）。本工具统一展示**用户看得到的产品名**，
> ``name`` 仍保留 ``trae-cn``，不影响既有备份与偏好缓存。
> 同族另一支：安装目录 ``TRAE SOLO CN`` -> 产品名 ``TraeWork CN``。

本文件是 **Trae 家族**（TraeCode CN / TraeWork CN）备份逻辑的唯一事实来源：
- 目录探测（``detect_root`` / ``detect_data_roots``）
- 备份条目构造（``build_items``）
- 通过统一的 :class:`~ai_env_clone.adapters.base.BaseAdapter` 接口暴露给 GUI / CLI

``trae_solo_cn.py`` 复用本文件的家族通用实现（仅 userData 目录名与扩展目录不同），
避免同一产品两条分支各自维护、行为漂移。

数据布局（2026-09-22 在本机 Windows 实测确认。Trae 是 VSCode 系 IDE，
沿用「userData 在 ``%APPDATA%``、extensions 在 ``~/.<product>``」的两处布局）：

- 用户数据（userData）``%APPDATA%/Trae CN/``：
  - ``ModularData/ai-agent/database.db``（+ ``-wal``/``-shm``）
    **AI 智能体会话库**（SQLite，但文件头非 ``SQLite format 3``——内容经产品侧
    加密，本工具无法解析内部结构）。这是 Trae 侧最核心的不可重建数据，默认勾选，
    按**整库不透明**方式备份。
  - ``ModularData/ai-agent/snapshot/``
    智能体会话快照。默认勾选。
  - ``ModularData/ckg_server/``
    代码知识图谱服务数据（索引类）。默认不勾。
  - ``User/globalStorage/``
    VSCode 全局状态（``state.vscdb`` + ``state.vscdb.backup`` + ``storage.json``，
    含各扩展的 memento 与 MCP 缓存）。默认勾选。
  - ``User/workspaceStorage/``
    各工作区的界面状态（``state.vscdb``）。可重建，默认不勾。
    其中 ``<hash>/workspace.json``（内容 =「这个工作区打开的是哪个工程文件夹」）
    **单独成一个条目、默认勾选**：Trae 的会话库是产品侧加密的，库外唯一能说明
    「会话原本属于哪个工程」的结构化线索就是它。按存在性生成（本机为空则不出现）。
  - ``User/History/``
    VSCode 本地文件编辑历史（不可重建），默认勾选。
  - ``User/settings.json``、``User/snippets/``
    用户设置与代码片段。属「还原后立刻能开工」类，默认勾选。
  - ``Workspaces/``、``remote-widgets/``、``solo-lite/``
    工作区元数据与小部件缓存。默认不勾。

- 扩展与插件（extensions 目录）``~/.trae-cn/``：
  - ``extensions/``：已安装扩展本体（本机实测 ≈1.8GB）。默认不勾（重装/重新下载可恢复）。
  - ``plugins/``：产品侧插件（如 ``trae-remote-official``）。默认不勾。
  - ``builtin_skills/``、``builtin/``：随产品分发的内置技能。默认不勾。
  - ``skill-config.json``：**本机技能状态**（禁用/托管/删除记录，本机实测 132 B）。
    属「设置」类，**默认勾选**（还原后技能的启用/禁用状态无需重新设置）。
    注意它只记状态，不含技能本体，更不含任何市场条目。
  - ``mcps/``、``plugin-config.json``、``installed-plugins.json``：MCP 与插件配置。默认不勾。
  - ``assistant/``：助手目录。默认不勾。
  - ``argv.json``：IDE 启动配置。属「设置」类，默认勾选。
  - ``worktrees/``：用户工作树（属工程内容），默认不勾。

- **不列入备份选项**（程序自身运行时/缓存/日志/遥测，与用户数据无关）：
  ``Cache/`` ``Code Cache/`` ``GPUCache/`` ``Dawn*Cache/`` ``CachedData/``
  ``CachedExtensionVSIXs/``（本机实测 ≈450MB） ``CachedConfigurations/``
  ``CachedProfilesData/`` ``Crashpad/`` ``logs/`` ``monitor/`` ``Network/``
  ``Partitions/`` ``Service Worker/`` ``Session Storage/`` ``Shared Dictionary/``
  ``WebStorage/`` ``Local Storage/`` ``IndexedDB/`` ``blob_storage/`` ``Backups/``
  ``aha/`` ``ahanet/`` ``shared_proto_db/`` ``Local State`` ``Preferences``
  ``machineid`` ``DIPS`` ``VideoDecodeStats/`` ``SharedStorage`` 等。

> ⚠️ Trae 家族的智能体会话内容是**产品侧加密**的（``database.db`` 非标准 SQLite），
> 因此本工具只能做**整库不透明备份/还原**，**无法**把其中的会话转换为其它软件的
> 原生格式（跨软件导入）。详见 ``ai_env_clone/import_matrix.py``。
>
> 同理，**加密的会话库里挖不出工作区路径**（2026-10-01 本机实测：11.7 MB 主库 +
> 12.9 MB WAL 中，一个 ≥24 字符的可打印片段都没有）。所以跨机还原时能把会话归回
> 原工程工作区的唯一线索，是 IDE 的 ``User/workspaceStorage/*/workspace.json``
> —— 以及 ``User/globalStorage/storage.json`` 里的 ``profileAssociations`` /
> ``backupWorkspaces``（后者随「全局状态」条目一起备份）。**注意这只是尽力而为**：
> 它只覆盖「在源机器上打开过文件夹」的工程。

所有条目 path 均在公共根 ``~`` 之下，归档按相对路径落回原位，恢复干净。
"""

from __future__ import annotations

import os
import sys

from ..core import ORIGIN_NOTE_CONVERSATION, BackupItem
from .base import BaseAdapter, register


def _home() -> str:
    """当前用户主目录（userData 与 extensions 两处数据的公共根）。"""
    return os.path.expanduser("~")


def appdata_dir(name: str) -> str:
    """``%APPDATA%/<name>``（Windows）/ 对应平台的应用配置目录。"""
    if sys.platform.startswith("win"):
        base = os.environ.get("APPDATA", os.path.join(_home(), "AppData", "Roaming"))
    elif sys.platform == "darwin":
        base = os.path.join(_home(), "Library", "Application Support")
    else:
        base = os.path.join(_home(), ".config")
    return os.path.join(base, name)


def user_data_dir(product: str) -> str:
    """VSCode 系 IDE 的 userData 目录（``%APPDATA%/<product>``）。"""
    return appdata_dir(product)


def extensions_dir(product_dir: str) -> str:
    """VSCode 系 IDE 的 extensions 目录（``~/.<product_dir>``）。"""
    return os.path.join(_home(), "." + product_dir)


def _db_with_companions(db_path: str) -> list[str]:
    """返回库文件及其 ``-wal``/``-shm`` 配套（存在的才返回）。"""
    return [p for p in (db_path, db_path + "-wal", db_path + "-shm") if os.path.exists(p)]


# --------------------------------------------------------------------------- #
# IDE 侧「已打开文件夹」记录（VSCode 通用：`<User>/workspaceStorage/<hash>/workspace.json`）
#
# 为什么单独成条目（2026-10-01 用户提出「Trae 的会话备份是否也不含工作区路径」）：
# Trae 的**会话库 ``database.db`` 是产品侧加密的** —— 本机实测 11.7 MB 主库 + 12.9 MB
# WAL 里**一个 ≥24 字符的可打印片段都没有**，无法从会话里挖出任何路径；而它对会话的
# 索引也不像 CodeBuddy 那样把路径转成哈希（连哈希都没有）。库外能记「打开过哪个工程」
# 的地方只剩 VSCode 自己的 ``workspace.json``：
#
#     {"folder": "file:///d%3A/project/ai-env-clone"}
#
# 内容只有几十字节纯路径，**不含 IDE 运行态**，属「让用户数据正确落位的参考信息」，
# 故默认勾选。同目录的 ``state.vscdb``（单条几十 KB 的界面状态）仍归 ``workspace_storage``
# 条目、默认不勾。
#
# 注意：它是**尽力而为**——只覆盖「在源机器上打开过」的工程。本机实测恰好为空
# （仅 1 个空窗口目录、无 workspace.json），所以别把它当成「一定有」。
# --------------------------------------------------------------------------- #


def detect_ide_workspace_records(user_data: str) -> list[str]:
    """列出 ``<userData>/User/workspaceStorage/*/workspace.json``（存在才返回）。

    :param user_data: Trae 的 userData 目录（``%APPDATA%/<产品名>``）。
    """
    ws_root = os.path.join(user_data, "User", "workspaceStorage")
    if not os.path.isdir(ws_root):
        return []
    try:
        buckets = sorted(os.listdir(ws_root))
    except OSError:
        return []
    out: list[str] = []
    for bucket in buckets:
        wj = os.path.join(ws_root, bucket, "workspace.json")
        if os.path.isfile(wj):
            out.append(wj)
    return out


def ide_workspace_record_item(record_path: str, key_prefix: str = "") -> BackupItem:
    """IDE 工作区记录（单个 ``workspace.json``）作为一个备份条目。

    ``key`` 带 ``:<bucket>`` 尾缀：一个工作区一个文件，但 GUI 按**第一个冒号前**的前缀
    聚合成一行（与 ``ai_agent_db:wal`` 同机制），勾选状态统一。
    """
    bucket = os.path.basename(os.path.dirname(record_path))
    return BackupItem(
        key=key_prefix + "ide_workspace_records:" + bucket,
        label="IDE 工作区记录（已打开文件夹）",
        path=record_path,
        uid=None,
        description="Trae 记下的「这个工作区打开的是哪个工程文件夹」（workspaceStorage/%s/"
                    "workspace.json）。体积极小，是跨机还原后把会话归回原工程工作区的关键"
                    "线索（Trae 的会话库本身加密、不含可读路径）。建议勾选。" % bucket,
        recommended=True,
        carries_origin="源机器上打开过的工程文件夹绝对路径",
    )


def build_family_items(
    root: str,
    user_data: str,
    ext_dir: str,
    *,
    key_prefix: str = "",
    has_vm: bool = False,
) -> list[BackupItem]:
    """构造 Trae 家族（Trae CN / Trae SOLO CN）的备份条目清单。

    :param root: 公共根（用户主目录）。
    :param user_data: userData 目录（``%APPDATA%/<product>``）。
    :param ext_dir: extensions 目录（``~/.<product>``）。
    :param key_prefix: 条目 key 前缀，保证不同适配器的聚合前缀互不冲突。
    :param has_vm: 是否为带沙箱虚拟机的形态（SOLO）。为 True 时显式提示
        ``ModularData/ai-agent/vm/`` 属程序运行态、不列入备份（仅备注，不生成条目）。
    """

    def K(k: str) -> str:
        return key_prefix + k

    items: list[BackupItem] = []
    ai_agent = os.path.join(user_data, "ModularData", "ai-agent")

    # 1) AI 智能体会话库（核心，整库不透明备份）。
    db_main = os.path.join(ai_agent, "database.db")
    for db_file in _db_with_companions(db_main):
        suffix = ":wal" if db_file.endswith("-wal") else (":shm" if db_file.endswith("-shm") else "")
        items.append(
            BackupItem(
                key=K("ai_agent_db") + suffix,
                label="AI 智能体会话库（database.db）",
                path=db_file,
                uid=None,
                description="Trae 智能体会话库（ModularData/ai-agent/database.db，产品侧加密，整体备份）。"
                            "核心数据，默认勾选。",
                recommended=True,
            )
        )

    # 2) 智能体会话快照。
    items.append(
        BackupItem(
            key=K("ai_agent_snapshot"),
            label="智能体会话快照（snapshot/）",
            path=os.path.join(ai_agent, "snapshot"),
            uid=None,
            description="智能体会话的过程快照（ModularData/ai-agent/snapshot/）。默认勾选。",
            recommended=True,
        )
    )

    # 3) VSCode 全局状态（含扩展 memento 与 MCP 缓存）。
    items.append(
        BackupItem(
            key=K("global_storage"),
            label="全局状态（User/globalStorage/）",
            path=os.path.join(user_data, "User", "globalStorage"),
            uid=None,
            description="VSCode 全局状态库（state.vscdb / storage.json，含各扩展 memento 与 MCP 目录缓存）。"
                        "默认勾选。",
            recommended=True,
        )
    )

    # 4) 本地文件编辑历史（不可重建）。
    items.append(
        BackupItem(
            key=K("user_history"),
            label="本地文件编辑历史（User/History/）",
            path=os.path.join(user_data, "User", "History"),
            uid=None,
            description="VSCode 本地文件编辑历史（User/History/，可逐次回退文件版本）。不可重建，默认勾选。",
            recommended=True,
            # 实测：History/<hash>/entries.json 里记着被编辑文件的原路径（如 D:/Desktop/project）。
            carries_origin=ORIGIN_NOTE_CONVERSATION,
        )
    )

    # 5) 代码知识图谱（索引类）。
    items.append(
        BackupItem(
            key=K("ckg_server"),
            label="代码知识图谱（ckg_server/）",
            path=os.path.join(user_data, "ModularData", "ckg_server"),
            uid=None,
            description="代码知识图谱服务数据（ModularData/ckg_server/）。属索引类，重新打开工程即重建，默认不勾。",
            recommended=False,
        )
    )

    # 6) 各工作区界面状态。
    #    注：该目录下的 workspace.json（「打开过哪个工程」）**另有独立条目**、默认勾选，
    #    见下方第 6b 段；本项只负责可重建的界面状态（state.vscdb）。
    items.append(
        BackupItem(
            key=K("workspace_storage"),
            label="工作区界面状态（User/workspaceStorage/）",
            path=os.path.join(user_data, "User", "workspaceStorage"),
            uid=None,
            description="各工作区的界面状态（User/workspaceStorage/*/state.vscdb）。可重建，默认不勾。"
                        "（其中的 workspace.json 属路径记录，已单独列为「IDE 工作区记录」项。）",
            recommended=False,
        )
    )

    # 6b) IDE 工作区记录（User/workspaceStorage/<hash>/workspace.json）。
    #     内容 = 「这个工作区打开的是哪个工程文件夹」（folder URI），逐个几十字节。
    #     Trae 的会话库是产品侧加密的（实测无可打印片段），库外唯一能说明「会话原本
    #     属于哪个工程」的结构化线索就是它 ⇒ 跨机还原后据此把会话归回原工作区，
    #     而不是统统落到默认目录。它不含 IDE 运行态，属「让用户数据正确落位的参考
    #     信息」，默认勾选。按存在性生成：本机若从未打开过文件夹工程则无此条目。
    for _rec in detect_ide_workspace_records(user_data):
        items.append(ide_workspace_record_item(_rec, key_prefix))

    # 7) 设置与代码片段。均属「还原后立刻能开工」类，默认勾选。
    items.append(
        BackupItem(
            key=K("user_settings"),
            label="用户设置（settings.json）",
            path=os.path.join(user_data, "User", "settings.json"),
            uid=None,
            description="用户设置（User/settings.json）。默认勾选，还原后无需重新配置。",
            recommended=True,
        )
    )
    items.append(
        BackupItem(
            key=K("user_snippets"),
            label="代码片段（snippets/）",
            path=os.path.join(user_data, "User", "snippets"),
            uid=None,
            description="用户代码片段（User/snippets/）。用户手工积累、无法从零重建，默认勾选。",
            recommended=True,
        )
    )

    # 8) 工作区元数据 / 小部件。
    for sub, label, desc in (
        (os.path.join("Workspaces"), "工作区元数据（Workspaces/）", "工作区元数据（Workspaces/）。默认不勾。"),
        (os.path.join("remote-widgets"), "远程小部件（remote-widgets/）", "远程小部件脚本缓存（remote-widgets/）。默认不勾。"),
        (os.path.join("solo-lite"), "SOLO Lite 缩略图（solo-lite/）", "SOLO Lite 缩略图资源（solo-lite/）。默认不勾。"),
    ):
        p = os.path.join(user_data, sub)
        if not os.path.isdir(p):
            continue
        # key 里**不写冒号**：三者是性质不同的东西（工作区元数据 / 脚本缓存 / 缩略图资源），
        # 用同一个聚合前缀会被合成一行、且界面上只显示第一个的 label
        # （实测 SOLO 下 3 项只显示「工作区元数据（Workspaces/）」，另两项被隐藏）。
        items.append(
            BackupItem(
                key=K("ui_misc_" + os.path.basename(sub)),
                label=label,
                path=p,
                uid=None,
                description=desc,
                recommended=False,
            )
        )

    # 9) extensions 目录侧：扩展 / 插件 / 内置技能 / MCP / 配置。
    items.append(
        BackupItem(
            key=K("extensions"),
            label="已安装扩展（extensions/）",
            path=os.path.join(ext_dir, "extensions"),
            uid=None,
            description="VSCode 系扩展安装目录（extensions/，体积很大）。重装/重新下载可恢复，默认不勾。",
            recommended=False,
        )
    )
    items.append(
        BackupItem(
            key=K("plugins"),
            label="产品插件（plugins/）",
            path=os.path.join(ext_dir, "plugins"),
            uid=None,
            description="产品侧插件目录（plugins/，如 trae-remote-official）。可重新下载，默认不勾。",
            recommended=False,
        )
    )
    items.append(
        BackupItem(
            key=K("builtin_skills"),
            label="内置技能（builtin_skills/）",
            path=os.path.join(ext_dir, "builtin_skills"),
            uid=None,
            description="随产品分发的内置技能（builtin_skills/）。随程序自带，默认不勾。",
            recommended=False,
        )
    )
    # 本机技能状态（禁用/托管/删除记录）。属「设置」类，但**默认勾选**——
    # 依据用户 2026-10-01 定策的判据（重新获取的成本）：132 B 的状态文件不带过去，
    # 用户就得在新机器上逐一重新禁用/启用技能，等于「还原完还不能开工」。
    # 明确区分：这里只记**本机状态**，不含任何技能本体或市场条目。
    items.append(
        BackupItem(
            key=K("skill_config"),
            label="本机技能状态（skill-config.json）",
            path=os.path.join(ext_dir, "skill-config.json"),
            uid=None,
            description="本机技能状态记录（skill-config.json：disabledSkills / builtinSkillStatus / "
                        "managedSkills / deletedSkills）。属「设置」类，默认勾选，"
                        "还原后技能的启用/禁用状态无需重新设置。",
            recommended=True,
        )
    )
    items.append(
        BackupItem(
            key=K("mcps"),
            label="MCP 服务器（mcps/）",
            path=os.path.join(ext_dir, "mcps"),
            uid=None,
            description="MCP 服务器配置目录（mcps/）。可从零重新创建，默认不勾。",
            recommended=False,
        )
    )
    items.append(
        BackupItem(
            key=K("plugin_config"),
            label="插件配置（plugin-config.json）",
            path=os.path.join(ext_dir, "plugin-config.json"),
            uid=None,
            description="插件配置（plugin-config.json）。可从零重新创建，默认不勾。",
            recommended=False,
        )
    )
    items.append(
        BackupItem(
            key=K("installed_plugins"),
            label="已安装插件清单（installed-plugins.json）",
            path=os.path.join(ext_dir, "installed-plugins.json"),
            uid=None,
            description="已安装插件清单（installed-plugins.json）。可从零重新创建，默认不勾。",
            recommended=False,
        )
    )
    items.append(
        BackupItem(
            key=K("assistant"),
            label="助手目录（assistant/）",
            path=os.path.join(ext_dir, "assistant"),
            uid=None,
            description="助手目录（assistant/）。默认不勾。",
            recommended=False,
        )
    )
    items.append(
        BackupItem(
            key=K("argv"),
            label="IDE 启动配置（argv.json）",
            path=os.path.join(ext_dir, "argv.json"),
            uid=None,
            description="IDE 启动配置（argv.json）。属「设置」类，默认勾选，还原后无需重新配置。",
            recommended=True,
        )
    )
    items.append(
        BackupItem(
            key=K("worktrees"),
            label="工作树（worktrees/）",
            path=os.path.join(ext_dir, "worktrees"),
            uid=None,
            description="智能体工作树目录（worktrees/，属工程内容）。默认不勾。",
            recommended=False,
        )
    )

    # 注：has_vm（SOLO）的 ModularData/ai-agent/vm/（沙箱虚拟机，本机实测 ≈2.1GB、
    # 7 万文件）属程序运行态，按统一策略**不列入备份选项**（不生成条目），此处仅留说明。
    return items


def family_data_roots(root: str, product: str, product_dir: str, has_vm: bool = False) -> list[dict]:
    """返回 Trae 家族各数据根目录信息（供 GUI 识别状态区展示）。"""
    home = root or _home()
    ud = user_data_dir(product)
    ext = extensions_dir(product_dir)

    # userData 下的会话数备注
    note = ""
    db = os.path.join(ud, "ModularData", "ai-agent", "database.db")
    if os.path.isfile(db):
        note = "（含 AI 智能体会话库 database.db）"

    ext_note = ""
    if os.path.isdir(ext):
        ext_note = "（含 extensions/ 与 plugins/）"

    vm_note = "（含沙箱虚拟机 vm/，运行态，不列入备份）" if has_vm else ""
    candidates = [
        (os.path.relpath(ud, home), ud, note),
        (os.path.relpath(ext, home), ext, ext_note),
    ]
    roots = [{"rel": rel, "exists": os.path.isdir(p), "note": n} for rel, p, n in candidates]
    if has_vm:
        vm = os.path.join(ud, "ModularData", "ai-agent", "vm")
        if os.path.isdir(vm):
            roots.append(
                {
                    "rel": os.path.relpath(vm, home),
                    "exists": True,
                    "note": vm_note,
                }
            )
    return roots


#: 产品标识：用于 Trae 家族各适配器的路径解析（name 与扩展目录名）
TRAE_CN_PRODUCT = "Trae CN"
TRAE_CN_PRODUCT_DIR = "trae-cn"

# --------------------------------------------------------------------------- #
# 条目 key 前缀：**用下划线，绝不能用冒号**
#
# 历史 bug（2026-10-01 发现）：原先写的是 ``"trae_cn:"``，而 GUI 的聚合前缀是
# ``key.split(":", 1)[0]``（见 ``__main__.QoderBackupApp._agg_prefix``）
# ⇒ 整个适配器的条目被压成**同一条前缀**，界面上 20 个条目只渲染出 1 行
# （实测：Trae CN 20 条 -> 1 行，而 CodeBuddy 16 条 -> 12 行）。
# 改成下划线后语义恢复为「前缀只做跨适配器唯一化，不参与聚合」：
# 聚合仍按第一个冒号切分，于是 ``…_ai_agent_db`` / ``…_ai_agent_db:wal`` 依旧
# 合成一行，而各逻辑项各占一行。
# --------------------------------------------------------------------------- #

#: TraeCode CN 的条目 key 前缀（唯一化用，见上）。
KEY_PREFIX_TRAE_CN = "trae_cn_"
#: TraeWork CN（SOLO）的条目 key 前缀（唯一化用，见上）。
KEY_PREFIX_TRAE_SOLO_CN = "trae_solo_cn_"


@register
class TraeCnAdapter(BaseAdapter):
    name = "trae-cn"
    #: 用户可见的产品名（安装目录仍为 ``Trae CN``）
    display_name = "TraeCode CN"

    #: Trae CN 专属压缩经验系数（档位 -> 类别 -> 压缩后/源 占比）。
    #:
    #:   - db     : ``ModularData/ai-agent/database.db``（已由产品侧加密/压缩，
    #:              再经 DEFLATE 几乎压不动，≈0.85）；``state.vscdb`` 同理 ≈0.6
    #:   - text   : ``settings.json`` 等小文本（≈0.15）
    #:   - struct : ``state.vscdb`` / ``storage.json``（≈0.45）
    #:   - binary : ``extensions/`` 内含大量二进制（≈0.99）
    #:   - other  : ``snapshot/`` 与杂项（≈0.5）
    COMPRESS_RATIO: dict[int, dict[str, float]] = {
        1: {  # 快速
            "text": 0.18,
            "db": 0.87,
            "struct": 0.5,
            "binary": 0.99,
            "other": 0.55,
        },
        6: {  # 正常（推荐）
            "text": 0.15,
            "db": 0.85,
            "struct": 0.45,
            "binary": 0.99,
            "other": 0.5,
        },
    }

    def detect_root(self) -> str | None:
        """探测 Trae CN 数据公共根（用户主目录 ``~``）。始终返回 ``~``。"""
        return _home()

    def build_default_root(self) -> str:
        """未探测到时的默认（建议）数据目录：用户主目录 ``~``。"""
        return _home()

    def detect_data_roots(self, root: str | None = None) -> list[dict]:
        return family_data_roots(root, TRAE_CN_PRODUCT, TRAE_CN_PRODUCT_DIR)

    def build_items(self, root_dir: str | None = None, current_uid: str | None = None) -> list[BackupItem]:
        """构造 Trae CN 备份条目（``root_dir`` 为公共根 ``~``）。

        ``current_uid`` 为兼容性参数（Trae 无 UID 拆分），忽略。
        """
        home = root_dir or _home()
        return build_family_items(
            home,
            user_data_dir(TRAE_CN_PRODUCT),
            extensions_dir(TRAE_CN_PRODUCT_DIR),
            key_prefix=KEY_PREFIX_TRAE_CN,
        )

    def match_structure(self, names: "Sequence[str]") -> "tuple[bool, list[str]]":
        """结构指纹：归档内出现 ``Trae CN/ModularData/ai-agent/database.db`` 即认作本工具。"""
        for n in names or []:
            j = n.replace("\\", "/")
            if j.endswith("Trae CN/ModularData/ai-agent/database.db"):
                return True, []
        return False, ["未找到 Trae CN/ModularData/ai-agent/database.db"]
