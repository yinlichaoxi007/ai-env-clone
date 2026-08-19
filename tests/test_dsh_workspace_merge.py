"""DSH 跨电脑还原「全局工作区索引合并」单元测试。

问题背景（用户实测）：DSH 的工作区名并不只靠 ``sessions/<workspace_dir>/``
目录遍历得到，还依赖一个全局索引 ``~/.dsh/storages/workspace.json``。直接覆盖
写入备份里带来的 ``workspace.json`` 会抹掉目标机器原本的其他工作区，使这些
工作区的会话在界面里变成 ``ungrouped``（磁盘内容都在，只是索引里查不到本机
工作区名）。

真实文件结构（三层）：
- ``unit``：元数据（name/version），标量。
- ``global``：``{initialized, workspaceIds:[uuid...], archivedSessionIds:[...]}``
- ``tables.workspaces``：``{uuid: {path, title, sessionIds:[...], createdAt, updatedAt}}``

本测试锁定（不依赖真机）：
1. 单元：递归合并 dict / list 并集去重 / 标量保留本机（path 用本机）。
2. 集成：``import_backup`` 还原后，目标机器原有的 workspace.json 不被覆盖抹掉，
   源的 workspace 被并入，sessionIds 并集、本机 path 保留。
"""
import json
import os
import tempfile
import unittest
import zipfile

from ai_env_clone.adapters import dsh
from ai_env_clone.core import import_backup, MANIFEST_NAME

WS_KEY = os.path.join("storages", "workspace.json")


def _ws_bytes(index: dict) -> bytes:
    return json.dumps(index, ensure_ascii=False, indent=2).encode("utf-8")


def _make_real_index(workspaces: dict) -> dict:
    """按真实结构构造一个 workspace.json（含 unit/global/tables）。"""
    return {
        "unit": {"name": "workspace", "version": 2},
        "global": {
            "initialized": True,
            "workspaceIds": list(workspaces.keys()),
            "archivedSessionIds": [],
        },
        "tables": {"workspaces": workspaces},
    }


class TestMergeWorkspaceIndexBytes(unittest.TestCase):
    def test_recursive_merge_keeps_local_and_unions_source(self):
        local = _make_real_index({
            "uuid-local": {
                "path": "D:/local/proj", "title": "LocalProj",
                "sessionIds": ["s-loc1", "s-loc2"],
                "createdAt": "t1", "updatedAt": "t2",
            },
        })
        source = _make_real_index({
            "uuid-local": {
                "path": "C:/other/proj", "title": "OtherLocal",  # 跨电脑路径，应被忽略
                "sessionIds": ["s-loc2", "s-src3"],  # s-loc2 重复
                "createdAt": "t1", "updatedAt": "t9",
            },
            "uuid-src": {
                "path": "E:/src/proj", "title": "SrcProj",
                "sessionIds": ["s-src4"],
                "createdAt": "t3", "updatedAt": "t4",
            },
        })
        merged = json.loads(
            dsh._merge_workspace_index_bytes(
                WS_KEY, _ws_bytes(source), _ws_bytes(local)
            ).decode("utf-8")
        )
        ws = merged["tables"]["workspaces"]
        # 本机工作区保留、path 用本机真实路径（源的跨电脑路径被忽略）
        self.assertIn("uuid-local", ws)
        self.assertEqual(ws["uuid-local"]["path"], "D:/local/proj")
        self.assertEqual(ws["uuid-local"]["title"], "LocalProj")
        # 会话 ID 并集去重（本机在前）
        self.assertEqual(ws["uuid-local"]["sessionIds"], ["s-loc1", "s-loc2", "s-src3"])
        # 源新增工作区并入
        self.assertIn("uuid-src", ws)
        self.assertEqual(ws["uuid-src"]["sessionIds"], ["s-src4"])
        # global.workspaceIds 并集去重
        self.assertEqual(merged["global"]["workspaceIds"], ["uuid-local", "uuid-src"])

    def test_source_corrupt_falls_back_to_local(self):
        local = _make_real_index({"uuid-local": {"path": "P", "title": "T", "sessionIds": ["a"], "createdAt": "c", "updatedAt": "u"}})
        merged = json.loads(
            dsh._merge_workspace_index_bytes(
                WS_KEY, b"{not valid", _ws_bytes(local)
            ).decode("utf-8")
        )
        self.assertEqual(merged, local)

    def test_no_original_uses_source_as_is(self):
        source = _make_real_index({"uuid-src": {"path": "P", "title": "T", "sessionIds": ["a"], "createdAt": "c", "updatedAt": "u"}})
        merged = json.loads(
            dsh._merge_workspace_index_bytes(WS_KEY, _ws_bytes(source), b"").decode("utf-8")
        )
        self.assertEqual(merged, source)


class TestDshRestoreMergeIntegration(unittest.TestCase):
    # 真实归档成员名相对公共根带前缀（如 ``.dsh/storages/workspace.json``）
    ARC_PREFIX = ".dsh/"

    def _make_backup(self, root: str, source_index: dict) -> str:
        manifest = {
            "version": 1, "kind": "backup", "tool": "dsh",
            "created_at": "2026-08-18T00:00:00", "source_root": root, "items": [],
        }
        zip_path = os.path.join(root, "dsh_backup.zip")
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            # 备份只带来源工作区 uuid-src（跨电脑迁移）
            zf.writestr(self.ARC_PREFIX + WS_KEY, _ws_bytes(source_index))
            zf.writestr(MANIFEST_NAME, json.dumps(manifest, ensure_ascii=False))
        return zip_path

    def test_restore_merges_local_workspace_index(self):
        root = tempfile.mkdtemp(prefix="dsh_merge_")
        # 目标机器还原前已有工作区 uuid-local（未被备份包含）
        local_index = _make_real_index({
            "uuid-local": {
                "path": "D:/local/proj", "title": "LocalProj",
                "sessionIds": ["s-loc1"], "createdAt": "c", "updatedAt": "u",
            },
        })
        tgt_dir = os.path.join(root, self.ARC_PREFIX, "storages")
        os.makedirs(tgt_dir, exist_ok=True)
        ws_path = os.path.join(tgt_dir, "workspace.json")
        with open(ws_path, "wb") as fh:
            fh.write(_ws_bytes(local_index))

        # 备份只含从另一台电脑迁移来的 uuid-src
        source_index = _make_real_index({
            "uuid-src": {
                "path": "E:/src/proj", "title": "SrcProj",
                "sessionIds": ["s-src4"], "createdAt": "c", "updatedAt": "u",
            },
        })
        zip_path = self._make_backup(root, source_index)

        adapter = dsh.DSHAdapter()
        import_backup(
            zip_path, root, make_rollback=False,
            restore_index_merge=adapter.restore_index_merge(),
            restore_index_merge_paths=adapter.restore_index_merge_paths(),
        )

        with open(ws_path, "rb") as fh:
            result = json.loads(fh.read().decode("utf-8"))

        ws = result["tables"]["workspaces"]
        # 关键断言：目标机器原有的 uuid-local 没有被抹掉（这正是 ungrouped 的根因）
        self.assertIn("uuid-local", ws)
        self.assertEqual(ws["uuid-local"]["path"], "D:/local/proj")
        # 备份迁移来的 uuid-src 也并入
        self.assertIn("uuid-src", ws)
        self.assertEqual(ws["uuid-src"]["sessionIds"], ["s-src4"])
        # workspaceIds 并集
        self.assertEqual(result["global"]["workspaceIds"], ["uuid-local", "uuid-src"])


if __name__ == "__main__":
    unittest.main()
