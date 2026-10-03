"""更新通道的**只读**部分：探测最新版本 + 定位本平台资产。

本模块**不导入 tkinter**，因此 ``--check-update`` 这类 CLI 分支可以生效在
``import tkinter`` 之前（与 ``--version`` / ``--docs`` 同样的理由）。

三条必须守住的规则（都有测试钉住）：

1. **GitHub 优先，失败回落 Gitee**；每侧最多 3 次、超时 8s、退避 1s/2s。
   两侧都失败时给**可诊断**的原因（``GitHub：连接超时；Gitee：HTTP 502``），
   而不是笼统的「检查更新失败」。
2. **按版本号挑 release、按资产名精确匹配取文件**。这两件事互补、缺一不可：
   - 「按版本号筛选」只解决「**挑对哪个 release**」；
   - 源码包（``<tag>.zip`` / ``<owner>-<repo>-<tag>.zip`` / ``.tar.gz``）
     就躺在**同一个 release 的 assets 里** ⇒ 必须靠「资产名精确匹配」才剔得掉。
   Gitee 的 release 列表还是**按 id 升序**（实测把旧的 ``v0.1.0rc`` 排在前面），
   所以顺序也绝不能信，一律自己排序。
3. ``SHA256SUMS`` 兼作**资产白名单**：清单里没出现过的资产名一律不认。
"""

from __future__ import annotations

import json
import re
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from .version import FINAL, is_newer, parse_version

__all__ = [
    "UpdateInfo",
    "UpdateResult",
    "ASSET_NAMES",
    "AUTO",
    "platform_asset_name",
    "check_update",
    "handle_check_update_flag",
    "describe_result",
    "fetch_releases",
    "pick_release",
    "highest_version",
    "parse_sums",
    "build_opener",
]

#: 仓库坐标（GitHub 与 Gitee 同名同主）。
OWNER = "yinlichaoxi007"
REPO = "ai-env-clone"

#: 四个平台的资产名。**必须精确匹配** —— 见模块 docstring 规则 2。
ASSET_NAMES = {
    "windows": "AiEnvClone-windows.exe",
    "macos-arm64": "AiEnvClone-macos-arm64.zip",
    "macos-x86_64": "AiEnvClone-macos-x86_64.zip",
    "linux": "AiEnvClone-linux",
}

#: 资产清单文件名（兼作白名单与哈希来源）。
SUMS_NAME = "SHA256SUMS"

#: 源码包（GitHub/Gitee 会自动生成）——只用于测试断言「它们必须被排除」，
#: 实现上靠「白名单 + 精确匹配」自然排除，不需要显式黑名单。
SOURCE_SUFFIXES = (".zip", ".tar.gz", ".tgz", ".gz")

_TIMEOUT = 8.0
_RETRIES = 3
_USER_AGENT = "AiEnvClone-updater"


def platform_asset_name(system: str | None = None, machine: str | None = None) -> str | None:
    """本平台对应的资产名；未知平台返回 ``None``（调用方应只做检测不做下载）。"""
    import platform
    import sys

    sysname = (system if system is not None else sys.platform).lower()
    arch = (machine if machine is not None else platform.machine() or "").lower()
    if sysname.startswith("win"):
        return ASSET_NAMES["windows"]
    if sysname == "darwin":
        return ASSET_NAMES["macos-arm64"] if "arm" in arch or "aarch" in arch else ASSET_NAMES["macos-x86_64"]
    if sysname.startswith("linux"):
        return ASSET_NAMES["linux"]
    return None


@dataclass
class UpdateInfo:
    """一个可安装的更新。"""

    version: str
    tag: str
    asset_name: str
    url: str
    size: int = 0
    sha256: str = ""
    source: str = "github"          # 实际取到列表的来源：github / gitee
    prerelease: bool = False
    notes: str = ""
    checksum_verified: bool = False  # 是否拿到了该资产的期望哈希


@dataclass
class UpdateResult:
    """检查结果。``error`` 非空表示**两侧都失败**（带可诊断原因）。"""

    info: UpdateInfo | None = None
    up_to_date: bool = False
    error: str = ""
    source_errors: dict = field(default_factory=dict)   # {"github": 原因, "gitee": 原因}
    asset_missing: bool = False        # 找到了新版但**没有本平台资产**
    skipped: bool = False              # 用户已选择跳过该版本
    #: 存在更高版本、但**被更新通道过滤掉**（如正式版用户看到 rc）。
    #: ★ 必须与「已是最新」区分：前者要提示「有新版但当前通道不含」，
    #: 后者只需一句「已是最新」——混为一谈会让正式版用户永久不知道 rc 存在。
    filtered_version: str = ""


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
def describe_result(result: UpdateResult, current: str, asset_name: str | None) -> str:
    """把检查结果翻成一句给用户看的话（GUI 与 ``--check-update`` 共用）。

    刻意**给出可诊断信息**：两侧各自失败的原因都摊开写，��户才知道该换代理
    还是换网络；笼统的「检查更新失败」等于让用户自己猜。
    """
    if result.error:
        return "检查更新失败：%s" % result.error
    if result.up_to_date and result.skipped:
        return "已跳过 %s（可在「设置 → 更新设置」里重新启用）" % (
            result.filtered_version or "该版本"
        )
    if result.up_to_date and result.filtered_version:
        return ("当前版本 %s 已是所选通道的最高版本；另有 %s 可用，"
                "可在「设置 → 更新设置」勾选「包含预发布版本」"
                % (current, result.filtered_version))
    if result.up_to_date:
        return "已是最新版本（%s）" % current
    if result.asset_missing:
        if asset_name is None:
            return "有更新，但当前平台暂不提供自动更新产物（只支持 Windows / macOS / Linux）"
        return "有更新，但该版本没有 %s 资产" % asset_name
    info = result.info
    assert info is not None
    lines = ["发现新版本 v%s（来源：%s）" % (info.version, info.source)]
    if not info.checksum_verified:
        # ★ 不静默降级：拿不到哈希清单就要说出来。
        lines.append("注意：本次未做哈希校验（发布源未提供 SHA256SUMS 清单）")
    if info.prerelease:
        lines.append("这是预发布版本，可能不够稳定")
    return "\n".join(lines)


def handle_check_update_flag(argv: list[str]) -> int | None:
    """``--check-update`` 时检查更新并打印结论，返回**退出码**（未命中返回 ``None``）。

    退出码：``0``=有新版本 / ``1``=已是最新 / ``2``=检查失败。与 ``--version``、
    ``--docs`` 同样处理在 ``import tkinter`` 之前，故本模块不导入 GUI 库。
    """
    if "--check-update" not in argv:
        return None
    from .prefs import update_settings
    from .version import __version__, emit_console

    cfg = update_settings()
    result = check_update(
        current=__version__,
        include_prerelease=bool(cfg.get("include_prerelease")),
        proxy=str(cfg.get("proxy") or ""),
        skipped_version=str(cfg.get("skipped_version") or ""),
    )
    emit_console(describe_result(result, __version__, platform_asset_name()))
    if result.error:
        return 2
    if result.up_to_date:
        return 1
    return 0


def build_opener(proxy: str = ""):
    """按给定代理构造 opener；``proxy`` 为空则走环境变量（urllib 默认行为）。

    ★ **不读系统 ``ProxyEnable``**：本机实测它是关的、但 ``ProxyServer`` 已填好，
    两者不一致；只认「界面设置 > 环境变量 > 直连」这条链。
    """
    if proxy:
        handler = urllib.request.ProxyHandler({"http": proxy, "https": proxy})
    else:
        handler = urllib.request.ProxyHandler()
    ctx = ssl.create_default_context()
    return urllib.request.build_opener(handler, urllib.request.HTTPSHandler(context=ctx))

def _get_json(opener, url: str, timeout: float = _TIMEOUT) -> object:
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT,
                                               "Accept": "application/json"})
    with opener.open(req, timeout=timeout) as resp:
        raw = resp.read()
    if not raw:
        raise ValueError("响应为空")
    return json.loads(raw.decode("utf-8"))


def _get_text(opener, url: str, timeout: float = _TIMEOUT) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    with opener.open(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace")


def _describe(exc: BaseException) -> str:
    """把异常翻译成一句人能看懂的话（用于「可诊断提示」）。

    刻意给出**具体原因**（`HTTP 502` / `连接超时` / `TLS 握手失败`）而不是
    「检查更新失败」——用户看到前者才知道该换代理还是该换网络。
    """
    if isinstance(exc, urllib.error.HTTPError):
        return "HTTP %s" % exc.code
    if isinstance(exc, urllib.error.URLError):
        return _describe(exc.reason) if isinstance(exc.reason, BaseException) else str(exc.reason)
    if isinstance(exc, TimeoutError):
        return "连接超时"
    if isinstance(exc, ssl.SSLError):
        return "TLS 握手失败"
    if isinstance(exc, ValueError):
        return "响应解析失败"
    if isinstance(exc, OSError):
        # socket 层错误常带 errno（如 ConnectionRefusedError）；有文字就展示文字
        text = str(exc) or getattr(exc, "strerror", "") or ""
        return text or type(exc).__name__
    return str(exc) or type(exc).__name__


def _fetch_with_retry(opener, url: str, sleep=time.sleep) -> tuple[object, str]:
    """返回 ``(数据, 空串)`` 或 ``(None, 原因)``；最多 3 次、退避 1s/2s。"""
    last = ""
    for attempt in range(1, _RETRIES + 1):
        try:
            return _get_json(opener, url), ""
        except Exception as exc:  # noqa: BLE001 - 任何失败都要降级到另一侧
            last = _describe(exc)
            if attempt < _RETRIES:
                sleep(1.0 * attempt)
    return None, last


# --------------------------------------------------------------------------- #
# release 列表
# --------------------------------------------------------------------------- #
def _github_url() -> str:
    # ★ 不能用 /releases/latest：它**不返回预发布**，本项目 release 目前全是预发布
    # ⇒ 该接口会 404。改用列表接口自己挑。
    return "https://api.github.com/repos/%s/%s/releases?per_page=20" % (OWNER, REPO)


def _gitee_url() -> str:
    # Gitee API 无需 token。
    return "https://gitee.com/api/v5/repos/%s/%s/releases?per_page=20" % (OWNER, REPO)


def fetch_releases(opener, source: str, sleep=time.sleep) -> tuple[list, str]:
    """取某一侧的 release 列表；失败返回 ``([], 原因)``。"""
    url = _github_url() if source == "github" else _gitee_url()
    data, err = _fetch_with_retry(opener, url, sleep=sleep)
    if err:
        return [], err
    if not isinstance(data, list):
        return [], "响应不是列表"
    return [r for r in data if isinstance(r, dict)], ""


def _asset_of(entry: dict, name: str) -> dict | None:
    for a in entry.get("assets") or []:
        if isinstance(a, dict) and a.get("name") == name:
            return a
    return None


def pick_release(releases: list, include_prerelease: bool, current: str) -> dict | None:
    """按**版本号**挑出应当安装的那个 release（不信任接口返回顺序）。

    ``include_prerelease=False`` 时只认正式版；被通道过滤掉的最高版本
    由 :func:`highest_version` 单独给出（用于「有新版但通道不含」的提示）。
    """
    best = None
    best_ver = None
    for r in releases:
        entry = _release_entry(r, include_prerelease)
        if entry is None:
            continue
        parsed, candidate = entry
        if best_ver is None or parsed > best_ver:
            best_ver = parsed
            best = candidate
    if best is None or not is_newer(_tag_of(best), current):
        return None
    return best


def _tag_of(entry: dict) -> str:
    return entry.get("tag_name") or entry.get("tagName") or ""


def _release_entry(r: dict, include_prerelease: bool):
    """``(解析后的版本元组, release)``；非版本 tag / 通道不符时返回 ``None``。"""
    if not isinstance(r, dict):
        return None
    tag = _tag_of(r)
    if not isinstance(tag, str) or not tag:
        return None
    parsed = parse_version(tag)
    if parsed is None:
        return None  # 非版本 tag 直接丢弃
    if not include_prerelease and parsed[1] != FINAL:
        return None
    return (parsed, r)


def highest_version(releases: list) -> str:
    """不限通道的**最高**版本号（无有效版本 tag 时返回空串）。

    用于正式版用户：即便通道不含预发布，也该告知「有 vX.Y.Z-rc.N 可用，
    可在更新设置里勾选『包含预发布版本』」，而不是笼统报「已是最新」。
    """
    best_ver = None
    best_tag = ""
    for r in releases:
        if not isinstance(r, dict):
            continue
        tag = _tag_of(r)
        parsed = parse_version(tag) if tag else None
        if parsed is None:
            continue
        if best_ver is None or parsed > best_ver:
            best_ver = parsed
            best_tag = tag
    return best_tag


def parse_sums(text: str) -> dict:
    """解析 ``SHA256SUMS`` → ``{资产名: 哈希}``。

    兼容两种格式：``<hash>  <name>``（GNU ``sha256sum``，两个空格）
    与 ``<hash> *<name>``（二进制模式）。
    """
    out: dict = {}
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"^([0-9a-fA-F]{64})\s+\*?(.+)$", line)
        if not m:
            continue
        out[m.group(2).strip()] = m.group(1).lower()
    return out


# --------------------------------------------------------------------------- #
# 对外主入口
# --------------------------------------------------------------------------- #
#: :func:`check_update` 的 ``asset_name`` 默认值。
#: ★ 用显式哨兵而非 ``None``：``None`` 本身是「本平台无对应资产」的结果值，
#: 若同时拿它当默认值，调用方就**没法**表达「我明确知道这平台不支持」。
#: （这正是「参数默认值与合法取值撞车」的典型坑。）
AUTO = object()


def check_update(
    current: str,
    include_prerelease: bool,
    proxy: str = "",
    asset_name=AUTO,
    skipped_version: str = "",
    opener=None,
    sleep=time.sleep,
) -> UpdateResult:
    """检查是否有可用更新（**只读**，不下载）。

    顺序：GitHub → （失败或无更新）判定；Gitee 兜底。
    两侧都失败时 :attr:`UpdateResult.error` 给出 ``GitHub：…；Gitee：…``
    的**可诊断**原因，而不是笼统的「检查更新失败」。

    ``asset_name`` 缺省按本平台自动判定；本平台无对应产物时返回
    :attr:`UpdateResult.asset_missing`。
    """
    if asset_name is AUTO:
        asset_name = platform_asset_name()

    opener = opener or build_opener(proxy)
    errors: dict = {}
    chosen: dict | None = None
    source = ""
    reached = False  # 是否至少有一侧「连上了」（连上但没新版 ≠ 连接失败）
    best_any = ""    # 不限通道的最高版本（用于「通道不含」提示）

    for name in ("github", "gitee"):
        releases, err = fetch_releases(opener, name, sleep=sleep)
        if err:
            errors[name] = err
            continue
        reached = True
        source = name
        top = highest_version(releases)
        if top and is_newer(top, current):
            best_any = top
        chosen = pick_release(releases, include_prerelease, current)
        if chosen is not None:
            break

    if not reached:
        return UpdateResult(
            error="；".join(
                "%s：%s" % (label, errors.get(key, "未知"))
                for key, label in (("github", "GitHub"), ("gitee", "Gitee"))
            ),
            source_errors=errors,
        )

    if chosen is None:
        # 至少一侧连上了、但按通道规则没有更高版本。若**不限通道**倒是有新版
        # （典型：正式版用户看到 rc），必须单独告知，不能混成「已是最新」。
        return UpdateResult(
            up_to_date=True, filtered_version=best_any, source_errors=errors
        )

    tag = chosen.get("tag_name") or chosen.get("tagName") or ""
    parsed = parse_version(tag)
    if skipped_version and parsed is not None and parsed == parse_version(skipped_version):
        return UpdateResult(up_to_date=True, skipped=True, source_errors=errors)
    if asset_name is None:
        return UpdateResult(asset_missing=True, source_errors=errors)

    asset = _asset_of(chosen, asset_name)
    if asset is None:
        return UpdateResult(asset_missing=True, source_errors=errors)

    url = asset.get("browser_download_url") or asset.get("url") or ""
    info = UpdateInfo(
        version=tag.lstrip("vV") if isinstance(tag, str) else "",
        tag=tag,
        asset_name=asset_name,
        url=url,
        size=int(asset.get("size") or 0),
        prerelease=(parsed[1] != ()) if parsed else False,
        notes=str(chosen.get("body") or "")[:2000],
        source=source,
    )

    sums_asset = _asset_of(chosen, SUMS_NAME)
    if sums_asset is not None:
        sums_url = sums_asset.get("browser_download_url") or sums_asset.get("url") or ""
        if sums_url:
            try:
                sums = parse_sums(_get_text(opener, sums_url))
            except Exception:  # noqa: BLE001 - 取不到就降级为「不做哈希校验」
                sums = {}
            expected = sums.get(asset_name)
            if expected:
                info.sha256 = expected
                info.checksum_verified = True
    return UpdateResult(info=info, source_errors=errors)
