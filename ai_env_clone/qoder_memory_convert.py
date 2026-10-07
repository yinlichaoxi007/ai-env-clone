"""Qoder 旧版明文记忆 → 新版 ``memory/*.md`` 格式的一次性转换器。

背景（Qoder 新版要点文档 §3.3 / §九 ①）：产品从旧代（lingma 账号）到新代（阿里云
账号）换代会话时**没有回填**，记忆也只搬了 5/1043（本机实测：产品自己转换过 5 条，
其余 971 条 ``agent_memory`` + 72 条 ``lingma_memory`` 全部是**明文**、可读可转）。
这是本工具相对产品自带导入「唯一能拉开差距」的功能。

**独立功能，不属于备份适配器**——绝不塞进 ``restore`` 悄悄改用户数据；只写**新文件**
（合并不覆盖），默认 dry-run，``--apply`` 才落盘。

三条守住的规则：

1. **不碰向量虚表**：``*_embedding*`` 是 sqlite-vec 的 vec0 虚表及其 shadow 表，
   逐行 SELECT 会触发向量解码路径——本模块只 SELECT 两张普通表；
2. **合并不覆盖**：目标文件已存在（同名同目录）一律跳过并计数，绝不改写既有记忆；
3. **落点跟随实证**：产品自己转出的 5 条落在 ``memories/<uid>/projects/<key>/<分类>/<标题>.md``
   （新版 frontmatter + 旧版目录树）——这是唯一被证实「新版可见」的形态，故为默认
   （``legacy``）；``new``（``memory/`` + ``projects/<key>/memory/``）是要点文档 §3.3
   的设想布局，作为备选，用 ``--layout new`` 显式选择。
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from dataclasses import dataclass, field

from .core import _longpath

__all__ = [
    "LegacyMemory",
    "ConversionEntry",
    "ConversionPlan",
    "read_legacy_memories",
    "render_memory_md",
    "derive_project_key",
    "plan_conversion",
    "apply_conversion",
]

#: 非法文件名字符（Windows 保留集）→ 替换为 ``-``。
_BAD_FN = re.compile(r'[\\/:*?"<>|]')

#: ``usage_scenario`` 列的常见形态：空串 / ``"[]"`` / JSON 数组字面量。
_SCENARIO_RX = re.compile(r"^\s*\[.*\]\s*$", re.S)


@dataclass
class LegacyMemory:
    """旧版记忆表的一行（``agent_memory`` / ``lingma_memory``，均为明文）。"""

    source_table: str          # agent_memory | lingma_memory
    scope: str                 # global | workspace
    scope_id: str              # workspace 时为项目绝对路径（明文）
    title: str
    content: str
    keywords: "list[str]" = field(default_factory=list)
    category: str = ""
    usage_scenario: "list[str]" = field(default_factory=list)
    gmt_create: str = ""

    @property
    def source(self) -> str:
        return self.source_table


@dataclass
class ConversionEntry:
    """计划中的一条：一条记忆 → 一个目标文件。"""

    memory: LegacyMemory
    target_path: str
    action: str = "create"     # create | exists（已存在，跳过）；apply 后为 created
    project_key: str = ""      # workspace 记忆解析出的项目 key（global 为空）


@dataclass
class ConversionPlan:
    """dry-run 产物：全部条目 + 汇总。"""

    entries: "list[ConversionEntry]" = field(default_factory=list)
    skipped_empty: int = 0          # 标题与正文皆空，无内容可转
    layout: str = "legacy"
    uid: str = ""

    @property
    def to_create(self) -> int:
        return sum(1 for e in self.entries if e.action == "create")

    @property
    def existing(self) -> int:
        return sum(1 for e in self.entries if e.action == "exists")

    def summary_lines(self) -> "list[str]":
        lines = [
            "转换计划（layout=%s，uid=%s）：%d 条记忆 → 新建 %d，已存在跳过 %d"
            % (self.layout, self.uid or "（未指定）",
               len(self.entries), self.to_create, self.existing)
        ]
        by_table: dict = {}
        for e in self.entries:
            by_table[e.memory.source_table] = by_table.get(e.memory.source_table, 0) + 1
        for t, n in sorted(by_table.items()):
            lines.append("  - %s：%d 条" % (t, n))
        return lines


def _parse_list_column(value) -> "list[str]":
    """``usage_scenario`` 列：空 / ``"[]"`` / JSON 数组字面量 / 单条文本。"""
    if value is None:
        return []
    text = str(value).strip()
    if not text or text == "[]":
        return []
    if _SCENARIO_RX.match(text):
        try:
            data = json.loads(text)
            if isinstance(data, list):
                return [str(x).strip() for x in data if str(x).strip()]
        except ValueError:
            pass
    return [text]


def read_legacy_memories(db_path: str, include_lingma: bool = True) -> "list[LegacyMemory]":
    """读旧版记忆表（**只读打开**；只 SELECT 普通表，绝不触碰向量虚表）。

    :param include_lingma: 是否并入更老一代的 ``lingma_memory``（72 条，同为明文）。
    """
    uri = "file:%s?mode=ro" % os.path.abspath(db_path).replace("\\", "/")
    con = sqlite3.connect(uri, uri=True)
    out: "list[LegacyMemory]" = []
    try:
        cur = con.cursor()
        tables = {r[0] for r in cur.execute(
            "select name from sqlite_master where type='table'")}
        wanted = ["agent_memory"] + (["lingma_memory"] if include_lingma else [])
        for table in wanted:
            if table not in tables:
                continue
            cols = [c[1] for c in cur.execute("pragma table_info(%s)" % table)]
            has_scenario = "usage_scenario" in cols
            # ★ 显式列名，不 SELECT *：同库还有大量无关表与向量虚表，别碰。
            sql = ("select scope, scope_id, title, content, keywords, category,"
                   " gmt_create" + (", usage_scenario" if has_scenario else "")
                   + " from %s" % table)
            for row in cur.execute(sql):
                scope, scope_id, title, content, keywords, category, gmt = row[:7]
                scenario = _parse_list_column(row[7]) if has_scenario else []
                out.append(LegacyMemory(
                    source_table=table,
                    scope=str(scope or "").strip(),
                    scope_id=str(scope_id or "").strip(),
                    title=str(title or "").strip(),
                    content=str(content or ""),
                    keywords=[k.strip() for k in str(keywords or "").split(",")
                              if k.strip()],
                    category=str(category or "").strip(),
                    usage_scenario=scenario,
                    gmt_create=str(gmt or "")[:10],
                ))
    finally:
        con.close()
    return out


def _yaml_str(text: str) -> str:
    """按产品样本的形态输出 YAML 标量：双引号包裹、反斜杠与双引号转义。"""
    return '"%s"' % str(text).replace("\\", "\\\\").replace('"', '\\"')


def render_memory_md(mem: LegacyMemory) -> str:
    """渲染为新版格式。**逐字段对照产品自己转换出的样本**
    （``memories/<uid>/projects/<key>/project_tech_stack/NET后端技术栈.md``）：
    frontmatter 只有 ``title`` / ``usage_scenario`` / ``keywords`` 三键，正文原样。"""
    lines = ["---", "title: %s" % _yaml_str(mem.title or "未命名记忆")]
    lines.append("usage_scenario: [%s]" % ", ".join(
        _yaml_str(s) for s in (mem.usage_scenario or [])))
    if mem.keywords:
        lines.append("keywords:")
        lines.extend("    - %s" % _yaml_str(k) for k in mem.keywords)
    else:
        lines.append("keywords: []")
    lines.append("---")
    lines.append("")
    body = (mem.content or "").rstrip()
    lines.append(body)
    return "\n".join(lines) + "\n"


def _safe_filename(title: str) -> str:
    name = _BAD_FN.sub("-", (title or "").strip()) or "未命名记忆"
    return name[:80].rstrip(". ")


def derive_project_key(scope_id: str) -> str:
    """项目绝对路径 → 项目目录 key 的**基准推导形**。

    实测产品存在多种 key 拼法（``D-project-TbmHmi_Alpha`` / ``C--Users-…``），
    推导只产出一种基准（盘符冒号剥除、路径分隔符→``-``），**匹配已有目录时按
    多形态对照**（见 :func:`match_project_key`），匹配不到才用推导值新建。
    """
    text = (scope_id or "").strip().replace("/", "\\")
    text = text.replace(":", "").replace("\\", "-")
    return text.strip("-") or "unknown-project"


def match_project_key(scope_id: str, existing_keys: "list[str]") -> "tuple[str, bool]":
    """把 ``scope_id`` 对到**已存在**的项目 key 目录（合并不覆盖原则下的关键一步）。

    对照三种实测拼法：分隔符→``-``、盘符冒号→``-``、以及大小写不敏感。
    :return: ``(命中的 key, 是否命中)``；未命中返回 ``(推导形, False)``。
    """
    want = derive_project_key(scope_id)
    want_lower = want.lower()
    want_colon = scope_id.strip().replace("/", "\\").replace(":", "-") \
        .replace("\\", "-").strip("-").lower()
    for key in existing_keys:
        k = key.strip().strip("-")
        if k.lower() in (want_lower, want_colon):
            return key, True
    return want, False


def _uid_dir_name(qoder_home: str, uid: str) -> str:
    return uid or "unknown"


def plan_conversion(db_path: str, qoder_home: str,
                    layout: str = "legacy", uid: str = "",
                    include_lingma: bool = True) -> ConversionPlan:
    """生成转换计划（**不写盘**）：每条记忆一个目标文件，已存在即跳过。"""
    plan = ConversionPlan(layout=layout, uid=uid)
    memories = read_legacy_memories(db_path, include_lingma=include_lingma)
    uid_dir = _uid_dir_name(qoder_home, uid)

    projects_dir = os.path.join(qoder_home, "projects")
    legacy_projects_dir = os.path.join(qoder_home, "memories", uid_dir, "projects")
    existing_keys: "list[str]" = []
    for base in (projects_dir, legacy_projects_dir):
        if os.path.isdir(base):
            existing_keys.extend(os.listdir(base))

    for mem in memories:
        if not mem.title and not mem.content.strip():
            plan.skipped_empty += 1
            continue
        category = mem.category or "misc"
        filename = _safe_filename(mem.title or "未命名记忆") + ".md"
        if mem.scope == "workspace" and mem.scope_id:
            if layout == "legacy":
                key, _hit = match_project_key(mem.scope_id, existing_keys)
                target = os.path.join(qoder_home, "memories", uid_dir,
                                      "projects", key, category, filename)
            else:
                key, _hit = match_project_key(mem.scope_id, existing_keys)
                target = os.path.join(projects_dir, key, "memory", category,
                                      filename)
        else:
            if layout == "legacy":
                target = os.path.join(qoder_home, "memories", uid_dir,
                                      "global", category, filename)
            else:
                target = os.path.join(qoder_home, "memory", category, filename)
        entry = ConversionEntry(memory=mem, target_path=target,
                                project_key=derive_project_key(mem.scope_id)
                                if mem.scope == "workspace" else "")
        entry.action = "exists" if os.path.isfile(target) else "create"
        plan.entries.append(entry)
    return plan


def apply_conversion(plan: ConversionPlan) -> dict:
    """执行计划：只新建、绝不覆盖；**不修改计划本身**（dry-run 产物保持可复查）。

    返回 ``{"created", "skipped", "errors", "created_paths"}``。
    """
    created = skipped = 0
    errors: "list[str]" = []
    created_paths: "list[str]" = []
    for entry in plan.entries:
        if entry.action != "create":
            skipped += 1
            continue
        try:
            os.makedirs(os.path.dirname(_longpath(entry.target_path)),
                        exist_ok=True)
            with open(_longpath(entry.target_path), "w",
                      encoding="utf-8", newline="\n") as f:
                f.write(render_memory_md(entry.memory))
            created += 1
            created_paths.append(entry.target_path)
        except OSError as exc:
            errors.append("%s：%s" % (entry.target_path, exc))
    return {"created": created, "skipped": skipped, "errors": errors,
            "created_paths": created_paths}


def infer_qoder_home(db_path: str) -> str:
    """从 local.db 路径推断 Qoder 数据根。

    标准位置是 ``<根>/shared_client/cache/db/local.db``，逐层剥掉
    ``db`` / ``cache`` / ``shared_client`` 三层（★ 只剥一层会把根停在
    ``shared_client``，落点全错——真实 dry-run 的「已存在 859」就是这么来的）。
    """
    home = os.path.abspath(db_path)
    if os.path.isfile(home):
        home = os.path.dirname(home)
    if os.path.basename(home) == "local.db":
        home = os.path.dirname(home)
    for part in ("db", "cache", "shared_client"):
        if os.path.basename(home) == part:
            home = os.path.dirname(home)
    return home


def main(argv: "list[str] | None" = None) -> int:
    """CLI：``python -m ai_env_clone.qoder_memory_convert <local.db> [选项]``。

    默认 dry-run（只打印计划），``--apply`` 才写盘（合并不覆盖）。
    """
    import argparse
    import sys

    parser = argparse.ArgumentParser(
        prog="python -m ai_env_clone.qoder_memory_convert",
        description="Qoder 旧版明文记忆 → 新版 memory/*.md 一次性转换（默认 dry-run）",
    )
    parser.add_argument("db", help="旧版 local.db 路径（或 Qoder 数据根，自动找 local.db）")
    parser.add_argument("--qoder-home", default=None,
                        help="Qoder 数据根（默认取 local.db 所在的数据根推断）")
    parser.add_argument("--uid", default="",
                        help="记忆归属 uid 目录名（默认 unknown；可用适配器检测出的 uid）")
    parser.add_argument("--layout", choices=["legacy", "new"], default="legacy",
                        help="落点布局：legacy=产品已证实可见的形态（默认），new=要点文档 §3.3 设想")
    parser.add_argument("--no-lingma", action="store_true",
                        help="不并入更老一代的 lingma_memory 表")
    parser.add_argument("--apply", action="store_true",
                        help="真正写盘（合并不覆盖；默认只打印计划）")
    args = parser.parse_args(argv)

    if sys.platform == "win32":
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (AttributeError, ValueError):
                pass

    db = args.db
    if os.path.isdir(db):
        candidate = os.path.join(db, "shared_client", "cache", "db", "local.db")
        db = candidate if os.path.isfile(candidate) else os.path.join(db, "local.db")
    if not os.path.isfile(db):
        print("找不到 local.db：%s" % db, file=sys.stderr)
        return 2
    qoder_home = args.qoder_home or infer_qoder_home(db)

    plan = plan_conversion(db, qoder_home, layout=args.layout, uid=args.uid,
                           include_lingma=not args.no_lingma)
    for line in plan.summary_lines():
        print(line)
    shown = 0
    for entry in plan.entries:
        if entry.action != "create" and shown >= 5:
            continue
        if shown < 20:
            print("  [%s] %s ← %s（%s）" % (
                entry.action, os.path.relpath(entry.target_path, qoder_home),
                entry.memory.title[:40], entry.memory.source_table))
            shown += 1
    if len(plan.entries) > 20:
        print("  …共 %d 条（其余省略）" % len(plan.entries))
    if not args.apply:
        print("\ndry-run 未写盘；确认无误后加 --apply 执行（合并不覆盖）。")
        return 0
    result = apply_conversion(plan)
    print("\n写盘完成：新建 %d，跳过（已存在）%d，失败 %d"
          % (result["created"], result["skipped"], len(result["errors"])))
    for err in result["errors"][:10]:
        print("  失败：", err)
    return 1 if result["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
