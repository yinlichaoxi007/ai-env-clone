"""「携带源设备信息」的登记、展示与跨机提示。

需求（用户 2026-09-27）：备份包里本来就带走源机器的部分路径等信息（会话正文里
大量出现绝对路径、IDE 工作区记录里是工程文件夹路径）。这些信息对「把数据还原回
原本的工程工作区」是必要的，但**用户应当知道包里有它们**——尤其是要分享/上传
备份包的场景，所以要在备份包详情里能看见，并明确提醒。

本组用例锁定三件事：
1. 登记口径（只登记确实存在的条目，不虚报）；
2. 清单结构（写进 ``manifest["origin_info"]``，旧包没有该字段也不能炸）；
3. 文案口径（同一逻辑项多文件合并成一行；说明里点出「分享前留意」）。
"""

import json
import os
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_env_clone import core  # noqa: E402


def _item(root: str, key: str, rel: str, note: str, content: str = "x"):
    path = os.path.join(root, rel.replace("/", os.sep))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return core.BackupItem(
        key=key, label="标签 " + key, path=path, description="d",
        carries_origin=note,
    )


class TestOriginInfoEntries(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="origin_test_")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))

    def test_keeps_order_and_dedupes_by_key(self):
        a = _item(self.tmp, "k1", "a.json", "说明一")
        b = _item(self.tmp, "k2", "b.json", "说明二")
        rows = core.origin_info_entries([a, b, a])
        self.assertEqual([r["key"] for r in rows], ["k1", "k2"])
        self.assertEqual(rows[0]["note"], "说明一")

    def test_sorted_by_key(self):
        z = _item(self.tmp, "zzz", "z.json", "n")
        a = _item(self.tmp, "aaa", "a.json", "n")
        rows = core.origin_info_entries([z, a])
        self.assertEqual([r["key"] for r in rows], ["aaa", "zzz"])

    def test_blank_note_is_not_registered(self):
        it = _item(self.tmp, "k", "a.json", "")
        plainly = _item(self.tmp, "k2", "b.json", "   ")
        self.assertEqual(core.origin_info_entries([it, plainly]), [])

    def test_label_is_carried_through(self):
        """label 要进清单：展示侧不能只拿 key（key 里有 :hash，给人看没意义）。"""
        rows = core.origin_info_entries([_item(self.tmp, "k:x1", "a.json", "n")])
        self.assertEqual(rows[0]["label"], "标签 k:x1")


class TestManifestRoundTrip(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="origin_test_")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.zip = os.path.join(self.tmp, "out.zip")

    def _export(self, items):
        return core.export_backup(self.zip, items, self.tmp, tool_name="codebuddy")

    def test_manifest_records_origin_info(self):
        items = [
            _item(self.tmp, "ide_workspace_records:a", "ws/a/workspace.json", "工程路径"),
            _item(self.tmp, "plain", "plain.json", ""),
        ]
        mf = self._export(items)
        self.assertEqual(mf["origin_info"],
                         [{"key": "ide_workspace_records:a",
                           "label": "标签 ide_workspace_records:a",
                           "note": "工程路径"}])

    def test_missing_item_not_registered(self):
        """不存在的条目不会进包，报它就是虚报——不能登记。"""
        items = [
            _item(self.tmp, "here", "here.json", "有"),
            core.BackupItem(key="gone", label="标签 gone",
                            path=os.path.join(self.tmp, "nope.json"),
                            description="d", carries_origin="无"),
        ]
        mf = self._export(items)
        self.assertEqual([r["key"] for r in mf["origin_info"]], ["here"])
        self.assertNotIn("gone", mf["items"])

    def test_inspect_reads_it_back(self):
        self._export([_item(self.tmp, "k", "a.json", "工程路径")])
        info = core.inspect_backup(self.zip)
        self.assertEqual(core.manifest_origin_info(info["manifest"])[0]["note"], "工程路径")

    def test_old_pack_without_field_is_fine(self):
        """旧版本备份包没有 origin_info：读它必须返回空表，而不是报错/编造。"""
        with zipfile.ZipFile(self.zip, "w") as zf:
            zf.writestr("a.json", "{}")
            zf.writestr(core.MANIFEST_NAME, json.dumps(
                {"version": core.MANIFEST_VERSION, "kind": "backup",
                 "tool": "codebuddy", "items": ["k"]}))
        info = core.inspect_backup(self.zip)
        self.assertEqual(core.manifest_origin_info(info["manifest"]), [])
        self.assertEqual(core.origin_info_lines(info["manifest"]), [])


class TestManifestOriginInfoTolerance(unittest.TestCase):
    def test_malformed_inputs(self):
        self.assertEqual(core.manifest_origin_info(None), [])
        self.assertEqual(core.manifest_origin_info({"origin_info": "nope"}), [])
        self.assertEqual(core.manifest_origin_info({"origin_info": [None, 1, "x"]}), [])
        # 缺 note / note 全空白 -> 丢弃（展示侧不显示半截行）
        rows = core.manifest_origin_info({"origin_info": [
            {"key": "a", "label": "A"}, {"key": "b", "note": "  "},
            {"key": "c", "note": "ok"},
        ]})
        self.assertEqual([r["key"] for r in rows], ["c"])


class TestOriginInfoLines(unittest.TestCase):
    def _lines(self, rows):
        return core.origin_info_lines({"origin_info": rows})

    def test_merges_same_label_and_note(self):
        """同一逻辑项的多个文件条目（如一条工作区一个 workspace.json）合并成一行。"""
        rows = [{"key": "ide:%d" % i, "label": "IDE 工作区记录", "note": "工程路径"}
                for i in range(3)]
        lines = self._lines(rows)
        body = [x for x in lines if "IDE 工作区记录" in x]
        self.assertEqual(len(body), 1)
        self.assertIn("工程路径", body[0])

    def test_different_notes_stay_separate(self):
        rows = [{"key": "a", "label": "IDE 工作区记录", "note": "工程路径"},
                {"key": "b", "label": "集中会话", "note": "会话正文含路径"}]
        lines = "\n".join(self._lines(rows))
        self.assertIn("工程路径", lines)
        self.assertIn("会话正文含路径", lines)

    def test_warns_about_sharing(self):
        """提醒的落点是「分享/上传包之前」——不能只罗列条目就当交代完了。"""
        text = "\n".join(self._lines([{"key": "k", "label": "L", "note": "N"}]))
        self.assertIn("分享", text)
        self.assertIn("携带的源设备信息", text)

    def test_mentions_source_user_name_in_archive(self):
        """归档结构本身也带源机器的用户名 / 登录用户标识，这句话不能省。"""
        text = "\n".join(self._lines([{"key": "k", "label": "L", "note": "N"}]))
        self.assertIn("用户名", text)
        self.assertIn("还原", text)

    def test_empty_rows_render_nothing(self):
        self.assertEqual(core.origin_info_lines({"origin_info": []}), [])
        self.assertEqual(core.origin_info_lines({}), [])


class TestAdaptersDeclareOriginInfo(unittest.TestCase):
    """适配器侧：实测「会带走源机器路径」的条目必须声明，且措辞统一。"""

    def _items(self, tool: str):
        from ai_env_clone.adapters import get_adapter
        ad = get_adapter(tool)
        root = ad.detect_root()
        return {it.key: it for it in ad.build_items(root)}, root

    def test_measured_items_are_marked(self):
        # 每一项都是 2026-09-27 本机实测「命中源机器绝对路径」的条目：
        # qoder project_sessions 71/80、session_db 1/1；
        # workbuddy projects 11/17、workspace_sessions 16/80、session_db 1/1；
        # codebuddy history / check-point（24519 个 JSON 里 20564 命中）。
        want = {
            "qoder": ["project_sessions", "session_db"],
            "workbuddy": ["projects", "workspace_sessions", "session_db"],
            "codebuddy": ["user_sessions:history", "user_sessions:checkpoint"],
        }
        for tool, keys in want.items():
            items, _root = self._items(tool)
            for key in keys:
                self.assertIn(key, items, "%s 缺条目 %s" % (tool, key))
                self.assertTrue(items[key].carries_origin,
                                "%s 的 %s 未声明携带源设备信息" % (tool, key))

    def test_unmeasured_items_are_not_marked(self):
        """没实测到路径的条目不做标注——避免「凡是会话类都说带路径」的宽泛断言。

        ``plan-task`` 实测 0/5 命中、``ai_agent_db`` 严格模式 0 命中（早先那点命中
        是随机二进制字节造成的假阳性），都**不标**。
        """
        items, _root = self._items("codebuddy")
        self.assertEqual(items["user_sessions:plan_task"].carries_origin, "")
        self.assertEqual(items["user_memories"].carries_origin, "")
        trae, _r = self._items("trae-cn")
        self.assertEqual(trae["trae_cn_ai_agent_db"].carries_origin, "")


class TestOriginInfoSurfacesInGui(unittest.TestCase):
    """界面也要说：勾选列表行、备份包详情、还原确认。

    只做纯函数的用例挡不住「引擎算对了、界面没说」——而这条需求的落点恰恰是界面。
    """

    def setUp(self) -> None:
        import shutil
        import tkinter as tk

        from ai_env_clone import __main__ as gui

        self.gui = gui
        self.tmp = tempfile.mkdtemp(prefix="origin_gui_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.zip = os.path.join(self.tmp, "pack.zip")
        core.export_backup(
            self.zip,
            [_item(self.tmp, "ide_workspace_records:a", "ws/a/workspace.json",
                   "源机器上打开过的工程文件夹绝对路径")],
            self.tmp, tool_name="codebuddy",
        )
        # 旧包对照：没有 origin_info
        self.old_zip = os.path.join(self.tmp, "old.zip")
        with zipfile.ZipFile(self.old_zip, "w") as zf:
            zf.writestr("a.json", "{}")
            zf.writestr(core.MANIFEST_NAME, json.dumps(
                {"version": core.MANIFEST_VERSION, "kind": "backup", "tool": "codebuddy"}))

        self.tk = tk.Tk()
        self.tk.withdraw()
        self.app = gui.QoderBackupApp(self.tk)

    def tearDown(self) -> None:
        try:
            self.app._closing = True
            self.tk.update_idletasks()
            self.tk.destroy()
        except Exception:  # noqa: BLE001
            pass

    def _detail_text(self, zip_path: str) -> str:
        info = core.inspect_backup(zip_path)
        browser = self.gui.BackupBrowser(self.app, self.tmp)
        try:
            browser._render_detail(info, zip_path)
            return browser.detail.get("1.0", "end")
        finally:
            try:
                browser.top.destroy()
            except Exception:  # noqa: BLE001
                pass

    def test_backup_detail_lists_carried_info(self):
        text = self._detail_text(self.zip)
        self.assertIn("携带的源设备信息", text)
        self.assertIn("打开过的工程文件夹绝对路径", text)
        self.assertIn("分享", text)

    def test_backup_detail_omits_section_for_old_pack(self):
        """旧包没有该字段：不显示空壳段落（否则「包很干净」会像工具给出的结论）。"""
        self.assertNotIn("携带的源设备信息", self._detail_text(self.old_zip))

    def test_item_row_shows_carried_info(self):
        """勾选列表那一行也要写明——「要不要把这个包发出去」是勾选时就决定的事。"""
        item = _item(self.tmp, "ide_workspace_records:b", "ws/b/workspace.json",
                     "源机器上打开过的工程文件夹绝对路径")
        self.app.items = [item]
        self.app.root_dir = self.tmp
        self.app._refresh_items()
        self.tk.update_idletasks()
        texts = []
        for row in self.app.list_frame.winfo_children():
            for col in row.winfo_children():
                for leaf in col.winfo_children():
                    try:
                        texts.append(str(leaf.cget("text")))
                    except Exception:  # noqa: BLE001
                        pass
                try:
                    texts.append(str(col.cget("text")))
                except Exception:  # noqa: BLE001
                    pass
        self.assertIn("携带源设备信息", "\n".join(texts))


class TestIdeRecordsRoundTrip(unittest.TestCase):
    """这条条目的**目的**是「还原后能把 workspaceId 还原回项目路径」——必须端到端验证。

    只断言「条目存在 / 清单登记了」是不够的：真正要证明的是
    「源机备份 → 目标机还原 → 反查得到源机的工程路径」这一整条链。
    """

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="origin_rt_")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.src_root = os.path.join(self.tmp, "src_machine")
        self.dst_root = os.path.join(self.tmp, "dst_machine")
        self.project = r"D:\project\demo" if os.name == "nt" else "/home/u/project/demo"
        self.user_rel = os.path.join("AppData", "Roaming", "CodeBuddy CN", "User") \
            if os.name == "nt" else os.path.join(".config", "CodeBuddy CN", "User")
        self.record = os.path.join(
            self.src_root, self.user_rel, "workspaceStorage", "hash-a", "workspace.json")
        os.makedirs(os.path.dirname(self.record), exist_ok=True)
        from urllib.parse import quote
        uri = "file:///" + quote(self.project.replace("\\", "/").lstrip("/"), safe="/:")
        with open(self.record, "w", encoding="utf-8") as f:
            json.dump({"folder": uri}, f)

    def test_restore_then_lookup_recovers_project_path(self):
        from ai_env_clone.adapters.codebuddy import (
            detect_ide_workspace_records, ide_workspace_record_item,
            iter_opened_workspace_paths)
        from ai_env_clone.workspace_plan import (
            codebuddy_workspace_id, codebuddy_workspace_path_index)

        item = ide_workspace_record_item(
            detect_ide_workspace_records(
                [os.path.join(self.src_root, self.user_rel)])[0])
        zip_path = os.path.join(self.tmp, "rec.zip")
        core.export_backup(zip_path, [item], self.src_root, tool_name="codebuddy")

        core.import_backup(zip_path, self.dst_root, progress=lambda _p: None,
                           make_rollback=False)

        dst_user = os.path.join(self.dst_root, self.user_rel)
        self.assertTrue(os.path.isfile(os.path.join(
            dst_user, "workspaceStorage", "hash-a", "workspace.json")),
            "还原后 workspace.json 必须落在目标机同一相对位置")

        paths = iter_opened_workspace_paths([dst_user])
        self.assertEqual(len(paths), 1)
        self.assertEqual(os.path.normcase(os.path.normpath(paths[0])),
                         os.path.normcase(os.path.normpath(self.project)))

        wid = codebuddy_workspace_id(self.project)
        index = codebuddy_workspace_path_index(paths)
        self.assertIn(wid, index)
        self.assertEqual(os.path.normcase(os.path.normpath(index[wid])),
                         os.path.normcase(os.path.normpath(self.project)))


if __name__ == "__main__":
    unittest.main()
