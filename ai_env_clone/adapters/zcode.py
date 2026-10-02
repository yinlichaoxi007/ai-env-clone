"""
ZCode 适配器。

本文件是 ZCode 备份逻辑的唯一事实来源：
- 目录探测（``detect_root`` / ``detect_data_roots``）
- 备份条目构造（``build_items``）
- 通过统一的 :class:`~ai_env_clone.adapters.base.BaseAdapter` 接口暴露给 GUI / CLI

ZCode 在同一台机器上可能出现**两套互不相同的目录布局**（2026-09-22 在本机
Windows 实测确认），本适配器**同时覆盖**：

1. 新版 CLI 布局：``<home>/.zcode/``
   - ``cli/db/db.sqlite``（+ ``-wal``/``-shm``）
     **核心会话库**。表：``session``（会话：id/project_id/workspace_id/slug/directory/
     title/time_created/…）、``message``（``data`` 为 JSON 文本）、``part``
     （``data`` 为 JSON 文本）、``session_entry``、``input_history``（输入历史）、
     ``todo``、``permission``、``model_usage`` / ``tool_usage`` / ``turn_usage``、
     ``local_setting``、``workflow_*``。默认勾选。
   - ``cli/plugins/``：插件与随包技能（``cache/`` 为内置市场缓存、``data/`` 为插件
     运行数据、``marketplaces/`` 为市场清单）。默认不勾。
   - ``cli/log/``：日志，**不列入备份选项**。
2. 旧版桌面（2.x）布局：``<home>/.zcode/v2/``
   - ``tasks-index.sqlite``（+ ``-wal``/``-shm``）：任务/自动化库。默认勾选。
   - ``config.json`` / ``setting.json`` / ``provider_config.json``：模型与界面设置。
     默认不勾（可从零重建）。
   - ``credentials.json``：**凭证文件**（值形如 ``enc:v1:...``）。敏感，默认不勾，
     导出时脱敏。
   - ``certs/``、``runtime/``、``crash/``、``logs/``、``telemetry-state.json``、
     ``coding-plan-cache.json``、``bot-config*.json``、``bot-state*.json``：
     运行态/缓存/日志，**不列入备份选项**。
   - ``workspace/``：工作区数据。默认不勾。

- 桌面端 Electron 运行态 ``%APPDATA%/ZCode/``（``session/`` 为 Chromium profile、
  ``*.updaterId``、``zcode-data-size-telemetry.json``）与更新器
  ``%LOCALAPPDATA%/@zcodedesktop-updater/``：**运行态，不列入备份选项**，仅在识别
  状态区标注。

### 关于「HOME 与用户主目录不一致」

ZCode 各处对「home」的解析并不统一（实测：CLI 用操作系统的用户主目录，
Electron 桌面端用 ``HOME`` 环境变量）。当两者不同（例如 Git Bash 把 ``HOME`` 指向
``C:\\Home`` 这类非用户主目录位置，而 ``USERPROFILE=C:\\Users\\<用户>``）时，数据
会分散在两个 ``.zcode`` 目录里。为保证**不遗漏**，本适配器这样处理：

- ``detect_root`` 默认返回用户主目录 ``~``（与其他适配器一致）；
- 若 ``$HOME/.zcode`` 与 ``~/.zcode`` **同时存在且不在同一处**，则把公共根上移到两者的
  最近公共祖先（例如 ``C:\\``），两个 ``.zcode`` 都以相对路径进入归档，恢复时各回原位。
  这种情形会在识别状态区明确列出两个数据根，便于用户核对。

所有条目 path 均在公共根之下，归档按相对路径落回原位，恢复干净。
"""

from __future__ import annotations

import json
import os
import sys

from ..core import BackupItem
from .base import BaseAdapter, register


def _os_home() -> str:
    """操作系统的用户主目录（``USERPROFILE`` / ``$HOME``，由 expanduser 解析）。"""
    return os.path.expanduser("~")


def _env_home() -> "str | None":
    """``HOME`` 环境变量指向的目录（若设置且与操作系统主目录不同）。"""
    raw = os.environ.get("HOME")
    if not raw:
        return None
    try:
        expanded = os.path.abspath(os.path.expanduser(raw))
    except (ValueError, OSError):
        return None
    try:
        if os.path.abspath(_os_home()) == expanded:
            return None
    except (ValueError, OSError):
        pass
    return expanded


def zcode_home_candidates() -> list[str]:
    """列出本机可能存在的 ``.zcode`` 数据根（去重，保持「操作系统主目录优先」顺序）。"""
    out: list[str] = []
    for base in (_os_home(), _env_home()):
        if not base:
            continue
        cand = os.path.join(base, ".zcode")
        if cand not in out:
            out.append(cand)
    return out


def existing_zcode_homes() -> list[str]:
    """返回确实存在的 ``.zcode`` 数据根（不存在则空列表）。"""
    return [p for p in zcode_home_candidates() if os.path.isdir(p)]


def resolve_root() -> str:
    """解析备份公共根。

    - 无 ``.zcode`` 目录：返回 ``~``（默认建议路径）。
    - 只有一个 ``.zcode`` 目录：返回其父目录（即对应的 home）。
    - 两个 ``.zcode`` 都存在（操作系统主目录与 ``HOME`` 不一致）：返回两者 home 的
      最近公共祖先，保证两个 ``.zcode`` 都能以相对路径进归档。
    """
    homes = [os.path.dirname(p) for p in existing_zcode_homes()]
    if not homes:
        return _os_home()
    if len(homes) == 1:
        return homes[0]
    try:
        common = os.path.commonpath(homes)
    except ValueError:
        # 跨盘符等无法求公共祖先：退回操作系统主目录（至少覆盖主布局）
        return _os_home()
    # commonpath 可能返回盘根（C:\），也可能返回某个共享父目录；均可用
    return common or _os_home()


def _appdata(name: str) -> str:
    """``%APPDATA%/<name>``（Windows）/ 对应平台的应用配置目录。"""
    if sys.platform.startswith("win"):
        base = os.environ.get("APPDATA", os.path.join(_os_home(), "AppData", "Roaming"))
    elif sys.platform == "darwin":
        base = os.path.join(_os_home(), "Library", "Application Support")
    else:
        base = os.path.join(_os_home(), ".config")
    return os.path.join(base, name)


def _localappdata(name: str) -> str:
    """``%LOCALAPPDATA%/<name>``（Windows）/ 对应平台的本地数据目录。"""
    if sys.platform.startswith("win"):
        base = os.environ.get("LOCALAPPDATA", os.path.join(_os_home(), "AppData", "Local"))
    elif sys.platform == "darwin":
        base = os.path.join(_os_home(), "Library", "Application Support")
    else:
        base = os.path.join(_os_home(), ".local", "share")
    return os.path.join(base, name)


def _sqlite_comp(db_path: str) -> list[tuple[str, str]]:
    """返回 ``(路径, key 后缀)`` 列表：主库 + 存在的 ``-wal``/``-shm``。"""
    out = [(db_path, "")]
    for suffix in ("-wal", "-shm"):
        p = db_path + suffix
        if os.path.exists(p):
            out.append((p, ":" + suffix.lstrip("-")))
    return out


def _under(path: str, root: str) -> bool:
    """判断 ``path`` 是否位于 ``root`` 之下（用于生成合法归档相对路径）。"""
    try:
        rel = os.path.relpath(path, root)
    except ValueError:
        return False
    return not rel.startswith("..")


def _display_rel(path: str, base: str) -> str:
    """识别状态区展示用的路径基准：**优先主目录相对**，与其它适配器口径一致。

    ZCode 的数据根按「所属盘符」探测，若直接以盘符为基准，界面会显示出
    ``Users\\<用户>\\.zcode`` 这种其它工具从不出现的写法（其余适配器均给
    ``.workbuddy`` / ``AppData\\Roaming\\...`` 这类主目录相对路径）。故统一为：

    1. 在主目录之下 -> 主目录相对（``.zcode``）；
    2. 否则若在探测基准之下 -> 基准相对（``C:\\Home\\.zcode`` -> ``Home\\.zcode``）；
    3. 都不在之下 -> 原样返回绝对路径。
    """
    home = _os_home()
    if _under(path, home):
        return os.path.relpath(path, home)
    if base and _under(path, base):
        return os.path.relpath(path, base)
    return path


def _home_tag(idx: int, homes: list[str]) -> str:
    """多 ``.zcode`` 根时的可读标签：给「操作系统主目录」之外的根标注来源。

    仅当存在两个及以上 ``.zcode`` 根（``HOME`` 与操作系统主目录不一致）时才加标签，
    且只标注非首个（``os.path.expanduser("~")`` 推导出的那个默认不加标签）。
    """
    if len(homes) <= 1 or idx == 0:
        return ""
    parent = os.path.dirname(homes[idx])
    if os.environ.get("HOME") and os.path.abspath(os.path.expanduser(os.environ["HOME"])) == os.path.abspath(parent):
        return "（HOME 环境变量指向）"
    return "（%s）" % parent


def build_items(root: str | None = None, zcode_home: str | None = None) -> list[BackupItem]:
    """构造 ZCode 备份条目清单。

    :param root: 公共根。``None`` 时由 :func:`resolve_root` 解析（会自动兼容
        ``HOME`` 与操作系统主目录不一致的情形）。
    :param zcode_home: 单个 ``.zcode`` 根；指定时只覆盖这一处（供测试或手动指定）。
    """
    if zcode_home:
        homes = [zcode_home]
        base_root = root or os.path.dirname(zcode_home)
    else:
        homes = existing_zcode_homes()
        base_root = root or resolve_root()
        if not homes:
            homes = [os.path.join(_os_home(), ".zcode")]

    items: list[BackupItem] = []

    for idx, zc in enumerate(homes):
        if not _under(zc, base_root) and os.path.abspath(zc) != os.path.abspath(
            os.path.join(base_root, ".zcode")
        ):
            # 公共根覆盖不到（例如跨盘符）：跳过，避免生成非法归档路径
            continue
        tag = _home_tag(idx, homes)

        # 1) 新版 CLI 会话库。
        cli_db = os.path.join(zc, "cli", "db", "db.sqlite")
        for path, suffix in _sqlite_comp(cli_db):
            items.append(
                BackupItem(
                    key="cli_db%s%s" % (suffix, _suffix_for(idx, homes)),
                    label="CLI 会话库（cli/db/db.sqlite）%s" % tag,
                    path=path,
                    uid=None,
                    description="ZCode CLI 会话/消息/任务库（session / message / part / session_entry / "
                                "input_history / todo / workflow_* 等表）。核心数据，默认勾选。%s" % tag,
                    recommended=True,
                )
            )

        # 2) 插件与市场缓存。
        plugins = os.path.join(zc, "cli", "plugins")
        items.append(
            BackupItem(
                key="cli_plugins%s" % _suffix_for(idx, homes),
                label="插件与市场缓存（cli/plugins/）%s" % tag,
                path=plugins,
                uid=None,
                description="ZCode 插件与随包技能、市场清单缓存（cli/plugins/）。可重新下载，默认不勾。%s" % tag,
                recommended=False,
            )
        )

        # 3) 旧版桌面（2.x）任务库。
        v2 = os.path.join(zc, "v2")
        if os.path.isdir(v2):
            for path, suffix in _sqlite_comp(os.path.join(v2, "tasks-index.sqlite")):
                items.append(
                    BackupItem(
                        key="v2_tasks%s%s" % (suffix, _suffix_for(idx, homes)),
                        label="旧版桌面任务库（v2/tasks-index.sqlite）%s" % tag,
                        path=path,
                        uid=None,
                        description="旧版 ZCode 桌面（2.x）任务/自动化库（tasks / task_groups / "
                                    "automations 等表）。默认勾选。%s" % tag,
                        recommended=True,
                    )
                )

            # 4) 旧版设置。属「设置」类 ⇒ 默认勾选（还原后立刻能开工）。
            #    其中 config.json / provider_config.json 含提供方与密钥配置，
            #    已由 export_transform_paths() 在导出时脱敏。
            for fname, desc, sens in (
                ("config.json", "模型与提供方配置（config.json，可能含 apiKey）", True),
                ("setting.json", "界面与行为设置（setting.json）", False),
                ("provider_config.json", "提供方规则（provider_config.json）", True),
            ):
                items.append(
                    BackupItem(
                        key="v2_config%s:%s" % (_suffix_for(idx, homes), fname),
                        label="旧版桌面设置（v2/）%s" % tag,
                        path=os.path.join(v2, fname),
                        uid=None,
                        description="%s。默认勾选，还原后无需重新配置%s。%s"
                        % (desc,
                           "；其中可能含 apiKey，备份时已脱敏、恢复后需手动补填" if sens else "",
                           tag),
                        recommended=True,
                        sensitive=sens,
                    )
                )

            # 5) 凭证（敏感）。
            items.append(
                BackupItem(
                    key="v2_credentials%s" % _suffix_for(idx, homes),
                    label="凭证（v2/credentials.json）%s" % tag,
                    path=os.path.join(v2, "credentials.json"),
                    uid=None,
                    description="ZCode 登录凭证与令牌（credentials.json，值形如 enc:v1:…）。"
                                "备份时已脱敏（替换为占位符），恢复后需在目标机重新登录。默认不勾。%s" % tag,
                    recommended=False,
                    sensitive=True,
                )
            )

            # 6) 证书与工作区。
            items.append(
                BackupItem(
                    key="v2_certs%s" % _suffix_for(idx, homes),
                    label="本地证书（v2/certs/）%s" % tag,
                    path=os.path.join(v2, "certs"),
                    uid=None,
                    description="本地证书与信任材料（v2/certs/）。默认不勾。%s" % tag,
                    recommended=False,
                )
            )

        ws = os.path.join(zc, "workspace")
        items.append(
            BackupItem(
                key="workspace%s" % _suffix_for(idx, homes),
                label="工作区数据（workspace/）%s" % tag,
                path=ws,
                uid=None,
                description="ZCode 工作区数据目录（workspace/）。默认不勾。%s" % tag,
                recommended=False,
            )
        )

    return items


def _suffix_for(idx: int, homes: list[str]) -> str:
    """多 ``.zcode`` 根时为 key 追加区分后缀，保证 key 唯一且聚合前缀统一。"""
    return "" if len(homes) <= 1 else "@%d" % idx


@register
class ZCodeAdapter(BaseAdapter):
    name = "zcode"
    display_name = "ZCode"

    #: ZCode 专属压缩经验系数（档位 -> 类别 -> 压缩后/源 占比）。
    #:
    #:   - db     : ``db.sqlite`` / ``tasks-index.sqlite``（SQLite，消息体为 JSON 文本，
    #:              可压到 ≈0.35）
    #:   - text   : ``config.json`` / ``setting.json`` 等小文本（≈0.15）
    #:   - struct : 结构化配置（≈0.3）
    #:   - binary : ``plugins/`` 内含二进制（≈0.99）
    #:   - other  : 杂项（≈0.3）
    COMPRESS_RATIO: dict[int, dict[str, float]] = {
        1: {  # 快速
            "text": 0.18,
            "db": 0.4,
            "struct": 0.35,
            "binary": 0.99,
            "other": 0.35,
        },
        6: {  # 正常（推荐）
            "text": 0.15,
            "db": 0.35,
            "struct": 0.3,
            "binary": 0.99,
            "other": 0.3,
        },
    }

    def detect_root(self) -> str | None:
        """探测 ZCode 各数据根的公共根。

        默认即用户主目录 ``~``；仅当 ``HOME`` 与操作系统主目录不一致、两处
        ``.zcode`` 都存在时，才把公共根上移到两者的最近公共祖先（见模块 docstring）。
        """
        return resolve_root()

    def build_default_root(self) -> str:
        """未探测到时的默认（建议）数据目录：用户主目录 ``~``。"""
        return _os_home()

    def detect_data_roots(self, root: str | None = None) -> list[dict]:
        """返回 ZCode 在本机的各数据根目录信息，供识别状态区展示。"""
        base = root or resolve_root()
        homes = existing_zcode_homes() or [os.path.join(_os_home(), ".zcode")]
        roots: list[dict] = []
        for zc in homes:
            note = ""
            db = os.path.join(zc, "cli", "db", "db.sqlite")
            if os.path.isfile(db):
                note = "（含 CLI 会话库 db.sqlite）"
            if os.path.isdir(os.path.join(zc, "v2")):
                note = (note + " 含旧版桌面 v2/").strip()
            roots.append(
                {
                    "rel": _display_rel(zc, base),
                    "exists": os.path.isdir(zc),
                    "note": note,
                }
            )
        # 桌面端与更新器的运行态目录（不列入备份，仅提示）
        for path, note in (
            (_appdata("ZCode"), "（桌面运行态，不列入备份）"),
            (_localappdata("@zcodedesktop-updater"), "（更新器，不列入备份）"),
        ):
            roots.append(
                {
                    "rel": _display_rel(path, base),
                    "exists": os.path.isdir(path),
                    "note": note,
                }
            )
        return roots

    def build_items(self, root_dir: str | None = None, current_uid: str | None = None) -> list[BackupItem]:
        """构造 ZCode 备份条目（``root_dir`` 为公共根，可传 None 自动取）。

        ``current_uid`` 为兼容性参数（ZCode 无 UID 拆分），忽略。
        """
        return build_items(root_dir)

    # ------------------------------------------------------------------ #
    # 导出脱敏：credentials.json / config.json 可能含明文令牌或 apiKey
    # ------------------------------------------------------------------ #
    def export_transform_paths(self) -> "Sequence[str] | None":
        """凭证与模型配置在导出前脱敏。"""
        return ["credentials.json", "provider_config.json", "config.json"]

    #: 字段名（含 ``oauth:bigmodel:access_token`` 这类复合键）出现这些片段即视为敏感
    SENSITIVE_HINTS = (
        "apikey", "api_key", "token", "secret", "password", "passwd",
        "accesskey", "access_key", "privatekey", "private_key",
        "credential", "auth", "cookie", "session_key",
    )

    @staticmethod
    def _looks_sensitive(key: str) -> bool:
        k = key.lower()
        return any(hint in k for hint in ZCodeAdapter.SENSITIVE_HINTS)

    def export_transform(self) -> "Callable[[str, bytes], bytes] | None":
        """把敏感字段的值替换为占位符 ``***REDACTED***``（非 JSON 原样保留）。"""
        REDACTED = "***REDACTED***"

        def _transform(rel_path: str, source: bytes) -> bytes:
            text = source.decode("utf-8", "replace")
            try:
                data = json.loads(text)
            except (ValueError, UnicodeDecodeError):
                return source
            changed = False

            def _scrub(obj):
                nonlocal changed
                if isinstance(obj, dict):
                    for k, v in obj.items():
                        if ZCodeAdapter._looks_sensitive(k) and isinstance(v, str) and v:
                            if not (v.startswith("${") and v.endswith("}")):
                                obj[k] = REDACTED
                                changed = True
                        else:
                            _scrub(v)
                elif isinstance(obj, list):
                    for item in obj:
                        _scrub(item)

            _scrub(data)
            if not changed:
                return source
            return json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")

        return _transform

    def match_structure(self, names: "Sequence[str]") -> "tuple[bool, list[str]]":
        """结构指纹：归档内出现 ``.zcode/cli/db/db.sqlite`` 或 ``.zcode/v2/tasks-index.sqlite``。"""
        for n in names or []:
            j = n.replace("\\", "/")
            if j.endswith(".zcode/cli/db/db.sqlite") or j.endswith(".zcode/v2/tasks-index.sqlite"):
                return True, []
        return False, ["未找到 .zcode/cli/db/db.sqlite"]
