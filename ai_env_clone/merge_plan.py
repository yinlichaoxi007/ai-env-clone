"""还原「增量合并」的纯计算层（不依赖 tkinter，便于单测）。

``core.import_backup`` 的 merge 分支只负责编排（解析包 → 快照 → 提交 → 失败回滚），
「某个成员该按哪种策略处理」「怎么把包内记录并入本机」都落在本模块。

策略取值（与 ``docs/local/还原增量合并方案.md`` §3.1 / §4 一致）：

- ``replace``    整文件覆盖（以包为准）——包里那份就是最终结果；
- ``merge``      调用适配器的就地合并钩子，把包内记录**并入**目标，不改动目标其余数据；
- ``keep_local`` 目标已存在则**跳过**（保留本机），不存在才写入。

冲突取值口径（§4）：与目标机强相关的数据（本机已有会话/记忆/规则）本机优先，
与备份包强相关的数据（新增会话、唯一配置）以包为准。
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from typing import Mapping, Sequence

REPLACE = "replace"
MERGE = "merge"
KEEP_LOCAL = "keep_local"

#: 全部合法策略（供契约测试枚举）。
POLICIES = (REPLACE, MERGE, KEEP_LOCAL)


# --------------------------------------------------------------------------- #
# 策略匹配
# --------------------------------------------------------------------------- #
def match_policy(relpath_norm: str, policy: Mapping[str, str] | None) -> str | None:
    """按**后缀**匹配策略；未命中返回 ``None``。

    归档内成员名相对公共根带根占位前缀（如 ``C__Users_x/.workbuddy/workbuddy.db``），
    故声明的相对片段用后缀判定，避免写死根前缀。成员名与片段统一规范为正斜杠。
    """
    if not policy:
        return None
    rel = (relpath_norm or "").replace("\\", "/")
    for suffix, mode in policy.items():
        frag = "/" + str(suffix).replace("\\", "/").lstrip("/")
        if rel == frag.lstrip("/") or rel.endswith(frag):
            return mode
    return None


# --------------------------------------------------------------------------- #
# 清单 / 索引：JSON 并集
# --------------------------------------------------------------------------- #
def _loads(data: bytes):
    if not data:
        return None
    try:
        return json.loads(data.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None


def _item_id(item):
    if isinstance(item, dict):
        return item.get("id")
    return None


def _all_items_have_id(seq: list) -> bool:
    return bool(seq) and all(_item_id(it) is not None for it in seq)


def union_json_by_id(source_bytes: bytes, local_bytes: bytes) -> bytes:
    """把两个 JSON 对象**并集**：本机优先；「含 id 的 dict 列表」按 id 并集。

    用于「清单 / 索引」类文件（CodeBuddy 的 ``index.json``）：直接覆盖会让本机较新的
    消息从清单里消失（文件还在磁盘上、界面却看不见，见方案 §2.3）。并集后两侧条目都在。

    规则：
    - 本机不是合法 JSON 对象 ⇒ 直接采用包内字节（等同覆盖）；
    - 包内不是合法 JSON 对象 ⇒ 保留本机（不写坏）；
    - 顶层键：本机缺的补入；同键且两侧都是「含 id 的 dict 列表」⇒ 按 id 并集；
      其余同键（标量 / 非 id 列表）**本机优先**，保持不动。
    """
    src = _loads(source_bytes)
    loc = _loads(local_bytes)
    if not isinstance(loc, dict):
        return source_bytes
    if not isinstance(src, dict):
        return local_bytes
    merged = dict(loc)
    for key, sval in src.items():
        if key not in merged:
            merged[key] = sval
            continue
        lval = merged[key]
        if isinstance(lval, list) and isinstance(sval, list):
            if not lval or not sval or (
                _all_items_have_id(lval) and _all_items_have_id(sval)
            ):
                seen = {_item_id(it) for it in lval}
                merged[key] = list(lval) + [
                    it for it in sval if _item_id(it) not in seen
                ]
    return json.dumps(merged, ensure_ascii=False, indent=2).encode("utf-8")


# --------------------------------------------------------------------------- #
# SQLite 库内并入（白名单表，单事务）
# --------------------------------------------------------------------------- #
def _quote(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _table_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    cur = conn.execute("SELECT * FROM %s LIMIT 0" % _quote(table))
    return [d[0] for d in (cur.description or [])]


def _tables(conn: sqlite3.Connection) -> set[str]:
    cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    return {r[0] for r in cur.fetchall()}


def _insert_sql(table: str, cols: Sequence[str], clause: str) -> str:
    """按规则拼 INSERT 语句。

    ``clause`` 支持两种形态（与方案 §3.5 一致）：
    - ``"insert or ignore"``（默认）⇒ ``INSERT OR IGNORE INTO t(...) VALUES (...)``
    - ``"on conflict(<cols>) do update set <...>"`` ⇒ 追加在 VALUES 之后
    """
    collist = ", ".join(_quote(c) for c in cols)
    placeholders = ", ".join("?" for _ in cols)
    base = "INSERT %sINTO %s(%s) VALUES (%s)" % (
        "OR IGNORE " if clause.strip().lower().startswith("insert or ignore") else "",
        _quote(table),
        collist,
        placeholders,
    )
    if clause.strip().lower().startswith("on conflict"):
        base += " " + clause
    return base


def merge_sqlite_tables(
    target_path: str,
    source_bytes: bytes,
    table_rules: Mapping[str, str],
    report: dict | None = None,
) -> dict:
    """把 ``source_bytes``（SQLite 库）中**白名单表**的记录并入 ``target_path``。

    - 源库落到**临时文件**后以只读方式打开（不把整库读进内存，方案 §5 风险 5）；
    - 逐表**显式列名**取源/目标列交集；源库缺列 / 目标库缺列都跳过该列并计入报告
      （应对 schema 漂移，方案 §5 风险 3）；
    - **单事务**提交，异常即回滚（方案 §3.4）；白名单外的表**绝不触碰**
      （应用自身状态保持本机原样，方案 §4）。

    :param table_rules: ``{表名: 冲突规则}``，冲突规则见 :func:`_insert_sql`。
    :param report: 可选，函数会往里填 ``{表名: {"rows": n, "skipped_columns": [...]}}``。
    :return: 同 ``report``。
    """
    report = report if report is not None else {}
    fd, tmp_path = tempfile.mkstemp(prefix="aienv_merge_", suffix=".db")
    os.close(fd)
    try:
        with open(tmp_path, "wb") as fh:
            fh.write(source_bytes)
        uri = "file:%s?mode=ro" % tmp_path.replace("?", "%3f").replace("#", "%23")
        src = sqlite3.connect(uri, uri=True)
        dst = sqlite3.connect(target_path)
        dst.isolation_level = None  # 自行显式控制事务
        try:
            dst_tables = _tables(dst)
            dst.execute("BEGIN IMMEDIATE")
            try:
                for table, clause in table_rules.items():
                    if table not in dst_tables:
                        report[table] = {"rows": 0, "skipped_columns": [],
                                         "skipped": "目标库无此表"}
                        continue
                    src_cols = _table_columns(src, table)
                    dst_cols = set(_table_columns(dst, table))
                    if not src_cols:
                        report[table] = {"rows": 0, "skipped_columns": [],
                                         "skipped": "源库无此表"}
                        continue
                    common = [c for c in src_cols if c in dst_cols]
                    missing = [c for c in src_cols if c not in dst_cols]
                    if not common:
                        report[table] = {"rows": 0, "skipped_columns": missing,
                                         "skipped": "源/目标列无交集"}
                        continue
                    select_sql = "SELECT %s FROM %s" % (
                        ", ".join(_quote(c) for c in common), _quote(table),
                    )
                    rows = src.execute(select_sql).fetchall()
                    ins = _insert_sql(table, common, clause)
                    before = dst.total_changes
                    dst.executemany(ins, rows)
                    report[table] = {
                        "rows": dst.total_changes - before,
                        "candidates": len(rows),
                        "skipped_columns": missing,
                    }
                dst.execute("COMMIT")
            except BaseException:
                dst.execute("ROLLBACK")
                raise
        finally:
            src.close()
            dst.close()
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
    return report


#: WorkBuddy 的白名单合并表（方案 §3.5）：只碰这三张与会话相关的表，
#: 其余（``automations`` / ``buddy_snapshots`` / ``migration_meta`` /
#: ``__workbuddy_drizzle_migrations`` / …）属应用自身状态，**绝不触碰**。
WORKBUDDY_MERGE_TABLES: dict[str, str] = {
    "sessions": "insert or ignore",
    "workspaces": (
        "on conflict(path) do update set "
        "last_opened_at=max(workspaces.last_opened_at, excluded.last_opened_at)"
    ),
    "session_usage": "insert or ignore",
}


def merge_workbuddy_db(target_path: str, source_bytes: bytes) -> dict:
    """WorkBuddy ``workbuddy.db`` 的就地合并（白名单三表）。"""
    return merge_sqlite_tables(target_path, source_bytes, WORKBUDDY_MERGE_TABLES)


# --------------------------------------------------------------------------- #
# 版本倒挂检测（只读，不写任何库）
# --------------------------------------------------------------------------- #
#: WorkBuddy 里「描述 App 自身迁移进度」的表，及其**代表迁移身份**的列。
#: 合并时这些表**绝不写**（本机优先），但它们恰好是「包来自哪个版本」的证据：
#: 包里有而本机没有的迁移项 ⇒ 源机 App 比本机新。
WORKBUDDY_VERSION_TABLES: tuple[tuple[str, str], ...] = (
    ("migration_meta", "key"),
    ("__workbuddy_drizzle_migrations", "hash"),
)


def _open_ro(path: str) -> sqlite3.Connection:
    """只读打开 SQLite（``mode=ro``：绝不因为一次提示就把用户库写出改动）。"""
    uri = "file:%s?mode=ro" % path.replace("?", "%3f").replace("#", "%23")
    return sqlite3.connect(uri, uri=True)


def _key_column_values(conn: sqlite3.Connection, table: str, column: str) -> "set | None":
    """取该表该列的取值集合；**表或列不存在返回 ``None``**（区别于「空表」的空集合）。"""
    if table not in _tables(conn):
        return None
    cols = _table_columns(conn, table)
    if column not in cols:
        return None
    return {r[0] for r in conn.execute("SELECT %s FROM %s" % (_quote(column), _quote(table)))}


def detect_version_regression(
    source_bytes: bytes,
    target_path: str,
    version_tables: "Sequence[tuple[str, str]]",
    label: str = "",
) -> list[str]:
    """判断「包比本机新」，返回给用户看的提示（没有则空列表）。

    为什么需要它：增量合并刻意**不动**应用自身的迁移记录（本机优先），于是当包来自
    更高版本时，会出现「数据行已经并进、本机 App 却没跑过对应的迁移」的状态——
    轻则新字段读不出，重则会话列表看不全。这一步在写盘**之前**只做只读比对，
    把这件事告诉用户，而不是让它变成一次神秘的「还原成功但界面不对」。

    判定口径：``version_tables`` 里每张表取「代表迁移身份」的那一列，
    **包里有、本机没有**的取值数 > 0 即视为倒挂。反方向（本机更新）是安全的，不提示。

    任何读取失败都返回空列表：一个提示不值得打断已经算好的还原计划。
    """
    if not version_tables or not source_bytes:
        return []
    if not os.path.isfile(target_path):
        return []          # 本机还没有这个库 ⇒ 不存在「比本机新」
    fd, tmp_path = tempfile.mkstemp(prefix="aienv_verchk_", suffix=".db")
    os.close(fd)
    src = dst = None
    try:
        with open(tmp_path, "wb") as fh:
            fh.write(source_bytes)
        src = _open_ro(tmp_path)
        dst = _open_ro(target_path)
        gaps: list[tuple[str, int]] = []
        for table, column in version_tables:
            src_keys = _key_column_values(src, table, column)
            if not src_keys:
                continue
            dst_keys = _key_column_values(dst, table, column)
            missing = src_keys if dst_keys is None else (src_keys - dst_keys)
            if missing:
                gaps.append((table, len(missing)))
        if not gaps:
            return []
        who = label or "该工具"
        detail = "、".join("%s 有 %d 项" % (t, n) for t, n in gaps)
        return [
            "包内的数据比本机 %s 更新（%s，本机没有对应的迁移记录）。"
            "「合并」只并数据行、不会替本机 %s 执行迁移，低版本可能读不出新结构下的内容；"
            "建议先把本机 %s 升到不低于源机的版本，或改用「全覆盖」（会先给本机生成回滚快照）。"
            % (who, detail, who, who)
        ]
    except Exception:  # noqa: BLE001 - 提示环节不得影响还原
        return []
    finally:
        if src is not None:
            src.close()
        if dst is not None:
            dst.close()
        try:
            os.remove(tmp_path)
        except OSError:
            pass
