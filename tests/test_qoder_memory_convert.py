"""Qoder 旧明文记忆 → 新版 md 转换器的单元测试（全合成样本 + 临时目录）。

钉住三条铁律（模块 docstring）：

1. **不碰向量虚表**：``*_embedding*`` 是 vec0 虚表，只 SELECT 两张普通表——
   测试库里放一张同名虚表（用普通表模拟 schema 登记），读取必须不炸也不读它；
2. **合并不覆盖**：目标文件已存在一律跳过，绝不改写既有记忆；
3. **落点跟随实证**：默认 legacy 布局 = 产品已证实可见的形态
   （``memories/<uid>/projects/<key>/<分类>/<标题>.md``），frontmatter 逐字段
   对照产品自己转换出的真实样本（title / usage_scenario / keywords）。
"""

import json
import os
import shutil
import sqlite3
import tempfile
import unittest

from ai_env_clone import qoder_memory_convert as qmc


def _make_db(tmp: str) -> str:
    """构造最小旧版 local.db：两张记忆表 + 一张「向量虚表」占位。"""
    path = os.path.join(tmp, "local.db")
    con = sqlite3.connect(path)
    con.execute("""create table agent_memory (
        id text primary key, gmt_create text, gmt_modified text,
        scope text, scope_id text, keywords text, title text, content text,
        session_id text, is_merged int, freq int, source text, token_count int,
        type text, user_id text, category text, retention_score real,
        status text, usage_scenario text)""")
    con.execute("""create table lingma_memory (
        id text primary key, scope text, scope_id text, keywords text,
        title text, content text, category text, gmt_create text, status text)""")
    # ★ 用普通表模拟「schema 里有 embedding 对象」：读取逻辑按表名黑名单根本
    #   不会 SELECT 它，这里只验证「列出表后不误读」。
    con.execute("create table agent_memory_embedding_info (id text)")
    rows = [
        ("a1", "global", "", "全局记忆一", "全局内容", "关键词A,关键词B",
         "expert_experience", "[]"),
        ("a2", "workspace", r"D:\demo\proj", "项目记忆一",
         "项目内容第一行\n\n**加粗**", "kw1,kw2", "project_introduction",
         '["代码评审"]'),
        ("a3", "workspace", r"D:\demo\proj", "标题与内容皆有的重复项",
         "内容", "", "project_tech_stack", "[]"),
        ("a4", "workspace", "", "scope_id 为空的工作区记忆（按 global 兜底落盘）",
         "内容", "kw", "misc", None),
        ("a5", "workspace", r"D:\demo\proj", "", "", "", "misc", None),  # 空记忆
    ]
    con.executemany(
        "insert into agent_memory values (%s)" % ",".join("?" * 19),
        [("a%d" % (i + 1), "2026-05-06", "2026-05-06", r[1], r[2], r[5], r[3],
          r[4], "", 0, 1, "auto", 100, "memory", "1316632530577119",
          r[6], 0.5, "active", r[7])
         for i, r in enumerate(rows)])
    con.execute("insert into lingma_memory values "
                "('l1','workspace',?,'老kw','更老一代记忆',\"更老一代内容\","
                "'project_specification','2025-06-01','active')",
                (r"D:\demo\proj",))
    con.commit()
    con.close()
    return path


class TestConvert(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="qmem_")
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.db = _make_db(self.tmp)

    def test_read_skips_embedding_tables_and_reads_both(self):
        mems = qmc.read_legacy_memories(self.db)
        tables = {m.source_table for m in mems}
        self.assertEqual(tables, {"agent_memory", "lingma_memory"})
        self.assertEqual(len(mems), 6)          # 5 + 1（a5 空记忆也在，plan 才过滤）

    def test_keywords_and_scenario_parsing(self):
        mems = {m.title: m for m in qmc.read_legacy_memories(self.db)}
        self.assertEqual(mems["全局记忆一"].keywords, ["关键词A", "关键词B"])
        self.assertEqual(mems["项目记忆一"].usage_scenario, ["代码评审"])
        self.assertEqual(mems["全局记忆一"].usage_scenario, [])

    def test_render_matches_product_sample_shape(self):
        """frontmatter 逐字段对照产品真实转换样本（title/usage_scenario/keywords）。"""
        mems = {m.title: m for m in qmc.read_legacy_memories(self.db)}
        md = qmc.render_memory_md(mems["项目记忆一"])
        self.assertTrue(md.startswith("---\n"))
        self.assertIn('title: "项目记忆一"', md)
        self.assertIn('usage_scenario: ["代码评审"]', md)
        self.assertIn('    - "kw1"', md)
        self.assertIn("**加粗**", md)              # 正文原样
        self.assertTrue(md.rstrip().endswith("项目内容第一行\n\n**加粗**".split("\n")[-1]))

    def test_plan_legacy_layout_groups_by_scope(self):
        plan = qmc.plan_conversion(self.db, os.path.join(self.tmp, "qoder"),
                                   layout="legacy", uid="019eb095")
        self.assertEqual(plan.skipped_empty, 1)     # a5 空记忆
        targets = {os.path.relpath(e.target_path,
                                   os.path.join(self.tmp, "qoder"))
                   for e in plan.entries}
        self.assertIn(os.path.join("memories", "019eb095", "global",
                                   "expert_experience", "全局记忆一.md"), targets)
        self.assertIn(os.path.join("memories", "019eb095", "projects",
                                   "D-demo-proj", "project_introduction",
                                   "项目记忆一.md"), targets)
        # global scope 的 workspace 记忆（scope_id 为空）按 global 兜底
        self.assertIn(os.path.join("memories", "019eb095", "global", "misc",
                                   "scope_id 为空的工作区记忆（按 global 兜底落盘）.md"),
                      targets)

    def test_plan_new_layout(self):
        plan = qmc.plan_conversion(self.db, os.path.join(self.tmp, "qoder"),
                                   layout="new", uid="019eb095")
        targets = {os.path.relpath(e.target_path,
                                   os.path.join(self.tmp, "qoder"))
                   for e in plan.entries}
        self.assertIn(os.path.join("memory", "expert_experience",
                                   "全局记忆一.md"), targets)
        self.assertIn(os.path.join("projects", "D-demo-proj", "memory",
                                   "project_introduction", "项目记忆一.md"), targets)

    def test_apply_creates_then_second_plan_skips(self):
        """合并不覆盖：首轮全建，复跑计划全部转 exists。"""
        qoder = os.path.join(self.tmp, "qoder")
        plan = qmc.plan_conversion(self.db, qoder, uid="u1")
        result = qmc.apply_conversion(plan)
        self.assertEqual(result["created"], plan.to_create)
        self.assertEqual(result["errors"], [])
        plan2 = qmc.plan_conversion(self.db, qoder, uid="u1")
        self.assertEqual(plan2.to_create, 0)
        self.assertEqual(plan2.existing, plan.to_create)
        result2 = qmc.apply_conversion(plan2)
        self.assertEqual(result2["created"], 0)

    def test_apply_never_overwrites_existing_file(self):
        """同名文件已存在 ⇒ 跳过且内容原样（用户的既有记忆一字不动）。"""
        qoder = os.path.join(self.tmp, "qoder")
        existing = os.path.join(qoder, "memories", "u1", "global",
                                "expert_experience", "全局记忆一.md")
        os.makedirs(os.path.dirname(existing))
        with open(existing, "w", encoding="utf-8") as f:
            f.write("用户自己的版本")
        plan = qmc.plan_conversion(self.db, qoder, uid="u1")
        qmc.apply_conversion(plan)
        with open(existing, encoding="utf-8") as f:
            self.assertEqual(f.read(), "用户自己的版本")

    def test_match_project_key_variants(self):
        """实测存在多种 key 拼法：命中已有目录优先，未命中才用推导形。"""
        hit, yes = qmc.match_project_key(
            r"D:\Desktop\project\TbmHmi_Alpha",
            ["D-Desktop-project-TbmHmi_Alpha"])
        self.assertTrue(yes)
        self.assertEqual(hit, "D-Desktop-project-TbmHmi_Alpha")
        hit, yes = qmc.match_project_key(
            r"D:\project\TbmHmi_Alpha", ["d-project-TbmHmi_Alpha"])
        self.assertTrue(yes)                        # 大小写不敏感
        hit, yes = qmc.match_project_key(r"D:\demo\proj", [])
        self.assertFalse(yes)
        self.assertEqual(hit, "D-demo-proj")

    def test_lingma_excluded_on_request(self):
        mems = qmc.read_legacy_memories(self.db, include_lingma=False)
        self.assertEqual({m.source_table for m in mems}, {"agent_memory"})

    def test_infer_qoder_home_strips_all_three_levels(self):
        """local.db 在 <根>/shared_client/cache/db/ 下：三层都要剥（只剥一层会把
        根停在 shared_client，落点全错——真实 dry-run 的「已存在 859」即此 bug）。"""
        root = os.path.join(self.tmp, "qoder")
        db = os.path.join(root, "shared_client", "cache", "db", "local.db")
        self.assertEqual(qmc.infer_qoder_home(db), root)
        self.assertEqual(qmc.infer_qoder_home(
            os.path.join(root, "local.db")), root)


if __name__ == "__main__":
    unittest.main()
