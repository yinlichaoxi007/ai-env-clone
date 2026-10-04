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
  3. 用户级设置：``~/.codebuddy/settings.json``（本机实测为 ``enabledPlugins``
     插件启用开关清单）。默认勾选。
     ⚠️ 同目录的 ``~/.codebuddy/skills-marketplace/`` **不列入备份**：本机实测它是
     **整个技能市场的本地镜像**（``skills/`` 295 个技能 ≈ 市场清单 293 条全量、
     ``icons/`` 137 个市场图标、``marketplace.json`` 目录索引，共 5019 文件 / 57.1 MB），
     属「市场数据」且可重新下载 —— 用户 2026-10-01 明确：skill 只备份本机 skill 数据，
     不备份 skill 市场数据。
  4. 用户级 MCP：``~/.codebuddy/mcp.json``（默认不勾）。
  5. 全局 IDE 配置：``~/.codebuddycn/``（``argv.json`` / ``extensions/``）。其中 ``argv.json``
     属「设置」类，默认勾选；``extensions/`` 属「扩展」类，默认不勾。
  6. 灵感（``~/.codebuddy/inspiration/``，按登录用户 UUID 隔离，非项目）默认勾选；
     专家历史（``~/.codebuddy/expert-history.json``）——用户级，默认不勾。
  7. **集中会话/检查点数据**（``%LOCALAPPDATA%/CodeBuddyExtension/Data/<uuid>/CodeBuddyIDE/<uuid>/``）：
     这是 CodeBuddy **跨工程集中存储的会话历史与 AI 回答检查点**（``history/`` 真实对话消息、
     ``check-point/`` 检查点快照、``plan-task/`` 计划任务等），**不是** ``<project>/.codebuddy``
     那种「项目级」数据（后者打包工程自然带着、不进选项）。该目录外层 ``<uuid>`` = 登录用户标识，
     是「用户级」集中存储，**必须备份**。其中 ``history/`` ``check-point/`` ``plan-task/`` 默认勾选；
     工程文件索引缓存 ``file-tree/`` 属程序自身运行态、重新打开工程即重建，与用户数据无关，
     **不列入备份选项**。
  8. **IDE 工作区记录**（``%APPDATA%/CodeBuddy CN/User/workspaceStorage/<hash>/workspace.json``）：
     内容是「这个工作区打开的是哪个工程文件夹」。它是会话工作区 id（``md5(项目路径)``，不可逆）
     反查路径的**唯一结构化映射**，却躺在 IDE 应用数据里、不属于任何会话/记忆条目——跨机还原
     后若丢失，从源机器搬来的会话就归不回原工程工作区。本身不含 IDE 运行态，属「让用户数据
     正确落位的参考信息」，默认勾选。（同目录 ``state.vscdb`` 与 ``globalStorage/storage.json``
     是运行态，不进包。）
  9. **会话工作区映射**（``<会话根>/session-workspaces.json``，**本工具生成、非产品数据**）：
     导出时自动生成（仅当勾选了集中会话）。它把每个工作区原本所属的工程路径**显式**记下来，
     随包携带、还原后回到会话根 —— 于是**不依赖「先还原 IDE 记录、再导入会话」的顺序**，
     跨机也能让界面说清「这条会话原本属于哪个工程」。能定路径的记路径；定不下来的保留正文
     候选路径（仅作提示，绝不自动采用）。实测量级：本机 21 个工作区中仅 1 个能还原出路径，
     所以推不出来的那部分改由**导入前显式确认**兜底（见 :meth:`CodeBuddyAdapter.export_generated`）。

所有用户级/全局数据的**公共根是用户主目录 ``~``**，故 ``detect_root`` / ``build_default_root``
均返回 ``~``，各条目 path 均在 ``~`` 之下，归档按相对路径落回原位，恢复干净。

注意区分两类「会话/项目」概念（用户 2026-08-08 纠正）：
- 「项目级」= ``<project>/.codebuddy/``（各工程自带、打包工程即带）→ 不进备份选项。
- 「集中会话」= ``CodeBuddyExtension/Data/<uuid>/CodeBuddyIDE/<uuid>/``（跨工程集中存储于
  用户 AppData，需显式备份）→ **进备份选项**，默认勾选（除工程索引 ``file-tree/``）。

备份哲学（统一标准，用户 2026-10-01 定策后更新）：默认勾选「无法从零重复创建、或缺失后
需重新逐项配置」的两类——① 会话、记忆、规则、IDE 工作区记录；② 设置、灵感、
自定义模型配置（后三者是为了「还原后立刻能开工」）。默认不勾**重新获取成本低**的两类
——插件、扩展、MCP、专家历史、其他用户数据、索引/运行态；程序自身的本地缓存、
运行态记录、日志与用户数据无关，不列入备份选项。含凭证的条目（models.json）
导出时一律脱敏。

**skill 的取舍**（用户 2026-10-01 明确）：只备份**本机 skill 数据**，不备份**skill 市场数据**。
CodeBuddy 这边没有独立的「本机技能目录」，唯一的 ``skills-marketplace/`` 就是市场镜像
（见上文第 3 条），故**整个不列入**。对比 WorkBuddy：``~/.workbuddy/skills/`` 存的是
本机安装的技能本体（含用户自建），照常默认勾选 —— 判断依据是**目录里装的是什么**，
不是目录名里有没有「skill」。

与 Qoder 不同，CodeBuddy 记忆是**单用户扁平结构**，无 UID 拆分（``inspiration/`` 下的
UUID 是登录用户标识，不是项目；``CodeBuddyIDE`` 外层 UUID 同理）。

新增其它工具时，仿照本文件新建 ``ai_env_clone/adapters/<tool>.py`` 即可，
用 ``@register`` 装饰类，无需改动任何入口代码。
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime

from .. import merge_plan
from ..core import (
    ORIGIN_NOTE_CONVERSATION,
    BackupItem,
    _longpath,
    _write_bytes_atomic,
)
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


# --------------------------------------------------------------------------- #
# IDE 侧「已打开文件夹」记录（把 workspaceId 还原回项目路径的唯一线索）
#
# 背景：CodeBuddy 的会话目录名 = ``md5(项目路径)``（见
# :func:`ai_env_clone.workspace_plan.codebuddy_workspace_id`），**哈希不可逆**。
# 会话的 ``index.json`` 里没有结构化的工作区/路径字段（只有 ``messages``/``requests``，
# 请求对象里也没有 ``cwd``/``folder``/``workspaceId``，2026-09-27 本机逐字段确认），
# 因此跨工具迁移时若只有哈希，就没法把会话归回它原本的工程工作区。
# 能反查的线索有两处：
#   1. **IDE 自己记下的「已打开文件夹」**（本函数）：把路径按同一规则求 md5，
#      与 history/ 下的目录名对齐即可（本机 1/1 命中）——但它只覆盖**本机打开过**的工程，
#      跨机还原后没打开过的工程就没有记录；
#   2. **会话正文里的绝对路径**（本文件下半部分「会话正文挖掘」）：随备份包一起过来，
#      但在本机没有 IDE 记录时才需要，且只有 md5 校验得上才能当结论用。
# --------------------------------------------------------------------------- #

#: IDE 应用数据根下产品目录名的前缀（``CodeBuddy`` / ``CodeBuddy CN`` …）。
_IDE_PRODUCT_PREFIX = "codebuddy"


def _ide_appdata_root() -> str:
    """CodeBuddy IDE（VS Code 分支）的应用数据根（其下即 ``<产品名>/User/``）。"""
    if sys.platform.startswith("win"):
        return os.environ.get("APPDATA", os.path.join(_home(), "AppData", "Roaming"))
    if sys.platform == "darwin":
        return os.path.join(_home(), "Library", "Application Support")
    return os.path.join(_home(), ".config")


def detect_ide_user_dirs(appdata_root: str | None = None) -> list[str]:
    """CodeBuddy IDE 的 ``User`` 目录候选（VS Code 每工作区存储就在这里）。

    本机实测：``%APPDATA%/CodeBuddy CN/User/{workspaceStorage,globalStorage}``。
    产品名随版本 / 地区而异（``CodeBuddy`` / ``CodeBuddy CN``），故按**前缀扫描**
    而不写死；带 ``extension`` 的同名目录（``CodeBuddyExtension``）属另一个产品，排除。

    :param appdata_root: 应用数据根；``None`` 时自动探测（便于测试注入临时目录）。
    """
    base = appdata_root or _ide_appdata_root()
    if not os.path.isdir(base):
        return []
    try:
        names = sorted(os.listdir(base))
    except OSError:
        return []
    out: list[str] = []
    for name in names:
        low = name.lower()
        if not low.startswith(_IDE_PRODUCT_PREFIX) or "extension" in low:
            continue
        user = os.path.join(base, name, "User")
        if os.path.isdir(user):
            out.append(user)
    return out


def file_uri_to_path(uri: str) -> str:
    """``file:///d%3A/project/x`` -> ``d:\\project\\x``；非本地 / 非法输入返回空串。

    IDE 记录里存的是 VS Code 的 folder URI（``file:///`` + 百分号编码），
    盘符前的斜杠要去掉（``/d:/...`` -> ``d:/...``），Windows 下统一回反斜杠，
    **盘符统一大写**（IDE 记的是小写 ``d:``，而 WorkBuddy 等产品自身记的是 ``D:``，
    不统一会在目标库里多出一条同名不同大小写的工作区记录）。
    远程工作区（``file://host/share``）没有本地路径，返回空串由调用方忽略。
    """
    if not isinstance(uri, str) or not uri.lower().startswith("file://"):
        return ""
    rest = uri[len("file://"):]
    if not rest.startswith("/"):
        return ""
    from urllib.parse import unquote
    path = unquote(rest)
    # ``/d:/project`` -> ``d:/project``（盘符前多余的前导斜杠）
    if len(path) > 2 and path[0] == "/" and path[1].isalpha() and path[2] == ":":
        path = path[1:]
    if os.name == "nt":
        path = path.replace("/", "\\")
        if len(path) > 1 and path[0].isalpha() and path[1] == ":":
            path = path[0].upper() + path[1:]
    return path


def iter_opened_workspace_paths(user_dirs: "Sequence[str] | None" = None) -> list[str]:
    """CodeBuddy IDE 记录过的「已打开文件夹」绝对路径（去重，保持发现顺序）。

    数据源（均为 IDE 自身记录，2026-09-27 本机实测）：

    1. ``<User>/workspaceStorage/<hash>/workspace.json``
       -> ``{"folder": "file:///d%3A/project/ai-env-clone"}``
    2. ``<User>/globalStorage/storage.json``
       -> ``profileAssociations.workspaces`` 的**键**（folder URI）
          与 ``windowsState`` 里的 ``folder``

    :param user_dirs: ``User`` 目录列表；``None`` 时自动探测。
    """
    dirs = list(user_dirs) if user_dirs is not None else detect_ide_user_dirs()
    uris: list[str] = []

    def _collect(obj) -> None:
        """递归收集任意嵌套结构里出现的 folder URI（含 dict 的键）。"""
        if isinstance(obj, str):
            if obj.lower().startswith("file://"):
                uris.append(obj)
        elif isinstance(obj, dict):
            for k, v in obj.items():
                _collect(k)
                _collect(v)
        elif isinstance(obj, list):
            for item in obj:
                _collect(item)

    def _load(path: str) -> None:
        try:
            with open(path, "r", encoding="utf-8") as f:
                _collect(json.load(f))
        except (OSError, ValueError):
            return

    for user in dirs:
        ws_root = os.path.join(user, "workspaceStorage")
        if os.path.isdir(ws_root):
            try:
                buckets = sorted(os.listdir(ws_root))
            except OSError:
                buckets = []
            for bucket in buckets:
                wj = os.path.join(ws_root, bucket, "workspace.json")
                if os.path.isfile(wj):
                    _load(wj)
        sj = os.path.join(user, "globalStorage", "storage.json")
        if os.path.isfile(sj):
            _load(sj)

    out: list[str] = []
    seen: set = set()
    for uri in uris:
        p = file_uri_to_path(uri)
        if p and p not in seen:
            seen.add(p)
            out.append(p)
    return out


def detect_ide_workspace_records(user_dirs: "Sequence[str] | None" = None) -> list[str]:
    """CodeBuddy IDE「每个工作区打开过哪个文件夹」的记录文件（``workspace.json``）。

    这些文件是 **``workspaceId`` 反查路径的唯一结构化映射**（见
    :func:`iter_opened_workspace_paths`），但它们躺在 IDE 的应用数据里、
    **不在任何工具的备份条目内**——于是跨机还原后会丢失：从另一台机器搬过来的会话，
    本机既没有 IDE 记录、又没打开过那个工程时，就只能退默认落点（详见机制文档 §2.8.1）。

    因此把**这些文件本身**纳入备份：内容只有 ``{"folder": "file:///d%3A/project/x"}``
    （本机实测 51~70 字节 / 个），不含任何 IDE 运行态，还原后映射就是现成的。

    只取 ``<User>/workspaceStorage/<hash>/workspace.json``，**不取**同目录下的
    ``state.vscdb``（IDE 运行态，单条 25~115 KB 且与用户数据无关）与
    ``globalStorage/storage.json``（窗口状态等运行态；其内容与 workspace.json 同源，
    本机实测三处 workspace.json 已覆盖全部路径，故不引入这份「覆盖会冲掉目标机窗口状态」
    的文件）。

    :param user_dirs: ``User`` 目录列表；``None`` 时自动探测。
    """
    dirs = list(user_dirs) if user_dirs is not None else detect_ide_user_dirs()
    out: list[str] = []
    for user in dirs:
        ws_root = os.path.join(user, "workspaceStorage")
        if not os.path.isdir(ws_root):
            continue
        try:
            buckets = sorted(os.listdir(ws_root))
        except OSError:
            continue
        for bucket in buckets:
            wj = os.path.join(ws_root, bucket, "workspace.json")
            if os.path.isfile(wj):
                out.append(wj)
    return out


def ide_workspace_record_item(record_path: str) -> BackupItem:
    """IDE 工作区记录（单个 ``workspace.json``）作为一个备份条目。

    ``key`` 带 ``:<hash>`` 尾缀：一个工作区一个文件，但 GUI 按首个 ``:`` 前的前缀
    聚合成一行（与 ``user_sessions:history`` 同机制），勾选状态统一。
    """
    bucket = os.path.basename(os.path.dirname(record_path))
    return BackupItem(
        key="ide_workspace_records:%s" % bucket,
        label="IDE 工作区记录（已打开文件夹）",
        path=record_path,
        uid=None,
        description="CodeBuddy IDE 记下的「打开过哪个工程文件夹」（%s/workspace.json）。"
                    "体积极小，是跨机还原后把会话正确归回原工程工作区的关键映射，"
                    "建议勾选。" % bucket,
        recommended=True,
        carries_origin="源机器上打开过的工程文件夹绝对路径",
    )


# --------------------------------------------------------------------------- #
# 会话正文挖掘：IDE 记录里没有该工作区时，从**会话正文**里找项目路径线索
#
# 背景（2026-09-27 本机逐文件实测，修正了此前「正文不带任何路径」的判断）：
#
# - ``history/<wid>/<sid>/messages/*.json``：工具调用参数、读文件、命令输出里
#   **大量出现绝对路径**（本机 24519 个 history JSON 里 20564 个命中）；
# - ``check-point/<wid>/<sid>/**``：快照本身带着被改文件的绝对路径（186 个文件命中）。
#
# 两者都在**默认备份范围内**，所以「另一台机器备份 → 本机还原」之后，路径线索
# 其实是随包一起过来的；缺的是「结构化字段」——``index.json`` 只有
# ``messages`` / ``requests``，请求对象里没有 ``cwd`` / ``folder`` / ``workspaceId``。
#
# 但「有线索」不等于「有答案」：正文里既有工程内的路径、也有工程外的，而
# 「哪一段前缀才是工程根」无法从字符串本身可靠判定（实测 7 个有线索的工作区里
# 3 个推错：推出过深的子目录、指到 IDE 的 playground、甚至把正文里的换行
# ``d:\n`` 当成路径）。因此这里只做两件事，**绝不自动采用猜测值**：
#
# 1. :func:`recover_workspace_path_by_hash`：把候选按同一规则求 md5 与目录名比对，
#    **哈希对得上才算**（md5 碰撞可忽略，这是唯一「可证明」的还原方式）；
# 2. :func:`session_project_root_hints`：还原不出来时给出**排好序的候选**，
#    由界面提示给人，让人决定要不要采用。
# --------------------------------------------------------------------------- #

#: 挖掘时只读这些扩展名（纯文本容器）。
_MINE_EXTS = (".json", ".jsonl", ".md", ".txt", ".log")
#: 单次挖掘的预算：预览要秒回，不能把整个 history 读一遍（单会话一轮约 60 个文件）。
_MINE_MAX_FILES = 60
_MINE_MAX_FILE_BYTES = 4 * 1024 * 1024
_MINE_MAX_TOTAL_BYTES = 12 * 1024 * 1024

#: 绝对路径字面量：盘符开头，正 / 反斜杠均可，反斜杠可连续出现（JSON 转义层数不定，
#: 真实数据里同一路径既有 2 个反斜杠也有 4 个的写法）。
_PATH_LITERAL_RX = re.compile(
    r"[A-Za-z]:(?:\\+|/)(?:[^\\/:*?\"<>|\r\n]{1,80})"
    r"(?:(?:\\+|/)[^\\/:*?\"<>|\r\n]{1,80})*"
)
#: 反斜杠被当转义符后产生的垃圾段（正文里的 ``d:\n`` / ``d:\t`` / ``d:\n1. Read``）。
#: 只认「单个转义字母 + 非路径续接字符」，避免误伤 ``nvidia`` / ``temp`` 这类正常目录名。
_JUNK_LEAD_RX = re.compile(r"^(?:n|r|t)(?:\d|[\s.,:;)\]\"'\-]|$)", re.IGNORECASE)
#: 空白段 / 控制字符段一律视为垃圾（``e:\ `` 这种）。
_JUNK_SEG_RX = re.compile(r"[\x00-\x1f]|^\s*$")


def unescape_path_literal(text: str) -> str:
    """把转义反斜杠压回单个并统一分隔符：``d:\\\\a\\\\b`` -> ``d:\\a\\b``。

    **反复压**到不再有成对反斜杠为止：真实数据里同一个路径既有 2 个反斜杠的写法
    （一层 JSON 转义），也有 4 个的（字符串里又套了一层）。单反斜杠不受影响。
    """
    out = text.replace("/", "\\")
    while "\\\\" in out:
        out = out.replace("\\\\", "\\")
    return out


def looks_like_junk_path(path: str) -> bool:
    """过滤「看着像路径、其实是正文里的转义序列 / 空白」的假命中。"""
    segs = [s for s in path.split("\\") if s]
    if len(segs) < 2:
        return True
    for seg in segs[1:]:
        if _JUNK_SEG_RX.search(seg) or _JUNK_LEAD_RX.match(seg):
            return True
    return False


def mine_session_path_occurrences(session_dir: str) -> dict:
    """挖出会话里出现过的绝对路径字面量 -> 出现次数（受预算约束）。

    :param session_dir: ``<history>/<workspaceId>/<sessionId>``。
    """
    occ: dict = {}
    if not session_dir or not os.path.isdir(session_dir):
        return occ
    n_files = 0
    n_bytes = 0
    for root, _dirs, files in os.walk(session_dir):
        for name in files:
            if not name.lower().endswith(_MINE_EXTS):
                continue
            full = os.path.join(root, name)
            try:
                size = os.path.getsize(full)
            except OSError:
                continue
            if size > _MINE_MAX_FILE_BYTES or n_bytes + size > _MINE_MAX_TOTAL_BYTES:
                continue
            try:
                with open(full, "r", encoding="utf-8", errors="replace") as f:
                    text = f.read()
            except OSError:
                continue
            n_files += 1
            n_bytes += size
            for raw in _PATH_LITERAL_RX.findall(text):
                lit = unescape_path_literal(raw)
                if looks_like_junk_path(lit):
                    continue
                occ[lit] = occ.get(lit, 0) + 1
            if n_files >= _MINE_MAX_FILES:
                return occ
    return occ


def session_project_root_hints(session_dir: str, limit: int = 3,
                               max_depth: int = 5, min_hits: int = 3,
                               min_files: int = 2) -> "list[tuple[str, int]]":
    """从会话正文里排出「可能的工程根」候选，供界面提示（**不保证正确**）。

    做法：把正文里每个绝对路径的各级祖先分别累计支持度（出现次数）与覆盖的
    不同文件数，再取达到门槛者按支持度排序。只用于「提示人」，**不参与自动判定**。

    :return: ``[(路径, 出现次数), ...]``，最多 ``limit`` 条（``limit <= 0`` 表示不限）。
    """
    occ = mine_session_path_occurrences(session_dir)
    if not occ:
        return []
    support: dict = {}
    covered: dict = {}
    for path, n in occ.items():
        parts = [s for s in path.split("\\") if s]
        for depth in range(2, min(len(parts), max_depth) + 1):
            anc = "\\".join(parts[:depth])
            support[anc] = support.get(anc, 0) + n
            covered.setdefault(anc, set()).add(path)
    rows = [(a, n) for a, n in support.items()
            if n >= min_hits and len(covered[a]) >= min_files]
    rows.sort(key=lambda kv: (-kv[1], len(kv[0]), kv[0]))
    return rows[:limit] if limit and limit > 0 else rows


# --------------------------------------------------------------------------- #
# 「会话 -> 原始工作区路径」映射：导出时随包携带
#
# 为什么需要它（用户 2026-10-01 提出，本机实测支撑）：
# - 会话目录名是 ``md5(项目路径)``，**不可逆**；而目标侧（WorkBuddy / DSH）的落点
#   必须是**路径**。跨机还原后若没有映射，会话只能落到工具默认目录
#   （``~/WorkBuddy/<时间戳>``），用户事后才发现，也说不清「它本来属于哪个工程」。
# - 该映射原本只存在于**源机器**的运行态里（IDE 的「已打开文件夹」记录）。把
#   ``workspace.json`` 纳入备份能救回一部分（见 :func:`ide_workspace_record_item`），
#   但**本机实测覆盖率仅 29%**（21 个工作区命中 1 个 = 9/31 条会话）。
# - 因此这里再落一份**显式、结构化、只读、随包走**的映射：能定路径的定下来；
#   定不下来的也保留正文候选路径 —— **至少让还原后的界面能提醒用户「它原本属于
#   哪里」**，而不是只丢一个默认落点。
# --------------------------------------------------------------------------- #

#: 会话工作区映射的文件名（落在会话根下，随备份包携带、还原后回到原位）。
SESSION_WORKSPACE_MAP_NAME = "session-workspaces.json"
SESSION_WORKSPACE_MAP_VERSION = 1

#: 构建映射时，最多对多少个工作区做「正文挖掘 + 哈希校验」（挖掘要读会话文件，耗时）。
_MAP_MAX_MINE = 200
#: 单个工作区最多尝试挖掘几条会话（同一工作区下的会话共享同一个工作区根）。
_MAP_MAX_SESSIONS_PER_WS = 3


def session_workspace_map_path(session_root: str) -> str:
    """「会话工作区映射」在会话根下的绝对落点。"""
    return os.path.join(session_root, SESSION_WORKSPACE_MAP_NAME)


def _iter_workspace_dirs(session_root: str) -> list:
    """``[(workspace_id, 目录绝对路径), ...]``。"""
    hist = os.path.join(session_root, "history")
    if not os.path.isdir(hist):
        return []
    try:
        names = sorted(os.listdir(hist))
    except OSError:
        return []
    return [(n, os.path.join(hist, n)) for n in names
            if os.path.isdir(os.path.join(hist, n))]


def _first_session_dirs(wdir: str, limit: int = _MAP_MAX_SESSIONS_PER_WS) -> list:
    """该工作区下最多 ``limit`` 个会话目录（挖掘用）。"""
    try:
        subs = sorted(x for x in os.listdir(wdir)
                      if os.path.isdir(os.path.join(wdir, x)))
    except OSError:
        return []
    return [os.path.join(wdir, s) for s in subs[:limit]]


def _ancestor_paths(occurrences: dict, max_depth: int = 8) -> list:
    """把「出现过的路径」展开成它们的各级祖先（去重、由浅到深）。"""
    seen: dict = {}
    for path in occurrences:
        parts = [s for s in path.split("\\") if s]
        for depth in range(2, min(len(parts), max_depth) + 1):
            seen.setdefault("\\".join(parts[:depth]), 0)
    return list(seen)


def mine_workspace_path(wid: str, session_dirs, wid_of) -> tuple[str, str]:
    """在会话正文里找该项目路径：**只有 md5 校验对得上才认**。

    ``md5`` 碰撞概率可忽略，所以这是「可证明」的还原方式，绝非猜测。

    :param wid_of: ``路径 -> workspaceId`` 的派生函数（由调用方传入，避免循环导入）。
    :return: ``(路径, 方式)``；找不到返回 ``("", "")``。
    """
    for sdir in session_dirs or []:
        occ = mine_session_path_occurrences(sdir)
        if not occ:
            continue
        for anc in _ancestor_paths(occ):
            if wid_of(anc) == wid:
                return anc, "content_hash"
    return "", ""


def build_session_workspace_map(session_root: str,
                                ide_paths: "list | None" = None,
                                max_mine: int = _MAP_MAX_MINE) -> dict:
    """构造「会话 -> 原始工作区路径」映射（导出时随包携带）。

    对 ``history/`` 下每个工作区尽力确定它原本的工程路径，优先级：

    1. **IDE「已打开文件夹」记录**（结构化、最可靠，``source = "ide_record"``）；
    2. **会话正文候选 + md5 校验**（可证明，``source = "content_hash"``）；
    3. 都失败 → ``path`` 留空，但保留 ``candidates``（正文里的路径线索，**只是线索、
       不采用**），供还原后界面提醒用户。

    实测（本机 21 个工作区 / 31 条会话）：1、2 合计覆盖 9 条（29%）。其余工作区的
    工程目录已不在磁盘上、正文里也没有它的完整形态 —— **这类推不出来是数据本身的
    限制，不是本函数的缺陷**；记录 ``candidates`` 正是为了在这种情况下仍能给出提示。
    """
    from ..workspace_plan import codebuddy_workspace_id  # 延迟导入，避免循环依赖

    if ide_paths is None:
        try:
            ide_paths = iter_opened_workspace_paths()
        except Exception:  # noqa: BLE001 - 探测属尽力而为，失败不拖垮导出
            ide_paths = []
    ide_index: dict = {}
    for p in ide_paths or []:
        w = codebuddy_workspace_id(p)
        if w and w not in ide_index:
            ide_index[w] = p

    workspaces: dict = {}
    total = resolved = 0
    mined = 0
    for wid, wdir in _iter_workspace_dirs(session_root):
        total += 1
        path = ide_index.get(wid, "")
        source = "ide_record" if path else ""
        candidates: list = []
        if not path and mined < max_mine:
            dirs = _first_session_dirs(wdir)
            if dirs:
                mined += 1
                path, source = mine_workspace_path(wid, dirs, codebuddy_workspace_id)
                if not path:
                    for d in dirs:
                        rows = session_project_root_hints(d, limit=3)
                        if rows:
                            candidates = [p for p, _n in rows]
                            break
        entry: dict = {"path": path, "source": source}
        if candidates:
            entry["candidates"] = candidates
        if path:
            resolved += 1
        workspaces[wid] = entry

    return {
        "version": SESSION_WORKSPACE_MAP_VERSION,
        "kind": "session-workspaces",
        "tool": "codebuddy",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "note": "本文件由 ai-env-clone 生成，随备份包一起走。它记录每条会话原本所属的"
                "工程工作区路径（会话目录名是项目路径的哈希、不可逆，源机器一旦丢失"
                "IDE 记录就再也推不出来）。还原后本工具据此把会话放回原工作区；"
                "推不出来的条目保留正文候选路径，仅用于提示用户。",
        "workspaces": workspaces,
        "stats": {"total": total, "resolved": resolved,
                  "unresolved": total - resolved},
    }


def load_session_workspace_map(session_root: str) -> dict:
    """读取会话根下的映射文件；不存在 / 损坏时返回 ``{}``（绝不抛异常）。"""
    p = session_workspace_map_path(session_root)
    if not os.path.isfile(p):
        return {}
    try:
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


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


def _merge_codebuddy_index(relpath: str, target: str, source_bytes: bytes) -> None:
    """合并钩子：把包内 ``index.json`` 清单与本机清单**并集**后原子写回。

    CodeBuddy 一个会话是「``index.json`` 清单 + ``messages/<mid>.json`` 实体」。
    还原旧包时若直接覆盖清单，本机新增消息会变成**孤儿文件**（磁盘上有、界面看不见）。
    并集（本机优先、按 id 补入包内独有项）正是消除该症状的手段，见方案 §2.3 / §3.4。
    """
    local = b""
    if os.path.isfile(_longpath(target)):
        with open(_longpath(target), "rb") as fh:
            local = fh.read()
    merged = merge_plan.union_json_by_id(source_bytes, local)
    _write_bytes_atomic(target, merged)


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

    # 3) 用户级设置（settings.json）。默认勾选。
    #    默认勾选的理由（用户 2026-10-01 定策）：设置是「还原后立刻能开工」的前提。
    items.append(
        BackupItem(
            key="user_settings",
            label="用户级设置（settings.json）",
            path=os.path.join(codebuddy_home, "settings.json"),
            uid=None,
            description="CodeBuddy 用户级设置（本机实测为 enabledPlugins 插件启用开关清单）。"
                        "默认勾选，还原后这些开关立即生效。",
            recommended=True,
        )
    )

    # 3b) ``~/.codebuddy/skills-marketplace/`` **不列入备份**（用户 2026-10-01 明确：
    #     「skill 只备份本机 skill 数据，不要备份 skill 市场数据」）。
    #     本机实测该目录是**整个技能市场的本地镜像**，不是「我装的技能」：
    #     ``skills/`` 295 个技能目录（≈ ``.codebuddy-skill/marketplace.json`` 里的
    #     293 条市场清单条目，即全量而非已装）、``icons/`` 137 个市场条目图标、
    #     ``marketplace.json`` 410 KB 目录索引、外加 ``logs/`` ``dist/`` 等市场客户端
    #     运行态 —— 合计 **5019 文件 / 57.1 MB**，且可由产品重新下载。
    #     ⇒ 属于「市场数据」，进包既臃肿又无「不可重建」价值。
    #     ⚠️ 与 WorkBuddy 的取舍要分清：那边 ``~/.workbuddy/skills/`` 存的是**本机装的
    #     技能本体**（含用户自建技能），所以照常默认勾选；只有 CodeBuddy 的目录名里
    #     才真的是一份市场镜像。

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

    # 5) 全局 IDE 配置（~/.codebuddycn/argv.json）。属「设置」类，默认勾选
    #    （用户 2026-10-01 定策：设置默认勾选，还原后立刻能开工）。
    items.append(
        BackupItem(
            key="global_argv",
            label="全局 IDE 配置（argv.json）",
            path=os.path.join(global_root, "argv.json"),
            uid=None,
            description="CodeBuddy 全局 IDE 配置（%s/argv.json）。默认勾选，还原后无需重新配置。"
            % global_root,
            recommended=True,
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
            carries_origin=ORIGIN_NOTE_CONVERSATION,
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
            carries_origin=ORIGIN_NOTE_CONVERSATION,
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

    # 7) IDE 工作区记录（<User>/workspaceStorage/<hash>/workspace.json）。
    #    内容 = 「这个工作区打开的是哪个文件夹」（folder URI），逐个 51~70 字节。
    #    存在的理由：CodeBuddy 的会话按 `md5(项目路径)` 派生的工作区 id 索引，而 id
    #    **不可逆**；这份记录是唯一的结构化「id → 路径」映射，却躺在 IDE 应用数据里、
    #    不在任何会话/记忆条目内。跨机还原后若丢失它，会话就归不回原工作区（只能退默认
    #    落点）。它不是程序运行态（不含 IDE 状态），而是**能让用户数据正确落位的参考信息**，
    #    故默认勾选。同目录的 state.vscdb（运行态）与 globalStorage/storage.json 不进包。
    for _rec in detect_ide_workspace_records():
        items.append(ide_workspace_record_item(_rec))

    # 8) 全局扩展目录（~/.codebuddycn/extensions/）。默认不勾（体积大、重装可恢复）。
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

    # 9) 灵感（~/.codebuddy/inspiration/，按登录用户 UUID 隔离，用户级）。默认勾选。
    items.append(
        BackupItem(
            key="user_inspiration",
            label="灵感（inspiration/）",
            path=os.path.join(codebuddy_home, "inspiration"),
            uid=None,
            description="CodeBuddy 灵感卡片（inspiration/，按登录用户隔离，用户级）。"
                        "用户自己攒的内容、无法从零重建，默认勾选。",
            recommended=True,
        )
    )

    # 10) 专家历史（~/.codebuddy/expert-history.json，用户级）。默认不勾。
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

    # 11) 插件（~/.codebuddy/plugins/）。默认不勾（体积大、重装可恢复）。
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

    # 12) 其他登录用户（Data/<uuid>，非当前用户）的集中会话/检查点。
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
                    carries_origin=(ORIGIN_NOTE_CONVERSATION
                                    if sub in ("history", "check-point") else ""),
                )
            )

    # 13) 自定义模型配置（~/.codebuddy/models.json，用户级）。默认勾选。
    #     可能含明文敏感凭证（如各模型的 apiKey / token / 私人令牌等）；备份时脱敏，
    #     恢复后需用户手动补填这些敏感凭证。敏感项。
    models_file = os.path.join(codebuddy_home, "models.json")
    items.append(
        BackupItem(
            key="user_models",
            label="自定义模型配置（models.json）",
            path=models_file,
            uid=None,
            description="CodeBuddy 自定义模型配置（models.json，可能含 apiKey/令牌等敏感凭证）。"
                        "默认勾选，还原后模型清单立即可用；但备份时已脱敏（敏感凭证替换为占位符），"
                        "恢复后需用户在目标机手动补填。",
            recommended=True,
            sensitive=True,
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
    #:   - binary : ``extensions/``、``plugins/`` 内含已编译/二进制（≈0.99）
    #:     （``skills-marketplace/`` 自 2026-10-01 起不列入备份，见 ``build_items`` 第 3b 条）
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

    #: 增量合并策略（方案 §2.3 / §4）。会话是**目录**结构：``index.json`` 清单走并集，
    #: ``messages/<mid>.json`` 实体与记忆 / 规则 / 检查点等一律**保留本机**、只补入缺的；
    #: 唯一配置（settings / mcp / argv）以包为准。未声明项取 :attr:`RESTORE_MERGE_DEFAULT`。
    RESTORE_POLICY: dict[str, str] = {
        "index.json": merge_plan.MERGE,
        "settings.json": merge_plan.REPLACE,
        "mcp.json": merge_plan.REPLACE,
        "argv.json": merge_plan.REPLACE,
    }
    RESTORE_MERGE_DEFAULT: str = merge_plan.KEEP_LOCAL
    RESTORE_CONFIG_FILES: tuple[str, ...] = ("settings.json", "mcp.json", "argv.json")

    def restore_merge_target(self) -> "Callable[[str, str, bytes], None] | None":
        """清单并集钩子：``index.json``（会话级与 ``messages/`` 聚合级）并集后写回。"""
        return _merge_codebuddy_index

    def detect_root(self) -> str | None:
        """探测 CodeBuddy 用户级/全局数据的公共根（用户主目录 ``~``）。始终返回 ``~``。"""
        return _home()

    def export_generated(self, items, root):
        """导出时附带「会话 -> 原始工作区路径」映射（随包携带、还原后回原位）。

        只在**勾选了集中会话条目**时生成：映射描述的是那些会话原本的归属，没备份
        会话就毫无意义。生成失败一律不影响备份本身（返回 ``(None, None)``）。

        这个文件的价值不在于「本机判断是否存在」（那是 :mod:`workspace_plan` 的活），
        而在于**跨机还原后仍能提醒用户**：那条会话原本属于哪个工程。若只靠 IDE 的
        「已打开文件夹」记录，本机实测只能覆盖 29% 的工作区。
        """
        keys = {getattr(i, "key", "") for i in items or []}
        if not any(k.startswith("user_sessions:") for k in keys):
            return None, None
        try:
            sroot = detect_session_root(detect_current_uid())
            data = build_session_workspace_map(sroot)
        except Exception:  # noqa: BLE001 - 附带信息，失败不拖垮备份
            return None, None
        stats = data.get("stats") or {}
        if not stats.get("total"):
            return None, None
        blob = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
        dest = session_workspace_map_path(sroot)
        # 同时落一份到本机会话根：
        #   - 包内那份负责**跨机** —— 还原后自动回到这里，导入时即可读到；
        #   - 磁盘这份负责**本机**「导出后立刻导入」的场景 —— 否则扫描会话时读不到
        #     映射，未确定落点的那些会话就连候选路径都带不出来。
        # 这不是产品数据、是我们自己的只读元数据；写失败不影响备份（包里那份仍在）。
        try:
            with open(dest, "wb") as f:
                f.write(blob)
        except OSError:
            pass
        meta = {"session_workspaces": {
            "file": SESSION_WORKSPACE_MAP_NAME,
            "total": stats.get("total", 0),
            "resolved": stats.get("resolved", 0),
            "unresolved": stats.get("unresolved", 0),
        }}
        return {dest: blob}, meta

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

    # ------------------------------------------------------------------ #
    # 导出脱敏：models.json 可能含明文敏感凭证，入库前替换为占位符
    # ------------------------------------------------------------------ #
    def export_transform_paths(self) -> "Sequence[str] | None":
        """
        ``models.json`` 里每个自定义模型可能含明文敏感凭证（apiKey / token / 令牌等），
        导出前脱敏。
        """
        return ["models.json"]

    # 字段名出现这些片段即视为「敏感凭证」，导出时脱敏
    SENSITIVE_HINTS = (
        "apikey", "api_key", "token", "secret", "password", "passwd",
        "accesskey", "access_key", "privatekey", "private_key",
        "credential", "auth",
    )

    @staticmethod
    def _looks_sensitive(key: str) -> bool:
        k = key.lower()
        return any(hint in k for hint in CodeBuddyAdapter.SENSITIVE_HINTS)

    def export_transform(self) -> "Callable[[str, bytes], bytes] | None":
        """
        扫描 ``models.json``，把任何「名称像敏感凭证」的字段（apiKey、token、secret、
        私人令牌等）的明文值替换为占位符 ``***REDACTED***``，避免明文凭证落入备份包。

        环境变量引用（``${ENV_VAR}`` 形式）本身不在配置文件里存明文，原样保留（跨电脑
        只需保证目标机环境变量一致）。非 JSON 内容原样保留，保证恢复后配置结构完整、
        模型条目仍可见，仅敏感凭证失效需用户在目标机手动补填。
        """
        REDACTED = "***REDACTED***"

        def _transform(rel_path: str, source: bytes) -> bytes:
            text = source.decode("utf-8", "replace")
            try:
                data = json.loads(text)
            except (ValueError, UnicodeDecodeError):
                return source  # 非 JSON，原样保留
            changed = False

            def _scrub(obj):
                nonlocal changed
                if isinstance(obj, dict):
                    for k, v in obj.items():
                        if CodeBuddyAdapter._looks_sensitive(k) and isinstance(v, str) and v:
                            # 仅脱敏「明文凭证」；环境变量引用不含明文，保留原样。
                            if not (v.startswith("${") and v.endswith("}")):
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
            # 尽量保留原缩进风格；ensure_ascii=False 保留中文模型名
            return json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")

        return _transform

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
