"""跨适配器「条目级契约」：所有已支持工具都必须满足的不变式。

这些不变式只与**条目本身**有关（key / recommended / GUI 聚合方式）。
本文件**只读取**（``detect_root`` / ``build_items`` 都是纯计算路径，不写任何文件），
不修改任何真实用户数据。

背景（2026-10-02 在 DSH 上踩过）：GUI 的备份内容区按 ``key.split(":", 1)[0]``
把条目**聚合成一行**，而该行只渲染**一个**勾选框、默认态取**组内首项**的
``recommended``（见 ``__main__._refresh_items``：``default = first.recommended and
any_exists``）。后果：

- 同前缀组的条目**只能有同一个推荐态**。若给组内某项单独设不同的
  ``recommended``，界面上会被首项吞掉、**完全看不出来**，而且「一行代表两种推荐态」
  本身自相矛盾。
- 要区别对待，**必须拆 key 前缀**（改用下划线，不要用冒号）。

这类缺陷是**纯界面层**的：功能测试全绿也照不出来（当年 Trae CN 20 个条目塌成 1 行，
就是同一类）。故用本文件把它锁死。
"""

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from ai_env_clone.adapters import get_adapter, list_adapters  # noqa: E402


def _build(adapter):
    """按适配器的签名差异构造条目（部分适配器多一个 ``current_uid`` 参数）。

    根取适配器自己探测的结果（= 用户界面看到的真实情形）；探测不到时才退回
    建议根。注意多数适配器的数据根由环境变量/主目录推导，**不**取决于入参，
    故这里与直接传临时目录相比更贴近实际、也更少偶然性。
    """
    root = adapter.detect_root() or adapter.build_default_root()
    try:
        return root, adapter.build_items(root)
    except TypeError:
        return root, adapter.build_items(root, None)


def _agg_prefix(key: str) -> str:
    """与 GUI 完全一致的聚合前缀算法（``__main__._agg_prefix``）。"""
    return key.split(":", 1)[0]


class TestItemContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.by_tool: dict[str, list] = {}
        cls.errors: dict[str, str] = {}
        for name in list_adapters():
            try:
                _root, items = _build(get_adapter(name))
            except Exception as exc:  # noqa: BLE001 - 收集后统一断言
                cls.errors[name] = repr(exc)
                items = []
            cls.by_tool[name] = items

    def test_build_items_never_raises(self) -> None:
        """任一适配器构造条目都不得抛异常（探测失败只应产出「未找到」占位项）。"""
        self.assertEqual(self.errors, {}, "以下适配器构造条目时抛错：%s" % self.errors)

    def test_every_adapter_yields_items(self) -> None:
        """每个适配器都必须产出条目，且**不得因数据根缺失而返回空**。

        条目清单是界面选项表的**骨架**：一旦某个适配器在「本机没装该工具 /
        数据根不存在」时返回空列表，界面就会整块消失，用户既看不到「未找到」、
        也无从判断是工具没装还是本工具坏了。故除占位项外不允许返回空。
        （ZCode 的条目带「必须落在公共根之下」的护栏，其真实根下同样会产出条目。）
        """
        for name, items in self.by_tool.items():
            self.assertTrue(
                items,
                "%s 没有产出任何条目——界面会失去该工具的整块选项；"
                "请确认它在数据根缺失时仍生成「未找到」占位项" % name,
            )

    def test_keys_are_unique_per_adapter(self) -> None:
        """同一适配器内 key 不得重复（重复会让勾选状态互相覆盖）。"""
        for name, items in self.by_tool.items():
            keys = [it.key for it in items]
            dup = sorted({k for k in keys if keys.count(k) > 1})
            self.assertEqual(dup, [], "%s 存在重复 key: %s" % (name, dup))

    def test_agg_prefix_is_wellformed(self) -> None:
        """聚合前缀不得为空、不得含路径分隔符。

        前缀（= key 冒号之前的部分）会直接充当 GUI 的选项标识与偏好缓存键，
        含路径分隔符或为空都会让渲染/持久化出问题。
        """
        for name, items in self.by_tool.items():
            for it in items:
                p = _agg_prefix(it.key)
                self.assertTrue(p, "%s 的条目 %s 聚合前缀为空" % (name, it.key))
                self.assertNotIn("/", p, "%s 的条目 %s 聚合前缀含 '/'" % (name, it.key))
                self.assertNotIn("\\", p, "%s 的条目 %s 聚合前缀含反斜杠" % (name, it.key))

    def test_no_prefix_group_mixes_recommended_states(self) -> None:
        """★ 核心不变式：同一聚合前缀（= 界面同一行）内的推荐态必须一致。

        界面一行只有一个勾选框、默认态取组内首项 ⇒ 组内推荐态不一致时，
        后设的那个**永远不会生效**（且用户无从察觉）。要区别对待就必须拆 key。
        """
        offenders = []
        for name, items in self.by_tool.items():
            groups: dict[str, list] = {}
            for it in items:
                groups.setdefault(_agg_prefix(it.key), []).append(it)
            for prefix, grp in groups.items():
                if len({it.recommended for it in grp}) > 1:
                    offenders.append(
                        (name, prefix,
                         [(it.key, it.recommended) for it in grp])
                    )
        self.assertEqual(
            offenders, [],
            "以下工具的同一界面行内出现了不同的默认勾选态——该行的默认态只看组内首项，"
            "其余设置不会生效。请拆 key 前缀（用下划线，勿用冒号）：\n%s"
            % "\n".join("  %s | %s -> %s" % o for o in offenders),
        )

    def test_gui_aggregation_helper_matches_expectation(self) -> None:
        """守住「聚合前缀 == key 冒号前部分」这一约定本身。

        ``__main__._agg_prefix`` 是私有静态方法，这里不导入 GUI 模块（托管
        Python 无 tkinter），改为断言本文件复刻的算法与已知样例一致——
        样例取自真实 key。
        """
        self.assertEqual(_agg_prefix("session_db:wal"), "session_db")
        self.assertEqual(_agg_prefix("memories_current:shared"), "memories_current")
        self.assertEqual(_agg_prefix("memories_others:13166325:root"), "memories_others")
        self.assertEqual(_agg_prefix("storages_workspace"), "storages_workspace")
        self.assertEqual(_agg_prefix("profiles_patch:desktop"), "profiles_patch")


if __name__ == "__main__":
    unittest.main()
