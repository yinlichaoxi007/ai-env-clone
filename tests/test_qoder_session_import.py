"""lingma 会话 → 新版 main.sqlite 有损导入的单元测试（全合成样本 + 临时目录）。

★ 库结构（表 + 触发器）逐字复刻自本机 ``main.sqlite`` 的 ``sqlite_master``，
否则测出来的「可写」是假的：FTS 索引与 ``chat_session_history_revisions`` 全靠
``chat_session_search_segments_*`` / ``chat_session_history_*`` 六个触发器维护。

钉住的规则：

1. **门控**：旧库 + 新库都在、且所选账号是新版（UUIDv7）形态才 ``ready``；
   所选仍是旧 16 位数字账号时必须拒绝（用户要求「才可在导入数据中提供此功能」）；
2. **确定性 ID**：同一旧会话两次计划 ⇒ 同一组新 id ⇒ 重复导入不写出第二份；
3. **只摘自己写的行**：回滚按台账删，产品自写行一根手指都不能碰；
4. **schema 漂移即拒**：目标库载荷版本不是验证过的 2 时，apply 必须拒绝且库保持原样；
5. **台账不存正文与标题**（隐私优先零新增披露）。
"""

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import unittest

from ai_env_clone import qoder_session_import as qsi

NEW_UID = "019eb095-47ef-70d2-8a41-ea6278c176b1"
OLD_UID = "1316632530577119"
OLD_SID = "1ff556c2-c123-4d86-99be-1461d7b5850f"
SECOND_SID = "46403dfd-1111-2222-3333-444455556666"
NOISE_SID = "3e605f43-0000-0000-0000-000000000000"
PRODUCT_PAYLOAD = '{"parts":[{"id":"p:text","type":"text","text":"产品自写的正文"}]}'

# --------------------------------------------------------------------------- #
# fixture DDL（取自本机 main.sqlite 的 sqlite_master，只留会话族必需列）
# --------------------------------------------------------------------------- #
_DDL = """
    create table workspaces (
        workspace_id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        workspace_source TEXT NOT NULL,
        root_paths_json TEXT NOT NULL,
        extra_json TEXT NOT NULL DEFAULT '{}',
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL,
        archived INTEGER NOT NULL DEFAULT 0,
        deleted_at INTEGER,
        product_mode TEXT NOT NULL DEFAULT 'coding'
          CHECK (product_mode IN ('coding','general')));

    create table chat_sessions (
        session_id TEXT PRIMARY KEY,
        origin_session_id TEXT,
        session_kind TEXT NOT NULL DEFAULT 'standard'
          CHECK (session_kind IN ('standard','sideChat','automationExecution')),
        owner_session_id TEXT,
        conversation_mode TEXT NOT NULL DEFAULT 'normal',
        product_mode TEXT NOT NULL DEFAULT 'coding',
        title TEXT NOT NULL,
        cwd TEXT NOT NULL,
        execution_kind TEXT,
        workspace_id TEXT,
        git_branch TEXT,
        model TEXT,
        permission_mode TEXT,
        additional_direcotries TEXT NOT NULL DEFAULT '{}',
        extra_json TEXT NOT NULL DEFAULT '{}',
        created_at TIMESTAMP NOT NULL,
        updated_at TIMESTAMP NOT NULL,
        execution_target_json TEXT NOT NULL DEFAULT '{}',
        archived INTEGER NOT NULL DEFAULT 0,
        unread INTEGER NOT NULL DEFAULT 0,
        archived_at TIMESTAMP,
        deleted_at TIMESTAMP,
        registration_json TEXT,
        sidebar_custom_order INTEGER,
        message_envelope_version INTEGER);

    create table chat_session_messages (
        session_id TEXT NOT NULL,
        message_id TEXT NOT NULL,
        turn_id TEXT,
        sequence INTEGER NOT NULL,
        payload_json TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'completed',
        feedback TEXT,
        source TEXT NOT NULL,
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL,
        role TEXT, message_stream TEXT, voice_session_id TEXT, subtype TEXT,
        runtime_input_state TEXT, fork_availability TEXT, turn_started_at INTEGER,
        is_compaction_boundary INTEGER, has_visible_content INTEGER,
        payload_bytes INTEGER, payload_hash TEXT, payload_schema_version INTEGER,
        PRIMARY KEY (session_id, message_id),
        FOREIGN KEY (session_id) REFERENCES chat_sessions(session_id) ON DELETE CASCADE);

    create unique index chat_session_messages_sequence
        on chat_session_messages(session_id, sequence);

    create table chat_session_search_segments (
        row_id INTEGER PRIMARY KEY,
        session_id TEXT NOT NULL,
        message_id TEXT NOT NULL,
        turn_id TEXT NOT NULL,
        sequence INTEGER NOT NULL,
        position INTEGER NOT NULL,
        segment_id TEXT NOT NULL,
        text TEXT NOT NULL,
        normalized_text TEXT NOT NULL,
        UNIQUE (session_id, segment_id),
        FOREIGN KEY (session_id, message_id)
          REFERENCES chat_session_messages(session_id, message_id) ON DELETE CASCADE);

    create table chat_session_history_revisions (
        session_id TEXT PRIMARY KEY REFERENCES chat_sessions(session_id) ON DELETE CASCADE,
        revision INTEGER NOT NULL DEFAULT 0);

    create virtual table chat_session_search_fts using fts5(
        normalized_text,
        content = 'chat_session_search_segments',
        content_rowid = 'row_id',
        tokenize = 'trigram');

    create trigger chat_session_search_segments_insert
        after insert on chat_session_search_segments begin
          insert into chat_session_search_fts(rowid, normalized_text)
            values (new.row_id, new.normalized_text);
        end;

    create trigger chat_session_search_segments_update
        after update on chat_session_search_segments begin
          insert into chat_session_search_fts(chat_session_search_fts, rowid, normalized_text)
            values ('delete', old.row_id, old.normalized_text);
          insert into chat_session_search_fts(rowid, normalized_text)
            values (new.row_id, new.normalized_text);
        end;

    create trigger chat_session_search_segments_delete
        after delete on chat_session_search_segments begin
          insert into chat_session_search_fts(chat_session_search_fts, rowid, normalized_text)
            values ('delete', old.row_id, old.normalized_text);
        end;

    create trigger chat_session_history_insert
        after insert on chat_session_messages begin
          insert into chat_session_history_revisions(session_id, revision)
            select new.session_id, 1
              where exists (select 1 from chat_sessions where session_id=new.session_id)
            on conflict(session_id) do update set revision=revision+1;
        end;

    create trigger chat_session_history_update
        after update on chat_session_messages
        when old.payload_json is not new.payload_json or old.status is not new.status begin
          insert into chat_session_history_revisions(session_id, revision)
            select new.session_id, 1
              where exists (select 1 from chat_sessions where session_id=new.session_id)
            on conflict(session_id) do update set revision=revision+1;
        end;

    create trigger chat_session_history_delete
        after delete on chat_session_messages begin
          insert into chat_session_history_revisions(session_id, revision)
            select old.session_id, 1
              where exists (select 1 from chat_sessions where session_id=old.session_id)
            on conflict(session_id) do update set revision=revision+1;
        end;
"""

#: 文字稿里的可见文本（「搜得到」与「台账不含正文」两类断言都靠它）
_TEXT_A = "第一段回答"
_TEXT_B = "第二段回答"
_TEXT_C = "另一条会话的回答"


def _new_db_path(home: str) -> str:
    return os.path.join(home, "AppData", "Roaming", "com.qodercn.app.stable", "main.sqlite")


def _make_new_db(home: str, with_product_row: bool = True, psv: int = 2,
                 product_cwd: str = r"D:\demo\proj") -> str:
    """建一份新版会话库；``with_product_row`` 时放一条**产品自写**会话（形状推断源）。"""
    path = _new_db_path(home)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    con = sqlite3.connect(path)
    # ★ executescript 而非按 ';' 切分：触发器体内部就有分号（begin ... end;）
    con.executescript(_DDL)
    if with_product_row:
        con.execute("insert into workspaces (workspace_id, name, workspace_source,"
                    " root_paths_json, created_at, updated_at)"
                    " values ('ws-existing','demo','local',?,1,1)",
                    (json.dumps([product_cwd]),))
        target = json.dumps({"mode": "local",
                             "host": {"id": "local", "kind": "local", "label": "Local"},
                             "workspace": {"id": "ws_feae55c6" + "0" * 56,
                                           "path": product_cwd}},
                            ensure_ascii=False, separators=(",", ":"))
        con.execute("insert into chat_sessions (session_id, title, cwd, workspace_id,"
                    " model, permission_mode, execution_kind, execution_target_json,"
                    " extra_json, created_at, updated_at)"
                    " values ('product-session','产品自己的会话',?,'ws-existing',"
                    " 'qwen-max','bypassPermissions','local',?,?,1000,900000000000)",
                    (product_cwd, target,
                     json.dumps({"source": "workspace", "cliSessionReady": False,
                                 "modelSource": {"kind": "system"},
                                 "provisionalTitle": "产品自己的会话",
                                 "titleResolved": True, "titleSource": "user",
                                 "titleRevision": 0},
                                ensure_ascii=False, separators=(",", ":"))))
        con.execute("insert into chat_session_messages (session_id, message_id, turn_id,"
                    " sequence, payload_json, source, created_at, updated_at, role,"
                    " payload_schema_version, payload_hash, payload_bytes,"
                    " has_visible_content, is_compaction_boundary)"
                    " values ('product-session','pm1','pt1',1,?,'sdk-projection',1,1,"
                    " 'assistant',?,?,?,1,0)",
                    (PRODUCT_PAYLOAD, psv,
                     hashlib.sha256(PRODUCT_PAYLOAD.encode("utf-8")).hexdigest(),
                     len(PRODUCT_PAYLOAD.encode("utf-8"))))
        con.execute("insert into chat_session_search_segments (session_id, message_id,"
                    " turn_id, sequence, position, segment_id, text, normalized_text)"
                    " values ('product-session','pm1','turn:pt1',1,0,'message:pm1:text',"
                    " '产品自写的正文','产品自写的正文')")
    con.commit()
    con.close()
    return path


def _transcript_file(home: str, sid: str) -> str:
    return os.path.join(home, ".qoder-cn", "cache", "projects", "demo-abcdef12",
                        "conversation-history", sid[:8], sid[:8] + ".jsonl")


def _write_transcript(home: str, sid: str, records) -> None:
    """按产品形态写文字稿：``<projects>/<key>/conversation-history/<sid8>/<sid8>.jsonl``。"""
    os.makedirs(os.path.dirname(_transcript_file(home, sid)), exist_ok=True)
    with open(_transcript_file(home, sid), "w", encoding="utf-8") as fh:
        for role, text in records:
            fh.write(json.dumps({"role": role,
                                 "message": {"content": [{"type": "text", "text": text}]}},
                                ensure_ascii=False) + "\n")


def _make_old_db(home: str, with_transcript: bool = True) -> str:
    """旧版 ``local.db``：3 条会话（2 条有明文文字稿、1 条只剩密文）+ 密文消息行。"""
    shared = os.path.join(home, ".qoder-cn", "shared_client", "cache", "db")
    os.makedirs(shared, exist_ok=True)
    path = os.path.join(shared, "local.db")
    con = sqlite3.connect(path)
    con.execute("create table chat_session (session_id varchar(64),"
                " user_id varchar(64) not null, session_title varchar(256) not null,"
                " project_id varchar(64) not null, project_uri varchar(512),"
                " project_name varchar(64), gmt_create integer, gmt_modified integer,"
                " mode varchar(64), session_type varchar(64), parent_session_id varchar(64))")
    con.execute("create table chat_message (id varchar(64), session_id varchar(64),"
                " role varchar(64), content text, gmt_create integer)")
    for sid, title, created in (
            (OLD_SID, "这里需要考虑兼容 3.0 版本的结构", 1_700_000_000_000),
            (SECOND_SID, "多语言工具菜单命令绑定", 1_700_004_000_000),
            (NOISE_SID, "只有密文的会话", 1_700_001_800_000)):
        con.execute("insert into chat_session values (?,?,?,?,?,?,?,?,?,?,?)",
                    (sid, OLD_UID, title, "pid", r"D:\demo\proj", "demo",
                     created, created + 3_600_000, "agent", "assistant", ""))
    for i, (role, ct) in enumerate([("user", 10), ("assistant", 20)] * 3):
        con.execute("insert into chat_message values (?,?,?,?,?)",
                    ("m%d" % i, OLD_SID, role, "列级密文……", ct))
    con.commit()
    con.close()
    if with_transcript:
        _write_transcript(home, OLD_SID, [
            ("user", "第一问：兼容 3.0 层级"),
            ("assistant", _TEXT_A),
            ("assistant", _TEXT_B),
            ("user", "第二问"),
            ("assistant", "回答二"),
        ])
        _write_transcript(home, SECOND_SID, [("user", "绑定一下吧"), ("assistant", _TEXT_C)])
    return path


class _Fixture(unittest.TestCase):
    """公共夹具：临时 home + 临时工程目录（``plan_import`` 会校验工程目录是否存在）。"""

    def setUp(self) -> None:
        self.home = tempfile.mkdtemp(prefix="qsi-home-")
        self.project = os.path.join(self.home, "workspace", "demo-proj")
        os.makedirs(self.project, exist_ok=True)
        self.new_db = ""
        self.old_db = ""

    def tearDown(self) -> None:
        shutil.rmtree(self.home, ignore_errors=True)

    def make(self, product_row: bool = True, psv: int = 2, transcript: bool = True,
             live_project: bool = True) -> None:
        """建新旧两库，并把旧库会话的工程路径指向本次的临时目录。

        新库产品行的 ``cwd`` / ``root_paths_json`` 一起改到同一目录，
        「复用已有工作区」这条路径才测得到（``plan()`` 走的就是它）。
        """
        self.new_db = _make_new_db(self.home, with_product_row=product_row,
                                   psv=psv, product_cwd=self.project)
        self.old_db = _make_old_db(self.home, with_transcript=transcript)
        target = self.project if live_project else os.path.join(self.home, "gone")
        con = sqlite3.connect(self.old_db)
        con.execute("update chat_session set project_uri = ?", (target,))
        con.commit()
        con.close()
        con = sqlite3.connect(self.new_db)
        con.execute("update chat_sessions set cwd = ? where session_id='product-session'",
                    (self.project,))
        con.execute("update workspaces set root_paths_json = ?"
                    " where workspace_id='ws-existing'", (json.dumps([self.project]),))
        con.commit()
        con.close()

    def plan(self, **kw):
        return qsi.plan_import(self.home, NEW_UID, self.new_db, [self.old_db], **kw)

    def counts(self) -> dict:
        con = sqlite3.connect(self.new_db)
        try:
            return {t: con.execute("select count(*) from %s" % t).fetchone()[0] for t in
                    ("chat_sessions", "chat_session_messages",
                     "chat_session_search_segments", "workspaces")}
        finally:
            con.close()


class EligibilityTests(_Fixture):
    def test_all_present_and_new_account(self) -> None:
        self.make()
        e = qsi.check_eligibility(self.home, NEW_UID, self.new_db, [self.old_db])
        self.assertTrue(e.ready, e.reason_text)
        self.assertEqual(e.legacy_uid, OLD_UID)
        self.assertEqual(e.sessions_total, 3)
        self.assertEqual(e.sessions_with_transcript, 2)
        self.assertTrue(e.notices)      # 有损、不进上下文、要退出应用等必须交代

    def test_old_account_selected_blocks(self) -> None:
        self.make()
        e = qsi.check_eligibility(self.home, OLD_UID, self.new_db, [self.old_db])
        self.assertFalse(e.ready)
        self.assertIn("不是新版阿里云账号", e.reason_text)

    def test_unknown_account_blocks(self) -> None:
        """取不到当前账号（界面没选、本机登录态也没有）同样不提供。

        条件 3 判的就是「所选账号是不是换代后的新账号」；判不出等于没判，
        放开入口只会让用户对着一个来源不明的库做写操作。
        """
        self.make()
        e = qsi.check_eligibility(self.home, "", self.new_db, [self.old_db])
        self.assertFalse(e.ready)
        self.assertIn("没取到当前登录账号", e.reason_text)

    def test_same_account_as_legacy_blocks(self) -> None:
        """形态是 UUIDv7、但前 8 位与旧库账号一致 ⇒ 本机还没换代，照样拒绝。"""
        self.make()
        e = qsi.check_eligibility(self.home, "13166325-47ef-70d2-8a41-ea6278c176b1",
                                  self.new_db, [self.old_db])
        self.assertFalse(e.ready)
        self.assertIn("还没发生换代", e.reason_text)

    def test_missing_new_db_blocks(self) -> None:
        self.make()
        e = qsi.check_eligibility(self.home, NEW_UID,
                                  os.path.join(self.home, "nope", "main.sqlite"),
                                  [self.old_db])
        self.assertFalse(e.ready)
        self.assertIn("main.sqlite", e.reason_text)

    def test_missing_transcript_blocks(self) -> None:
        self.make(transcript=False)
        e = qsi.check_eligibility(self.home, NEW_UID, self.new_db, [self.old_db])
        self.assertFalse(e.ready)
        self.assertIn("明文文字稿", e.reason_text)

    def test_uid_form(self) -> None:
        self.assertTrue(qsi.is_new_generation_uid(NEW_UID))
        self.assertFalse(qsi.is_new_generation_uid(OLD_UID))
        self.assertFalse(qsi.is_new_generation_uid(""))
        self.assertFalse(qsi.is_new_generation_uid("019eb095"))


class ReadSideTests(_Fixture):
    def test_turn_grouping_keeps_assistant_segments(self) -> None:
        self.make()
        by = {s.session_id: s for s in qsi.list_legacy_sessions([self.old_db], self.home)}
        s = by[OLD_SID]
        self.assertTrue(s.has_transcript)
        self.assertEqual(len(s.turns), 2)
        self.assertEqual(s.turns[0].assistant_parts, [_TEXT_A, _TEXT_B])
        self.assertEqual(s.turns[1].assistant_parts, ["回答二"])
        self.assertEqual(s.db_message_count, 6)
        self.assertEqual(by[NOISE_SID].turns, [])

    def test_leading_assistant_record_dropped(self) -> None:
        """没有对应提问的前导 assistant 记录会被丢弃（造一轮「空提问」会误导界面）。"""
        _write_transcript(self.home, "abc12345", [("assistant", "没有提问的回答")])
        with open(_transcript_file(self.home, "abc12345"), "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"role": "user", "message": {"content": [
                {"type": "text", "text": "问"}]}}) + "\n")
        turns, records = qsi.parse_transcript(_transcript_file(self.home, "abc12345"))
        self.assertEqual(records, 2)
        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0].assistant_parts, [])


class PlanTests(_Fixture):
    def test_reuses_existing_workspace_and_infers_shape(self) -> None:
        self.make()
        plan = self.plan()
        self.assertEqual(len(plan.sessions), 2)
        sp = plan.sessions[0]
        self.assertIsNone(sp.workspace_row)          # 产品已有同路径工作区 ⇒ 不新增
        self.assertEqual(sp.new_workspace_id, "ws-existing")
        self.assertEqual(plan.shape.model, "qwen-max")
        self.assertEqual(plan.shape.permission_mode, "bypassPermissions")
        self.assertEqual(plan.shape.payload_schema_version, 2)
        # 工作区描述符整份复用产品行，而不是自造
        self.assertIn("ws_feae55c6", sp.session_row["execution_target_json"])
        self.assertEqual(sp.inferred, ())

    def test_new_workspace_row_and_declared_inference_when_path_unseen(self) -> None:
        self.make(product_row=False)
        plan = self.plan()
        self.assertEqual(len(plan.sessions), 2)
        sp = plan.sessions[0]
        self.assertIsNotNone(sp.workspace_row)
        self.assertEqual(json.loads(sp.workspace_row["root_paths_json"]), [self.project])
        self.assertTrue(any("sha256" in x for x in sp.inferred), sp.inferred)

    def test_skips_no_transcript_session(self) -> None:
        self.make()
        plan = self.plan()
        self.assertEqual([x[0] for x in plan.skipped], [NOISE_SID[:8]])
        self.assertIn("明文文字稿", plan.skipped[0][2])

    def test_missing_project_dir_skipped_unless_include_all(self) -> None:
        self.make(live_project=False)
        plan = self.plan()
        self.assertTrue(plan.empty)
        self.assertIn("工程目录已不存在", "；".join(r for _s, _t, r in plan.skipped))
        self.assertEqual(len(self.plan(include_all=True).sessions), 2)

    def test_session_ids_selection_accepts_short_prefix(self) -> None:
        self.make()
        plan = self.plan(session_ids=[OLD_SID[:8]])
        self.assertEqual([s.legacy.session_id for s in plan.sessions], [OLD_SID])

    def test_ids_are_deterministic(self) -> None:
        self.make()
        # 固定身份：默认排序按 gmt_modified desc，OLD_SID 不是第一位
        a, b = self.plan(session_ids=[OLD_SID[:8]]), self.plan(session_ids=[OLD_SID[:8]])
        self.assertEqual([s.new_session_id for s in a.sessions],
                         [s.new_session_id for s in b.sessions])
        self.assertEqual(a.sessions[0].new_session_id,
                         qsi._rid(qsi._namespace(OLD_SID), "session"))

    def test_payload_hash_ids_and_segment_uniqueness(self) -> None:
        self.make()
        sp = self.plan(session_ids=[OLD_SID[:8]]).sessions[0]
        self.assertEqual([m["role"] for m in sp.message_rows],
                         ["user", "assistant", "user", "assistant"])
        for m in sp.message_rows:
            raw = m["payload_json"].encode("utf-8")
            self.assertEqual(m["payload_hash"], hashlib.sha256(raw).hexdigest())
            self.assertEqual(m["payload_bytes"], len(raw))
            self.assertEqual(m["payload_schema_version"], 2)
            self.assertEqual(m["source"], "sdk-projection")
        self.assertEqual(sp.message_rows[1]["message_id"],
                         "assistant:" + sp.message_rows[0]["message_id"])
        self.assertEqual(sp.message_rows[0]["turn_id"], sp.message_rows[1]["turn_id"])
        seg_ids = [s[4] for s in sp.segment_rows]
        self.assertEqual(len(seg_ids), len(set(seg_ids)))
        # 一段可见文本一行片段：user 1 + assistant 2 + user 1 + assistant 1
        self.assertEqual(len(sp.segment_rows), 5)

    def test_summary_reports_loss_and_coverage(self) -> None:
        self.make()
        plan = self.plan()
        text = "\n".join(plan.summary_lines())
        self.assertIn("有损", text)
        self.assertIn("会话共 3 条，其中 2 条取到明文文字稿", text)
        self.assertIn("payload_schema_version=2", text)
        self.assertIn("刻意不写", "\n".join(plan.detail_lines()))


class ApplyRollbackTests(_Fixture):
    def test_apply_then_verify_then_rollback(self) -> None:
        self.make()
        before = self.counts()
        # 只导 OLD_SID：它的文字稿里才有 _TEXT_B，且排序不保证它在第一位
        plan = self.plan(session_ids=[OLD_SID[:8]])
        res = qsi.apply_import(plan)
        self.assertTrue(res["ok"], res["error"])
        self.assertEqual(res["problems"], [])
        self.assertEqual(res["skipped_existing"], [])
        mid = self.counts()
        n_msgs = sum(len(s.message_rows) for s in plan.sessions)
        n_segs = sum(len(s.segment_rows) for s in plan.sessions)
        self.assertEqual(mid["chat_sessions"], before["chat_sessions"] + 1)
        self.assertEqual(mid["chat_session_messages"], before["chat_session_messages"] + n_msgs)
        self.assertEqual(mid["chat_session_search_segments"],
                         before["chat_session_search_segments"] + n_segs)
        self.assertEqual(mid["workspaces"], before["workspaces"])   # 复用 ⇒ 不新增行

        con = sqlite3.connect(self.new_db)
        try:
            sid = plan.sessions[0].new_session_id
            # FTS 虚表只登记 normalized_text 一列，取会话 id 要经 row_id 回表
            hit = [r[0] for r in con.execute(
                "select s.session_id from chat_session_search_fts"
                " join chat_session_search_segments as s"
                " on s.row_id = chat_session_search_fts.rowid"
                " where chat_session_search_fts match '\"第二段回答\"'")]
            self.assertEqual(hit, [sid])             # 触发器把 FTS 索引维护上了
            rev = con.execute("select revision from chat_session_history_revisions"
                              " where session_id=?", (sid,)).fetchone()
            self.assertEqual(rev[0], len(plan.sessions[0].message_rows))
            # 产品自写的会话与消息原封不动
            self.assertEqual(con.execute(
                "select payload_json from chat_session_messages where message_id='pm1'"
                ).fetchone()[0], PRODUCT_PAYLOAD)
            self.assertEqual(con.execute(
                "select count(*) from chat_sessions where session_id='product-session'"
                ).fetchone()[0], 1)
            self.assertEqual(con.execute(
                "select model from chat_sessions where session_id='product-session'"
                ).fetchone()[0], "qwen-max")
        finally:
            con.close()

        # 重复导入 ⇒ 确定性 id 命中，只跳过、不写第二份
        res2 = qsi.apply_import(self.plan(session_ids=[OLD_SID[:8]]))
        self.assertTrue(res2["ok"], res2["error"])
        self.assertEqual(len(res2["skipped_existing"]), 1)
        self.assertEqual(self.counts(), mid)

        back = qsi.rollback_import(self.new_db)
        self.assertTrue(back["ok"], back["error"])
        self.assertEqual(self.counts(), before)
        con = sqlite3.connect(self.new_db)
        try:
            self.assertEqual(con.execute(
                "select count(*) from chat_session_search_fts"
                " where chat_session_search_fts match '\"第二段回答\"'").fetchone()[0], 0)
            self.assertEqual(con.execute(
                "select payload_json from chat_session_messages where message_id='pm1'"
                ).fetchone()[0], PRODUCT_PAYLOAD)
        finally:
            con.close()

    def test_only_selected_sessions_rollback(self) -> None:
        self.make()
        plan = self.plan()
        self.assertTrue(qsi.apply_import(plan)["ok"])
        sid = plan.sessions[0].new_session_id
        back = qsi.rollback_import(self.new_db, [sid[:8]])
        self.assertTrue(back["ok"], back["error"])
        self.assertEqual(back["rolled_back"], [sid])
        con = sqlite3.connect(self.new_db)
        try:
            self.assertEqual(con.execute("select count(*) from chat_sessions where session_id=?",
                                         (sid,)).fetchone()[0], 0)
            self.assertEqual(con.execute(
                "select count(*) from chat_sessions where session_id not in (?,"
                " 'product-session')", (sid,)).fetchone()[0], 1)
        finally:
            con.close()

    def test_snapshot_created_once_and_reused(self) -> None:
        self.make()
        first = qsi.apply_import(self.plan(session_ids=[OLD_SID[:8]]))
        self.assertTrue(first["ok"], first["error"])
        self.assertEqual(first["snapshot"]["reused"], False)
        self.assertTrue(os.path.isfile(qsi._snapshot_path(self.new_db)))
        second = qsi.apply_import(self.plan(session_ids=[SECOND_SID[:8]]))
        self.assertTrue(second["ok"], second["error"])
        self.assertEqual(second["snapshot"]["reused"], True)
        self.assertEqual(second["snapshot"]["sha256"], first["snapshot"]["sha256"])

    def test_tampered_snapshot_aborts(self) -> None:
        self.make()
        qsi.apply_import(self.plan(session_ids=[OLD_SID[:8]]))
        with open(qsi._snapshot_path(self.new_db), "ab") as fh:
            fh.write(b"\x00")
        before = self.counts()
        with self.assertRaises(qsi.ImportAbort):
            qsi.apply_import(self.plan(session_ids=[SECOND_SID[:8]]))
        self.assertEqual(self.counts(), before)

    def test_psv_drift_refuses_and_leaves_db_untouched(self) -> None:
        self.make(psv=1)
        plan = self.plan()
        self.assertEqual(plan.shape.payload_schema_version, 1)
        before = self.counts()
        res = qsi.apply_import(plan)
        self.assertFalse(res["ok"])
        self.assertIn("payload_schema_version=1", res["error"])
        self.assertEqual(self.counts(), before)
        self.assertFalse(os.path.isfile(qsi._snapshot_path(self.new_db)))

    def test_ledger_stores_no_title_no_body(self) -> None:
        self.make()
        qsi.apply_import(self.plan())
        with open(qsi._manifest_path(self.new_db), encoding="utf-8") as fh:
            raw = fh.read()
        for secret in (_TEXT_A, _TEXT_B, _TEXT_C, "这里需要考虑兼容", "多语言工具"):
            self.assertNotIn(secret, raw)
        self.assertIn("payload_hashes", raw)

    def test_created_workspace_row_removed_on_rollback(self) -> None:
        self.make(product_row=False)
        before = self.counts()
        res = qsi.apply_import(self.plan())
        self.assertTrue(res["ok"], res["error"])
        self.assertEqual(self.counts()["workspaces"], before["workspaces"] + 2)
        back = qsi.rollback_import(self.new_db)
        self.assertTrue(back["ok"], back["error"])
        self.assertEqual(self.counts(), before)
        self.assertEqual(len(back["workspaces_removed"]), 2)

    def test_rollback_never_touches_unledged_rows(self) -> None:
        """台账只记本工具写过的行：产品会话的 id 传进来也不得被动到。"""
        self.make()
        back = qsi.rollback_import(self.new_db, ["product-session"])
        self.assertFalse(back["ok"])
        con = sqlite3.connect(self.new_db)
        try:
            self.assertEqual(con.execute(
                "select count(*) from chat_sessions where session_id='product-session'"
                ).fetchone()[0], 1)
        finally:
            con.close()


class SchemaDriftTests(_Fixture):
    def test_clean_fixture_has_no_drift(self) -> None:
        self.make()
        self.assertEqual(qsi.schema_drift(self.new_db), [])

    def test_missing_trigger_is_drift(self) -> None:
        self.make()
        con = sqlite3.connect(self.new_db)
        con.execute("drop trigger chat_session_search_segments_insert")
        con.commit()
        con.close()
        drift = qsi.schema_drift(self.new_db)
        self.assertTrue(any("chat_session_search_segments_insert" in d for d in drift), drift)

    def test_missing_fts_table_is_drift(self) -> None:
        self.make()
        con = sqlite3.connect(self.new_db)
        con.execute("drop table chat_session_search_fts")
        con.commit()
        con.close()
        self.assertTrue(any("chat_session_search_fts" in d
                            for d in qsi.schema_drift(self.new_db)))

    def test_drift_blocks_eligibility(self) -> None:
        self.make()
        con = sqlite3.connect(self.new_db)
        con.execute("drop trigger chat_session_history_insert")
        con.commit()
        con.close()
        e = qsi.check_eligibility(self.home, NEW_UID, self.new_db, [self.old_db])
        self.assertFalse(e.ready)
        self.assertIn("结构", e.reason_text)


class PreflightTests(_Fixture):
    def test_empty_plan_reports(self) -> None:
        self.make(transcript=False)
        plan = self.plan()
        self.assertTrue(plan.empty)
        self.assertEqual(qsi.preflight(plan), ["计划为空：没有任何会话可写入"])

    def test_rollback_without_ledger_is_noop(self) -> None:
        self.make()
        res = qsi.rollback_import(self.new_db, ["deadbeef-0000-0000-0000-000000000000"])
        self.assertFalse(res["ok"])
        self.assertIn("台账里没有匹配的写入记录", res["error"])

    def test_missing_target_db_reports(self) -> None:
        self.make()
        plan = self.plan()
        plan.new_db = os.path.join(self.home, "gone", "main.sqlite")
        self.assertTrue(qsi.preflight(plan)[0].startswith("目标库不存在"))

    def test_preflight_blocks_non_new_generation_account(self) -> None:
        """命令行没有界面把关，账号形态必须在写前兜底复核（三种误操作都拦住）。"""
        cases = (
            ("", "未能确定「当前所选账号」"),
            (OLD_UID, "不是新版阿里云账号"),
            # UUIDv7 形态、但前 8 位与旧账号目录名一致 ⇒ 本机其实还没换代
            ("13166325-0000-7000-8000-000000000000", "仍是同一个"),
        )
        self.make()
        base = self.plan()
        for uid, expect in cases:
            with self.subTest(uid=uid):
                plan = base
                plan.current_uid = uid
                self.assertIn(expect, qsi.preflight(plan)[0])

    def test_preflight_allows_validated_account(self) -> None:
        """反例：条件齐备时这几条不许误拦（否则界面上的按钮永远点不动）。"""
        self.make()
        self.assertEqual(qsi.preflight(self.plan()), [])
