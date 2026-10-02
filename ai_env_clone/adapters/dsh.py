"""
DeepSeek Harness（DSH）适配器。

本文件是 DSH 备份逻辑的唯一事实来源：
- 目录探测（``detect_dsh_root``）
- 备份条目构造（``build_items``）
- 通过统一的 :class:`~ai_env_clone.adapters.base.BaseAdapter` 接口暴露给 GUI / CLI

数据布局（本机 Windows 实测确认，对照 DSH 的 ``docs/subsystems/persistence.md``
文档约定推导 macOS / Linux 对应位置）：

DSH 数据全部位于 ``~/.dsh/`` 目录下（``$DSH_HOME`` 环境变量可覆盖）：

- 用户数据根（``~/.dsh``）：
  - ``sessions/<workspace_dir>/session-<uuid>/session.jsonl.zstd``
    会话事件日志（Zstandard 压缩的 JSONL），每会话独立文件。
    这是 DSH 的**核心会话历史**，不可从零重建，默认勾选。
  - ``storages/workspace.json``
    工作区与关联会话的索引（会话 ID → 所属工作区），single 布局（整单元一份文件）。
    与 ``sessions/`` 配套，核心数据，默认勾选；还原时走**并集合并**（不覆盖目标机已有工作区）。
  - ``storages/session_projcache/sessions/<id>.json``
    逐会话投影缓存（列表标题、统计、goal 快照），per-record 布局（一条记录一份文件）。
    ⚠️ 同级单文件 ``storages/session_projcache.json`` 是本单元**旧的 single 布局**：
    storage-json 后端把它迁成目录树时会「保持源文件不变」，故它会一直留在盘上但不再写入
    （本机实测单文件 mtime 停在 2026-09-20 23:07，与 ``settings.yaml`` 被导入同一刻）。
    **备份必须指向目录树**，指向单文件等于「备份成功了、还原出来却是空的」。
    ⚠️ 本单元是**纯缓存**（官方 README：「日志领先，缓存跟随」，缺失时消费方从会话日志
    冷重折叠即可重建）⇒ 属「可重建」，默认**不**勾选。
  - ``AGENTS.md``
    用户全局指令（AI 助手工作准则，跨项目、跨会话自动加载）。
    等同于「用户级规则」，默认勾选。
  - ``profiles/<profile>/cordis.patch.yml``
    实时配置的落点（profile 编辑器写入的配置覆盖：LLM 提供商、locale、reasoningEffort、
    agent-loop 等）。属「设置」类，**默认勾选**（还原后无需重新配置）。
    ⚠️ 本文件**只保存引用、不含明文密钥**（实测为 ``apiKeyEnv: <环境变量名>``，
    真密钥在 ``.credentials.yaml``）⇒ **不标记为敏感项**（否则会把用户带去
    本文件里找一个根本不存在的明文密钥）；仅保留导出脱敏作兜底，防手改后误带明文。
  - ``.credentials.yaml``
    ``refs`` 段 = **可移植的 API 密钥**（``cordis.patch.yml`` 的 ``apiKeyEnv`` 指向这里）；
    ``records`` 段 = 机器绑定的登录态 / 设备标识。
    属「凭证」类，默认不勾（含明文，随包分享即外泄；登录态换机本就须重新登录）。
    ⇒ 未勾选它、却勾了 ``cordis.patch.yml`` 时，备份完成提示会提醒用户**单独备份本文件**。
  - ``profiles/``（其余内容）
    ``cordis.patch.yml`` 之外的 ``cordis.yml``（实测为**纯注释模板**）、``package.json``、
    ``pnpm-workspace.yaml`` 与 ``node_modules/`` 依赖树。属「插件/扩展」类，可从零重建，
    默认不勾。它与上面 ``cordis.patch.yml`` 的条目**并存不冲突**：归档按相对路径去重
    （``core.scan_items`` 的 ``seen``），故只有勾了 ``profiles/`` 才会连带把依赖树打进包。
  - ``settings.yaml``（**已废弃，不列入备份选项**）
    新版 DSH 已移除该全局设置文件：源码 ``packages/settings/settings/src/index.ts`` 的
    ``SettingsForms.importLegacyDocument()`` 里 ``if (!existsSync(path)) return`` 是它
    **唯一**的读写点，全仓没有任何写入路径 ⇒ **干净安装不会生成**它，旧版升级上来的机器上
    也只是被一次性导入 profile 后改名为 ``settings.yaml.imported``。
    故按「当前支持备份的版本中不存在的条目不列为备份选项」的规则**移除条目**
    （判定过程见 ``docs/local/新增工具适配核查.md``）。
  - ``.anonymous-user-id``
    匿名用户标识文件，运行态，**不列入备份选项**。

备份哲学（统一标准，用户 2026-10-01 定策后更新）：默认勾选「无法从零重复创建、或缺失后
需重新逐项配置」的两类——① 会话、存储索引、用户规则；② 实时配置（``cordis.patch.yml``，
含 LLM 提供商配置）。默认不勾**重新获取成本低 / 需重新授权 / 可重建**的——凭证、配置文件
（``profiles/`` 其余内容）、会话投影缓存（``storages/session_projcache/``）；程序自身的
本地缓存、运行态标识与用户数据无关，不列入备份选项。

★ 条目取舍的总规则（用户 2026-10-02 定）：**当前支持备份的版本中不存在的条目，不列为
备份选项**；仅当 (a) 明确「旧版本迁移后仍然需要」（如迁移标记必须与数据同进同出），或
(b)「备份新版还原到旧版、为保证数据正确必须依赖」时，才列为条目且 ``recommended=False``。
注意区分「版本已移除」（可删条目）与「本机未使用该功能 / 工具未装」（**保留**条目，
按存在性探测，缺失由界面显示「未找到」，不推断数据丢失）。
"""

from __future__ import annotations

import json
import os
import sys

from ..core import ORIGIN_NOTE_CONVERSATION, BackupItem, _longpath
from ..redact import redact_config_bytes
from .base import BaseAdapter, register


def _home() -> str:
    """当前用户主目录（DSH 数据的公共根）。"""
    return os.path.expanduser("~")


def _dsh_home() -> str:
    """DSH 数据根目录（``$DSH_HOME`` 或默认 ``~/.dsh``）。"""
    env_home = os.environ.get("DSH_HOME")
    if env_home:
        return os.path.expanduser(env_home)
    return os.path.join(_home(), ".dsh")


def _dsh_sessions_root() -> str:
    """DSH 会话目录：``<dsh_home>/sessions``。"""
    return os.path.join(_dsh_home(), "sessions")


def _dsh_storages_root() -> str:
    """DSH 存储目录：``<dsh_home>/storages``。"""
    return os.path.join(_dsh_home(), "storages")


def _dsh_profiles_root() -> str:
    """DSH 配置文件目录：``<dsh_home>/profiles``。"""
    return os.path.join(_dsh_home(), "profiles")


def _list_dsh_profiles(profiles_root: str) -> list[str]:
    """列出 ``profiles/`` 下的 profile 名（排除 ``node_modules``），按名排序。

    profile 名由**启动方**决定（``dsh --profile <name>``），DSH 并不存在单一默认名
    —— 随附的 profile 就有 ``desktop`` / ``web`` / ``sdk`` / ``sdk-minimal`` /
    ``headless`` 等（见源码 ``packages/boot/app-boot/src/profile.ts`` 的
    ``resolveProfileDir``）。故只能**按磁盘枚举**，不能写死某一个名字，
    否则换个 profile 启动的用户会漏掉自己的实时配置。

    :param profiles_root: ``<dsh_home>/profiles`` 路径（可能不存在）。
    :return: 子目录名列表；目录不存在或无子目录时返回空列表。
    """
    try:
        names = os.listdir(profiles_root)
    except OSError:
        return []
    return sorted(
        n
        for n in names
        if n != "node_modules"
        and os.path.isdir(os.path.join(profiles_root, n))
    )


def _detect_workspace_session_dirs() -> list[str]:
    """
    探测会话目录下各工作区子目录，返回绝对路径列表。

    DSH 的会话按工作区组织：``sessions/<workspace_dir>/session-<uuid>/``，
    每个会话目录下含 ``session.jsonl.zstd`` 文件。

    找不到或 ``sessions`` 不存在时返回空列表。
    """
    sessions_root = _dsh_sessions_root()
    if not os.path.isdir(sessions_root):
        return []

    # 遍历工作区级别目录（如 ``--D-project-ai-env-clone--``）
    workspace_dirs: list[str] = []
    for name in os.listdir(sessions_root):
        full = os.path.join(sessions_root, name)
        if os.path.isdir(full) and not name.startswith("."):
            workspace_dirs.append(full)
    return workspace_dirs


def _safe_json_load(raw: bytes):
    """把字节安全解析为 JSON，失败返回 ``None``。"""
    if not raw:
        return None
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    return parsed


def _merge_values(base, incoming):
    """
    递归合并两个值，返回合并结果。

    规则（以「本机为基底，并入源，绝不删除本机条目」）：

    - dict：逐 key 合并——源有的 key 并入；本机已有的 key 递归合并；
      标量 key 保留**本机值**（重要：``tables.workspaces.<uuid>.path/title``
      等路径元数据必须用本机真实路径，源的跨电脑绝对路径不可用）。
    - list：并集去重，本机元素在前、源新增元素追加在后。
    - 标量：保留本机值（base）。
    """
    # 都是 dict -> 递归
    if isinstance(base, dict) and isinstance(incoming, dict):
        out = dict(base)
        for k, iv in incoming.items():
            if k not in out:
                out[k] = iv
            else:
                out[k] = _merge_values(out[k], iv)
        return out
    # 都是 list -> 并集去重（本机在前）
    if isinstance(base, list) and isinstance(incoming, list):
        seen = list(base)
        seen_set = set(base)
        for x in incoming:
            if x not in seen_set:
                seen_set.add(x)
                seen.append(x)
        return seen
    # 其他情况（含标量、或类型不一致）：保留本机值
    return base


def _merge_workspace_index_bytes(
    relpath: str, source_bytes: bytes, original_bytes: bytes
) -> bytes:
    """
    合并 DSH 的全局工作区索引 ``storages/workspace.json``（core 在覆盖前调用）。

    问题背景：DSH 客户端显示工作区名时**不只遍历** ``sessions/<workspace_dir>/``
    目录，而是先查全局索引 ``workspace.json``。直接覆盖写入备份里带来的
    ``workspace.json`` 会**抹掉目标机器原本的其他工作区**，使这些工作区的会话
    在界面里变成 ``ungrouped``（磁盘内容都在，只是索引里查不到本机工作区名）。

    真实文件结构（三层）：
    - ``unit``：元数据（``name``/``version``），标量，取本机即可。
    - ``global``：``{initialized, workspaceIds:[uuid...], archivedSessionIds:[...]}``
      —— **需合并**；``workspaceIds`` 是本机所有工作区 UUID 列表，整体覆盖会
      丢掉本机其他工作区（ungrouped 根因之一）。
    - ``tables.workspaces``：``{uuid: {path, title, sessionIds:[...], createdAt,
      updatedAt}}`` —— **核心需合并**；每个工作区 UUID 含其会话 ID 列表，整体
      覆盖同样会丢本机工作区（ungrouped 根因之二）。

    合并策略（保留本机全部工作区，并入源机器新增项，绝不删本机条目）：

    - 以「目标机器还原前已有的 ``workspace.json``」（``original_bytes``）为基底；
    - 备份里带来的 ``workspace.json``（``source_bytes``）作为**源**；
    - 递归合并：dict 逐 key 合并、list 并集去重（本机在前）、标量保留本机值；
    - 特别地：``tables.workspaces.<uuid>`` 已存在时保留本机 ``path``/``title``
      （本机真实路径），仅把源 ``sessionIds`` 并入本机列表；源新增的 UUID 直接
      采用源条目（跨电脑迁移来的工作区）。
    - 返回合并后的 JSON 字节；源损坏则回退保留本机原内容，绝不破坏本机工作区。

    :param relpath: 归档内相对路径（``.dsh/storages/workspace.json`` 等带前缀）。
    :param source_bytes: 备份包里 ``workspace.json`` 的原始字节。
    :param original_bytes: 还原前目标机器上该文件的已有字节（不存在则为 ``b""``）。
    :return: 合并后应写入的 JSON 字节。
    """
    source = _safe_json_load(source_bytes)
    original = _safe_json_load(original_bytes)

    # 源损坏 / 非预期：保留本机，不破坏本机工作区
    if not isinstance(source, dict):
        if isinstance(original, dict):
            return json.dumps(original, ensure_ascii=False, indent=2).encode("utf-8")
        return source_bytes

    # 本机无原文件：直接采用源（首次还原到空机器）
    if not isinstance(original, dict):
        return json.dumps(source, ensure_ascii=False, indent=2).encode("utf-8")

    merged = _merge_values(original, source)
    return json.dumps(merged, ensure_ascii=False, indent=2).encode("utf-8")


def _count_session_files(sessions_root: str) -> int:
    """
    统计指定工作区目录下的会话数。

    会话目录下存在**任意一个** canonical 会话日志即算一个会话：
    ``session.jsonl.zstd``（v0）或更高代际 ``session.vN.jsonl.zstd`` / 明文
    ``session.jsonl``。DSH 加载器按代际取最高者，但计数只关心是否存在——
    只看 ``session.jsonl.zstd`` 会漏掉「仅有 session.v2.jsonl.zstd」的会话
    （本机实测有 5 个），导致标签上的会话数偏少（实际归档不受影响，
    归档按目录整体 os.walk）。
    """
    from ..dsh_repair import find_generation_log

    count = 0
    if not os.path.isdir(sessions_root):
        return 0
    for name in os.listdir(sessions_root):
        full = os.path.join(sessions_root, name)
        if not os.path.isdir(full):
            continue
        if find_generation_log(full)[0] is not None:
            count += 1
    return count


# --------------------------------------------------------------------------- #
# 导出脱敏：实时配置 cordis.patch.yml（+ 旧版 settings.yaml 兜底）
#
# 背景：DSH 的实时设置落在 ``<dsh_home>/profiles/<profile>/cordis.patch.yml``
# （profile 编辑器写入），自 2026-10-02 起**默认勾选**（承接原 settings.yaml 条目的
# 「设置属还原后立刻能开工」定位；后者已随新版 DSH 一并移除，见文件头说明）。
#
# ★ 关键事实（实测）：该文件**不含明文密钥** —— provider 下是
#   ``apiKeyEnv: <环境变量名>`` 这样的**引用**，真密钥在上层 ``.credentials.yaml``。
#   ⇒ 正常情况下这段脱敏**不会改动任何一行**，它的作用是兜底：万一用户手改过、
#     把明文密钥直接写进 cordis.patch.yml，导出时也要抹掉（本工具承诺包内无明文密钥）。
#   ⇒ 脱敏必须「引用感知」：``ai_env_clone.redact.key_is_reference`` 会跳过
#     ``apiKeyEnv`` / ``keyFile`` 这类键，否则会把引用名抹成占位符、把配置改坏
#     （2026-10-01 曾实际引入该 bug 并修复）。
#
# ``settings.yaml`` 仍留在后缀表里：新版已无该文件（不生成、不读取），但**旧版机器**
# 上它可能还在（迁移前的形态），且是纯文本配置文件 —— 留着只是**零成本的安全网**，
# 不构成任何一个备份条目。
#
# 实现见 :mod:`ai_env_clone.redact`（行级脱敏，与 Reasonix config.toml 共用）。
# --------------------------------------------------------------------------- #

def build_items(
    root: str | None = None,
    dsh_home: str | None = None,
) -> list[BackupItem]:
    """
    构造 DeepSeek Harness 备份条目清单。

    所有条目 path 均在 ``root``（用户主目录 ``~``）之下，归档按相对路径落回原位。

    :param root: 公共根（用户主目录）。``None`` 时自动取 ``~``。
    :param dsh_home: DSH 数据根目录 ``~/.dsh``；``None`` 时自动探测。
    """
    home = root or _home()
    dsh = dsh_home or _dsh_home()
    sessions_root = os.path.join(dsh, "sessions")
    storages_root = os.path.join(dsh, "storages")
    profiles_root = os.path.join(dsh, "profiles")

    items: list[BackupItem] = []

    # 1) 会话历史（sessions/<workspace>/session-<uuid>/session.jsonl.zstd 文件）。
    #    核心数据，默认勾选。每个工作区按目录整体备份，GUI 聚合为一行。
    workspace_dirs = _detect_workspace_session_dirs()
    if workspace_dirs:
        total_sessions = sum(
            _count_session_files(wd) for wd in workspace_dirs
        )
        # 会话目录整体作为一个条目（core 的 os.walk 会递归扫描所有文件）
        items.append(
            BackupItem(
                key="sessions",
                label="会话历史（sessions/）",
                path=sessions_root,
                uid=None,
                description="DSH 会话事件日志（sessions/）。共 %d 个工作区、%d 个会话。核心，默认勾选。"
                % (len(workspace_dirs), total_sessions),
                recommended=True,
                carries_origin=ORIGIN_NOTE_CONVERSATION,
            )
        )
    else:
        # 无会话目录时生成占位项，保持选项结构稳定
        items.append(
            BackupItem(
                key="sessions",
                label="会话历史（sessions/）",
                path=sessions_root,
                uid=None,
                description="DSH 会话事件日志（sessions/）。未找到会话数据。",
                recommended=True,
                carries_origin=ORIGIN_NOTE_CONVERSATION,
            )
        )

    # 2) 存储数据（storages/）下的两个单元。二者性质不同（一核心索引、一可重建缓存），
    #    因此 key **刻意不共用冒号前缀**：GUI 按 key.split(":",1)[0] 聚合成一行，
    #    若都用 `storages:` 前缀就会被合成**一个勾选框**，默认态取组内首项 ⇒
    #    「缓存默认不勾」会被首项的 true 吞掉、且一行代表两种推荐态（自相矛盾）。
    #    ⚠️ 这是「界面塌陷」类坑（同 Trae 的 ui_misc_* 处理），改 key 前先想清楚。
    #
    #    (a) workspace.json —— 工作区与会话关联索引（single 布局，整单元一份文件）。
    #        与会话配套、还原后产品才能列出历史会话，属核心数据 ⇒ 默认勾选。
    #        还原侧由 restore_index_merge 做**并集合并**（不覆盖目标机已有工作区），
    #        满足「默认勾选必须能正确还原」的要求。
    ws_file = os.path.join(storages_root, "workspace.json")
    items.append(
        BackupItem(
            key="storages_workspace",
            label="存储数据（storages/）",
            path=ws_file,
            uid=None,
            description="工作区与会话关联索引（workspace.json）。核心，默认勾选。",
            recommended=True,
        )
    )
    #
    #    (b) session_projcache/ —— 会话投影缓存（per-record 布局，一条记录一份
    #        ``sessions/<id>.json``）。
    #        ⚠ 本条目曾长期指向同级单文件 ``storages/session_projcache.json``，
    #          那是该单元**旧的 single 布局**：storage-json 后端把它迁成 per-record
    #          目录树时「保持源文件不变」，故那个文件会一直留在盘上、且不再被写入
    #          （本机实测：单文件 mtime 停在 2026-09-20 23:07，与 settings.yaml 被
    #          导入的同一刻；此后新增的会话只写进目录树）。
    #          ⇒ 备份必须指向目录树，否则「备份到了、还原却是空的」而无人察觉。
    #        ⚠ 该单元是**纯缓存**：官方 README 明确「日志领先，缓存跟随」，缺缓存时
    #          消费方从会话日志冷重折叠即可重建，坏记录也只被当作不存在。
    #          ⇒ 不属于「无法从零重建」，按统一口径默认**不**勾选（与 code_index、
    #          plugins 同类）。用户若想连列表加速数据一起带走，可手动勾选。
    sp_dir = os.path.join(storages_root, "session_projcache")
    items.append(
        BackupItem(
            key="storages_session_projcache",
            label="会话投影缓存（storages/session_projcache/）",
            path=sp_dir,
            uid=None,
            description="逐会话投影缓存（列表标题、统计、goal 快照）。可从会话日志"
                        "冷重折叠重建，故默认不勾；勾选可省去目标机首次列表演算。"
                        "不含明文密钥。",
            recommended=False,
        )
    )

    # 3) 用户全局指令（AGENTS.md）。
    #    等同于「用户级规则」，默认勾选。
    agents_file = os.path.join(dsh, "AGENTS.md")
    items.append(
        BackupItem(
            key="user_agents",
            label="用户全局指令（AGENTS.md）",
            path=agents_file,
            uid=None,
            description="DSH 用户全局指令（AGENTS.md，跨项目自动加载）。核心，默认勾选。",
            recommended=True,
        )
    )

    # 4) 实时配置（profiles/<profile>/cordis.patch.yml）。
    #    ★ 2026-10-02 起**替换**原「用户设置（settings.yaml）」条目。
    #      DSH 新版已移除 $DSH_HOME/settings.yaml —— 源码
    #      packages/settings/settings/src/index.ts 的 importLegacyDocument() 里
    #      ``if (!existsSync(path)) return`` 是它**唯一**的读写点，全仓没有任何写入路径
    #      ⇒ 「干净安装不会生成、旧版升级机只是被消费一次后改名为 .imported」，
    #      即它在**当前支持的版本中不存在**（不是「本机缺失」）⇒ 按规则移除条目；
    #      也**不**适用例外——迁移早已把内容并入 profile，旧版/新版都不再读它。
    #    profile 编辑器写入的 cordis.patch.yml 才是实时设置的落点：用户手改出来的
    #    配置覆盖（LLM 提供商、locale、reasoningEffort、agent-loop 等），**不可从零重建**
    #    ⇒ 默认勾选（承接原 settings 条目的「还原后立刻能开工」定位）。
    #    ⚠️ 只存 provider 的密钥**引用**：provider 下是 ``apiKeyEnv: <环境变量名>``，
    #    真密钥在 ``.credentials.yaml`` 的 ``refs`` 段 ⇒ **不标 sensitive**（标了会让
    #    「备份后定位敏感文件」把用户带去本文件找一个根本不存在的明文密钥；引用值反而
    #    会被脱敏逻辑抹坏，见 :func:`ai_env_clone.redact.key_is_reference`）。
    #    仍保留 export_transform 作**兜底**：若用户手改过、把明文密钥直接写进本文件，
    #    导出时会被抹掉 —— 本工具承诺「备份包不含明文密钥」。
    #    再挂 companion：未勾选 .credentials.yaml 时，备份完成提示用户单独备份该文件。
    #    profile 名由启动方决定（``dsh --profile <name>``，无单一默认名）⇒ 按磁盘枚举，
    #    每个 profile 一条；key 用 ``profiles_patch:<profile>``，GUI 按前缀聚合为一行。
    for _prof_name in _list_dsh_profiles(profiles_root):
        items.append(
            BackupItem(
                key="profiles_patch:%s" % _prof_name,
                label="实时配置（cordis.patch.yml）",
                path=os.path.join(profiles_root, _prof_name, "cordis.patch.yml"),
                uid=None,
                description="DSH 当前 profile 的实时配置（LLM 提供商、locale、"
                            "reasoningEffort、agent-loop 等，profile 为 %s）。默认勾选，"
                            "还原后无需重新配置。注意：本文件只保存 provider 的密钥**引用**"
                            "（如 apiKeyEnv 指向的环境变量名），不含明文密钥；"
                            "真正的密钥在同目录上游的「凭证（.credentials.yaml）」里。"
                            % _prof_name,
                recommended=True,
                companion=(
                    "credentials",
                    "「实时配置（cordis.patch.yml）」已勾选，但它的配套文件「凭证"
                    "（.credentials.yaml）」未勾选。DSH 的模型密钥存放在后者"
                    "（cordis.patch.yml 里只有 apiKeyEnv 这样的引用名），"
                    "不随包携带则还原后模型会因取不到密钥而不可用。"
                    "如需跨机保留密钥，请单独备份该文件；不需要时在新机器重新填写即可"
                    "（该文件含明文密钥，请勿随备份包分享）。",
                ),
            )
        )

    # 5) 凭证（.credentials.yaml）。
    #    refs 段是**可移植的 API 密钥**（cordis.patch.yml 的 apiKeyEnv 就指向这里）；
    #    records 段是机器绑定的登录态 / 设备标识（换机须重新登录）。
    #    默认不勾：含明文，随包分享即外泄；登录态那半本就跨机无意义。
    #    未勾选时由上面 cordis.patch.yml 条目的 companion 在备份完成时提示「单独备份」。
    creds_file = os.path.join(dsh, ".credentials.yaml")
    items.append(
        BackupItem(
            key="credentials",
            label="凭证（.credentials.yaml）",
            path=creds_file,
            uid=None,
            description="DSH 凭证（.credentials.yaml）。其中 refs 段是可移植的 API 密钥"
                        "（cordis.patch.yml 的 apiKeyEnv 就指向这里），records 段是机器绑定的"
                        "登录态/设备标识。默认不勾：含明文，分享备份包会外泄，"
                        "且登录态换机本就须重新登录；如需把密钥带到新机器，请单独备份本文件。",
            recommended=False,
        )
    )

    # 6) 配置文件（profiles/）。`cordis.patch.yml` 之外的都属于「插件/扩展」类：
    #    `cordis.yml`（实测为**纯注释模板**）、`package.json`、`pnpm-workspace.yaml`
    #    与 `node_modules/` 依赖树，均可从零重建 ⇒ 默认不勾。
    #    注意与上面 `profiles_patch:*` 条目**并存不冲突**：归档按相对路径去重
    #    （`core.scan_items` 的 `seen`），故只有勾了这一项才会把依赖树一并打进包。
    items.append(
        BackupItem(
            key="profiles",
            label="配置文件（profiles/ 其余内容）",
            path=profiles_root,
            uid=None,
            description="DSH 配置目录（profiles/，含 cordis.yml、package.json、"
                        "pnpm-workspace.yaml、node_modules/ 依赖树）。可从零重建，默认不勾。"
                        "注意：实时配置 cordis.patch.yml 已单独成一个条目（默认勾选），"
                        "勾本项会额外带上依赖树（体积大），一般不需要。",
            recommended=False,
        )
    )

    # 注意：.anonymous-user-id 属运行态标识，与用户数据无关，按统一策略
    # **不列入备份选项**（不生成条目），此处不再添加。

    return items


@register
class DSHAdapter(BaseAdapter):
    name = "dsh"
    display_name = "DeepSeek Harness"

    #: DSH 专属压缩经验系数（档位 -> 类别 -> 压缩后/源 占比）。
    #:
    #: 备份数据构成（据此归类）：
    #:   - text   : ``AGENTS.md``、``cordis.patch.yml``、``.credentials.yaml`` 文本（高度可压，≈0.12）
    #:   - db     : 暂无本地 SQLite，按通用经验 ≈0.5
    #:   - struct : ``storages/*.json`` 结构化数据（≈0.4）
    #:   - binary : 通用已压缩/二进制（≈0.99，几乎压不动）
    #:   - other  : ``session.jsonl.zstd``（Zstandard 已压缩，DEFLATE 几乎压不动，但
    #:              ``compress_estimate.category_of`` 未收录 ``.zstd`` 扩展名、会落入 other 类）
    #:             与 ``storages/*.json`` 等杂项。折中取 ≈0.2，首次估算可能偏低，
    #:              真实备份后按扩展名自动校准（.zstd 实测 ≈0.99 在 other 合理区间内会被采纳）。
    #: 注：首次真实备份后会自动校准写入本工具校准文件，此处仅为回退兜底。
    COMPRESS_RATIO: dict[int, dict[str, float]] = {
        1: {  # 快速
            "text": 0.15,
            "db": 0.55,
            "struct": 0.45,
            "binary": 0.99,
            "other": 0.25,
        },
        6: {  # 正常（推荐）
            "text": 0.12,
            "db": 0.5,
            "struct": 0.4,
            "binary": 0.99,
            "other": 0.2,
        },
    }

    #: DSH 的 session.jsonl.zstd 已是 Zstandard 压缩格式，DEFLATE 几乎压不动，
    #: 校准数据可能不准确，首次估算有偏差属正常。
    supports_calibration: bool = True

    def detect_root(self) -> str | None:
        """探测 DSH 数据公共根（用户主目录 ``~``）。始终返回 ``~``。"""
        return _home()

    def build_default_root(self) -> str:
        """未探测到时的默认（建议）数据目录：用户主目录 ``~``。"""
        return _home()

    def detect_data_roots(self, root: str | None = None) -> list[dict]:
        """返回 DSH 在 ``root``（用户主目录）下的各数据根目录信息，供识别状态区展示。

        与其他适配器粒度一致：**同一根目录只显示一行**。DSH 各类数据
        （会话 sessions/、存储 storages/、规则 AGENTS.md 等）全部位于
        ``~/.dsh`` 这一个根目录之下，故只返回这一行，子目录不单独列出；
        根存在时备注会话数概览。
        """
        home = root or _home()
        dsh = _dsh_home()

        # 统计会话数（备注用，子目录本身不单独显示）
        workspace_dirs = _detect_workspace_session_dirs()
        total_sessions = sum(
            _count_session_files(wd) for wd in workspace_dirs
        )
        note = (
            "（含 %d 个工作区、%d 个会话）" % (len(workspace_dirs), total_sessions)
            if workspace_dirs
            else ""
        )

        return [
            {
                "rel": os.path.relpath(dsh, home),
                "exists": os.path.isdir(dsh),
                "note": note,
            }
        ]

    def build_items(self, root_dir: str | None = None, current_uid: str | None = None) -> list[BackupItem]:
        """构造 DSH 备份条目（root_dir 为公共根 ``~``，可传 None 自动取）。

        ``current_uid`` 为兼容性参数（DSH 单用户扁平结构，无 UID 拆分），忽略。
        """
        return build_items(root_dir, _dsh_home())

    def restore_index_merge_paths(self) -> "Sequence[str] | None":
        """还原时需「合并而非覆盖」的索引文件：DSH 全局工作区索引。

        该文件记录「工作区名 -> 会话 ID 列表」映射；直接覆盖会抹掉目标机器
        原本的其他工作区，使它们变为 ``ungrouped``。故还原时合并本机已有索引。
        """
        return [os.path.join("storages", "workspace.json")]

    def restore_index_merge(self) -> "Callable[[str, bytes, bytes], bytes] | None":
        """返回 DSH 工作区索引合并回调（见 :func:`_merge_workspace_index_bytes`）。"""
        return _merge_workspace_index_bytes

    # ------------------------------------------------------------------ #
    # 导出脱敏：cordis.patch.yml（+ 旧版 settings.yaml 兜底）
    # ------------------------------------------------------------------ #
    def export_transform_paths(self) -> "Sequence[str] | None":
        """需要导出脱敏的归档内相对路径后缀：``cordis.patch.yml`` 与 ``settings.yaml``。

        ``cordis.patch.yml`` 是当前版本实时配置的落点，其条目**默认勾选**，而 LLM
        提供商配置里理论上可能出现明文 apiKey / token（包常被同步到网盘或转发他人）
        ⇒ 保留脱敏作**兜底**。

        ``settings.yaml`` 是**旧版**遗留文件名（新版不生成也不读取，故已无对应条目）：
        留在这里只是零成本安全网 —— 旧版机器上它可能仍存在，纯文本配置一旦进包同样
        需要抹掉明文。

        注意（实测）：DSH 正常写的是 ``apiKeyEnv: <环境变量名>`` 这种**引用**，
        真密钥在 ``.credentials.yaml``；引用型键由
        :func:`ai_env_clone.redact.key_is_reference` 跳过，故正常文件**逐字不变**。
        """
        return ["cordis.patch.yml", "settings.yaml"]

    def export_transform(self) -> "Callable[[str, bytes], bytes] | None":
        """返回配置文件的脱敏回调（见 :mod:`ai_env_clone.redact`）。

        只改写「键名像凭证」的值，YAML 结构原样保留 —— 与 ``models.json``
        口径一致：抹掉明文而不是跳过整个文件（跳过会导致恢复后缺文件、界面报配置缺失）。
        """
        return lambda _rel, source: redact_config_bytes(source)
