# dev_tmp —— 开发期「核查 / 实测」脚本的固定落点

本目录**只放开发与测试过程中用到的临时脚本**，不放任何产品代码、测试用例或文档。

## 为什么有这个目录

以前这些脚本都以 `_tmp_xxx.py` 的形式**散在仓库根目录**，跑一次就留下一堆
`.py` / `.out` / `.log`，既容易误提交，也让根目录看不出哪些是真正的工程文件。

现在的约定：

1. **一次性脚本用完就删**，不要留在根目录、也不要丢进本目录积灰；
2. **可能再跑的脚本放本目录**，下次同类核查直接改参数复用，而不是重写一遍；
3. **输出（`.out` / `.log` / `.json` / `.txt`）一律即时清理**——结论要留就写进
   `docs/local/` 或 `.workbuddy/memory/` 的文档里，不要留原始输出文件。

## 本目录与 Git 的关系

除 `README.md` 与 `.gitignore` 外，本目录内容**默认被忽略**（见 `.gitignore`）。
原因是这些脚本普遍依赖**本机真实产品数据**（各 AI 工具的数据目录、会话库），
对协作者没有意义，且输出里可能夹带本机路径与用户数据。

## 现有脚本

| 脚本 | 用途 |
| --- | --- |
| `_tmp_audit_items.py` | 盘点全部适配器的备份条目（key / 标签 / 默认勾选 / 本机是否存在 / 体积），用于执行「本机不存在的条目不列为备份选项」这条规则 |
| `_tmp_missing.py` | 漏项探测：把每个适配器的真实数据根与 `build_items()` 的条目路径做覆盖比对 |
| `_tmp_arcsrc.py` | 验证「从备份包导入」方案：归档内路径布局（不带根占位前缀）、能否定位可导入会话 |
| `_tmp_probe_import.py` | 探测各适配器公共根、会话类条目落点与真实归档内相对路径 |
| `_tmp_db_probe.py` | 探测「整库覆盖型」数据库结构，为「按记录合并」还原方案取证（只读打开） |
| `_tmp_db_probe2.py` | 探测 Qoder / Trae / ZCode 真实库结构，并判断会话正文是否可读（只读） |
| `_tmp_dbclass.py` | 探测哪些默认勾选条目是「聚合型单文件库」（决定还原语义是否可合并） |
| `_tmp_ximport_e2e.py` | 跨工具会话导入端到端回归（4 来源 × 4 目标），只用合成样本 + 临时目录 |
| `_tmp_scan_perf.py` | 量「递归找备份包」的真实成本（有界版：深度 / 目录数 / 命中数 / 时间预算） |
| `_tmp_scan_bfs.py` | 对比「深度优先」与「逐层扫描」在深层目录下的可用性 |
| `_tmp_measure_height.py` | 实测主窗口启动高度、内容区可视高、是否需要外层滚动条 |
| `_tmp_probe_versions.py` | 采集各工具在本机的实测版本，用于核对 README 的「支持的工具」版本表 |
| `_tmp_repo_hygiene_check.py` | **重写历史 / 强推之后**核查仓库是否真的清干净：远端 ref（GitHub 与 Gitee **双侧**）、fork 与 PR 数、旧提交的网页可达性、远端 HEAD 署名 |
| `_tmp_shot.py` | **重截文档截图**（`docs/images/main_window.png`）：进程先声明 DPI 感知、选中 DSH、把数据目录换成占位路径 `C:\Users\用户名`，只截客户区（不含标题栏）。用法 `python dev_tmp/_tmp_shot.py [宽] [高]`，默认 780×980（刚好装下整张表单）。 |
| `_tmp_shot_dialogs.py` | **截「使用说明查看器」与「关于」对话框**，改这两个界面后用它肉眼验收（不看效果就改 UI 是浪费一轮）。含两个修法值得复用：① `Text.yview_moveto(分数)` 按**显示行**算，而 `see(idx)` 只做「最小必要滚动」（目标已在视口内就完全不动作）⇒ 用 `count(…, "displaylines")` 数显示行再算分数，并用 `index("@0,0")` 回读首行核对；② 滚动到位后要 `update()` 再截，否则抓到的是上一帧。 |
| `_tmp_help_width.py` | ★ **验证「使用说明」表格随窗口宽度重排**：同一份文档在 880 / 1500 / 520 px 三种宽度下各打印「实测列数 + `table`/`records` 段落数 + 总字符数」并截图。**判据是总字符数随宽度变化**（说明真的重排了，而不是渲染一次就定死），以及**极窄时两张表同时**退成分条（形态不混搭）。改 `doctext` 表格逻辑或查看器宽度测量后跑它。用法 `python dev_tmp/_tmp_help_width.py [输出目录]`。 |
| `_tmp_table_thresholds.py` | ★ **量「可用宽度 → 表格形态」的切换点**：对真文档逐列扫 20~360，打印每张表的列数 / 自然宽 / 下限阈值，以及 `table` / `records` 段数发生变化的宽度。**改阈值常量（`_TABLE_MIN_WIDTH` / `_TABLE_FIT_MIN_COL` / `_TABLE_GAP`）后必跑**——判据是「所有表的切换点相同，且只有一个」（用户实测反馈「阈值不该错开」）。 |
| `_drill_update_replace.py` | ★ **更新替换全流程真机推演**（自动更新方案第八部分的「演练」）：本机 127.0.0.1 临时 HTTP 发布源 + 假新版本 exe（`cmd.exe` 副本），走「下载 → 三道校验 → 原地替换 → **真实重启**（输出落盘为证）→ `.old` 清理」并验证杀软锁定场景的**整体回滚**与哈希不符拒收。全部写盘在临时目录，不碰真实安装。 |
| `_tmp_table_look.py` | **打印真文档第一张表在指定宽度下的实际观感**（172/157/120/88/70/69/68/67 列），用来肉眼判断「像不像表格」。改折行断点 / 列分隔符 / 列宽分配后用它验收。 |
| `probe_asar_workspace_path.py` | **在 DSH 安装包里搜界面文案 / 判定关键字**（默认搜一组「工作区路径缺失」相关词，也可传任意关键字）：用于回答「DSH 界面上那句话到底对应什么机制」，比猜可靠。`python dev_tmp/probe_asar_workspace_path.py <关键字>`。 |
| `probe_report_dialog.py` | **验证「可滚动报告弹窗」**（DSH 修复计划确认窗口用的那套）：造一段 60 行的正文，检查窗口默认尺寸被夹进工作区、正文是只读 `Text` + 滚动条、`继续/取消` 按钮常驻可见、滚到底能到 `yview≈1.0`。**注意必须用可见的父窗口**：`transient` 子窗在 `withdraw()` 的父窗下不会被映射，读到的几何是未映射态（1x1），那是探测假象而不是缺陷。改 `_show_plain_report_dialog` 后跑它。 |
| `verify_descriptor_repair.py` | **子代理描述符版本修复的官方链复算**：把真实的 v0 子代理会话（`subagent/descriptor.version = 2`，TeaVision 那 6 个「加载错误」会话之一）复制到 `dev_tmp/tee_check/`，分别导出「修复前 / 修复后」的产物交给官方校验链——**判据是「修复前 FAIL（`uses unsupported descriptor version 2`）、修复后 OK」**。改 `_fix_descriptor_version` 或描述符语义检查后跑它。 |
| `verify_subagent_import.py` | ★ **子代理会话导入端到端回归**（真实数据、写到 `dev_tmp/verify_subagent/` 的假 DSH 主目录）：从本机 ZCode 备份包里取「1 个父会话 + 它的子代理」，跑 `plan_dsh_import` → `migrate_session`，断言裸 uuid 目录、header 的 `parentSession`/`origin`/`delegationDepth`、seq 0 的 `subagent/descriptor`、父会话 `subagent/catalog` 与子会话 `createdAt` 一致、子代理**未**登记 `workspace.json`。改 `session_migration` 的 DSH 写出逻辑后跑它。 |
| `verify_relink_apply.py` | ★ **子代理关系修复端到端回归**（真实数据、只在 `dev_tmp/relink_home/` 的**副本**上写盘，真实 `~/.dsh` 只读）：把真实主目录里 34 条旧导入会话复制成一份最小主目录 → `plan_dsh_repair(relink_source=备份包)` → `apply_dsh_repair`，断言 23 条被改造成裸 uuid 原生子代理会话、旧目录移入 `sessions/.removed/`、索引里不再有旧子代理 id（父会话仍在）、父会话已发布的 v4 被改名移走；末尾导出 `artifacts.json` 交给官方校验链复算。改 `subagent_relink` 后跑它。**来源自动查找**那条路可以直接用 CLI 验：`python -m ai_env_clone.dsh_repair plan`（不给 `--relink-source`，应当自己找到 ZCode 备份包并列出 23 条改造）。 |

### DSH 会话格式：用产品自带校验链复算

改 `_dsh_v3_text` / `write_dsh` 的**写出格式**（尤其子代理结构）后，光看字段「像不像」不够——
DSH 加载会话时会跑官方迁移链 + 当前代际校验，任一关系不满足就**静默丢弃**整份日志。
下面三步可直接用产品自带的 `session-format-catalog` 复算（需 Node，路径见 `_dsh-runtimes`）：

| 脚本 | 用途 |
| --- | --- |
| `asar_extract.py` | 从 `…/DeepSeek Harness/resources/app.asar` 解出指定的包（`--only 'node_modules/@deepseek-ai/'`），供 Node 侧 import；解出的几十 MB 用完即删 |
| `dump_artifacts_for_validation.py` | 把 `verify_subagent_import.py` 写出的会话日志导成 `artifacts.json`（header + 事件行 + 子会话证据） |
| `validate_artifacts_official.mjs` | 用 `createSessionFormatCatalogWithChildren` + `recovery: 'strict', validation: 'current'` 复算每份日志，打印 OK/FAIL |

```bash
python dev_tmp/asar_extract.py "<安装目录>/resources/app.asar" dev_tmp/asar_dsh --only "node_modules/@deepseek-ai/"
python dev_tmp/verify_subagent_import.py     # 或 verify_relink_apply.py（会覆盖同一份 artifacts.json）
python dev_tmp/dump_artifacts_for_validation.py
node dev_tmp/validate_artifacts_official.mjs dev_tmp/asar_dsh/dsh dev_tmp/asar_dsh/dsh/artifacts.json
```

实测结论（2026-10-05）：

- 本工具写出的父会话 + 4 个子代理会话全部通过；把父会话 `subagent/catalog` 的
  `childCreatedAt` 改成与子会话 `createdAt` 不一致时，校验链会以
  `conflicts with its parent catalog` 拒绝——即**这条一致性是被官方校验的**；
- **父会话日志不动也能有 catalog**：真实父会话 v3 日志（50 条事件）+ 2 条子会话证据
  → 迁移结果 52 条事件、目录项 2，即 DSH 自己按「子会话证据完整 ⇒ 追加 version-0
  目录事实」补上了 `subagent/catalog`（子代理关系修复正是靠这条）；
- 子代理关系修复的 23 条产物（4 个父会话按各自子会话证据补齐 + 23 个子会话）全部通过。

### 非脚本文件

（暂无。第二批开工用的 `_batch2_wip.patch` 已随第二批落地删除——stash 那类东西会额外生成提交对象，署的是**创建 stash 那一刻**的 `user.name`，留着会污染「仓库里没有任何真实姓名」这条判据。）

## 运行方式

脚本一律用**系统 Python** 跑（受管 Python 没装 tkinter，涉及剪贴板 / GUI 的会失败）：

```bash
E:/Programs/Python/Python313/python.exe dev_tmp/_tmp_xxx.py
```

涉及 DSH（zstd 压缩后端）的验证也必须用系统 Python。
