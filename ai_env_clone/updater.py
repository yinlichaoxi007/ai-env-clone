"""更新通道：探测最新版本 + 定位本平台资产 + **下载 / 校验 / 落地**。

本模块**不导入 tkinter**，因此 ``--check-update`` / ``--update`` /
``--resume-update`` 这类 CLI 分支可以生效在 ``import tkinter`` 之前
（与 ``--version`` / ``--docs`` 同样的理由）。

四条必须守住的规则（都有测试钉住）：

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
4. **校验失败绝不落地**：下载与替换之间必须过「体积 + SHA256 + 结构魔数」三道校验
   （``SHA256SUMS`` 缺失时降级为魔数 + 结构，但要在界面上明示，不静默）；
   替换失败必须把 ``.old`` 改回原名整体回滚（见 :func:`stage_and_replace`）。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from .version import FINAL, PROJECT_OWNER, PROJECT_REPO, is_newer, parse_version

__all__ = [
    "UpdateInfo",
    "UpdateResult",
    "ASSET_NAMES",
    "AUTO",
    "UpdaterError",
    "platform_asset_name",
    "check_update",
    "handle_check_update_flag",
    "handle_update_flags",
    "describe_result",
    "fetch_releases",
    "pick_release",
    "highest_version",
    "parse_sums",
    "build_opener",
    "download_update",
    "verify_file",
    "stage_and_replace",
    "cleanup_pending_old",
    "ensure_dir_writable",
    "update_download_dir",
    "is_frozen",
    "PENDING_OLD_FILENAME",
]

#: 仓库坐标（GitHub 与 Gitee 同名同主）。★ **定义在 ``version.py``**，
#: 与「关于」对话框里的项目链接同源——两处各写一遍 owner/repo 迟早漂移。
OWNER = PROJECT_OWNER
REPO = PROJECT_REPO

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

    ★ **不要自己造 ``SSLContext`` 递给 urllib**（曾写成
    ``build_opener(handler, HTTPSHandler(context=ssl.create_default_context()))``）。
    实测 Gitee 侧据此返回 **403**：同一 URL、同样请求头，交替各 3 次 ——
    交给 urllib 默认 handler 的 3/3 得 200，自带 context 的 3/3 得 403
    （GitHub 两种写法都 200，故只有 Gitee 判）。

    ⚠️ 别把结论记成「不能有 context」：urllib 3.13 自己构造的默认
    ``HTTPSHandler`` 也是**带** context 的，且它与手搓 ``create_default_context()``
    在 ``verify_mode`` / ``options`` / 加密套件 / ALPN / 证书库上**逐项完全相同**，
    却一个 200 一个 403 ⇒ 起作用的是「context 由谁、何时构造」，不是它的属性。
    因此这里只构造 ProxyHandler，HTTPSHandler 一律留给 ``build_opener`` 默认添加
    （证书校验照旧是开的）。契约由 ``tests/test_updater.py``
    ``TestOpenerUsesUrllibDefaultSSLContext`` 钉住。
    """
    if proxy:
        handler = urllib.request.ProxyHandler({"http": proxy, "https": proxy})
    else:
        handler = urllib.request.ProxyHandler()
    return urllib.request.build_opener(handler)


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
        prerelease=(parsed[1] != FINAL) if parsed else False,
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


# --------------------------------------------------------------------------- #
# 下载与校验（方案第五部分：体积 / SHA256 / 结构魔数三道校验，缺一不可）
# --------------------------------------------------------------------------- #
class UpdaterError(Exception):
    """更新流程的**可诊断**失败；信息会原样展示给用户，不吞异常。"""


class UpdateCancelled(UpdaterError):
    """用户主动取消（调用方据此显示中性提示，不当成失败）。"""


#: 各类资产的**结构魔数**（第三道校验）：Windows PE、macOS zip、Linux ELF。
#: Windows/mac/linux 的资产分别是裸 exe / zip / 裸二进制（见 :data:`ASSET_NAMES`）。
_MAGIC_RULES = (
    ("AiEnvClone-windows.exe", b"MZ"),
    ("AiEnvClone-macos", b"PK"),
    ("AiEnvClone-linux", b"\x7fELF"),
)

#: 下载暂存目录名（位于 ``compress_estimate.cache_dir()`` 之下）。
UPDATE_DIRNAME = "update"

#: 「旧 exe 待清理」标记文件名（成功启动一次后由 :func:`cleanup_pending_old` 清掉）。
PENDING_OLD_FILENAME = "pending_old.json"

_CHUNK = 1 << 16


def is_frozen() -> bool:
    """是否运行在 PyInstaller 打包产物里（源码模式的 ``sys.executable`` 是 python.exe，
    原地替换那一套对它**不适用**，调用方必须先判这个）。"""
    return bool(getattr(sys, "frozen", False))


def update_download_dir() -> str:
    """下载暂存目录：``compress_estimate.cache_dir()/update/``（方案 5.2）。"""
    from .compress_estimate import cache_dir

    return os.path.join(cache_dir(), UPDATE_DIRNAME)


def _expected_magic(asset_name: str) -> bytes | None:
    lowered = (asset_name or "").lower()
    for prefix, magic in _MAGIC_RULES:
        if lowered.startswith(prefix.lower()):
            return magic
    return None


def _sha256_of_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_file(path: str, info: UpdateInfo) -> "tuple[bool, str]":
    """对已落盘的文件跑三道校验，返回 ``(是否通过, 失败原因)``。

    1. **体积**：release 资产声明了 ``size`` 就必须相等（Gitee 不返回 size 时跳过）；
    2. **SHA256**：拿到期望哈希就必须一致——不一致**立即判失败**；
    3. **结构魔数**：Windows ``MZ`` / macOS ``PK`` / Linux ``\\x7fELF``，
       识别不了的资产名跳过这条（不误伤未来新增的产物形态）。
    """
    try:
        actual_size = os.path.getsize(path)
    except OSError as exc:
        return False, "读取下载文件失败：%s" % exc
    if info.size and actual_size != info.size:
        return False, "体积不符：预期 %d 字节，实际 %d 字节" % (info.size, actual_size)
    if info.sha256:
        actual = _sha256_of_file(path)
        if actual != info.sha256.lower():
            return False, "SHA256 不符：预期 %s…，实际 %s…" % (
                info.sha256[:12], actual[:12])
    magic = _expected_magic(info.asset_name)
    if magic is not None:
        try:
            with open(path, "rb") as f:
                head = f.read(len(magic))
        except OSError as exc:
            return False, "读取下载文件失败：%s" % exc
        if head[:len(magic)] != magic:
            return False, "文件结构不对（缺少 %r 魔数），可能不是本平台产物" % magic
    return True, ""


def _download_one(opener, url: str, dest_path: str, info: UpdateInfo,
                  progress, cancel, deadline_head: bytes) -> None:
    """单次流式下载（边写边算 SHA256）。任何失败抛异常；取消抛 :class:`UpdaterError`。"""
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    digest = hashlib.sha256()
    count = 0
    tmp_path = dest_path + ".part"
    with opener.open(req, timeout=_TIMEOUT * 4) as resp, \
            open(tmp_path, "wb") as out:
        while True:
            if cancel is not None and cancel():
                raise UpdateCancelled("已取消下载")
            chunk = resp.read(_CHUNK)
            if not chunk:
                break
            out.write(chunk)
            digest.update(chunk)
            count += len(chunk)
            if info.size and count > info.size:
                raise UpdaterError("下载体积超过声明值（%d 字节），已中止" % info.size)
            if progress is not None:
                if info.size:
                    progress(min(99, count * 100 // info.size))
                else:
                    progress(-1)          # 无 size 时只表意「在进行」
    if info.size and count != info.size:
        raise UpdaterError("下载不完整：预期 %d 字节，实际 %d 字节" % (info.size, count))
    with open(tmp_path, "rb") as f:
        head = f.read(len(deadline_head)) if deadline_head else b""
    if deadline_head and head != deadline_head:
        raise UpdaterError("下载内容不是本平台产物（魔数不符）")
    os.replace(tmp_path, dest_path)


def download_update(
    info: UpdateInfo,
    proxy: str = "",
    progress=None,
    cancel=None,
    opener=None,
    sleep=time.sleep,
    dest_dir: str | None = None,
) -> str:
    """下载 :class:`UpdateInfo` 指向的资产并过三道校验，返回落盘路径。

    - 流式写盘、边写边算哈希（方案 5.2，不整块读内存）；
    - ``progress(0~100)``（拿不到总大小时传 ``-1``）与 ``cancel()`` 由**调用方线程**
      提供，本函数可能在后台线程里跑（GUI 侧自行用 ``after()`` 回主线程）；
    - 同一 URL 最多重试 3 次、退避 1s/2s；仍失败则**换另一侧发布源**重下同一资产
      （方案 3.3 ④：下载降级与版本列表同一条链路）；
    - 下载期间写 ``<名>.part``，完整通过校验后才原子改名——**校验失败绝不落地**
      （半截文件 / 被替换的内容都不会以正式名存在）。
    """
    if not info.url:
        raise UpdaterError("该版本没有可用的下载地址")
    dest_dir = dest_dir or update_download_dir()
    try:
        os.makedirs(dest_dir, exist_ok=True)
    except OSError as exc:
        raise UpdaterError("无法创建下载目录 %s：%s" % (dest_dir, exc)) from exc
    dest_path = os.path.join(dest_dir, info.asset_name)
    magic = _expected_magic(info.asset_name) or b""

    opener = opener or build_opener(proxy)
    magic = _expected_magic(info.asset_name) or b""
    last_err = ""
    pending = [info.url]           # ★ 备用源**惰性**求值：主 URL 耗尽重试后才去取
    while pending:
        url = pending.pop(0)
        for attempt in range(1, _RETRIES + 1):
            try:
                _download_one(opener, url, dest_path, info, progress, cancel, magic)
            except UpdaterError:
                raise                 # 取消 / 体积越界不做无谓重试
            except Exception as exc:  # noqa: BLE001 - 网络/HTTP 错误逐级降级
                last_err = _describe(exc)
                _remove_quiet(dest_path + ".part")
                if attempt < _RETRIES:
                    sleep(1.0 * attempt)
                continue
            ok, reason = verify_file(dest_path, info)
            if not ok:
                _remove_quiet(dest_path)
                raise UpdaterError("校验失败，已丢弃下载文件：%s" % reason)
            if progress is not None:
                progress(100)
            return dest_path
        if not pending:
            pending = _fallback_urls(info, opener)   # 同一资产换另一侧重下
    raise UpdaterError("下载失败：%s" % (last_err or "未知原因"))


def _fallback_urls(info: UpdateInfo, opener) -> list:
    """同一资产在**另一侧发布源**的下载地址（取不到就空着，不影响主路径）。"""
    other = "gitee" if info.source == "github" else "github"
    releases, err = fetch_releases(opener, other)
    if err:
        return []
    for r in releases:
        if _tag_of(r) != info.tag:
            continue
        asset = _asset_of(r, info.asset_name)
        if asset is not None:
            url = asset.get("browser_download_url") or asset.get("url") or ""
            if url and url != info.url:
                return [url]
    return []


def _remove_quiet(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# 应用更新（方案 6.1：Windows 原地替换 + 自动重启；失败整体回滚）
# --------------------------------------------------------------------------- #
def ensure_dir_writable(directory: str) -> bool:
    """可写性预检：试建一个临时文件再删掉（比 ``os.access`` 可靠——后者对
    ACL / 只读卷经常给出与实际写入相反的答案）。"""
    try:
        fd, probe = tempfile.mkstemp(prefix=".update_probe_", dir=directory)
    except OSError:
        return False
    try:
        os.close(fd)
        os.remove(probe)
    except OSError:
        pass
    return True


def _pending_old_path() -> str:
    return os.path.join(update_download_dir(), PENDING_OLD_FILENAME)


def stage_and_replace(
    downloaded: str,
    target: str | None = None,
    restart: bool = True,
    popen=None,
) -> str:
    """把**已校验通过**的下载文件原地替换到 ``target``（默认当前程序自身）。

    流程（方案 6.1 ③~⑧）：

    1. 目标目录可写性预检——不可写就抛错，**绝不静默提权**；
    2. 先把新文件挪到**同一卷**的目标目录（``os.replace`` 不能跨卷）；
    3. ``target → target.old``（运行中的 exe 不能删但能改名）→ 新文件落位；
    4. 任一步失败 ⇒ 把 ``.old`` 改回原名**整体回滚**后再报错；
    5. 成功则记下「待清理标记」，``restart=True`` 时启动新版本并返回 ``.old`` 路径
       （调用方随后退出当前进程；``.old`` 留到新版本成功启动一次后由
       :func:`cleanup_pending_old` 清掉——新版本启动即崩时它就是回滚的最后退路）。

    ``popen`` 仅供测试注入；默认真启动 ``target``。
    """
    target = target or sys.executable
    directory = os.path.dirname(os.path.abspath(target))
    if not os.path.isdir(directory):
        raise UpdaterError("程序所在目录不存在：%s" % directory)
    if not ensure_dir_writable(directory):
        raise UpdaterError(
            "程序所在目录不可写，无法自动替换：%s\n"
            "请以管理员身份运行，或手动替换程序文件。" % directory)
    if not os.path.isfile(downloaded):
        raise UpdaterError("下载文件不存在：%s" % downloaded)

    # 新文件先落到同卷同目录（沿用下载文件名加 .new 后缀，避免撞名）。
    staged = os.path.join(
        directory, "%s.new-%s" % (os.path.basename(target), _stamp()))
    try:
        shutil.move(downloaded, staged)
    except (OSError, shutil.Error) as exc:
        raise UpdaterError("无法把新版本移入程序目录：%s" % exc) from exc
    # 哈希旁证（若下载侧写了）一并带走，供 --resume-update 复核。
    sidecar = downloaded + ".sha256"
    if os.path.isfile(sidecar):
        try:
            shutil.move(sidecar, staged + ".sha256")
        except OSError:
            pass

    old = target + ".old"
    if os.path.exists(old):
        try:
            os.remove(old)
        except OSError:
            # 上一次的 .old 还被占用（前进程未退净）：换名保存，别挡本次更新。
            old = "%s.old-%s" % (target, _stamp())

    try:
        os.rename(target, old)
    except OSError as exc:
        # 目标 exe 本身被占用（杀软扫描 / 其它进程锁住）时改名会失败——此刻还没动
        # 任何东西，原样报错即可，但要给可诊断的话术而不是裸 PermissionError。
        raise UpdaterError(
            "旧版本改名失败（可能被杀毒软件或其它程序占用）：%s" % exc) from exc
    try:
        os.replace(staged, target)
    except OSError as exc:
        try:
            os.rename(old, target)
        except OSError as rollback_exc:
            raise UpdaterError(
                "替换失败（%s），且回滚也失败（%s）：\n旧版本保留在 %s，请手动恢复。"
                % (exc, rollback_exc, old)) from exc
        raise UpdaterError(
            "替换新版本失败（可能被杀毒软件拦截），已回滚到原版本：%s" % exc) from exc

    try:
        os.makedirs(update_download_dir(), exist_ok=True)
        with open(_pending_old_path(), "w", encoding="utf-8") as f:
            json.dump({"old": old, "target": target}, f, ensure_ascii=False)
    except OSError:
        pass                    # 标记写不上只影响「自动清 .old」，不影响更新本身

    if restart:
        (popen or subprocess.Popen)([target])
    return old


def _stamp() -> str:
    return time.strftime("%Y%m%d%H%M%S")


def cleanup_pending_old(marker_path: str | None = None) -> bool:
    """新版本成功启动后清掉上一次更新留下的 ``.old``（方案 6.1 ⑧）。

    只做**尽力而为**：``.old`` 还被占用（前一进程未完全退出）就留着下次再试，
    **不算错误、不弹任何提示**。返回是否清掉了标记。
    """
    marker_path = marker_path or _pending_old_path()
    try:
        with open(marker_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return False
    old = data.get("old") if isinstance(data, dict) else None
    if isinstance(old, str) and old:
        try:
            os.remove(old)
        except OSError:
            return False          # 还被占用：留待下次（此时新版本已能跑，无害）
    try:
        os.remove(marker_path)
    except OSError:
        return False
    return True


# --------------------------------------------------------------------------- #
# CLI：--update / --resume-update（方案第七部分；生效在 import tkinter 之前）
# --------------------------------------------------------------------------- #
def _run_cli_update(argv: list[str]) -> int:
    from .prefs import update_settings
    from .version import __version__, emit_console

    state = {"last": -10}

    def _progress(pct: int) -> None:
        """CLI 进度：每 10% 打一行（无控制台时 emit_console 兜底为消息框）。"""
        if pct >= 0 and pct - state["last"] < 10 and pct != 100:
            return
        state["last"] = pct
        emit_console("下载进度：%s%%" % ("…" if pct < 0 else pct))

    cfg = update_settings()
    result = check_update(
        current=__version__,
        include_prerelease=bool(cfg.get("include_prerelease")),
        proxy=str(cfg.get("proxy") or ""),
        skipped_version=str(cfg.get("skipped_version") or ""),
    )
    if result.error:
        emit_console("检查更新失败：%s" % result.error)
        return 2
    if result.up_to_date or result.info is None:
        emit_console(describe_result(result, __version__, platform_asset_name()))
        return 1 if result.up_to_date else 2
    info = result.info
    emit_console(describe_result(result, __version__, platform_asset_name()))
    emit_console("开始下载 %s …" % info.asset_name)
    try:
        path = download_update(info, proxy=str(cfg.get("proxy") or ""),
                               progress=_progress)
    except UpdaterError as exc:
        emit_console("下载失败：%s" % exc)
        return 2
    emit_console("已下载并通过校验：%s" % path)

    if sys.platform.startswith("win") and is_frozen():
        try:
            stage_and_replace(path)
        except UpdaterError as exc:
            emit_console("更新失败：%s" % exc)
            return 2
        emit_console("已启动新版本，当前程序即将退出。")
        return 0
    if sys.platform.startswith("linux"):
        try:
            os.chmod(path, 0o755)     # HTTP 下载不保留可执行位（方案 L3）
        except OSError:
            pass
    emit_console("本平台暂不支持自动替换，请手动用该文件替换当前程序。\n"
                 "（源码模式请改用 git pull 更新源码。）")
    return 0


def _run_resume_update(staged: str) -> int:
    """``--resume-update``：提权后的新进程**只做校验 → 替换 → 重启**（不重新下载）。"""
    from .version import emit_console

    target = sys.executable
    if not sys.platform.startswith("win") or not is_frozen():
        emit_console("--resume-update 仅支持打包版 Windows。")
        return 2
    if not os.path.isfile(staged):
        emit_console("待安装的更新文件不存在：%s" % staged)
        return 2
    # 复核用：size 一律免检（staged 已在下载时查过），魔数按 Windows 资产名取。
    info = UpdateInfo(version="", tag="", asset_name=ASSET_NAMES["windows"],
                      url="", size=0)
    sidecar = staged + ".sha256"
    if os.path.isfile(sidecar):
        try:
            with open(sidecar, "r", encoding="utf-8") as f:
                expected = f.read().strip().lower()
            if re.fullmatch(r"[0-9a-f]{64}", expected):
                actual = _sha256_of_file(staged)
                if actual != expected:
                    emit_console("更新文件校验不符，已放弃安装。")
                    return 2
        except OSError:
            pass
    ok, reason = verify_file(staged, info)
    if not ok:
        emit_console("更新文件校验失败：%s" % reason)
        return 2
    try:
        stage_and_replace(staged, target=target)
    except UpdaterError as exc:
        emit_console("更新失败：%s" % exc)
        return 2
    emit_console("已启动新版本。")
    return 0


def handle_update_flags(argv: list[str]) -> int | None:
    """``--update`` / ``--resume-update`` 的 CLI 分支；未命中返回 ``None``。

    与 ``--check-update`` 同样处理在 ``import tkinter`` 之前；无控制台（``--windowed``
    产物）时输出经 :func:`version.emit_console` 退化为系统消息框。
    """
    for i, arg in enumerate(argv):
        if arg == "--update":
            return _run_cli_update(argv)
        if arg.startswith("--resume-update="):
            return _run_resume_update(arg.split("=", 1)[1].strip().strip('"'))
        if arg == "--resume-update" and i + 1 < len(argv):
            return _run_resume_update(argv[i + 1].strip().strip('"'))
    return None
