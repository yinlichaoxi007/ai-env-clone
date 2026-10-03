"""用户偏好持久化：上次选择的工具 + 更新设置（**读-改-写**，非整文件覆盖）。

为什么单独成模块（而不留在 ``__main__.py``）：

- ``--docs`` / ``--check-update`` 这些 CLI 分支必须生效在 ``import tkinter``
  **之前**，它们也要读「更新设置」（代理、通道）⇒ 偏好读写不能依赖 GUI 库。

★ **必须读-改-写，不能整文件覆盖**：``prefs.json`` 历史上只存 ``last_tool``，
旧实现 ``json.dump({"last_tool": name})`` 整文件覆盖。若直接复用，
「切一次工具」就会把同文件的 ``update`` 段（自动检查开关、代理…）**全部抹掉**。
:func:`save_prefs` 因此做**深度合并**。

坏 JSON / 缺字段 / 类型不对一律回退默认，不抛异常（偏好读不出来不该挡住启动）。
"""

from __future__ import annotations

import copy
import json
import os
from datetime import datetime, timedelta

from .version import is_prerelease

__all__ = [
    "PREFS_FILENAME",
    "prefs_path",
    "load_prefs",
    "save_prefs",
    "load_last_tool",
    "save_last_tool",
    "update_settings",
    "save_update_settings",
    "CHECK_INTERVALS",
    "INTERVAL_LABELS",
    "default_update_settings",
    "should_check_now",
    "record_check",
]

#: 偏好缓存文件名，存于 ``compress_estimate.cache_dir()``（用户级缓存目录）。
PREFS_FILENAME = "prefs.json"

#: 「检查频率」下拉的取值 → 中文标签。键是**持久化值**，勿随意改名。
CHECK_INTERVALS = ("startup", "daily", "3days", "weekly", "monthly")
INTERVAL_LABELS = {
    "startup": "每次启动",
    "daily": "每天一次",
    "3days": "每 3 天",
    "weekly": "每周一次",
    "monthly": "每月一次",
}

#: 各间隔对应的天数（``"startup"`` 不按时间比较，故为 ``None``）。
_INTERVAL_DAYS = {
    "daily": 1,
    "3days": 3,
    "weekly": 7,
    "monthly": 30,
}


def _cache_dir() -> str:
    """延迟导入 ``cache_dir``：保持「测试可 monkeypatch 该函数」的既有做法。"""
    from .compress_estimate import cache_dir

    return cache_dir()


def prefs_path() -> str:
    """偏好缓存文件完整路径。"""
    return os.path.join(_cache_dir(), PREFS_FILENAME)


def _default_prefs() -> dict:
    return {"last_tool": None, "update": default_update_settings()}


def default_update_settings() -> dict:
    """更新设置的默认值。

    - ``auto_check`` 默认 **关**：本机是「需代理才连得上 GitHub」的环境，
      静默检查每次启动都要白等几秒；
    - ``include_prerelease`` 默认由**当前版本是否为预发布**自动决定
      （预发布用户默认接预发布，正式版用户默认只接正式版），
      用户手动改过后才持久化其选择。
    """
    return {
        "auto_check": False,
        "interval": "daily",
        "include_prerelease": is_prerelease(),
        "last_check": "",
        "skipped_version": "",
        "proxy": "",
    }


def _read_raw() -> dict:
    """读磁盘上的原始内容；无文件 / 坏 JSON / 顶层不是对象一律当空 dict。"""
    try:
        with open(prefs_path(), "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError, TypeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def load_prefs() -> dict:
    """读整份偏好，**与默认值深度合并**（坏 JSON / 缺字段都回退默认）。"""
    return _merge(_read_raw())


def _merge(raw: dict) -> dict:
    data = _default_prefs()
    for key, value in raw.items():
        if key == "update":
            if isinstance(value, dict):
                merged = data["update"]
                for ukey, uvalue in value.items():
                    if ukey in merged:
                        merged[ukey] = uvalue
                data["update"] = merged
        elif key in data:
            data[key] = value
    return data


def _deep_merge(base: dict, patch: dict) -> dict:
    out = dict(base)
    for key, value in patch.items():
        if key == "update" and isinstance(value, dict) and isinstance(out.get(key), dict):
            sub = dict(out[key])
            sub.update(value)
            out[key] = sub
        else:
            out[key] = value
    return out


def save_prefs(patch: dict) -> bool:
    """把 ``patch`` **深度合并**写回偏好文件，返回是否成功。

    写失败静默返回 ``False``（偏好存不下不该影响主流程）。
    ★ 这正是「不能沿用整文件覆盖写」的原因：``save_prefs({"last_tool": "dsh"})``
    不会抹掉 ``update`` 段。

    ★ **只写用户真正设置过的键**：默认值由 :func:`load_prefs` 负责补齐，
    不落盘。好处有二 —— ① 文件保持最小、不会出现「升级后新增的默认项被
    旧文件里的值悄悄钉死」；② 坏 JSON 被保存一次即自动净化。
    """
    if not isinstance(patch, dict):
        return False
    merged = _deep_merge(_read_raw(), patch)
    try:
        directory = os.path.dirname(prefs_path())
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(prefs_path(), "w", encoding="utf-8") as f:
            json.dump(merged, f, ensure_ascii=False, indent=2)
        return True
    except OSError:
        return False


def load_last_tool() -> str | None:
    """读上次选择的工具标识；无缓存 / 损坏 / 该工具已注销返回 ``None``。"""
    from .adapters import list_adapters

    name = load_prefs().get("last_tool")
    if not isinstance(name, str) or name not in list_adapters():
        return None
    return name


def save_last_tool(name: str) -> bool:
    """记录当前选择的工具（**只改 ``last_tool`` 一个键**）。"""
    return save_prefs({"last_tool": name})


def update_settings() -> dict:
    """更新设置（已与默认值合并、类型归一）。"""
    data = load_prefs()["update"]
    data = copy.deepcopy(data)
    if not isinstance(data.get("auto_check"), bool):
        data["auto_check"] = False
    if data.get("interval") not in CHECK_INTERVALS:
        data["interval"] = "daily"
    if not isinstance(data.get("include_prerelease"), bool):
        data["include_prerelease"] = is_prerelease()
    for key in ("last_check", "skipped_version", "proxy"):
        if not isinstance(data.get(key), str):
            data[key] = ""
    return data


def save_update_settings(**fields) -> bool:
    """保存更新设置的若干字段（未传的字段保持原值）。"""
    return save_prefs({"update": dict(fields)})


def should_check_now(settings: dict | None = None) -> bool:
    """按「自动检查开关 + 频率 + 上次检查时间」判定此刻该不该检查。

    ``"startup"``（每次启动）不做时间比较；其余按 ``last_check`` 距今是否
    超过间隔判定。时间不可解析（缺字段 / 格式错）时**按该检查**处理。
    """
    cfg = settings if settings is not None else update_settings()
    if not cfg.get("auto_check"):
        return False
    if cfg.get("interval") == "startup":
        return True
    days = _INTERVAL_DAYS.get(cfg.get("interval") or "", 1)
    last = cfg.get("last_check") or ""
    if not last:
        return True
    try:
        when = datetime.fromisoformat(last)
    except (TypeError, ValueError):
        return True
    return datetime.now() - when >= timedelta(days=days)


def record_check() -> None:
    """记录「刚刚检查过」的时间（供频率判定）。"""
    save_update_settings(last_check=datetime.now().isoformat(timespec="seconds"))
