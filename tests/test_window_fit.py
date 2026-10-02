"""主窗口高度自适应测试：弹性区域等比缩放 + 按需竖向滚动条。

覆盖用户反馈的核心问题——**主窗口高度被调小后，底部区域（操作按钮 / 进度条 /
状态栏）会被裁掉看不见**。修复后的契约：

1. 根布局用 grid：内容区在 row 0（weight=1），进度条 row 1、状态栏 row 2
   固定不参与分配 ⇒ 任何窗口高度下底部两行都完整可见。
2. 窗口高度 > 内容自然高：把富余高度**按比例放大**各弹性区域（备份项列表区、
   数据导入说明区），界面铺满、底部不留大片空白。
3. 窗口高度 < 内容自然高：**按比例压缩**这两个弹性区域；压缩到各自下限仍
   不够时，内容保持压缩后的高度并**出现竖向滚动条** ⇒ 内容不会被裁掉。

headless 下 ``winfo_height`` 恒为 1，故通过 mock ``_avail_body_height`` 注入
「可用高度」，直接驱动布局算法。
"""
import os
import sys
import unittest

if "tkinter" not in sys.modules:
    import tkinter as tk  # noqa: F401  (ensure available for headless tests)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from unittest import mock

from ai_env_clone import __main__ as m


class TestWindowFit(unittest.TestCase):
    """整套用例共享一个窗口实例（构建完整 GUI 较慢，逐个重建会拖成几分钟）。"""

    root = None
    app = None

    @classmethod
    def setUpClass(cls):
        import tkinter as tk
        from ai_env_clone.__main__ import QoderBackupApp

        # 隔离真实文件系统：本用例只关心几何布局，不需要扫描真实数据目录
        # （既可提速，也避免测试进程去读本机用户目录下的产品数据）。
        fake_root = os.path.join(ROOT, "tests", "_fake_data_root")
        with mock.patch("ai_env_clone.__main__._load_last_tool", return_value=None), \
             mock.patch("ai_env_clone.__main__._save_last_tool", return_value=None), \
             mock.patch.object(QoderBackupApp, "_detect_root",
                               return_value=fake_root):
            cls.root = tk.Tk()
            cls.root.withdraw()
            cls.app = QoderBackupApp(cls.root)

    @classmethod
    def tearDownClass(cls):
        try:
            cls.app._cancel_after()
            cls.root.destroy()
        except Exception:
            pass

    def _fit(self, app, avail):
        """以指定可用高度跑一次布局重排。"""
        with mock.patch.object(app, "_avail_body_height", return_value=avail):
            app._fit_layout()

    def _natural_base(self, app):
        """弹性区域取自然值时的内容总高（布局算法的基准）。"""
        self._fit(app, 0)
        return app._content_height()

    # ------------------------------------------------------------------ 缩放 --
    def test_flex_grows_when_window_taller(self):
        """窗口比内容自然高更高 → 弹性区域按比例放大，且不出现滚动条。"""
        app = self.app
        base = self._natural_base(app)
        nat_list = int(app._list_inner.cget("height"))
        nat_rows = int(app.imp_text.cget("height"))
        self.assertEqual(nat_list, m._LIST_H_NATURAL)

        self._fit(app, base + 400)
        self.assertGreater(
            int(app._list_inner.cget("height")), nat_list,
            "窗口富余时备份项列表区应被放大",
        )
        self.assertGreaterEqual(
            int(app.imp_text.cget("height")), nat_rows,
            "窗口富余时数据导入说明区不应被缩小",
        )
        self.assertFalse(app._content_scrollable, "内容铺满时不应出现滚动条")

    def test_flex_shrinks_when_window_shorter(self):
        """窗口比内容自然高更矮 → 弹性区域按比例压缩。"""
        app = self.app
        base = self._natural_base(app)
        nat_list = int(app._list_inner.cget("height"))

        self._fit(app, max(1, base - 300))
        self.assertLess(
            int(app._list_inner.cget("height")), nat_list,
            "窗口变矮时备份项列表区应被压缩",
        )

    def test_flex_clamped_to_bounds(self):
        """弹性区域的缩放被自身上下限夹紧，不会压成 0 也不会无限放大。"""
        app = self.app
        base = self._natural_base(app)

        self._fit(app, base + 100000)
        self.assertLessEqual(int(app._list_inner.cget("height")), m._LIST_H_MAX)
        self.assertLessEqual(int(app.imp_text.cget("height")), m._IMP_ROWS_MAX)

        self._fit(app, 10)
        self.assertGreaterEqual(int(app._list_inner.cget("height")), m._LIST_H_MIN)
        self.assertGreaterEqual(int(app.imp_text.cget("height")), m._IMP_ROWS_MIN)

    # -------------------------------------------------------------- 滚动兜底 --
    def test_scrollbar_appears_when_too_short(self):
        """窗口矮到压缩到底也装不下 → 出现竖向滚动条（内容不再是「看不到」）。"""
        app = self.app
        self._natural_base(app)
        self._fit(app, 120)
        self.assertTrue(app._content_scrollable, "极矮窗口应启用竖向滚动条")
        self.assertEqual(
            app._content_sb.winfo_manager(), "grid",
            "滚动条应被 grid 管理器接管（可见）",
        )
        self.assertGreater(app._content_height(), 0, "内容高度应为正、不被裁成 0")

    def test_scrollbar_hidden_when_fits(self):
        """窗口足够高 → 滚动条收起，不占位、不留白条。"""
        app = self.app
        base = self._natural_base(app)
        self._fit(app, base + 200)
        self.assertFalse(app._content_scrollable)
        self.assertEqual(app._content_sb.winfo_manager(), "", "滚动条应收起")

    def test_no_avail_height_keeps_natural(self):
        """量不到可用高度（无头 / 未映射）→ 保持自然尺寸，不乱压缩也不显滚动条。"""
        app = self.app
        self._fit(app, 0)
        self.assertEqual(int(app._list_inner.cget("height")), m._LIST_H_NATURAL)
        self.assertEqual(int(app.imp_text.cget("height")), m._IMP_ROWS_NATURAL)
        self.assertFalse(app._content_scrollable)

    # ------------------------------------------------------------ 与项数无关 --
    def test_flex_independent_of_item_count(self):
        """弹性结果只取决于窗口高度，与备份项数（列表请求高度）完全无关。"""
        app = self.app
        base = self._natural_base(app)
        with mock.patch.object(app.list_frame, "winfo_reqheight",
                               return_value=99999):
            self._fit(app, base + 400)
            huge_list = int(app._list_inner.cget("height"))
            huge_content = app._content_height()
        with mock.patch.object(app.list_frame, "winfo_reqheight",
                               return_value=10):
            self._fit(app, base + 400)
            tiny_list = int(app._list_inner.cget("height"))
            tiny_content = app._content_height()
        self.assertEqual(huge_list, tiny_list, "列表区高度不应随项数变化")
        self.assertEqual(huge_content, tiny_content, "内容总高不应随项数变化")

    # ------------------------------------------------------ 底部区域恒可见 --
    def test_bottom_rows_pinned_by_grid(self):
        """进度条 / 状态栏固定在根 grid 的 row 1/2，内容区在 row 0。"""
        app = self.app
        self.assertEqual(int(app.pbar.grid_info()["row"]), 1)
        self.assertEqual(int(app.status.grid_info()["row"]), 2)
        # 内容区滚动容器：root row 0 且带 weight（吸收全部剩余高度）
        body_outer = app.content.master.master
        self.assertEqual(body_outer.grid_info()["row"], 0)
        self.assertEqual(int(self.root.rowconfigure(0)["weight"]), 1)
        # row 1/2 不参与剩余空间分配 ⇒ 高度不足时先压缩 row 0，而不是裁底部
        self.assertEqual(int(self.root.rowconfigure(1)["weight"]), 0)
        self.assertEqual(int(self.root.rowconfigure(2)["weight"]), 0)

    def test_configure_triggers_relayout(self):
        """窗口尺寸变化会触发一次节流重排；子控件自身的 Configure 不算。"""
        app = self.app
        with mock.patch.object(app, "_fit_layout") as fit:
            app._on_root_configure(mock.Mock(widget=app.status))
            fit.assert_not_called()
            app._on_root_configure(mock.Mock(widget=self.root))
        self.assertIsNotNone(app._relayout_job)
        app._cancel_after()

    # -------------------------------------------------- 重排只发生一次、不闪 --
    def test_analytic_height_matches_measured(self):
        """解析算出的内容总高必须与真实量出来的高度一致（否则滚动条会误判）。

        新算法不再「复位自然值→量→压缩」两段式，而是用
        ``非弹性高 + 弹性高`` 解析求总高，故必须与实测对齐。
        """
        app = self.app
        base = self._natural_base(app)
        for extra in (-500, -200, -60, 0, 200, 900):
            avail = max(1, base + extra)
            self._fit(app, avail)
            app._refresh_geometry()  # 量真实内容高（此前的实现里由重排内部完成）
            measured = app._content_height()
            self.assertEqual(
                app._content_scrollable, measured > avail,
                "avail=%d 时滚动条状态与实测内容高 %d 不一致" % (avail, measured),
            )

    def test_resize_height_does_not_refresh_geometry(self):
        """只改窗口高度时不得再刷新几何（刷新=一次全屏重绘，是拖动闪烁的来源）。

        前提是内容结构与宽度都没变——此时非弹性高度可直接复用缓存。
        """
        app = self.app
        base = self._natural_base(app)
        self._fit(app, base + 100)  # 暖机：建立基准缓存
        with mock.patch.object(app, "_refresh_geometry") as refresh:
            for avail in (base + 300, base + 120, base + 20, base + 500, base - 80):
                self._fit(app, max(1, avail))
            refresh.assert_not_called()

    def test_clamped_drag_does_not_rewrite_flex(self):
        """窗口高度在「弹性区已被上下限夹紧」的区间内变化 → 不再反复改控件尺寸。

        这段区间内目标尺寸恒定不变，旧实现每次都要复位到自然值再压回去，
        于是每帧都重排一次；现在应当一次都不写。
        """
        app = self.app
        base = self._natural_base(app)
        self._fit(app, base - 200)  # 暖机：进入完全夹紧区，目标 = (MIN, MIN)
        writes = []
        real = m.QoderBackupApp._apply_flex

        def spy(self_, list_h, imp_rows):
            before = self_._current_flex()
            real(self_, list_h, imp_rows)
            if self_._current_flex() != before:
                writes.append((list_h, imp_rows))

        with mock.patch.object(m.QoderBackupApp, "_apply_flex", spy):
            for i in range(40):
                self._fit(app, max(1, base - 200 - i * 2))
        self.assertLessEqual(
            len(writes), 1,
            "夹紧区间内不应反复改弹性尺寸（实测真写了 %d 次）" % len(writes),
        )

    def test_imp_metrics_model_matches_measured(self):
        """说明区高度模型 max(行数×每行+常量, 最小高) 必须与实测逐点吻合。

        说明框内并排放着竖向滚动条，它有最小请求高度：行数很少时整块高度由它
        决定，实测 2 行与 3 行完全同高。纯线性模型会在 2 行处低估约 20px，
        导致内容明明装得下却出现滚动条。
        """
        app = self.app
        app._imp_metrics_cache = None
        row_px, pad, floor = app._imp_metrics()
        self.assertGreater(row_px, 0)
        self.assertGreaterEqual(floor, pad + 2 * row_px, "最小高应不小于 2 行的线性高")
        for rows in (2, 3, 4, 5, 6, 8):
            app.imp_text.configure(height=rows)
            app._refresh_geometry()
            measured = app.imp_frame.winfo_reqheight()
            self.assertAlmostEqual(
                app._imp_block_px(rows), measured, delta=1.0,
                msg="%d 行时模型 %s 与实测 %d 不符"
                    % (rows, app._imp_block_px(rows), measured),
            )
        app.imp_text.configure(height=m._IMP_ROWS_NATURAL)
        app._refresh_geometry()

    def test_refresh_items_builds_others_block_once(self):
        """换工具/重检测重建列表时，"其他用户"区块只重建一次。

        旧实现里 ``_rebuild_others_block`` + ``_schedule_relayout`` 成对重复了一遍，
        纯属白做一遍重建，是「切换工具后要等好几秒才稳定」的成因之一。
        """
        app = self.app
        with mock.patch.object(app, "_rebuild_others_block") as rebuild:
            app._refresh_items()
            self.assertEqual(rebuild.call_count, 1)
        app._cancel_after()


class TestContentHorizontalScroll(unittest.TestCase):
    """窗口比内容更窄时，内容区出现横向滚动条兜底（内容不被裁掉）。

    与 ``TestWindowFit`` 不同，本组用例需要**真实映射的窗口**才量得到 canvas
    可视宽，故把窗口摆到屏幕外（不 withdraw —— withdraw 后宽度恒为 1）。

    两条测量纪律（都被这条用例的假失败踩过）：

    1. 请求宽必须在改完窗口尺寸之后再测。它随可用宽变化（长说明按可用宽折行），
       压窄后必然变小；拿压窄前的旧值比，会得出「内嵌窗被压窄了」的假结论。
    2. 不要拿绝对像素值当基准。Tk 的像素量随进程 DPI 感知状态变化：
       ``test_dpi_layout`` 会真实调用 ``SetProcessDpiAwareness``（整套 discover
       里它排在前面），之后同进程内所有字体量按显示器缩放（本机 125%）放大，
       请求宽从 721 涨到 892、硬宽从 625 涨到 789 ⇒ 单跑与整套跑数字不同。
       所以断言只写关系式（谁大谁小、是否等于 max(...)），并把「压到多窄」
       取到硬宽以下足够远的地方（460），使两种缩放下前提都成立。
    """

    root = None
    app = None
    initial_min_w = 0

    @classmethod
    def setUpClass(cls):
        import tkinter as tk
        from ai_env_clone.__main__ import QoderBackupApp

        fake_root = os.path.join(ROOT, "tests", "_fake_data_root")
        with mock.patch("ai_env_clone.__main__._load_last_tool", return_value=None), \
             mock.patch("ai_env_clone.__main__._save_last_tool", return_value=None), \
             mock.patch.object(QoderBackupApp, "_detect_root",
                               return_value=fake_root):
            cls.root = tk.Tk()
            cls.app = QoderBackupApp(cls.root)
        cls.initial_min_w = int(cls.root.minsize()[0])
        # 摆到屏幕外（拿到真实几何但不干扰用户），并放开最小宽度以便压窄验证
        cls.root.minsize(1, 1)
        cls.root.geometry("+4000+4000")
        cls.root.update()

    @classmethod
    def tearDownClass(cls):
        try:
            cls.app._cancel_after()
            cls.root.destroy()
        except Exception:
            pass

    def _resize(self, width):
        """把窗口压/撑到指定宽并跑一次宽度同步，返回 (可视宽, 请求宽, 内嵌宽, 横滚)。"""
        self.root.geometry("%dx820+4000+4000" % width)
        self.root.update()
        self.app._sync_content_width()
        self.root.update()
        if self.app._content_canvas.winfo_width() <= 1:
            self.skipTest("窗口未真正映射（无头环境），无法验证宽度行为")
        return self._snapshot()

    def _snapshot(self):
        """当前宽度状态：(可视宽, 内容请求宽, 内嵌窗宽, 横滚条是否启用)。

        ⚠️ 请求宽必须**在窗口尺寸确定之后**读：它是随可用宽变化的（长说明按
        可用宽折行），也是随进程 DPI 感知状态变化的（见类 docstring），拿改尺寸
        之前量的值当基准会算出假的「被裁」。
        """
        canvas = self.app._content_canvas
        return (
            canvas.winfo_width(),
            self.app.content.winfo_reqwidth(),
            int(float(canvas.itemcget("__ct__", "width") or 0)),
            bool(self.app._content_hscrollable),
        )

    def test_hscroll_appears_when_window_narrower_than_content(self):
        """窗口比内容更窄（压到内容最低硬宽以下）→ 横向滚动条兜底，右侧不被裁。

        内容请求宽 = max(不可再折的硬宽, 折行后的实际需要宽)：窗口变窄时长说明
        会折行，请求宽随之**变小**，所以「比内容窄」只有压到硬宽（实测 1.0 倍
        DPI 下约 625px）以下才成立。这里直接压到 460，并在压窄后**重新测一次**
        请求宽——旧写法在压窄前量到 721，压窄后实际请求宽已降到 625，于是拿旧值
        断言「内嵌窗 625 ≥ 721」而误报「右侧会被裁掉」。
        """
        self._resize(460)
        vis, req, inner, hscroll = self._snapshot()
        self.assertGreater(req, 0, "内容应有非零请求宽")
        self.assertGreater(req, vis, "本用例前提是「窗口比内容窄」（已压到硬宽以下）")
        self.assertTrue(hscroll, "窗口比内容窄时应启用横向滚动条")
        self.assertEqual(self.app._content_hsb.winfo_manager(), "grid",
                         "横向滚动条应被 grid 接管（可见）")
        self.assertGreaterEqual(
            inner, req,
            "内嵌内容窗口宽度不得小于内容请求宽（否则右侧会被裁掉）")
        self.assertEqual(inner, max(vis, req),
                         "内嵌窗宽应恰为 max(可视宽, 请求宽)：够就贴合可视宽、不够才超出去")

    def test_hscroll_hidden_when_wide_enough(self):
        """窗口足够宽 → 横向滚动条收起，不占位、不留白条。

        先压到硬宽（横滚开启），再撑到「硬宽 + 400」验证能收起。「够宽」由窗口
        自己量出来，不预设像素值——DPI 缩放会把这些数整体放大（见类 docstring）。
        """
        self._resize(460)
        hard_w = self.app.content.winfo_reqwidth()
        vis, req, inner, hscroll = self._resize(hard_w + 400)
        self.assertGreater(vis, req, "本用例前提是「窗口比内容宽」")
        self.assertFalse(hscroll, "窗口比内容宽时横滚条应收起")
        self.assertEqual(self.app._content_hsb.winfo_manager(), "",
                         "横向滚动条应收起")
        self.assertEqual(inner, max(vis, req),
                         "内嵌窗宽应恰为 max(可视宽, 请求宽)")

    def test_content_request_width_adapts_to_window(self):
        """长说明随可用宽折行：窗口很宽时内容请求宽不得把可视宽顶开。

        这是本轮修的另一半——把「按钮 + 长说明」拆成两行并给说明加 wraplength
        后，宽窗口下内容请求宽不再超过可视宽（修前 Qoder 诊断行 1016px / DSH
        行 1122px，均 > 可视宽 899/939 ⇒ 横滚条常驻、右侧被裁，每次切工具还要
        为此多刷一遍整屏）。用例取 1600 的宽窗口，给 DPI 缩放留足余量。
        """
        vis, req, inner, hscroll = self._resize(1600)
        self.assertLessEqual(
            req, vis,
            "窗口很宽时内容请求宽不应超过可视宽（长说明应折行而不是撑宽）")
        self.assertFalse(hscroll, "窗口很宽时横滚条不应常驻")
        self.assertEqual(inner, vis, "内容不足可视宽宽时，内嵌窗应贴合可视宽")

    def test_min_width_leaves_room_to_narrow(self):
        """初始最小宽度必须留出可收窄的余地。

        若 minsize 宽卡得比内容请求宽还大，用户根本拖不到「需要横向滚动」的
        宽度——横向滚动条就成了永远触达不到的死代码。
        """
        self.assertLessEqual(
            self.initial_min_w, 700,
            "最小宽度应留出收窄余地（超出部分由横向滚动条兜底）")

    # ------------------------------------------------ 长文案必须自己折行 --
    def test_long_hint_labels_have_wraplength(self):
        """带长说明的 label 必须设了 wraplength 并登记进 _hint_labels。

        没有 wraplength 的长说明会把整块区域撑宽（实测 DSH 行 1122px、
        Qoder 诊断行 1016px，均超过可视宽 899/939）⇒ 横向滚动条常驻且右侧被裁，
        每次切换工具还要为此多刷一遍整屏。
        """
        hints = [lbl for lbl in self.app._hint_labels if lbl.winfo_exists()]
        self.assertTrue(hints, "至少应有 DSH / Qoder 两处长说明")
        for lbl in hints:
            self.assertGreater(
                int(lbl.cget("wraplength")), 0,
                "长说明 label 必须设 wraplength，否则会撑宽内容区")

    def test_long_hint_is_on_its_own_row(self):
        """「按钮 + 长说明」不得在同一行——两者同行时整行请求宽必然超限。

        等价判据：说明 label 的 pack side 为 top（独占一行），而不是被
        side=LEFT 塞进按钮行。
        """
        diag = [
            lbl for lbl in self.app._hint_labels
            if "对比新旧两处数据根" in str(lbl.cget("text"))
        ]
        self.assertEqual(len(diag), 1, "应能定位到 Qoder 历史诊断行的长说明")
        info = diag[0].pack_info()
        self.assertEqual(
            str(info.get("side", "top")), "top",
            "长说明必须另起一行（与按钮同行会把右侧挤出可视区）")


if __name__ == "__main__":
    unittest.main()
