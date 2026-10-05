"""
跨软件「数据导入」能力矩阵。

本模块声明「目标工具 ← 可导入的来源软件」的能力清单，是 GUI「数据导入」区
与导入流程的**唯一事实来源**：每个目标工具列出可导入的来源软件、实测版本、
可导入的数据范围与实现状态。实际的解析 / 原生写出逻辑由
:mod:`ai_env_clone.session_migration` 提供。

矩阵语义
--------

``status`` 取值：

- :data:`SUPPORTED`：解析 + 原生写出均已实现，可直接导入（会话历史为主）。
- :data:`BACKUP_ONLY`：仅支持整库备份 / 还原；会话主库为**产品侧加密**，
  无法转换成其它软件的原生格式（这是产品设计，不是本工具的缺陷）。
- :data:`PLANNED`：结构已探明，写出实现待实测确认（暂不可作为导入目标）。

``scope``：可导入的数据范围，逐条人类可读。

导向说明
--------

「导入」在这里 = **把来源软件的历史会话，以目标软件的原生格式写出**，
使其能被目标软件像原生会话一样打开（见 ``session_migration``）。
备份/还原（整库不透明拷贝）是另一条独立通路，由各适配器的 ``export`` /
``restore`` 负责，与本矩阵无关。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Sequence

# --------------------------------------------------------------------------- #
# 状态常量
# --------------------------------------------------------------------------- #
SUPPORTED = "supported"
BACKUP_ONLY = "backup_only"
PLANNED = "planned"

#: 状态 -> 界面展示文案
STATUS_LABELS: dict[str, str] = {
    SUPPORTED: "支持导入",
    BACKUP_ONLY: "仅备份/还原",
    PLANNED: "待支持",
}

#: 状态 -> 界面配色（前景色，浅色主题下可读）
STATUS_COLORS: dict[str, str] = {
    SUPPORTED: "#0a7a3d",
    BACKUP_ONLY: "#a05a00",
    PLANNED: "#777777",
}


# --------------------------------------------------------------------------- #
# 数据模型
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class SourceCapability:
    """「某个来源软件 → 某目标软件」的一条导入能力。"""

    tool: str                       # 来源软件标识（适配器 name）
    display: str                    # 来源软件展示名
    versions: str                   # 实测 / 适配版本
    scope: tuple                    # 可导入的数据范围（人类可读字符串元组）
    status: str                     # SUPPORTED / BACKUP_ONLY / PLANNED
    note: str = ""                  # 附加说明（限制、落点提示等）
    parser: str = ""                # session_migration.SessionParser 方法名（仅 SUPPORTED）
    writer: str = ""                # session_migration.SessionWriter 方法名（仅 SUPPORTED）
    changes: tuple = ()             # 作为导入源时【必然发生】的变化（info 级）
    losses: tuple = ()              # 需要注意的来源侧损失 / 降级（warn 级）

    @property
    def status_label(self) -> str:
        return STATUS_LABELS.get(self.status, self.status)


@dataclass(frozen=True)
class TargetCapability:
    """「某目标软件」可接受的全部来源。"""

    tool: str                       # 目标软件标识
    display: str                    # 目标软件展示名
    sources: tuple = ()             # SourceCapability 元组

    @property
    def importable(self) -> tuple:
        """可真正导入（status == SUPPORTED）的来源。"""
        return tuple(s for s in self.sources if s.status == SUPPORTED)

    @property
    def has_any(self) -> bool:
        return bool(self.sources)


# --------------------------------------------------------------------------- #
# 版本（本机实测，**2026-10-02 复采**）
#
# 取值口径：优先取各工具**本地安装目录自带的版本清单**与**系统「应用和功能」
# 里给用户看的版本**，不联网查询——
#   ``resources/app/product.json``（genieVersion / appVersion / tronBuildVersion）、
#   ``resources/app/package.json``（version）、注册表 Uninstall 的 DisplayVersion。
# 展示名同理：以**用户实际看到的产品名**为准（注册表 DisplayName /
#   product.json 的 win32NameVersion），而不是安装目录名。
# 举例：安装目录叫 ``TRAE SOLO CN``，但对用户展示的产品名是 ``TraeWork CN``
#   （``win32NameVersion``），另一个是 ``TraeCode CN``（目录 ``Trae CN``）。
# 同时存在「构建版本」与「应用版本」时，以工具**自身给用户看的版本**为主，
#   另一者列在括号内（tronBuildVersion / genieVersion）。
#
# ★ 改这里的任何数字时，**必须同步 README「支持的工具」版本表**：两处口径一致，
#   否则会出现「界面说 A、文档说 B」。README 的版本说明段写明了允许的取值来源。
# --------------------------------------------------------------------------- #
V_WORKBUDDY = "5.6.2"
V_CODEBUDDY = "1.106.1，genie 版本 4.12.0"
V_QODER = "0.4.3（另一台 0.3.4）"
V_TRAE = "3.3.99，构建版本 2.3.82600"            # 安装目录 Trae CN，产品名 TraeCode CN
V_TRAE_WORK = "0.1.69，构建版本 2.3.87413"        # 安装目录 TRAE SOLO CN，产品名 TraeWork CN
V_ZCODE = "3.14.4"
# DSH：0.2.0-rc.2 起 `$DSH_HOME/settings.yaml` 已被移除（设置改存
# `profiles/<profile>/cordis.patch.yml`）⇒ 备份条目随之调整，版本号必须写实。
V_DSH = "0.2.0-rc.2"
V_REASONIX = "1.21.5，当前未安装"

#: 各来源软件的展示名与版本（多处复用）
_SRC_META: dict[str, tuple] = {
    "reasonix": ("Reasonix", V_REASONIX),
    "codebuddy": ("CodeBuddy CN", V_CODEBUDDY),
    "workbuddy": ("WorkBuddy", V_WORKBUDDY),
    "zcode": ("ZCode", V_ZCODE),
    "dsh": ("DeepSeek Harness", V_DSH),
    "qoder": ("Qoder CN", V_QODER),
    "trae-cn": ("TraeCode CN", V_TRAE),
    "trae-solo-cn": ("TraeWork CN", V_TRAE_WORK),
}

#: 适配器 name -> 展示名（英文文档 / 界面别名，避免各处硬编码）
DISPLAY_ALIASES: dict[str, str] = {
    "trae-solo-cn": "TraeWork CN（安装目录 TRAE SOLO CN）",
    "trae-cn": "TraeCode CN（安装目录 Trae CN）",
}

#: 会话历史（统一命名，便于矩阵中复用）
_SESSION_SCOPE = ("历史会话（用户 / 助手消息、推理过程、工具调用）",)

# --------------------------------------------------------------------------- #
# 变化预告：把「导入后会变成什么样」变成数据（界面直接渲染，见方案 §6.3）
#
# 中间模型 Session / SessionMessage 只有 role / content / reasoning_content /
# tool_calls / created_at / title / scope 七个字段 —— **凡这七个之外的都带不过去**。
# 在这里声明成数据，是为了让「源侧变化」「目标侧损失」都有单一事实来源，
# 将来改 writer 忘改文案会被 tests/test_import_notices.py 的契约测试拦下。
# --------------------------------------------------------------------------- #
#: 所有来源共有的「必然变化」（A 级，info）。各来源若确有差异可单独覆盖。
COMMON_CHANGES: tuple = (
    "会话 id 与消息 id 全部重新生成，不会覆盖目标机已有会话",
    "没有时间戳的消息会记为「导入时刻」",
    "模型信息、token 用量、计费统计不带过去",
    "会话级设置（系统提示词 / 置顶 / 归档 / 标签）不带过去",
    "多模态内容（图片 / 附件）会被拍平成文本或丢弃",
)

#: 目标工具 -> 写出时的**内容层损失**（C 级，warn）；键与写出口径一一对应。
#: 值为 ``(损失标识, 人类可读说明)``；契约测试据此核对 writer 行为。
TARGET_LOSSES: dict[str, tuple] = {
    "codebuddy": (
        ("tool_calls", "工具调用不会写出：目标会话里看不到「调用了什么工具」，只剩文本"),
    ),
    "workbuddy": (
        ("tool_results", "工具执行结果不会作为独立结果块写出，只留在助手正文的文本里"),
    ),
    "dsh": (
        ("tool_calls", "工具调用不会写出：目标会话里看不到「调用了什么工具」，只剩正文文本"),
        ("tool_results", "工具执行结果不会写出"),
    ),
    "reasonix": (),
}

#: 目标工具 -> 分组 / 归属的重新映射规则（B 级，warn）。
TARGET_GROUPING: dict[str, tuple] = {
    "reasonix": (
        "分组按「项目名」重新映射：项目名里的非字母数字字符会被替换为 '-'，"
        "取不到时落到 global-workspace",
    ),
    "codebuddy": (
        "分组按目标 workspaceId 重新映射（由项目路径哈希派生、不可逆）："
        "优先用包内的会话工作区映射反查，反查不到则落到默认落点",
    ),
    "workbuddy": (
        "分组按「工作区路径」重新映射：目标机已有同名工作区时，导入的会话会"
        "并入该工作区（表现为分组被合并）",
    ),
    "dsh": (
        "分组按「工作区路径」重新映射并登记进 storages/workspace.json："
        "目标机已有同名工作区时会并入",
    ),
}

#: 目标工具 -> 无工作区信息时的兜底落点（B5 级，warn）。
TARGET_DEFAULT_LANDING: dict[str, str] = {
    "reasonix": "无工作区信息的会话落到 global-workspace",
    "codebuddy": "无工作区信息的会话落到目标工具的默认落点",
    "workbuddy": "无工作区信息的会话落到 ~/WorkBuddy/<时间戳>（本次现造的新目录）",
    "dsh": "无工作区信息的会话落到用户主目录对应的默认工作区",
}


# --------------------------------------------------------------------------- #
# 来源能力模板
# --------------------------------------------------------------------------- #
def _src(tool: str, scope: Sequence[str] = _SESSION_SCOPE, status: str = SUPPORTED,
         note: str = "", parser: str = "", writer: str = "",
         changes: Sequence[str] = COMMON_CHANGES,
         losses: Sequence[str] = ()) -> SourceCapability:
    display, versions = _SRC_META[tool]
    return SourceCapability(
        tool=tool, display=display, versions=versions, scope=tuple(scope),
        status=status, note=note, parser=parser, writer=writer,
        changes=tuple(changes), losses=tuple(losses),
    )


# 明文可读 → 可解析、可写出
_PLAIN_SOURCES: dict[str, SourceCapability] = {
    "reasonix": _src("reasonix", status=SUPPORTED,
                     parser="parse_reasonix", writer="write_reasonix",
                     note="明文 JSONL，无损（含推理过程）。"),
    "codebuddy": _src(
        "codebuddy", status=SUPPORTED,
        scope=_SESSION_SCOPE + ("规则 / 记忆文件（按文件拷贝，非会话格式）",),
        parser="parse_codebuddy", writer="write_codebuddy",
        note="明文分片 JSON；目标机需项目路径同源，否则会话可能不被索引。"
             "工作区 id 由项目路径哈希派生（不可逆），导入 WorkBuddy / DSH 时会先按 IDE 的"
             "「已打开文件夹」记录反查回路径；反查不到则落到目标工具默认落点并在预览里说明。",
    ),
    "workbuddy": _src(
        "workbuddy", status=SUPPORTED,
        scope=("历史会话（事件流：消息 / 推理 / 工具调用 / 工具结果）",),
        parser="parse_workbuddy", writer="write_workbuddy",
        note="明文 JSONL；写出时同时登记 workbuddy.db 的 sessions 行。",
        losses=("一个助手回合的多条事件（推理 / 工具调用 / 工具结果 / 正文）"
                "会被聚合为一条助手消息",),
    ),
    "zcode": _src(
        "zcode", status=SUPPORTED,
        scope=("历史会话（message / part 表，含正文与推理）",),
        parser="parse_zcode", writer="",
        note="明文 SQLite，只读解析；ZCode 自身的写出格式尚未实测，暂不作为导入目标。",
        losses=("未知类型的消息片段会尽量保留可读文本，可能不是原文结构",),
    ),
    "dsh": _src(
        "dsh", status=SUPPORTED,
        scope=("历史会话（事件流：user/assistant message、tool call）",),
        parser="parse_dsh", writer="write_dsh",
        note="Zstandard 压缩 JSONL；读 / 写均需 zstd 后端（工具内可一键安装）。",
        losses=("相邻的「纯工具调用」助手消息会被合并为一条",),
    ),
}

# 主库不可解析（列级密文 / 私有格式）→ 只能整库备份/还原
_ENCRYPTED_SOURCES: dict[str, SourceCapability] = {
    "qoder": _src(
        "qoder", status=BACKUP_ONLY, changes=(),
        scope=("整库备份 / 还原（会话库不可解析）",),
        note="旧版 local.db 为标准 SQLite 但会话正文列为列级密文、新版会话分散在各自的 SQLite / 明文 JSONL 中，均无法跨软件转换格式。",
    ),
    "trae-cn": _src(
        "trae-cn", status=BACKUP_ONLY, changes=(),
        scope=("整库备份 / 还原（会话库不可解析）",),
        note="智能体会话为产品侧加密的 database.db，非标准 SQLite，无法解析。",
    ),
    "trae-solo-cn": _src(
        "trae-solo-cn", status=BACKUP_ONLY, changes=(),
        scope=("整库备份 / 还原（会话库不可解析）",),
        note="与 TraeCode CN 同族，会话同为产品侧加密，无法解析。",
    ),
}

#: 全部来源（含加密），按 tool 索引
ALL_SOURCES: dict[str, SourceCapability] = {**_PLAIN_SOURCES, **_ENCRYPTED_SOURCES}


def _sources_for(target: str, order: Sequence[str]) -> tuple:
    """为 ``target`` 组装来源列表（排除自身；保留 ``order`` 给定顺序）。"""
    out = []
    for tool in order:
        if tool == target:
            continue
        cap = ALL_SOURCES.get(tool)
        if cap is not None:
            out.append(cap)
    return tuple(out)


#: reasonix 目标：明文来源里除自身外都可导入
_MATRIX: dict[str, TargetCapability] = {
    "reasonix": TargetCapability(
        tool="reasonix", display=_SRC_META["reasonix"][0],
        sources=_sources_for("reasonix", ("codebuddy", "workbuddy", "zcode", "dsh")),
    ),
    "codebuddy": TargetCapability(
        tool="codebuddy", display=_SRC_META["codebuddy"][0],
        sources=_sources_for("codebuddy", ("reasonix", "workbuddy", "zcode", "dsh")),
    ),
    "workbuddy": TargetCapability(
        tool="workbuddy", display=_SRC_META["workbuddy"][0],
        sources=_sources_for("workbuddy", ("reasonix", "codebuddy", "zcode", "dsh")),
    ),
    "dsh": TargetCapability(
        tool="dsh", display=_SRC_META["dsh"][0],
        sources=_sources_for("dsh", ("reasonix", "codebuddy", "workbuddy", "zcode")),
    ),
    # ZCode 写出格式待实测：明确标为「不作为导入目标」，但把可解析的来源列出（供导出用）
    "zcode": TargetCapability(
        tool="zcode", display=_SRC_META["zcode"][0],
        sources=(
            SourceCapability(
                tool="*", display="ZCode 会话",
                versions=_SRC_META["zcode"][1],
                scope=("可把 ZCode 会话导出到其它工具（见各目标工具）",),
                status=PLANNED,
                note="ZCode 的 session/message/part 写出格式尚未在本机实测确认，"
                     "为避免写坏其库，暂不支持「导入到 ZCode」；反向导出已支持。",
            ),
        ),
    ),
    # 加密工具：只能整库备份/还原
    "qoder": TargetCapability(
        tool="qoder", display=_SRC_META["qoder"][0],
        sources=(
            SourceCapability(
                tool="*", display="Qoder CN 会话",
                versions=_SRC_META["qoder"][1], scope=(),
                status=BACKUP_ONLY,
                note="Qoder 会话正文不可解析（旧版 local.db 为列级密文），只能整体备份 / 还原，不能与其它软件互换会话。",
            ),
        ),
    ),
    "trae-cn": TargetCapability(
        tool="trae-cn", display=_SRC_META["trae-cn"][0],
        sources=(
            SourceCapability(
                tool="*", display="TraeCode CN 会话",
                versions=_SRC_META["trae-cn"][1], scope=(),
                status=BACKUP_ONLY,
                note="TraeCode CN（安装目录 Trae CN）智能体会话为产品侧加密数据库，"
                     "只能整体备份 / 还原。",
            ),
        ),
    ),
    "trae-solo-cn": TargetCapability(
        tool="trae-solo-cn", display=_SRC_META["trae-solo-cn"][0],
        sources=(
            SourceCapability(
                tool="*", display="TraeWork CN 会话",
                versions=_SRC_META["trae-solo-cn"][1], scope=(),
                status=BACKUP_ONLY,
                note="TraeWork CN（安装目录 TRAE SOLO CN）会话为产品侧加密数据库，"
                     "只能整体备份 / 还原。",
            ),
        ),
    ),
}


# --------------------------------------------------------------------------- #
# 查询接口
# --------------------------------------------------------------------------- #
def matrix_for(tool: str) -> Optional[TargetCapability]:
    """返回目标工具 ``tool`` 的导入能力；未登记返回 ``None``。"""
    return _MATRIX.get(tool)


def known_targets() -> list:
    """返回全部已登记的目标工具标识（保持定义顺序）。"""
    return list(_MATRIX)


def session_import_targets() -> list:
    """返回**支持接收会话导入**的目标工具标识（即至少有一个 SUPPORTED 来源）。"""
    return [t for t, cap in _MATRIX.items() if cap.importable]


def importable_sources_for(target: str) -> tuple:
    """返回目标工具 ``target`` 可真正导入（status == SUPPORTED）的来源能力元组。

    未登记 / 无可用来源时返回空元组（调用方无需判 None）。
    """
    cap = _MATRIX.get(target)
    return cap.importable if cap is not None else ()


def notices_for(source_tool: str, target_tool: str) -> dict:
    """组装「本次导入的变化预告」三段文案（界面直接渲染）。

    :return: ``{"must": [...], "warn": [...], "keep": [...]}``

        - ``must``：**必然变化**（info 级）—— 来自来源侧的 ``changes``；
        - ``warn``：**需要注意**（warn 级）—— 来源侧损失 + 目标侧分组重映射 +
          目标侧兜底落点 + 目标侧内容损失；
        - ``keep``：**不影响**（info 级）—— 用户真正关心的「什么会被完整保留」，
          按目标工具是否会丢工具调用 / 工具结果动态生成。

    同工具（``source_tool == target_tool``）不产生预告（不走导入通路）。
    """
    if not source_tool or not target_tool or source_tool == target_tool:
        return {"must": [], "warn": [], "keep": []}

    src = ALL_SOURCES.get(source_tool)
    must = list(src.changes) if src is not None else []

    warn: list = []
    if src is not None:
        warn.extend(src.losses)
    warn.extend(TARGET_GROUPING.get(target_tool, ()))
    landing = TARGET_DEFAULT_LANDING.get(target_tool)
    if landing:
        warn.append(landing)
    warn.extend(text for _key, text in TARGET_LOSSES.get(target_tool, ()))

    return {"must": must, "warn": warn, "keep": list(_keep_for(target_tool))}


def _keep_for(target_tool: str) -> tuple:
    """「不影响的」清单：按目标工具是否会丢工具调用 / 工具结果动态生成。"""
    dropped = {key for key, _text in TARGET_LOSSES.get(target_tool, ())}
    out = ["用户正文与助手正文均保留", "推理过程（thinking）保留"]
    if "tool_calls" not in dropped:
        out.append("工具调用的名称与参数保留")
    if "tool_results" not in dropped:
        out.append("工具执行结果保留")
    return tuple(out)


def describe(tool: str) -> dict:
    """把目标工具 ``tool`` 的导入能力整理成界面易用的字典。

    :return: ``{"tool","display","has_importable","rows":[ {...} ], "summary"}``
        ``rows`` 每项：``display / versions / scope(list) / status / status_label /
        color / note``；``summary`` 为一句概览。
    """
    cap = _MATRIX.get(tool)
    if cap is None:
        return {"tool": tool, "display": tool, "has_importable": False,
                "rows": [], "summary": "该工具未登记导入能力。"}

    rows = []
    for s in cap.sources:
        rows.append({
            "display": s.display,
            "versions": s.versions,
            "scope": list(s.scope),
            "status": s.status,
            "status_label": s.status_label,
            "color": STATUS_COLORS.get(s.status, "#333333"),
            "note": s.note,
        })

    importable = cap.importable
    if importable:
        names = "、".join(s.display for s in importable)
        summary = "可导入 %d 个来源：%s" % (len(importable), names)
    elif any(r["status"] == BACKUP_ONLY for r in rows):
        summary = "仅支持整库备份 / 还原（会话加密，不能跨软件导入）"
    else:
        summary = "暂不支持导入（写出格式待实测）"
    return {
        "tool": tool, "display": cap.display,
        "has_importable": bool(importable),
        "rows": rows, "summary": summary,
    }


def format_lines(tool: str) -> list:
    """把导入能力整理成多行纯文本（CLI / 弹窗 复用）。"""
    info = describe(tool)
    lines = ["【%s】数据导入能力：%s" % (info["display"], info["summary"])]
    for r in info["rows"]:
        head = "· %s（%s）— %s" % (r["display"], r["versions"], r["status_label"])
        lines.append(head)
        for item in r["scope"]:
            lines.append("    - 可导入范围：%s" % item)
        if r["note"]:
            lines.append("    说明：%s" % r["note"])
    return lines
