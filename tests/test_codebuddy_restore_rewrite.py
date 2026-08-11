"""CodeBuddy 跨电脑还原 UUID 重映射单元测试。

验证：在新电脑上还原 CodeBuddy 备份时，归档内相对路径里的旧登录用户 UUID
（``Data/<旧uuid>/CodeBuddyIDE/<旧uuid>``）会被重映射为本机当前用户 UUID，
使会话数据落到本机 CodeBuddy 实际读取的目录，而不是一个「死目录」——
否则界面会出现「历史会话列表看得到、点开却没内容」的症状。

用模拟场景锁定（不依赖真机）：构造一个含旧 UUID 路径的假归档，mock 本机
当前用户 UUID 为新值，断言还原后落盘到新 UUID 目录、旧 UUID 目录不被创建。
"""
import json
import os
import tempfile
import unittest
import zipfile

from ai_env_clone.adapters import codebuddy
from ai_env_clone.core import import_backup, MANIFEST_NAME


OLD_UID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
NEW_UID = "ffffffff-1111-2222-3333-444444444444"

# 归档内相对路径：模拟 Windows 下 CodeBuddy 集中会话真实布局
ARC_OLD = (
    "AppData/Local/CodeBuddyExtension/Data/%s/CodeBuddyIDE/%s/"
    "history/ws-session-1/messages/msg-0001.json"
) % (OLD_UID, OLD_UID)

# 一条与 UUID 无关的路径，验证重映射不会误伤它
ARC_OTHER = "AppData/Local/CodeBuddyExtension/Data/Public/.memories/foo.mdc"


def _make_fake_backup(path: str) -> None:
    manifest = {
        "version": 1,
        "kind": "backup",
        "tool": "codebuddy",
        "created_at": "2026-08-11T00:00:00",
        "source_root": "C:/Users/olduser",
        "items": [],
    }
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(ARC_OLD, json.dumps({"hello": "world"}))
        zf.writestr(ARC_OTHER, "rule content")
        zf.writestr(MANIFEST_NAME, json.dumps(manifest, ensure_ascii=False))


class TestCodeBuddyRestoreRewrite(unittest.TestCase):
    def _run(self, root: str):
        adapter = codebuddy.CodeBuddyAdapter()
        rewrite = adapter.restore_path_rewrite()
        self.assertIsNotNone(rewrite, "本机当前 UUID 可取到时应返回重写函数")
        zip_path = os.path.join(root, "cb_backup.zip")
        _make_fake_backup(zip_path)
        import_backup(zip_path, root, path_rewrite=rewrite, make_rollback=False)
        return root

    def test_rewrite_maps_old_uid_to_new_uid(self):
        # mock 本机当前登录 UUID 为新值，模拟跨电脑场景
        orig = codebuddy.detect_current_uid
        codebuddy.detect_current_uid = lambda: NEW_UID
        try:
            root = tempfile.mkdtemp(prefix="cb_restore_")
            self._run(root)

            new_path = os.path.join(
                root,
                "AppData/Local/CodeBuddyExtension/Data/%s/CodeBuddyIDE/%s/"
                "history/ws-session-1/messages/msg-0001.json"
                % (NEW_UID, NEW_UID),
            )
            old_path = os.path.join(
                root,
                "AppData/Local/CodeBuddyExtension/Data/%s/CodeBuddyIDE/%s/"
                "history/ws-session-1/messages/msg-0001.json"
                % (OLD_UID, OLD_UID),
            )
            self.assertTrue(
                os.path.isfile(new_path), "会话应落盘到新 UUID 目录（本机当前用户可读）"
            )
            self.assertFalse(
                os.path.exists(old_path),
                "不应在新电脑上创建旧 UUID 死目录",
            )

            # 与 UUID 无关的路径不应被误伤
            other_path = os.path.join(root, ARC_OTHER.replace("/", os.sep))
            self.assertTrue(os.path.isfile(other_path), "非 UUID 路径应保持原样还原")
        finally:
            codebuddy.detect_current_uid = orig

    def test_rewrite_is_none_when_no_current_uid(self):
        # 本机从未登录过 CodeBuddy（取不到 UUID）时不重写，至少不破坏原路径
        orig = codebuddy.detect_current_uid
        codebuddy.detect_current_uid = lambda: ""
        try:
            adapter = codebuddy.CodeBuddyAdapter()
            self.assertIsNone(
                adapter.restore_path_rewrite(),
                "本机无当前 UUID 时应返回 None（不改写）",
            )
        finally:
            codebuddy.detect_current_uid = orig


if __name__ == "__main__":
    unittest.main()
