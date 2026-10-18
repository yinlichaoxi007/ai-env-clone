"""旧版 Qoder CN（lingma 账号）历史会话 → 新版桌面端 ``main.sqlite`` 的**有损**导入。

背景
----
产品从 lingma 账号换代到阿里云账号时只搬了**记忆**、没回填**会话**：新版侧栏读的是
``%APPDATA%/com.qodercn.app.stable/main.sqlite`` 的 ``chat_sessions`` + ``workspaces``，
而旧会话留在旧版 ``local.db``，其 ``chat_message.content`` / ``chat_record.question``
是**列级密文**（容器本身仍是标准 SQLite，整库可备份、正文取不出）。真正能取出的明文
只有产品自己写的**文字稿**
``~/.qoder-cn/cache/projects/<key>/conversation-history/<sid8>/<sid8>.jsonl``
——本机实测 16 份、3381 条记录**全部只有** ``type="text"``，工具调用与推理过程不在里面。

所以这里是**有损导入**：把文字稿的可见文字，按新版会话行的真实形状（全部实测自本机
产品自写行）写进 ``main.sqlite``，使旧会话在新版里**可见、可搜、可续聊**。
2026-10-18 已用 1 条会话实测通过。

能力边界（界面与 CLI 都必须如实说）
--------------------------------
- **不进模型上下文**：导入的轮次只进展示层与检索。续聊时 CLI 会话文件从新一轮才开始写，
  直接问「之前你做了什么」模型答「没有记录」；让它 ``read_chat_session`` 查本会话才读得到。
- **只有文字**：没有工具调用 / 工具结果 / diff / 推理过程。
- **时间是插值**：文字稿本身没有时间戳，按旧库会话首尾时刻在轮次间线性插值（属推断）。
- **不进官方导入账本**：``sessionMigration.sqlite`` 不体现本次导入。

安全前提（写进代码的硬约束）
--------------------------
1. 默认 dry-run，只有 :func:`apply_import` 才写盘；
2. 首次写入前对 ``main.sqlite`` 做一次**在线快照**并记 sha256；之后每次写入都先复核
   快照摘要——对不上说明回退依据已不可信，直接中止；
3. 写入前 ``integrity_check`` + 主键冲突检查，写入后读回逐字段 + 哈希校验；
4. 单事务 + ``begin immediate``：拿不到写锁（Qoder 正在运行）就拒绝，绝不留下
   「写了但没生效」的现场；
5. 所有 ID 用 ``uuid5`` 由**旧会话 id 确定性派生**：重复导入不会写出第二份，
   也不可能与产品自产的随机 ID 相撞；
6. **只摘自己写的行**：:func:`rollback_import` 按写入台账逐会话删（先片段、再消息、
   后会话），绝不整库覆盖，绝不碰产品写的行；
7. 台账只存 ID 与哈希，**不存会话正文与标题**；
8. **账号形态是写前的最后一道闸**：:func:`preflight` 复核「当前账号已是新版
   （UUIDv7）且与旧会话归属账号不同」——界面靠门控决定入口给不给，命令行没有
   界面把关，这一步保证 ``--uid`` 传错也不会把旧会话补进还没换代的库里。
"""

from __future__ import annotations

import datetime as _dt
import glob
import hashlib
import json
import os
import re
import shutil
import sqlite3
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .core import _longpath, snapshot_sqlite

__all__ = [
    "Eligibility", "check_eligibility",
    "LegacyTurn", "LegacySession", "list_legacy_sessions", "find_transcripts",
    "TargetShape", "read_target_shape",
    "SessionPlan", "ImportPlan", "plan_import",
    "ImportAbort", "apply_import", "verify_import", "rollback_import",
    "resolve_new_db", "resolve_old_dbs",
]

#: 本模块验证过的载荷 schema 版本。**只有这些版本才允许写盘**：形状是按 0.4.8 的
#: 产品自写行逐字段复刻的，产品升版后载荷结构可能变（``payload_schema_version`` 曾经
#: 从 1 变成 2），届时宁可拒绝，也不要凭猜测写进用户的活库。
VALIDATED_PS_VERSIONS: Tuple[int, ...] = (2,)

#: 新版会话侧栏可见性谓词（实测：满足即出现在列表里，按 updated_at 倒序）。
SIDEBAR_WHERE = ("deleted_at is null and archived = 0 and session_kind = 'standard'")

#: 台账 / 快照文件名后缀（放在 ``main.sqlite`` 同目录，产品数据目录里自带说明性）。
SNAPSHOT_SUFFIX = ".before-lingma-import"
MANIFEST_SUFFIX = ".before-lingma-import.manifest.json"

_UUID_RX = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
                      r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_NUMERIC_UID_RX = re.compile(r"^\d{10,20}$")

#: 写入时**刻意不写**的关联表（界面与 CLI 都要列出来，避免用户以为「全都有」）。
NOT_WRITTEN_TABLES: Tuple[str, ...] = (
    "chat_session_input_receipts",    # 输入回执：产品自己的派发凭证，伪造会让重开时会话状态异常
    "chat_session_context_usage",     # 上下文用量：产品会按新会话自己统计
    "chat_session_highlights",        # 摘要要点：需模型生成
    "chat_session_recaps",            # 回顾：需模型生成
    "turn_file_change_sets",          # 文件改动集：文字稿里没有改动信息
    "chat_session_turn_states",       # 回合状态：会话已终结，无状态可恢复
    "chat_session_sidebar_placements",
    "chat_session_sidebar_group_memberships",
)


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #
def _ro_uri(db_path: str) -> str:
    return "file:%s?mode=ro" % os.path.abspath(db_path).replace("\\", "/").replace("?", "%3f")


def _open_ro(db_path: str) -> sqlite3.Connection:
    con = sqlite3.connect(_ro_uri(db_path), uri=True)
    con.row_factory = sqlite3.Row
    return con


def _norm_key(path: str) -> str:
    """工作区路径的比对键：反斜杠归一 + 小写（Windows 路径大小写不敏感）。"""
    return (path or "").strip().replace("/", "\\").rstrip("\\").lower()


def _jsonz(obj: Any) -> str:
    """紧凑 JSON —— 与应用侧写法一致（冒号后不空格），否则 ``payload_hash`` 对不上。"""
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def _iso(ms: int) -> str:
    return (_dt.datetime.fromtimestamp(ms / 1000, _dt.timezone.utc)
            .strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (ms % 1000))


def _now_ms() -> int:
    return int(_dt.datetime.now(_dt.timezone.utc).timestamp() * 1000)


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(_longpath(path), "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _load_json(path: str) -> Any:
    with open(_longpath(path), encoding="utf-8") as fh:
        return json.load(fh)


def _sql_str(v: Any) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, (int, float)):
        return str(v)
    return "'" + str(v).replace("'", "''") + "'"


def _table_exists(con: sqlite3.Connection, name: str) -> bool:
    return con.execute("select 1 from sqlite_master where type='table' and name=?",
                       (name,)).fetchone() is not None


# --------------------------------------------------------------------------- #
# 1) 路径解析与资格判定（界面据此决定「此功能是否提供」）
# --------------------------------------------------------------------------- #
def resolve_old_dbs(home: str = "") -> List[str]:
    """旧版会话库候选（存在才返回）。

    两代旧根并存且都会留有会话：``~/.qoder-cn/shared_client/cache/db/local.db``（主）
    与 ``~/.lingma/cache/db/local.db``（更老一代）。
    """
    home = home or os.path.expanduser("~")
    out = []
    for base in (os.path.join(home, ".qoder-cn", "shared_client"),
                 os.path.join(home, ".lingma")):
        p = os.path.join(base, "cache", "db", "local.db")
        if os.path.isfile(p):
            out.append(p)
    return out


def resolve_new_db(home: str = "") -> str:
    """新版桌面端会话库路径（``main.sqlite``）；取不到返回空串。"""
    from .adapters.qoder import detect_electron_root
    base = detect_electron_root(home or None)
    p = os.path.join(base, "main.sqlite")
    return p if os.path.isfile(p) else ""


@dataclass(frozen=True)
class Eligibility:
    """「本机是否具备提供 lingma 会话导入的条件」。"""

    ready: bool
    reasons: Tuple[str, ...] = ()          # 未满足的条件（界面逐条展示）
    notices: Tuple[str, ...] = ()          # 满足但仍要提醒用户的事
    old_dbs: Tuple[str, ...] = ()
    new_db: str = ""
    legacy_uid: str = ""                   # 旧库里会话归属的账号（16 位数字形态）
    current_uid: str = ""                  # 界面「当前用户」下拉选中的账号
    transcript_root: str = ""
    sessions_total: int = 0
    sessions_with_transcript: int = 0

    @property
    def reason_text(self) -> str:
        return "；".join(self.reasons) if self.reasons else "条件已满足"


def is_new_generation_uid(uid: str) -> bool:
    """所选账号是否「新版（阿里云）账号」形态：完整 UUIDv7。

    旧 lingma 账号在本机数据里是 16 位纯数字（``1316632530577119``，目录名截成前 8 位
    ``13166325``），新版登录态与 ``.models/<uid>`` 用的是 UUIDv7
    （``019eb095-47ef-70d2-8a41-ea6278c176b1``）。**形态就是账号体系的差别**，
    不是同一份数据的两种写法——旧账号不可能长成 UUIDv7。
    """
    return bool(_UUID_RX.match((uid or "").strip()))


def check_eligibility(home: str = "", current_uid: str = "",
                      new_db: str = "", old_dbs: Optional[Sequence[str]] = None) -> Eligibility:
    """判定能否在界面上提供「lingma 会话导入」。

    四个条件**全部**成立才 ``ready=True``（用户的原话是「检测到数据路径均存在，且当前
    所选用户就是新的阿里云账号，才可在此提供此功能」）：

    1. 旧版会话库存在（至少一份 ``local.db``）；
    2. 新版会话库 ``main.sqlite`` 存在（不存在 = 新版桌面端还没建库，写进去也没人读）；
    3. 当前所选账号是**新版阿里云账号**（UUIDv7 形态），且与旧库里会话归属的账号不是
       同一个 —— 若仍停在旧账号，说明「换代」这件事在本机还没发生，导入属于自找麻烦；
       **取不到账号也算不满足**（判不出等于没判）；
    4. 取到明文文字稿（没有文字稿就没有可导入的正文，旧库正文是列级密文）。
    """
    home = home or os.path.expanduser("~")
    dbs = list(old_dbs) if old_dbs is not None else resolve_old_dbs(home)
    nd = new_db or resolve_new_db(home)
    reasons: List[str] = []
    notices: List[str] = []

    if not dbs:
        reasons.append("没找到旧版会话库 local.db（查过 ~/.qoder-cn/shared_client/cache/db/ "
                       "与 ~/.lingma/cache/db/）")
    if not nd:
        reasons.append("没找到新版桌面端会话库 main.sqlite：请先启动一次新版 Qoder CN"
                       "（阿里云账号登录）让它建库")
    elif not os.path.isfile(nd):
        reasons.append("新版会话库 main.sqlite 不存在：%s" % nd)

    # 旧库里会话归属的账号（可能多个，取出现最多的那个作为代表）
    legacy_uid = ""
    legacy_counts: Dict[str, int] = {}
    for db in dbs:
        try:
            con = _open_ro(db)
        except sqlite3.Error:
            continue
        try:
            if _table_exists(con, "chat_session"):
                for r in con.execute("select user_id, count(*) c from chat_session"
                                     " group by user_id order by c desc"):
                    uid = str(r["user_id"] or "")
                    legacy_counts[uid] = legacy_counts.get(uid, 0) + int(r["c"])
        except sqlite3.Error:
            pass
        finally:
            con.close()
    if legacy_counts:
        legacy_uid = max(legacy_counts, key=lambda k: legacy_counts[k])

    cur = (current_uid or "").strip()
    if not cur:
        from .adapters.qoder import detect_current_uid, detect_qoder_root
        paths = detect_qoder_root(os.path.join(home, ".qoder-cn"))
        cur = detect_current_uid(paths.root, paths.shared) or ""
        if cur:
            notices.append("界面未指定当前用户，已按本机登录态自动取 %s…" % cur[:8])

    if not cur:
        reasons.append("没取到当前登录账号：请先在新版 Qoder CN 里登录一次阿里云账号，"
                       "或在本工具的「当前用户」下拉里选一个 —— 条件 3 要判的就是这个账号")
    elif not is_new_generation_uid(cur):
        reasons.append("当前所选用户「%s」不是新版阿里云账号（UUIDv7 形态），"
                       "看起来还是旧 lingma 账号的写法——本功能只在换到新账号后提供" % cur)
    if cur and legacy_uid and _uid_same(cur, legacy_uid):
        reasons.append("当前所选用户与旧会话所属账号（%s）仍是同一个，本机还没发生换代；"
                       "旧会话本来就在当前账号的库里，无需导入" % legacy_uid)

    tr_root = os.path.join(home, ".qoder-cn", "cache", "projects")
    transcripts = find_transcripts(home)
    total = with_ts = 0
    seen: set = set()
    for db in dbs:
        try:
            con = _open_ro(db)
        except sqlite3.Error:
            continue
        try:
            if _table_exists(con, "chat_session"):
                for r in con.execute("select session_id from chat_session"):
                    sid = str(r["session_id"] or "")
                    # 两份旧库会共存同一条会话（本机实测 .lingma 里那条在 shared_client
                    # 也有）：按 id 去重，否则「会话 29 条」与计划里的「共 28 条」对不上。
                    if not sid or sid in seen:
                        continue
                    seen.add(sid)
                    total += 1
                    if transcripts.get(sid[:8]):
                        with_ts += 1
        except sqlite3.Error:
            pass
        finally:
            con.close()
    if not transcripts:
        reasons.append("没找到明文文字稿（%s）：旧库正文是列级密文，只有文字稿里的明文可用"
                       % tr_root)
    elif with_ts == 0:
        reasons.append("文字稿与旧库会话对不上（会话 %d 条、命中 0 条）" % total)

    # schema 漂移：会话族表或 FTS 维护触发器缺失 ⇒ 直接判定不具备条件
    if nd and os.path.isfile(nd):
        drift = schema_drift(nd)
        if drift:
            reasons.append("新版库结构与本工具验证过的形状不一致：%s" % "；".join(drift))

    notices.append("导入是有损的：只有文字稿里的可见文字，没有工具调用 / 推理过程，"
                   "轮次时间按插值推断")
    notices.append("导入的旧轮次不进模型上下文：续聊时模型读不到旧内容，"
                   "除非它主动用 read_chat_session 查本会话")
    notices.append("写入前请先完全退出 Qoder CN（含系统托盘）：否则拿不到写锁，"
                   "或新版内存态覆盖掉刚写入的行")
    notices.append("不写官方导入账本 sessionMigration.sqlite；侧栏按更新时间排序，"
                   "导入的会话排在旧会话下方，可用标题或搜索定位")

    return Eligibility(
        ready=not reasons, reasons=tuple(reasons), notices=tuple(notices),
        old_dbs=tuple(dbs), new_db=nd, legacy_uid=legacy_uid, current_uid=cur,
        transcript_root=tr_root, sessions_total=total, sessions_with_transcript=with_ts,
    )


def _uid_prefix(uid: str) -> str:
    return (uid or "").split("-")[0][:8].lower()


def _uid_same(a: str, b: str) -> bool:
    """两种写法是否同一账号（前 8 位一致；实测三种写法前 8 位都相同）。"""
    return bool(a) and bool(b) and _uid_prefix(a) == _uid_prefix(b)


def schema_drift(new_db: str) -> List[str]:
    """检出「本工具验证过的形状已经不成立」的硬漂移。

    只把**缺失**当漂移：多出来的列/表不影响（产品自己会填默认值），少了就没法保证写入
    的行能被读到。触发器缺失尤其致命——``chat_session_search_fts`` 是 external-content
    FTS5，索引完全靠 3 个触发器维护，没有触发器就会写出「库里搜不到」的行。
    """
    out: List[str] = []
    try:
        con = _open_ro(new_db)
    except sqlite3.Error as exc:
        return ["库打不开（%s）" % exc]
    try:
        for t in ("workspaces", "chat_sessions", "chat_session_messages",
                  "chat_session_search_segments", "chat_session_search_fts"):
            if not _table_exists(con, t):
                out.append("缺表 %s" % t)
        cols = {r["name"] for r in con.execute("pragma table_info(chat_session_messages)")} \
            if _table_exists(con, "chat_session_messages") else set()
        for c in ("payload_json", "payload_hash", "payload_bytes", "payload_schema_version",
                  "has_visible_content", "source", "role", "sequence", "turn_id"):
            if cols and c not in cols:
                out.append("缺列 chat_session_messages.%s" % c)
        trig = {r[0] for r in con.execute(
            "select name from sqlite_master where type='trigger'"
            " and tbl_name in ('chat_session_messages','chat_session_search_segments')")}
        for need in ("chat_session_search_segments_insert",
                     "chat_session_history_insert"):
            if need not in trig:
                out.append("缺触发器 %s（FTS/修订改由产品维护，本工具不再写入）" % need)
    except sqlite3.Error as exc:
        out.append("结构探测失败（%s）" % exc)
    finally:
        con.close()
    return out


# --------------------------------------------------------------------------- #
# 2) 读取侧：旧库会话 + 明文文字稿
# --------------------------------------------------------------------------- #
@dataclass
class LegacyTurn:
    """一个问答轮次（文字稿里的可见文字）。"""

    user_text: str
    assistant_parts: List[str] = field(default_factory=list)

    @property
    def chars(self) -> int:
        return len(self.user_text) + sum(len(p) for p in self.assistant_parts)


@dataclass
class LegacySession:
    source_db: str
    session_id: str
    user_id: str
    title: str
    project_path: str
    project_name: str
    mode: str
    created_ms: int
    updated_ms: int
    db_message_count: int
    turns: List[LegacyTurn] = field(default_factory=list)
    transcript_path: str = ""
    transcript_records: int = 0

    @property
    def has_transcript(self) -> bool:
        return bool(self.transcript_path) and bool(self.turns)

    @property
    def chars(self) -> int:
        return sum(t.chars for t in self.turns)


def find_transcripts(home: str = "") -> Dict[str, str]:
    """旧会话 id 前 8 位 → 明文文字稿路径。

    文字稿按 ``<projects>/<key>/conversation-history/<sid8>/<sid8>.jsonl`` 存放，
    ``<key>`` 的命名规则在版本间变过（本机同名项目就存在两个 key：
    ``TbmHmi_Alpha-4864fb3`` 与 ``TbmHmi_Alpha-be532793``），所以**不猜 key**，
    直接全量扫。前 8 位冲突（两个不同会话撞同一短前缀）时该键作废并记进
    :data:`ambiguous`——宁可不导，也不要把 A 的正文挂到 B 上。
    """
    home = home or os.path.expanduser("~")
    roots = [os.path.join(home, ".qoder-cn", "cache", "projects"),
             os.path.join(home, ".lingma", "cache", "projects")]
    found: Dict[str, str] = {}
    taken: set = set()
    for root in roots:
        for path in glob.glob(os.path.join(root, "*", "conversation-history", "*", "*.jsonl")):
            key = os.path.basename(os.path.dirname(path))[:8]
            if key in taken:
                # 同一短前缀来自两份文件 ⇒ 标记为歧义，后续据此跳过
                found.pop(key, None)
                taken.add(key)
                found[key] = ""
                continue
            taken.add(key)
            found[key] = path
    return {k: v for k, v in found.items() if v}


def ambiguous_transcripts(home: str = "") -> List[str]:
    """短前缀冲突（同一 8 位前缀对应多份文字稿）的键，供界面如实说明。"""
    home = home or os.path.expanduser("~")
    counter = {}
    for root in (os.path.join(home, ".qoder-cn", "cache", "projects"),
                 os.path.join(home, ".lingma", "cache", "projects")):
        for path in glob.glob(os.path.join(root, "*", "conversation-history", "*", "*.jsonl")):
            key = os.path.basename(os.path.dirname(path))[:8]
            counter[key] = counter.get(key, 0) + 1
    return sorted(k for k, n in counter.items() if n > 1)


def parse_transcript(path: str) -> Tuple[List[LegacyTurn], int]:
    """文字稿 JSONL → 轮次列表。

    :return: ``(turns, 记录数)``。记录里非 text 的块会被计数并丢弃（实测本机全部记录
             只有 text，故这条路径通常是空转），``turns`` 里 user 与其后的 assistant
             记录配成一轮：遇到新的 user 记录就开一轮，同轮内多条 assistant 记录保留为
             **各自独立**的一段（对应新版载荷里多个 ``assistant-segment`` part）。
    """
    turns: List[LegacyTurn] = []
    cur: Optional[LegacyTurn] = None
    records = 0
    with open(_longpath(path), encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            records += 1
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            role = str(rec.get("role") or "")
            msg = rec.get("message") or {}
            text = "".join(str(b.get("text") or "")
                           for b in (msg.get("content") or [])
                           if isinstance(b, dict) and b.get("type") == "text")
            if role == "user":
                if cur is not None:
                    turns.append(cur)
                cur = LegacyTurn(user_text=text)
            elif role == "assistant":
                if cur is None:
                    # 没有对应的用户提问 ⇒ 不是完整轮次（文字稿里为前导记录），
                    # 造一轮「空提问」会让界面显示成「用户什么都没说」，宁可丢弃。
                    continue
                if text.strip():
                    cur.assistant_parts.append(text)
    if cur is not None:
        turns.append(cur)
    return turns, records


def list_legacy_sessions(old_dbs: Optional[Sequence[str]] = None,
                         home: str = "",
                         transcripts: Optional[Dict[str, str]] = None) -> List[LegacySession]:
    """列出旧库里全部会话（含没有明文文字稿的，标 ``transcript_path=''``）。

    ``session_type`` / ``parent_session_id`` 用于剔除子会话：本机实测 28 条全是
    ``assistant`` 且父 id 为空，但产品可能在别的机器上留下子代理会话——那种记录正文是
    父代理写下的任务提示词，导进侧栏会被误读成用户自己的会话，故直接跳过。
    """
    home = home or os.path.expanduser("~")
    dbs = list(old_dbs) if old_dbs is not None else resolve_old_dbs(home)
    tr = transcripts if transcripts is not None else find_transcripts(home)
    out: List[LegacySession] = []
    seen: set = set()
    for db in dbs:
        try:
            con = _open_ro(db)
        except sqlite3.Error:
            continue
        try:
            if not _table_exists(con, "chat_session"):
                continue
            rows = con.execute(
                "select session_id, user_id, session_title, project_uri, project_name,"
                " gmt_create, gmt_modified, mode, session_type, parent_session_id"
                " from chat_session order by gmt_modified desc").fetchall()
            counts = {r["session_id"]: r["c"] for r in con.execute(
                "select session_id, count(*) c from chat_message group by session_id")} \
                if _table_exists(con, "chat_message") else {}
            for r in rows:
                sid = str(r["session_id"] or "")
                if not sid or sid in seen:
                    continue
                seen.add(sid)
                created = int(r["gmt_create"] or 0)
                updated = int(r["gmt_modified"] or 0)
                if updated and created and updated < created:
                    created, updated = updated, created
                if not created:
                    created = updated or _now_ms()
                if not updated:
                    updated = max(created, _now_ms())
                path = tr.get(sid[:8], "")
                turns: List[LegacyTurn] = []
                recs = 0
                if path:
                    try:
                        turns, recs = parse_transcript(path)
                    except OSError:
                        path = ""
                out.append(LegacySession(
                    source_db=db, session_id=sid,
                    user_id=str(r["user_id"] or ""),
                    title=re.sub(r"\s+", " ", str(r["session_title"] or "")).strip(),
                    project_path=str(r["project_uri"] or "").strip(),
                    project_name=str(r["project_name"] or "").strip(),
                    mode=str(r["mode"] or ""),
                    created_ms=created, updated_ms=updated,
                    db_message_count=int(counts.get(sid, 0)),
                    turns=turns, transcript_path=path, transcript_records=recs,
                ))
        except sqlite3.Error:
            pass
        finally:
            con.close()
    return out


# --------------------------------------------------------------------------- #
# 3) 目标形状推断：不硬编码产品取值，从活库现有行反推
# --------------------------------------------------------------------------- #
@dataclass
class TargetShape:
    payload_schema_version: int = VALIDATED_PS_VERSIONS[0]
    model: Optional[str] = None
    permission_mode: Optional[str] = None
    execution_kind: Optional[str] = "local"
    product_mode: str = "coding"
    conversation_mode: str = "normal"
    additional_direcotries: str = "{}"
    message_envelope_version: Optional[int] = None
    extra_json: Dict[str, Any] = field(default_factory=dict)
    #: 小写路径 → 产品自写行里用过的 ``ws_<64hex>`` 描述符
    ws_handle_by_path: Dict[str, str] = field(default_factory=dict)
    #: 小写路径 → 已存在的 ``workspaces.workspace_id``
    workspace_id_by_path: Dict[str, str] = field(default_factory=dict)
    #: 小写路径 → 同路径产品会话行的 ``execution_target_json`` 原文（优先整份复用）
    target_json_by_path: Dict[str, str] = field(default_factory=dict)
    #: 会话行/消息行是否为空库（空库时无处推断，只能用验证过的默认值）
    has_existing_rows: bool = False
    notes: Tuple[str, ...] = ()


_DEFAULT_EXTRA_JSON: Dict[str, Any] = {
    "source": "workspace", "cliSessionReady": False,
    "modelSource": {"kind": "system"}, "provisionalTitle": "",
    "titleResolved": True, "titleSource": "user", "titleRevision": 0,
}


def read_target_shape(new_db: str) -> TargetShape:
    """从新版活库（**只读**）反推会话行该长什么样。

    ``ws_<64hex>`` 描述符的生成规则没能从产品的混淆字符串表里反解出来，因此**不硬造**：
    优先整份复用同 cwd 产品会话行的 ``execution_target_json``；复用不到才按
    ``ws_ + sha256(小写路径)`` 造一个同形态值（只出现在 ``execution_target_json`` 里，
    侧栏归属靠 ``workspace_id``，不受它影响），并在计划明细里声明为推断。
    """
    shape = TargetShape()
    notes: List[str] = []
    con = _open_ro(new_db)
    try:
        # 载荷 schema 版本：取产品自写消息行里最常见的非空值
        rows = con.execute("select payload_schema_version v, count(*) c"
                           " from chat_session_messages where payload_schema_version is not null"
                           " group by v order by c desc limit 1").fetchone()
        if rows:
            shape.payload_schema_version = int(rows["v"])
        else:
            notes.append("目标库还没有产品自写的消息行，载荷按本工具验证过的 "
                         "payload_schema_version=%d 形状写入" % shape.payload_schema_version)

        # 会话行默认值：取最近一条「标准、未删除」会话
        s = con.execute("select * from chat_sessions where %s order by updated_at desc limit 1"
                        % SIDEBAR_WHERE).fetchone()
        if s is not None:
            shape.has_existing_rows = True
            shape.model = s["model"]
            shape.permission_mode = s["permission_mode"]
            shape.execution_kind = s["execution_kind"] or "local"
            shape.product_mode = s["product_mode"] or "coding"
            shape.conversation_mode = s["conversation_mode"] or "normal"
            shape.additional_direcotries = s["additional_direcotries"] or "{}"
            shape.message_envelope_version = s["message_envelope_version"]
            try:
                ex = json.loads(s["extra_json"] or "{}")
                if isinstance(ex, dict):
                    shape.extra_json = ex
            except ValueError:
                pass
            key = _norm_key(s["cwd"] or "")
            if key and s["execution_target_json"] and s["execution_target_json"] != "{}":
                shape.target_json_by_path[key] = s["execution_target_json"]
        else:
            notes.append("目标库里还没有可见会话，会话行按本工具验证过的默认取值写入")

        # 全部会话行：按路径收集描述符与 execution_target_json（同一路径可能有多份，
        # 保留最近更新那条 —— 与侧栏口径一致）
        latest = {}
        for r in con.execute("select cwd, execution_target_json, workspace_id, updated_at"
                            " from chat_sessions where cwd is not null"):
            key = _norm_key(r["cwd"] or "")
            if not key:
                continue
            prev = latest.get(key, 0)
            if _as_ms(r["updated_at"]) < prev:
                continue
            latest[key] = _as_ms(r["updated_at"])
            if r["execution_target_json"] and r["execution_target_json"] != "{}":
                shape.target_json_by_path.setdefault(key, r["execution_target_json"])
                handle = _handle_from_target_json(r["execution_target_json"])
                if handle:
                    shape.ws_handle_by_path[key] = handle

        for r in con.execute("select workspace_id, root_paths_json from workspaces"
                             " where deleted_at is null"):
            try:
                paths = json.loads(r["root_paths_json"] or "[]")
            except ValueError:
                continue
            for p in paths if isinstance(paths, list) else []:
                key = _norm_key(str(p))
                if key:
                    shape.workspace_id_by_path.setdefault(key, str(r["workspace_id"]))
    finally:
        con.close()
    if not shape.extra_json:
        shape.extra_json = dict(_DEFAULT_EXTRA_JSON)
    elif set(shape.extra_json) != set(_DEFAULT_EXTRA_JSON):
        notes.append("产品会话行的 extra_json 键集与本工具验证过的模板不同（产品可能已升版）："
                     "仍按已验证形态写入，若导入后会话不可见，请只做只读预览并等本工具跟进")
    shape.notes = tuple(notes)
    return shape


def _as_ms(value: Any) -> int:
    """``updated_at`` 在新旧行里既有毫秒整数也有 ISO 文本，统一成可比较的毫秒。"""
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value or "")
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})", text)
    if m:
        try:
            d = _dt.datetime(*(int(g) for g in m.groups()), tzinfo=_dt.timezone.utc)
            return int(d.timestamp() * 1000)
        except ValueError:
            return 0
    return 0


def _handle_from_target_json(raw: str) -> str:
    try:
        obj = json.loads(raw or "{}")
    except ValueError:
        return ""
    ws = obj.get("workspace") if isinstance(obj, dict) else None
    if isinstance(ws, dict):
        hid = str(ws.get("id") or "")
        if hid.startswith("ws_"):
            return hid
    return ""


# --------------------------------------------------------------------------- #
# 4) 计划
# --------------------------------------------------------------------------- #
@dataclass
class SessionPlan:
    legacy: LegacySession
    new_session_id: str
    new_workspace_id: str
    #: 需要新建的 workspaces 行；``None`` 表示复用目标机已有工作区（不新增行）
    workspace_row: Optional[Dict[str, Any]]
    session_row: Dict[str, Any]
    message_rows: List[Dict[str, Any]] = field(default_factory=list)
    segment_rows: List[Tuple[str, str, int, int, str, str]] = field(default_factory=list)
    inferred: Tuple[str, ...] = ()
    turns_written: int = 0
    chars_written: int = 0


@dataclass
class ImportPlan:
    old_dbs: Tuple[str, ...]
    new_db: str
    current_uid: str
    legacy_uid: str
    shape: TargetShape
    sessions: List[SessionPlan] = field(default_factory=list)
    #: ``(旧会话 id 前 8 位, 标题, 原因)`` —— 不导的与原因，界面必须如实列出
    skipped: List[Tuple[str, str, str]] = field(default_factory=list)
    sessions_total: int = 0
    with_transcript: int = 0
    ambiguous_prefixes: Tuple[str, ...] = ()

    @property
    def empty(self) -> bool:
        return not self.sessions

    def counts(self) -> Dict[str, int]:
        return {
            "sessions": len(self.sessions),
            "messages": sum(len(p.message_rows) for p in self.sessions),
            "segments": sum(len(p.segment_rows) for p in self.sessions),
            "workspaces": sum(1 for p in self.sessions if p.workspace_row),
            "chars": sum(p.chars_written for p in self.sessions),
            "turns": sum(p.turns_written for p in self.sessions),
        }

    def summary_lines(self) -> List[str]:
        c = self.counts()
        lines = [
            "导入计划（有损）：旧库会话共 %d 条，其中 %d 条取到明文文字稿；"
            "本次计划写入 %d 条会话 / %d 个轮次 / %d 行消息 / %d 行检索片段 / %d 个新工作区，"
            "合计 %d 字" % (self.sessions_total, self.with_transcript, c["sessions"],
                            c["turns"], c["messages"], c["segments"], c["workspaces"],
                            c["chars"]),
            "  目标库：%s" % self.new_db,
            "  旧库：%s（会话归属账号 %s）" % ("、".join(self.old_dbs) or "（无）",
                                               self.legacy_uid or "（未知）"),
            "  当前账号：%s" % self.current_uid,
            "  载荷形状：payload_schema_version=%d（本工具验证过 %s）"
            % (self.shape.payload_schema_version, "/".join(
                str(v) for v in VALIDATED_PS_VERSIONS)),
        ]
        if self.skipped:
            lines.append("  不导入 %d 条：" % len(self.skipped))
            for sid8, title, why in self.skipped[:10]:
                lines.append("    - %s… %s：%s" % (sid8, title[:28], why))
            if len(self.skipped) > 10:
                lines.append("    …其余 %d 条省略" % (len(self.skipped) - 10))
        if self.ambiguous_prefixes:
            lines.append("  文字稿短前缀冲突（无法判定归属，已跳过）：%s"
                         % "、".join(self.ambiguous_prefixes))
        for n in self.shape.notes:
            lines.append("  · %s" % n)
        return lines

    def detail_lines(self, limit: int = 8) -> List[str]:
        lines: List[str] = []
        for p in self.sessions[:limit]:
            lines.append("  ○ %s…" % p.legacy.session_id[:8])
            lines.append("      标题：%s" % p.legacy.title[:70])
            lines.append("      工程：%s%s" % (p.legacy.project_path,
                                              "（本机该目录已不存在）"
                                              if p.legacy.project_path and
                                              not os.path.isdir(_longpath(p.legacy.project_path))
                                              else ""))
            lines.append("      新会话 id：%s    工作区：%s" % (
                p.new_session_id,
                p.new_workspace_id + ("（复用已有）" if p.workspace_row is None
                                      else "（新建行）")))
            lines.append("      轮次 %d / 消息 %d / 片段 %d / %d 字，时间 %s → %s（插值）" % (
                p.turns_written, len(p.message_rows), len(p.segment_rows), p.chars_written,
                _iso(p.legacy.created_ms), _iso(p.legacy.updated_ms)))
            for msg in p.message_rows[:4]:
                head = str(_payload_first_text(msg["payload_json"]))[:44].replace("\n", " ")
                lines.append("        seq=%-3d %-9s %-16s %r" % (
                    msg["sequence"], msg["role"], msg["status"], head))
            if len(p.message_rows) > 4:
                lines.append("        …其余 %d 行省略" % (len(p.message_rows) - 4))
            for hint in p.inferred:
                lines.append("      ⚠ %s" % hint)
        if len(self.sessions) > limit:
            lines.append("  …共 %d 条会话（其余省略）" % len(self.sessions))
        lines.append("  刻意不写的关联表：%s" % "、".join(NOT_WRITTEN_TABLES))
        return lines


def _payload_first_text(payload_json: str) -> str:
    try:
        obj = json.loads(payload_json)
    except ValueError:
        return ""
    for part in obj.get("parts") or []:
        if isinstance(part, dict) and part.get("text"):
            return str(part["text"])
    return ""


def _namespace(old_session_id: str) -> uuid.UUID:
    """由旧会话 id 确定性派生 ID 的命名空间：同一批重复导入 ⇒ 同一组新 ID。"""
    return uuid.uuid5(uuid.NAMESPACE_URL, "qoder-cn-lingma-import/" + old_session_id)


def _rid(ns: uuid.UUID, kind: str, i: int = 0) -> str:
    return str(uuid.uuid5(ns, "%s/%d" % (kind, i)))


def plan_session(legacy: LegacySession, shape: TargetShape) -> SessionPlan:
    """一条旧会话 → 一份可写入 ``main.sqlite`` 的行集（不写盘）。"""
    ns = _namespace(legacy.session_id)
    new_sid = _rid(ns, "session")
    key = _norm_key(legacy.project_path)
    inferred: List[str] = []

    reuse_ws = shape.workspace_id_by_path.get(key, "") if key else ""
    ws_row: Optional[Dict[str, Any]] = None
    if reuse_ws:
        new_ws_id = reuse_ws
    else:
        new_ws_id = _rid(ns, "workspace")
        ws_row = {
            "workspace_id": new_ws_id,
            "name": legacy.project_name or (os.path.basename(key) if key else "imported"),
            "workspace_source": "local",
            "root_paths_json": _jsonz([legacy.project_path]),
            "extra_json": _jsonz({"workspaceIcon": "folder", "workspaceColor": "primary"}),
            "created_at": legacy.created_ms,
            "updated_at": legacy.updated_ms,
            "archived": 0,
            "deleted_at": None,
            "product_mode": shape.product_mode,
        }

    # 执行目标：优先整份复用同路径产品行；复用不到才自造描述符并声明为推断
    target_json = shape.target_json_by_path.get(key, "") if key else ""
    if not target_json:
        handle = (shape.ws_handle_by_path.get(key, "") if key else "") or (
            "ws_" + hashlib.sha256((legacy.project_path or "").lower().encode("utf-8")).hexdigest())
        if not (shape.ws_handle_by_path.get(key, "") if key else ""):
            inferred.append("ws_ 工作区描述符由「小写路径的 sha256」自造（产品生成规则没能反解出来）："
                            "只出现在 execution_target_json，侧栏归属靠 workspace_id，不受它影响")
        target_json = _jsonz({"mode": "local",
                              "host": {"id": "local", "kind": "local", "label": "Local"},
                              "workspace": {"id": handle, "path": legacy.project_path}})

    extra = dict(shape.extra_json or _DEFAULT_EXTRA_JSON)
    extra["provisionalTitle"] = legacy.title
    extra["source"] = "workspace"
    extra["cliSessionReady"] = False
    extra["modelSource"] = {"kind": "system"}
    extra["titleResolved"] = True
    extra["titleSource"] = "user"
    extra["titleRevision"] = 0

    session_row = {
        "session_id": new_sid,
        "origin_session_id": None,
        "session_kind": "standard",
        "owner_session_id": None,
        "conversation_mode": shape.conversation_mode,
        "product_mode": shape.product_mode,
        "title": legacy.title or "(无标题)",
        "cwd": legacy.project_path,
        "execution_kind": shape.execution_kind,
        "workspace_id": new_ws_id,
        "git_branch": None,
        "model": shape.model,
        "permission_mode": shape.permission_mode,
        "additional_direcotries": shape.additional_direcotries,
        "extra_json": _jsonz(extra),
        "created_at": legacy.created_ms,
        "updated_at": legacy.updated_ms,
        "execution_target_json": target_json,
        "archived": 0,
        "unread": 0,
        "archived_at": None,
        "deleted_at": None,
        "registration_json": None,
        "sidebar_custom_order": None,
        "message_envelope_version": shape.message_envelope_version,
    }

    n = len(legacy.turns)
    span = max(0, legacy.updated_ms - legacy.created_ms)
    starts = [legacy.created_ms + span * i // n for i in range(n)]
    messages: List[Dict[str, Any]] = []
    segments: List[Tuple[str, str, int, int, str, str]] = []
    seq = 0
    turns_written = 0
    for i, turn in enumerate(legacy.turns):
        last = i == n - 1
        s, e = starts[i], (legacy.updated_ms if last else starts[i + 1])
        t_user = s
        t_done = e if last else s + (e - s) * 9 // 10
        t_created = s + (e - s) * 4 // 10
        uid_ = _rid(ns, "user", i)
        turn_id = uid_
        if turn.user_text.strip():
            seq += 1
            payload = _jsonz({"issueId": None, "attachments": [], "selectedSkillNames": [],
                              "selectedPluginIds": [], "selectedConnectorIds": [],
                              "selectedAgentNames": [], "selectedCapabilityCommands": [],
                              "referencedChatSessions": [],
                              "parts": [{"id": "%s:text" % uid_, "type": "text",
                                         "text": turn.user_text}]})
            messages.append(_message_row(new_sid, uid_, turn_id, seq, payload, "user",
                                         shape, t_user, t_user))
            segments.append((uid_, turn_id, seq, 0, "message:%s:text" % uid_, turn.user_text))
        if turn.assistant_parts:
            seq += 1
            aid = "assistant:" + uid_
            parts = [{"id": "assistant-segment:%d:text:1" % k, "type": "text",
                      "text": line, "parentToolUseId": None}
                     for k, line in enumerate(turn.assistant_parts, start=1)]
            dur = max(0, t_done - t_created)
            payload = _jsonz({"forkBoundaryProviderId": "chatcmpl-" + _rid(ns, "provider", i),
                              "requestSetId": _rid(ns, "requestSet", i),
                              "resultUuid": _rid(ns, "result", i),
                              "completedAt": _iso(t_done), "durationMs": dur,
                              "turnMetrics": {"durationMs": dur, "turnCount": len(parts)},
                              "parts": parts, "finalTextId": parts[-1]["id"]})
            messages.append(_message_row(new_sid, aid, turn_id, seq, payload, "assistant",
                                         shape, t_created, t_done, turn_started_at=t_created))
            for k, line in enumerate(turn.assistant_parts, start=1):
                segments.append((aid, turn_id, seq, k - 1,
                                 "message:%s:part:assistant-segment:%d:text:1:text" % (aid, k),
                                 line))
        if turn.user_text.strip() or turn.assistant_parts:
            turns_written += 1

    return SessionPlan(
        legacy=legacy, new_session_id=new_sid, new_workspace_id=new_ws_id,
        workspace_row=ws_row, session_row=session_row,
        message_rows=messages, segment_rows=segments,
        inferred=tuple(inferred), turns_written=turns_written,
        chars_written=legacy.chars,
    )


def _message_row(session_id: str, message_id: str, turn_id: str, seq: int,
                 payload: str, role: str, shape: TargetShape,
                 created_ms: int, updated_ms: int,
                 turn_started_at: Optional[int] = None) -> Dict[str, Any]:
    """一行消息：形状逐项对齐产品自写行（``source='sdk-projection'``）。"""
    raw = payload.encode("utf-8")
    return {
        "session_id": session_id,
        "message_id": message_id,
        "turn_id": turn_id,
        "sequence": seq,
        "payload_json": payload,
        "status": "completed",
        "feedback": None,
        "source": "sdk-projection",
        "created_at": created_ms,
        "updated_at": updated_ms,
        "role": role,
        "message_stream": None,
        "voice_session_id": None,
        "subtype": None,
        "runtime_input_state": None,
        "fork_availability": None,
        "turn_started_at": turn_started_at,
        "is_compaction_boundary": 0,
        "has_visible_content": 1,
        "payload_bytes": len(raw),
        "payload_hash": hashlib.sha256(raw).hexdigest(),
        "payload_schema_version": shape.payload_schema_version,
    }


def plan_import(home: str = "", current_uid: str = "", new_db: str = "",
                old_dbs: Optional[Sequence[str]] = None,
                session_ids: Optional[Sequence[str]] = None,
                limit: int = 0, include_all: bool = False) -> ImportPlan:
    """生成导入计划（**不写盘**）。

    :param session_ids: 只导这些旧会话（支持传前 8 位）；为空则按 ``limit`` 取全部可导的。
    :param include_all: 置 True 时连「本机工程目录已不存在」的会话一起计划
                        （``cwd`` 指向不存在的目录不会写坏库，只是点开时新版可能报路径）。
    """
    home = home or os.path.expanduser("~")
    dbs = list(old_dbs) if old_dbs is not None else resolve_old_dbs(home)
    nd = new_db or resolve_new_db(home)
    shape = read_target_shape(nd) if nd else TargetShape()
    ambiguous = set(ambiguous_transcripts(home))
    legs = list_legacy_sessions(dbs, home)
    cur = (current_uid or "").strip()
    if not cur:
        # CLI 单独跑时没人指定账号：按本机登录态确定式取（GUI 会把它下拉选中的传进来）
        from .adapters.qoder import detect_current_uid, detect_qoder_root
        paths = detect_qoder_root(os.path.join(home, ".qoder-cn"))
        cur = detect_current_uid(paths.root, paths.shared) or ""
    plan = ImportPlan(old_dbs=tuple(dbs), new_db=nd, current_uid=cur,
                      legacy_uid="", shape=shape, sessions_total=len(legs),
                      with_transcript=sum(1 for x in legs if x.has_transcript),
                      ambiguous_prefixes=tuple(sorted(ambiguous)))
    counts: Dict[str, int] = {}
    for s in legs:
        counts[s.user_id] = counts.get(s.user_id, 0) + 1
    if counts:
        plan.legacy_uid = max(counts, key=lambda k: counts[k])

    wanted = {x.strip().lower() for x in (session_ids or ()) if x.strip()}
    for s in legs:
        if wanted and s.session_id.lower() not in wanted \
                and s.session_id[:8].lower() not in wanted:
            continue
        if not s.project_path:
            plan.skipped.append((s.session_id[:8], s.title, "旧库没记工程路径，无法归属工作区"))
            continue
        if not s.transcript_path:
            plan.skipped.append((s.session_id[:8], s.title,
                                 "没有明文文字稿（旧库正文是列级密文，取不出内容）"))
            continue
        if s.session_id[:8] in ambiguous:
            plan.skipped.append((s.session_id[:8], s.title, "文字稿短前缀冲突，无法判定归属"))
            continue
        if not s.turns:
            plan.skipped.append((s.session_id[:8], s.title, "文字稿里没有一个完整问答轮次"))
            continue
        if not include_all and not os.path.isdir(_longpath(s.project_path)):
            plan.skipped.append((s.session_id[:8], s.title,
                                 "本机工程目录已不存在（%s）；确认要导可加 include_all"
                                 % s.project_path))
            continue
        sp = plan_session(s, shape)
        if not sp.message_rows:
            plan.skipped.append((s.session_id[:8], s.title, "拍平后没有任何可见文本"))
            continue
        plan.sessions.append(sp)
        if limit and len(plan.sessions) >= limit:
            break
    return plan


# --------------------------------------------------------------------------- #
# 5) 写入：快照 → 预检 → 单事务 → 读回校验 → 台账
# --------------------------------------------------------------------------- #
class ImportAbort(RuntimeError):
    """预检不通过：**库未改动**，原因交给界面展示。"""


def _manifest_path(new_db: str) -> str:
    return new_db + MANIFEST_SUFFIX


def _snapshot_path(new_db: str) -> str:
    return new_db + SNAPSHOT_SUFFIX


def ensure_snapshot(new_db: str) -> Dict[str, Any]:
    """确保存在一份「写入前」的在线快照，返回台账里的 snapshot 段。

    已存在则**只复核不重做**（首份快照才代表真正的写入前状态）；摘要对不上说明这份
    兜底已经不可信，直接中止 —— 拿一个可能被改过的文件当回退依据比没有依据更危险。
    """
    snap = _snapshot_path(new_db)
    mani_path = _manifest_path(new_db)
    if os.path.isfile(snap):
        digest = _sha256_file(snap)
        if os.path.isfile(mani_path):
            try:
                old = _load_json(mani_path)
            except ValueError:
                raise ImportAbort("台账 %s 不是合法 JSON，回退依据不可信，中止。" % mani_path)
            want = ((old.get("snapshot") or {}).get("sha256") or "")
            if want and want != digest:
                raise ImportAbort(
                    "已有快照 %s 的 sha256 与台账记录不符（可能被动过），"
                    "中止：请先人工确认这份兜底是否仍可用。" % snap)
        con = _open_ro(snap)
        try:
            ok = con.execute("pragma integrity_check").fetchone()[0]
        except sqlite3.Error as exc:
            raise ImportAbort("已有快照完整性检查失败：%s" % exc)
        finally:
            con.close()
        if ok != "ok":
            raise ImportAbort("已有快照 integrity_check=%s，不可用作回退依据，中止。" % ok)
        return {"path": snap, "sha256": digest, "reused": True,
                "created_at": (os.path.getmtime(snap))}
    if not snapshot_sqlite(new_db, snap):
        raise ImportAbort("生成 %s 的在线快照失败（源库打不开或磁盘异常），未写入任何数据。"
                          % new_db)
    return {"path": snap, "sha256": _sha256_file(snap), "reused": False,
            "created_at": _now_ms()}


def _existing_new_ids(new_db: str) -> set:
    """目标库里已存在的新会话 id 集合（确定性 ID ⇒ 重复导入命中同一份，用来去重）。"""
    try:
        con = _open_ro(new_db)
    except sqlite3.Error:
        return set()
    try:
        return {str(r["session_id"]) for r in con.execute("select session_id from chat_sessions")}
    except sqlite3.Error:
        return set()
    finally:
        con.close()


def preflight(plan: ImportPlan) -> List[str]:
    """写前检查，返回问题列表（空 = 可写）。不抛异常，交给调用方决定。"""
    problems: List[str] = []
    if not plan.new_db or not os.path.isfile(plan.new_db):
        return ["目标库不存在：%s" % (plan.new_db or "（未解析到 main.sqlite）")]
    if plan.empty:
        return ["计划为空：没有任何会话可写入"]
    # 账号形态是门控条件，GUI 靠按钮把关、CLI 靠这里兜底：拿旧账号（或没认出账号）
    # 往活库里补旧会话，属于用户「还没换代」时的误操作，一律拒。
    if not plan.current_uid:
        return ["未能确定「当前所选账号」：GUI 里从下拉选一个，CLI 里传 --uid"]
    if not is_new_generation_uid(plan.current_uid):
        return ["当前所选账号「%s」不是新版阿里云账号（UUIDv7 形态）：本功能只在换代"
                "之后把旧会话补进新账号的库里，仍停在 lingma 账号时无需导入"
                % plan.current_uid]
    if plan.legacy_uid and _uid_same(plan.current_uid, plan.legacy_uid):
        return ["当前所选账号与旧会话归属账号（%s）仍是同一个：本机还没发生换代，"
                "旧会话本来就在当前账号的库里，无需导入" % plan.legacy_uid]
    if plan.shape.payload_schema_version not in VALIDATED_PS_VERSIONS:
        return ["目标库消息载荷为 payload_schema_version=%s，本工具只验证过 %s 的写入形状，"
                "为避免写坏产品数据已中止"
                % (plan.shape.payload_schema_version,
                   "/".join(str(v) for v in VALIDATED_PS_VERSIONS))]
    drift = schema_drift(plan.new_db)
    if drift:
        return ["库结构与验证过的形状不一致：%s" % "；".join(drift)]
    con = _open_ro(plan.new_db)
    try:
        if con.execute("pragma integrity_check").fetchone()[0] != "ok":
            problems.append("目标库写入前 integrity_check 已不为 ok —— 不碰")
        for p in plan.sessions:
            if con.execute("select 1 from chat_sessions where session_id=?",
                           (p.new_session_id,)).fetchone():
                problems.append("会话 %s…（旧 %s…）已存在于目标库，重复导入将跳过这一条"
                                % (p.new_session_id[:8], p.legacy.session_id[:8]))
    except sqlite3.Error as exc:
        problems.append("预检读库失败：%s" % exc)
    finally:
        con.close()
    return problems


def apply_import(plan: ImportPlan, *, verify: bool = True) -> Dict[str, Any]:
    """执行写入：快照 → 抢写锁 → 单事务插入 → 提交 → 读回校验 → 记台账。

    :return: ``{"ok", "error", "written", "skipped_existing", "snapshot", "problems"}``；
             任何一步失败都不提交（库保持原样）。

    「会话已在目标库」不算失败：确定性 ID 命中的条目被剔除后继续写其余条目
    （重复点「导入」因此不会写出第二份，也不会整批中止）。
    """
    outcome: Dict[str, Any] = {"ok": False, "error": "", "written": [],
                               "skipped_existing": [], "snapshot": None, "problems": []}
    if not plan.new_db:
        outcome["error"] = "未解析到目标库路径。"
        return outcome

    existing = _existing_new_ids(plan.new_db)
    kept: List[SessionPlan] = []
    for p in plan.sessions:
        if p.new_session_id in existing:
            outcome["skipped_existing"].append(p.new_session_id)
        else:
            kept.append(p)
    plan.sessions = kept
    if not plan.sessions:
        outcome["ok"] = True
        outcome["error"] = ("没有需要写入的新行：计划里的会话都已存在于目标库"
                            "（确定性 id 命中），或计划本身为空。")
        return outcome

    problems = preflight(plan)
    if problems:
        outcome["error"] = "预检未通过：\n  - " + "\n  - ".join(problems)
        return outcome

    outcome["snapshot"] = ensure_snapshot(plan.new_db)

    con = sqlite3.connect(_longpath(plan.new_db), timeout=15.0)
    con.row_factory = sqlite3.Row
    try:
        con.execute("pragma busy_timeout=15000")
        con.execute("pragma foreign_keys=ON")
        try:
            con.execute("begin immediate")
        except sqlite3.Error as exc:
            outcome["error"] = ("拿不到 main.sqlite 写锁（%s）：请先完全退出 Qoder CN"
                               "（含系统托盘图标）再重试。库未改动。" % exc)
            return outcome
        for p in plan.sessions:
            if p.workspace_row is not None:
                _insert(con, "workspaces", p.workspace_row)
            _insert(con, "chat_sessions", p.session_row)
            for m in p.message_rows:
                _insert(con, "chat_session_messages", m)
            for mid, turn, seq, pos, seg_id, text in p.segment_rows:
                # FTS 索引与 history_revisions 由表上的触发器自动维护，不手写
                con.execute(
                    "insert into chat_session_search_segments (session_id, message_id,"
                    " turn_id, sequence, position, segment_id, text, normalized_text)"
                    " values (?,?,?,?,?,?,?,?)",
                    (p.new_session_id, mid, "turn:" + turn, seq, pos, seg_id, text,
                     text.lower()))
        con.commit()
        outcome["written"] = [_session_record(p) for p in plan.sessions]
        outcome["ok"] = True
    except sqlite3.Error as exc:
        try:
            con.rollback()
        except sqlite3.Error:
            pass
        outcome["error"] = "写入失败，事务已回滚，库未改动：%s" % exc
        return outcome
    finally:
        con.close()

    if verify:
        outcome["problems"] = verify_import(plan)
    _append_ledger(plan, outcome)
    return outcome


def _insert(con: sqlite3.Connection, table: str, row: Dict[str, Any]) -> None:
    cols = list(row)
    con.execute("insert into %s (%s) values (%s)"
                % (table, ", ".join('"%s"' % c for c in cols),
                   ", ".join("?" * len(cols))),
                [row[c] for c in cols])


def _session_record(p: SessionPlan) -> Dict[str, Any]:
    """台账里的一条：**只记 ID 与哈希，不记标题与正文**。"""
    return {
        "new_session_id": p.new_session_id,
        "old_session_id": p.legacy.session_id,
        "old_db": p.legacy.source_db,
        "workspace_id": p.new_workspace_id,
        "workspace_created": p.workspace_row is not None,
        "message_ids": [m["message_id"] for m in p.message_rows],
        "segment_ids": [s[4] for s in p.segment_rows],
        "payload_hashes": [m["payload_hash"] for m in p.message_rows],
        "counts": {"messages": len(p.message_rows), "segments": len(p.segment_rows),
                   "turns": p.turns_written, "chars": p.chars_written},
    }


def _read_manifest(new_db: str) -> Dict[str, Any]:
    path = _manifest_path(new_db)
    if not os.path.isfile(path):
        return {"new_db": new_db, "snapshot": {}, "imports": []}
    try:
        data = _load_json(path)
    except ValueError:
        return {"new_db": new_db, "snapshot": {}, "imports": []}
    if not isinstance(data, dict):
        return {"new_db": new_db, "snapshot": {}, "imports": []}
    data.setdefault("imports", [])
    return data


def _append_ledger(plan: ImportPlan, outcome: Dict[str, Any]) -> None:
    data = _read_manifest(plan.new_db)
    snap = outcome.get("snapshot") or {}
    if snap.get("path") and not data.get("snapshot", {}).get("sha256"):
        data["snapshot"] = {"path": snap["path"], "sha256": snap["sha256"],
                            "created_at": _iso(int(snap.get("created_at") or _now_ms()))}
    data["imports"].append({
        "written_at": _iso(_now_ms()),
        "current_uid": plan.current_uid,
        "legacy_uid": plan.legacy_uid,
        "payload_schema_version": plan.shape.payload_schema_version,
        "sessions": outcome.get("written") or [],
        "verify_problems": outcome.get("problems") or [],
    })
    path = _manifest_path(plan.new_db)
    tmp = path + ".tmp"
    with open(_longpath(tmp), "w", encoding="utf-8", newline="\n") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)
    os.replace(_longpath(tmp), _longpath(path))


def verify_import(plan: ImportPlan) -> List[str]:
    """读回校验：逐会话查计数、完整性、外键、载荷哈希与侧栏可见性。返回不一致项。"""
    problems: List[str] = []
    con = _open_ro(plan.new_db)
    try:
        if con.execute("pragma integrity_check").fetchone()[0] != "ok":
            problems.append("写入后 integrity_check 不为 ok")
        for row in con.execute("pragma foreign_key_check"):
            problems.append("外键违规：%s" % dict(row))
        for p in plan.sessions:
            n_sess = con.execute("select count(*) c from chat_sessions where session_id=?",
                                (p.new_session_id,)).fetchone()["c"]
            if n_sess != 1:
                problems.append("会话 %s 行数=%s" % (p.new_session_id[:8], n_sess))
            got = con.execute("select count(*) c from chat_session_messages where session_id=?",
                             (p.new_session_id,)).fetchone()["c"]
            if got != len(p.message_rows):
                problems.append("会话 %s 消息行数 %s ≠ 计划 %s"
                                % (p.new_session_id[:8], got, len(p.message_rows)))
            got = con.execute("select count(*) c from chat_session_search_segments"
                              " where session_id=?", (p.new_session_id,)).fetchone()["c"]
            if got != len(p.segment_rows):
                problems.append("会话 %s 片段行数 %s ≠ 计划 %s"
                                % (p.new_session_id[:8], got, len(p.segment_rows)))
            orphan = con.execute(
                "select count(*) c from chat_session_search_segments s where s.session_id=?"
                " and not exists (select 1 from chat_session_messages m"
                " where m.session_id=s.session_id and m.message_id=s.message_id)",
                (p.new_session_id,)).fetchone()["c"]
            if orphan:
                problems.append("会话 %s 有 %s 行片段找不到对应消息" % (p.new_session_id[:8], orphan))
            for m in p.message_rows:
                r = con.execute("select payload_json pj, payload_hash ph, payload_bytes pb,"
                                " sequence sq, role, status, source, turn_id"
                                " from chat_session_messages where session_id=? and message_id=?",
                                (p.new_session_id, m["message_id"])).fetchone()
                if r is None:
                    problems.append("消息 %s… 读不到" % m["message_id"][:8])
                    continue
                pj = r["pj"]
                raw = pj.encode("utf-8")
                if (hashlib.sha256(raw).hexdigest() != r["ph"] != m["payload_hash"]
                        or len(raw) != r["pb"] or json.loads(pj) != json.loads(m["payload_json"])
                        or (r["sq"], r["role"], r["status"], r["source"], r["turn_id"])
                        != (m["sequence"], m["role"], m["status"], m["source"], m["turn_id"])):
                    problems.append("消息 %s… 内容或哈希与计划不符" % m["message_id"][:8])
            visible = con.execute("select 1 from chat_sessions where session_id=? and %s"
                                  % SIDEBAR_WHERE, (p.new_session_id,)).fetchone()
            if visible is None:
                problems.append("会话 %s… 不在侧栏可见集合里" % p.new_session_id[:8])
    except sqlite3.Error as exc:
        problems.append("校验读库失败：%s" % exc)
    finally:
        con.close()
    return problems


def rollback_import(new_db: str, session_ids: Optional[Sequence[str]] = None,
                    drop_snapshot: bool = False) -> Dict[str, Any]:
    """按台账只摘**本工具写入**的行（先片段、再消息、后会话，最后回收新建的工作区行）。

    :param session_ids: 只回滚这些**新**会话 id；为空则回滚台账里全部。
    """
    out: Dict[str, Any] = {"ok": False, "error": "", "rolled_back": [], "counts": {},
                           "workspaces_removed": []}
    data = _read_manifest(new_db)
    wanted = {s.strip().lower() for s in (session_ids or ()) if s.strip()}
    targets: List[Dict[str, Any]] = []
    for imp in data.get("imports") or []:
        for rec in imp.get("sessions") or []:
            sid = str(rec.get("new_session_id") or "")
            if wanted and sid.lower() not in wanted and sid[:8].lower() not in wanted:
                continue
            if sid:
                targets.append(rec)
    if not targets:
        out["error"] = "台账里没有匹配的写入记录（可能已回滚过，或从未由本工具写入）。"
        return out

    con = sqlite3.connect(_longpath(new_db), timeout=15.0)
    try:
        con.execute("pragma busy_timeout=15000")
        con.execute("pragma foreign_keys=ON")
        try:
            con.execute("begin immediate")
        except sqlite3.Error as exc:
            out["error"] = ("拿不到 main.sqlite 写锁（%s）：请先完全退出 Qoder CN 再重试。"
                            "库未改动。" % exc)
            return out
        removed_ws: List[str] = []
        for rec in targets:
            sid = rec["new_session_id"]
            n1 = con.execute("delete from chat_session_search_segments where session_id=?",
                             (sid,)).rowcount
            n2 = con.execute("delete from chat_session_messages where session_id=?",
                             (sid,)).rowcount
            con.execute("delete from chat_session_history_revisions where session_id=?", (sid,))
            n3 = con.execute("delete from chat_sessions where session_id=?", (sid,)).rowcount
            out["counts"][sid[:8]] = {"segments": n1, "messages": n2, "sessions": n3}
            out["rolled_back"].append(sid)
            if rec.get("workspace_created"):
                wid = rec.get("workspace_id") or ""
                left = con.execute("select count(*) from chat_sessions where workspace_id=?",
                                   (wid,)).fetchone()[0]
                if wid and not left:
                    con.execute("delete from workspaces where workspace_id=?", (wid,))
                    removed_ws.append(wid)
        con.commit()
        out["workspaces_removed"] = removed_ws
        out["ok"] = True
        # 台账里剔除已回滚的记录（保留其余，避免重复回滚时误删产品行）
        done = {s.lower() for s in out["rolled_back"]}
        for imp in data.get("imports") or []:
            imp["sessions"] = [r for r in (imp.get("sessions") or [])
                               if str(r.get("new_session_id") or "").lower() not in done]
        data["rollbacks"] = (data.get("rollbacks") or []) + [
            {"at": _iso(_now_ms()), "sessions": sorted(done)}]
        path = _manifest_path(new_db)
        with open(_longpath(path + ".tmp"), "w", encoding="utf-8", newline="\n") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=1)
        os.replace(_longpath(path + ".tmp"), _longpath(path))
    except sqlite3.Error as exc:
        try:
            con.rollback()
        except sqlite3.Error:
            pass
        out["error"] = "回滚失败，事务已撤销，库未改动：%s" % exc
        return out
    finally:
        con.close()

    if drop_snapshot:
        snap = (data.get("snapshot") or {}).get("path") or _snapshot_path(new_db)
        for extra in ("", "-wal", "-shm"):
            p = snap + extra
            if os.path.isfile(_longpath(p)):
                try:
                    os.remove(_longpath(p))
                except OSError:
                    pass
    return out


def restore_from_snapshot(new_db: str) -> str:
    """把台账记录的快照**覆盖回**活动库（最后手段）。

    返回说明文本；调用方必须先确认 Qoder 已完全退出，且只在
    ``rollback_import`` 摘不干净时才用（例如写入了预期外的行）。
    快照本身是「写入前」的完整副本，覆盖 = 丢弃写入之后产品自己新产生的数据。
    """
    data = _read_manifest(new_db)
    snap = (data.get("snapshot") or {}).get("path") or _snapshot_path(new_db)
    if not os.path.isfile(_longpath(snap)):
        return "没找到快照 %s，无法整库回退。" % snap
    digest = _sha256_file(snap)
    want = (data.get("snapshot") or {}).get("sha256") or ""
    if want and want != digest:
        return ("快照 %s 的 sha256 与台账不符，拒绝覆盖（这一步不可逆，请先人工核对）。" % snap)
    con = _open_ro(snap)
    try:
        ok = con.execute("pragma integrity_check").fetchone()[0]
    finally:
        con.close()
    if ok != "ok":
        return "快照 integrity_check=%s，不可用于覆盖。" % ok
    for p in (new_db + "-wal", new_db + "-shm"):
        if os.path.isfile(_longpath(p)):
            os.remove(_longpath(p))
    shutil.copyfile(_longpath(snap), _longpath(new_db))
    return "已用快照覆盖 %s（写入之后产品新产生的会话一并丢弃）。" % new_db


# --------------------------------------------------------------------------- #
# 6) 界面文案（GUI 直接渲染，避免两处口径漂移）
# --------------------------------------------------------------------------- #
def eligibility_lines(e: Eligibility) -> List[str]:
    lines = ["条件：%s" % ("✅ 已满足，可提供 lingma 会话导入" if e.ready else "❌ 未满足")]
    for r in e.reasons:
        lines.append("  · 缺：%s" % r)
    lines.append("  · 旧库：%s" % ("、".join(e.old_dbs) or "（无）"))
    lines.append("  · 新库：%s" % (e.new_db or "（未找到）"))
    lines.append("  · 所选账号：%s%s" % (e.current_uid or "（未取到）",
                                        "（新版阿里云账号）" if is_new_generation_uid(e.current_uid)
                                        else ""))
    lines.append("  · 旧会话归属账号：%s" % (e.legacy_uid or "（未知）"))
    lines.append("  · 覆盖率：会话 %d 条 / 有明文文字稿 %d 条"
                 % (e.sessions_total, e.sessions_with_transcript))
    for n in e.notices:
        lines.append("  ⚠ %s" % n)
    return lines


# --------------------------------------------------------------------------- #
# CLI（与 GUI 共用上面同一套函数；``python -m ai_env_clone.qoder_session_import``）
# --------------------------------------------------------------------------- #
def main(argv: Optional[List[str]] = None) -> int:
    import argparse
    import sys

    ap = argparse.ArgumentParser(
        prog="python -m ai_env_clone.qoder_session_import",
        description="旧版 Qoder CN（lingma）会话 → 新版 main.sqlite 有损导入（默认只出计划）")
    ap.add_argument("command", nargs="?", default="plan",
                    choices=["eligibility", "list", "plan", "apply", "rollback"])
    ap.add_argument("--home", default="", help="用户主目录（默认取当前用户）")
    ap.add_argument("--uid", default="", help="当前所选账号（新阿里云账号 UUIDv7）")
    ap.add_argument("--new-db", default="", help="目标 main.sqlite（默认自动探测）")
    ap.add_argument("--old-db", action="append", dest="old_dbs",
                    help="旧版 local.db，可重复传（默认自动探测）")
    ap.add_argument("--session", action="append", dest="sessions",
                    help="只处理这些旧会话 id（可重复，支持前 8 位）")
    ap.add_argument("--limit", type=int, default=0, help="最多计划几条会话")
    ap.add_argument("--include-all", action="store_true",
                    help="连「本机工程目录已不存在」的会话也纳入计划")
    ap.add_argument("--drop-snapshot", action="store_true",
                    help="rollback 后一并删除快照文件")
    args = ap.parse_args(argv)

    if sys.platform == "win32":
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (AttributeError, ValueError):
                pass

    home = args.home or os.path.expanduser("~")
    if args.command == "eligibility":
        e = check_eligibility(home, args.uid, args.new_db or "",
                              args.old_dbs)
        for line in eligibility_lines(e):
            print(line)
        return 0 if e.ready else 2

    if args.command == "list":
        tr = find_transcripts(home)
        for s in list_legacy_sessions(args.old_dbs, home, tr):
            mark = "✓" if s.has_transcript else "✗"
            print("%s %.8s %-34s 轮次=%-3d 字数=%-7d 工程=%s 消息行=%d" % (
                mark, s.session_id, s.title[:32], len(s.turns), s.chars,
                s.project_path or "（无）", s.db_message_count))
        return 0

    plan = plan_import(home, args.uid, args.new_db or "", args.old_dbs,
                       args.sessions, args.limit, args.include_all)
    if args.command == "rollback":
        res = rollback_import(plan.new_db, args.sessions, args.drop_snapshot)
        print(res.get("error") or "已摘除 %d 个会话：%s" % (
            len(res.get("rolled_back") or []),
            "、".join(s[:8] for s in res.get("rolled_back") or [])))
        return 0 if res.get("ok") else 1

    for line in plan.summary_lines():
        print(line)
    for line in plan.detail_lines():
        print(line)

    if args.command == "plan":
        print("\n未写盘（默认只出计划）。确认无误后把末尾参数换成 apply：")
        print("  python -m ai_env_clone.qoder_session_import apply"
              " --uid %s --limit %d" % (args.uid or "<新账号UUID>", args.limit))
        problems = preflight(plan)
        for p in problems:
            print("预检：%s" % p)
        return 0

    res = apply_import(plan)
    if not res["ok"]:
        print("\n未写入：%s" % res["error"])
        return 1
    print("\n写入完成：%d 个会话 / %d 行消息 / %d 行片段（快照 %s）" % (
        len(res["written"]),
        sum(s["counts"]["messages"] for s in res["written"]),
        sum(s["counts"]["segments"] for s in res["written"]),
        (res.get("snapshot") or {}).get("path", "")))
    for pr in res["problems"]:
        print("  校验异常：%s" % pr)
    print("  回滚：python -m ai_env_clone.qoder_session_import rollback"
          " --session <新会话id前8位>")
    return 0 if not res["problems"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
