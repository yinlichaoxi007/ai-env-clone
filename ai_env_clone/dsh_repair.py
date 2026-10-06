"""
DSH 旧会话数据「未分组 / 无法加载」检测与修复（纯标准库，零第三方依赖）。

背景（对某第三方修复脚本思路的核实结论）：

1. **未分组**：会话日志在 ``~/.dsh/sessions/<projectKey>/<sessionId>/`` 存在，
   但 ``~/.dsh/storages/workspace.json`` 的 ``tables.workspaces.<uuid>.sessionIds``
   没有登记它，DSH 界面就把这类会话归入「未分组」（官方 ``ui-workspace`` 的
   ``group.ungrouped`` 桶）。成因通常是：旧版本 / 崩溃 / 手工拷贝 / 还原时
   索引被整体覆盖等。修复 = 把会话 id 补进正确工作区记录的 ``sessionIds``
   （视需要补建工作区记录并登记进 ``global.workspaceIds``）——这与 DSH 官方
   ``workspaceRegistry.bootstrap`` 的行为等价，只是离线执行、无需启动 DSH。
   该脚本的 ``tools/补全工作区索引.mjs`` 正是这一修复的手工版。

2. **无法加载**：包络拆分前旧构建写入的扁平 ``replayState``
   （``{kind: 'pi-ai', version: 1, ..., blocks: [...]}``，无 ``response`` 成员）
   会被官方 released-format 校验器拒绝，机制有两道（见「规则一」注释）：
   a) finish 块携带的 replayState 必须是 ``{response, blocks}`` 信封
      （``replayEnvelopeValue`` 的 ``exactRecord``）；
   b) ``message.source.replayState`` 必须与从内嵌 stream 重组出来的那份
      完全相等（replayState 是 stream 的镜像，只改一侧必被拒）。
   修复 = **双侧一致升级**：把同一事件里出现的每处扁平 replayState 都升级成
   同一个 ``{response, blocks}`` 信封（v0 形态无内嵌 stream，只有 source 一侧，
   迁移器会自动把它复制进合成 stream 的 finish 块）。max-tokens 剪枝场景
   （需要两侧取不同值）本工具跳过并上报，绝不猜测改写。

3. **子代理描述符版本不合规（可修）**：旧构建写下的 ``subagent/descriptor`` 事件
   ``version`` 不是 3 时，官方 v0→v1 迁移直接拒绝
   （``SessionFormatUnsupportedMigrationError``，实测报错文案
   ``subagent/descriptor 0 uses unsupported descriptor version 2``），会话无法加载。
   修复 = 在**载荷满足 v3 语义**时只把 ``version`` 改成 3（规则三，见
   :func:`_fix_descriptor_version`）；载荷本身不满足 v3 语义（provider 为空、
   continuable 缺 label、agentProvider/agentModel 未成对等）时**跳过并上报**，
   绝不拼凑一个语义不同的描述符。

4. **投影缓存陈旧（会导致侧边栏看不到 / 标题错）**：DSH 侧边栏对**冷会话**只读
   ``storages/session_projcache/sessions/<sid>.json`` 的投影；该记录的
   ``identity`` 只绑 header 的**不变字段**——日志被带外改写而 header 不变时，
   旧记录继续被采信，于是 ``sessionListMetadata.blank`` 仍为 true（隐藏）或
   ``title`` 仍是旧值。检测判据只认「缓存结论与日志矛盾」（日志含 ``turn/start``
   而缓存标 blank；缓存标题与日志不一致），不认 ``seq`` 落后（检查点本就常落在
   会话中途）。修复 = **把陈旧记录移出**（同目录改名为 ``.bak-<ts>``），由 DSH
   冷读时自行重算——依据官方 ``session-projection-cache`` spec：陈旧/不可读的
   缓存「只是多一次尾部重放，绝不给出错误值」，无缓存则 ``blank`` 默认 false
   故会话可见。**绝不就地重写缓存值**。

zstd 说明：会话日志是 Zstandard 压缩的 JSONL，Python 标准库**没有** zstd。
本模块按需探测可用后端（``zstandard`` / ``pyzstd`` / 系统 ``zstd`` 命令），
没有则自动降级为「仅目录级检测」（不读文件内容）；修复仍可完成
「按目录名匹配已有工作区」的部分。所有写入前自动备份原文件，默认 dry-run，
**绝不删除任何条目**（唯一会「摘除」的动作是子代理关系修复把子会话 id 从
``workspace.json`` 的 ``sessionIds`` 里去掉，见下条，且旧目录/旧缓存都只改名移走）。

5. **子代理关系修复（来源自动查找，无需指定）**：导入器早期版本把来源工具的子代理会话
   当成普通会话写进了 DSH（``delegationDepth = 0`` 的顶层会话、登记在工作区里，
   在侧边栏与用户会话平级出现，正文却只是 AI 的干活过程）。本模块按来源侧的
   ``parent_id``（ZCode 的 ``task_type = 'subagent_child'`` / DSH 的
   ``origin: "subagent"``）+ 「标题 + 首条用户正文」指纹把它们**就地改造成 DSH 原生
   子代理会话**（裸 uuid 目录、header 带 ``parentSession`` / ``origin`` /
   ``delegationDepth``、seq 0 为 ``subagent/descriptor``；正文一字不改），旧顶层会话
   目录整体移到 ``sessions/.removed/``、id 从工作区列表摘除、旧投影缓存改名移走。
   父会话日志**不用改**：DSH 加载 v3 父会话时按「子会话证据完整 ⇒ 追加目录事实」自动补
   ``subagent/catalog``（实测：父会话 50 条事件 + 2 条子会话证据 → 迁移结果 52 条、
   目录项 2）；只有父会话**已发布 v4** 时才把它那份 v4 改名移走，让它回落到 v3 重新迁移。
   来源按「本机 ZCode 数据 → 本工具备份目录 / 上次用过的包目录里的 ``zcode_backup_*.zip``
   与 ``dsh_backup_*.zip``」自动查找，``--relink-source`` 仅作显式覆盖。
   实现见 :mod:`ai_env_clone.subagent_relink`。

用法（CLI 自动化脚本）：

    python -m ai_env_clone.dsh_repair scan [--dsh-home PATH] [--json]
    python -m ai_env_clone.dsh_repair plan [--dsh-home PATH]
    python -m ai_env_clone.dsh_repair fix  [--dsh-home PATH] [--apply] [--no-backup]

GUI：在主界面选择「DeepSeek Harness」后，数据目录区域会出现
「检测会话健康」与「修复会话数据」两个按钮（见 ``__main__.py``）。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Optional

from .core import _longpath

__all__ = [
    "project_key",
    "SessionOnDisk",
    "DetectResult",
    "RepairMutation",
    "RepairPlan",
    "ApplyResult",
    "resolve_dsh_home",
    "scan_sessions",
    "load_workspace_index",
    "validate_workspace_index",
    "detect_ungrouped",
    "detect_legacy_replay_state",
    "StaleProjcache",
    "detect_stale_projcache",
    "plan_workspace_index_repair",
    "apply_workspace_index_repair",
    "zstd_backend",
    "main",
]

#: Zstandard 帧魔数（小端 ``28 B5 2F FD``），用于帧切分。
ZSTD_MAGIC = 0xFD2FB528

#: 无 cwd 会话的项目目录名（官方 ``projectDir(root, undefined)``）。
NO_CWD_PROJECT = "_no-cwd"


# --------------------------------------------------------------------------- #
# 路径与时间工具
# --------------------------------------------------------------------------- #
def resolve_dsh_home(dsh_home: str | None = None) -> str:
    """DSH 数据根目录：显式传入 > ``$DSH_HOME`` > 默认 ``~/.dsh``。"""
    if dsh_home:
        return os.path.expanduser(dsh_home)
    env_home = os.environ.get("DSH_HOME")
    if env_home:
        return os.path.expanduser(env_home)
    return os.path.join(os.path.expanduser("~"), ".dsh")


def _now_iso() -> str:
    """与 DSH 官方 ``new Date().toISOString()`` 同形（UTC，毫秒，``Z`` 结尾）。"""
    now = datetime.now(timezone.utc)
    return "%s.%03dZ" % (now.strftime("%Y-%m-%dT%H:%M:%S"), now.microsecond // 1000)


def _normalize_path(path: str) -> str:
    """工作区路径的唯一化比较：大小写（Windows）+ 分隔符归一。"""
    norm = os.path.normpath(os.path.expanduser(path))
    if os.name == "nt":
        return norm.lower()
    return norm


def _utf16_units(text: str) -> list[int]:
    """按 UTF-16 码元迭代（与 JS ``charCodeAt`` 语义一致，支持星号等增补字符）。"""
    raw = text.encode("utf-16-le")
    return [
        int.from_bytes(raw[i : i + 2], "little") for i in range(0, len(raw), 2)
    ]


# --------------------------------------------------------------------------- #
# projectKey：DSH 会话项目目录名的有损编码（对照 session-persistence-jsonl）
# --------------------------------------------------------------------------- #
def project_key(cwd: str) -> str:
    """把工作区路径编码为 ``--...--`` 目录名（与 DSH 官方 ``projectKey`` 等价）。

    规则（官方 format.ts）：``/ \\ :`` 等分隔符折叠为 ``-``；不安全码元按
    UTF-16 码元编码为 ``~XXXX``（大写十六进制，4 位补零）；结果去前导 ``-``、
    空则 ``root``，包 ``--`` 后截断到 251 字符。
    """
    if len(cwd) == 0:
        raise ValueError("cannot encode an empty project path")
    readable: list[str] = []
    separator_run = False
    for unit in _utf16_units(cwd):
        ch = chr(unit)
        if ch in "/\\:":
            if not separator_run:
                readable.append("-")
            separator_run = True
        elif ch != "~" and re.fullmatch(r"[A-Za-z0-9._-]", ch):
            readable.append(ch)
            separator_run = False
        else:
            readable.append("~%04X" % unit)
            separator_run = False
    slug = "".join(readable).lstrip("-") or "root"
    return "--%s--" % slug[:251]


# --------------------------------------------------------------------------- #
# zstd 后端（按需探测，缺失时优雅降级）
# --------------------------------------------------------------------------- #
#: 测试注入点：``(decompress, compress, name)`` 或 None。
_ZSTD_OVERRIDE: "tuple[Callable[[bytes], bytes], Callable[[bytes], bytes] | None, str] | None" = None


def _set_zstd_backend(
    decompress: "Callable[[bytes], bytes]",
    name: str,
    compress: "Callable[[bytes], bytes] | None" = None,
) -> None:
    """测试注入：覆盖 zstd 后端探测结果（compress 可为 None：只读检测）。"""
    global _ZSTD_OVERRIDE
    _ZSTD_OVERRIDE = (decompress, compress, name)


def _reset_zstd_backend() -> None:
    """测试清理：恢复自动探测。"""
    global _ZSTD_OVERRIDE
    _ZSTD_OVERRIDE = None


def zstd_backend() -> "tuple[Callable[[bytes], bytes] | None, Callable[[bytes], bytes] | None, str]":
    """探测可用的 zstd 后端。

    :return: ``(decompress, compress, name)``；无可用后端时 ``(None, None, '')``。
        支持 ``zstandard`` / ``pyzstd`` 模块与系统 ``zstd`` 命令。
        ``compress`` 用于会话文件内容修复时的帧重压缩；只读检测只用
        ``decompress``。
    """
    if _ZSTD_OVERRIDE is not None:
        return _ZSTD_OVERRIDE
    try:
        import zstandard  # type: ignore

        def _zstd_standard_decompress(data: bytes) -> bytes:
            if not data:
                return b""
            # 不能用 ZstdDecompressor().decompress()：它要求帧头带内容长度，
            # 而 DSH 写的帧不含内容长度（实测报 "could not determine content
            # size in frame header"）。改用流式 DecompressionObj；注意
            # ``decompress()`` 返回已产出的输出、``flush()`` 只返回剩余部分，
            # 两者都要拼接。
            #
            # ⚠️ 必须 read_across_frames=True：DSH 的会话文件是**多帧** zstd
            # 拼接（本机一个 4.3MB 的 session.v3.jsonl.zstd 实测含多帧），
            # 默认 False 时只解第一帧——4.3MB 只解出 205 字节（首行 session 头），
            # 会把「内容完好」的会话误判为截断/空。
            obj = zstandard.ZstdDecompressor().decompressobj(read_across_frames=True)
            return obj.decompress(data) + obj.flush()

        def _zstd_standard_compress(data: bytes) -> bytes:
            return zstandard.ZstdCompressor(write_checksum=True).compress(data)

        return _zstd_standard_decompress, _zstd_standard_compress, "zstandard"
    except ImportError:
        pass
    try:
        import pyzstd  # type: ignore

        def _pyzstd_decompress(data: bytes) -> bytes:
            if not data:
                return b""
            # pyzstd.decompress() 原生支持多帧拼接；decompressobj 不一定，
            # 故优先用前者，失败再退回流式（容忍无内容长度的帧）。
            try:
                return pyzstd.decompress(data)
            except (AttributeError, TypeError, ValueError):
                obj = pyzstd.decompressobj()
                return obj.decompress(data) + obj.flush()

        def _pyzstd_compress(data: bytes) -> bytes:
            try:
                return pyzstd.compress(data, write_checksum=True)
            except TypeError:
                return pyzstd.compress(data)

        return _pyzstd_decompress, _pyzstd_compress, "pyzstd"
    except ImportError:
        pass
    # 系统 zstd 命令（stdin 管道）作为最后兜底
    if shutil_which("zstd") is not None:
        def _zstd_cli_decompress(data: bytes) -> bytes:
            proc = subprocess.run(
                ["zstd", "-d", "-c"],
                input=data,
                capture_output=True,
                check=False,
            )
            if proc.returncode != 0:
                raise ValueError(
                    "zstd CLI 解压失败: %s" % proc.stderr.decode("utf-8", "replace")[:200]
                )
            return proc.stdout

        def _zstd_cli_compress(data: bytes) -> bytes:
            proc = subprocess.run(
                ["zstd", "-c"],
                input=data,
                capture_output=True,
                check=False,
            )
            if proc.returncode != 0:
                raise ValueError(
                    "zstd CLI 压缩失败: %s" % proc.stderr.decode("utf-8", "replace")[:200]
                )
            return proc.stdout

        return _zstd_cli_decompress, _zstd_cli_compress, "zstd-cli"
    return None, None, ""


def shutil_which(cmd: str) -> "str | None":
    """轻量 which（避免顶层依赖 shutil 语义差异）。"""
    from shutil import which

    return which(cmd)


def _scan_zstd_frames(buffer: bytes) -> list[tuple[int, int]]:
    """把拼接的 zstd 独立帧切成 ``(start, end)`` 区间（对照 第三方修复脚本
    ``tools/*.mjs`` 的 ``scanZstdFrames``，供逐帧解压，避免多帧一次解压失败）。"""
    frames: list[tuple[int, int]] = []
    offset = 0
    n = len(buffer)
    while offset < n:
        start = offset
        if n - offset < 4:
            break
        if int.from_bytes(buffer[offset : offset + 4], "little") != ZSTD_MAGIC:
            break
        offset += 4
        if offset == n:
            break
        descriptor = buffer[offset]
        offset += 1
        content_size_flag = descriptor >> 6
        single_segment = (descriptor & 0x20) != 0
        checksum = (descriptor & 0x04) != 0
        dictionary_flag = descriptor & 0x03
        dictionary_bytes = 4 if dictionary_flag == 3 else dictionary_flag
        content_size_bytes = (
            (1 if single_segment else 0) if content_size_flag == 0 else (1 << content_size_flag)
        )
        remaining_header_bytes = (0 if single_segment else 1) + dictionary_bytes + content_size_bytes
        if n - offset < remaining_header_bytes:
            break
        offset += remaining_header_bytes
        while True:
            if n - offset < 3:
                break
            block_header = int.from_bytes(buffer[offset : offset + 3], "little")
            offset += 3
            last_block = (block_header & 1) != 0
            block_type = (block_header >> 1) & 0x03
            block_size = block_header >> 3
            payload_bytes = 1 if block_type == 0x01 else block_size
            if n - offset < payload_bytes:
                break
            offset += payload_bytes
            if last_block:
                break
        if checksum and n - offset >= 4:
            offset += 4
        frames.append((start, offset))
    return frames


def _decompress_all(buffer: bytes, decompress: "Callable[[bytes], bytes]") -> bytes:
    """逐帧解压并拼接为完整文本字节。"""
    frames = _scan_zstd_frames(buffer)
    if not frames:
        raise ValueError("不是有效的 zstd 拼接帧")
    return b"".join(decompress(buffer[s:e]) for s, e in frames)


def _scan_zstd_frames_with_tail(buffer: bytes) -> "tuple[list[tuple[int, int]], int | None]":
    """帧切分 + 尾部不完整检测（供内容修复使用，需区分「写断的残留字节」）。

    :return: ``(帧列表, 尾部起点)``；``尾部起点`` 非 None 表示该位置起存在写断
        的残留字节（对照 第三方修复脚本 的 torn-tail 判定），必须原样保留、
        不得参与重写。
    :raises ValueError: 帧头保留位被置位 / 块类型非法（无法安全修复的数据）。
    """
    frames: list[tuple[int, int]] = []
    offset = 0
    n = len(buffer)
    while offset < n:
        start = offset
        if n - offset < 4:
            return frames, start
        if int.from_bytes(buffer[offset : offset + 4], "little") != ZSTD_MAGIC:
            return frames, start
        offset += 4
        if offset == n:
            return frames, start
        descriptor = buffer[offset]
        offset += 1
        if descriptor & 0x18:
            raise ValueError("帧头保留位被置位，偏移 %d" % (offset - 1))
        content_size_flag = descriptor >> 6
        single_segment = (descriptor & 0x20) != 0
        checksum = (descriptor & 0x04) != 0
        dictionary_flag = descriptor & 0x03
        dictionary_bytes = 4 if dictionary_flag == 3 else dictionary_flag
        content_size_bytes = (
            (1 if single_segment else 0) if content_size_flag == 0 else (1 << content_size_flag)
        )
        remaining_header_bytes = (0 if single_segment else 1) + dictionary_bytes + content_size_bytes
        if n - offset < remaining_header_bytes:
            return frames, start
        offset += remaining_header_bytes
        while True:
            if n - offset < 3:
                return frames, start
            block_header = int.from_bytes(buffer[offset : offset + 3], "little")
            offset += 3
            last_block = (block_header & 1) != 0
            block_type = (block_header >> 1) & 0x03
            block_size = block_header >> 3
            if block_type == 0x03:
                raise ValueError("块类型保留值，偏移 %d" % (offset - 3))
            payload_bytes = 1 if block_type == 0x01 else block_size
            if n - offset < payload_bytes:
                return frames, start
            offset += payload_bytes
            if last_block:
                break
        if checksum:
            if n - offset < 4:
                return frames, start
            offset += 4
        frames.append((start, offset))
    return frames, None


# --------------------------------------------------------------------------- #
# 会话扫描
# --------------------------------------------------------------------------- #
@dataclass
class SessionOnDisk:
    """磁盘上的一个会话目录。"""

    project_dir: str            # sessions/ 下的项目目录名（--...-- 或 _no-cwd）
    session_dir: str            # 会话目录名（session-<uuid> 或编码后的 id）
    session_id: str             # 会话 id（目录名，即 session-<uuid>）
    path: str                   # 会话目录绝对路径
    header: Optional[dict] = None   # 首个 zstd 帧解析出的 header（无 zstd 时 None）
    header_error: str = ""      # header 读取失败原因（空串=成功/未尝试）
    legacy_replay: bool = False  # 内容含扁平 replayState（旧格式，需 zstd）
    dup_call_ids: bool = False   # 同 step 内重复宣告 tool-call id（需 zstd）
    descriptor_bad: bool = False  # 含官方迁移不支持的 subagent/descriptor 版本（无法加载，暂不修复）
    log_file: str = ""          # 生效的会话日志文件（同目录内代际最高者）
    log_version: int = 0        # 该文件的格式代际（0 = session.jsonl.zstd）

    @property
    def cwd(self) -> "str | None":
        """会话 header 里的 cwd（无 zstd / 读取失败时为 None）。"""
        if not self.header:
            return None
        return self.header.get("cwd")


# 代际文件名：session.jsonl.zstd（v0）/ session.v1.jsonl.zstd / ... / 明文 .jsonl
_JS_SAFE_INTEGER_MAX = 2 ** 53 - 1


def parse_generation_log_filename(name: str, compression: str = "zstd") -> "int | None":
    """对照 DSH 的 ``parseGenerationLogFilename``：解析 canonical 代际文件名。

    - ``session.jsonl.zstd`` → 0（不带版本号的原始名）；
    - ``session.v27.jsonl.zstd`` → 27；
    - 非 canonical 名（临时文件、大写、前导零、``v0``、超过 JS 安全整数、
      多余后缀、压缩类型不匹配）→ ``None``。
    """
    # DSH 先剥离压缩后缀，再交给 parseSessionFormatLogFilename 处理
    suffix = ".zstd" if compression == "zstd" else ""
    if not name.endswith(suffix):
        return None
    stem = name[: -len(suffix)] if suffix else name
    if not stem.endswith(".jsonl"):
        return None
    base = stem[: -len(".jsonl")]
    if base == "session":
        return 0
    if not base.startswith("session.v"):
        return None
    digits = base[len("session.v"):]
    if not digits or not digits.isascii() or not digits.isdigit():
        return None
    if len(digits) > 1 and digits[0] == "0":
        return None
    version = int(digits)
    if version == 0 or version > _JS_SAFE_INTEGER_MAX:
        return None
    return version


def find_generation_log(session_dir: str, compression: str = "zstd") -> "tuple[str | None, int]":
    """返回会话目录中**生效**的日志 ``(绝对路径, 代际)``；无 canonical 文件时 ``(None, -1)``。

    对照 DSH 的 ``resolveJsonlGeneration``：同一目录多个代际并存时取版本号**最高**
    者（``session.v2.jsonl.zstd`` 优先于 ``session.jsonl.zstd``），其余代际不会
    被加载器读取。DSH 禁止同一目录同时存在两种压缩格式，故先查 zstd、再查明文。
    """
    try:
        entries = os.listdir(_longpath(session_dir))
    except OSError:
        return None, -1
    for mode in (compression, "none" if compression == "zstd" else "zstd"):
        best_path, best_version = None, -1
        for name in entries:
            version = parse_generation_log_filename(name, mode)
            if version is None or version <= best_version:
                continue
            best_path, best_version = os.path.join(session_dir, name), version
        if best_path is not None:
            return best_path, best_version
    return None, -1


def _read_log_text(log_file: str, decompress: "Callable[[bytes], bytes] | None") -> str:
    """读取会话日志全文（zstd 拼接帧或明文 JSONL，与 ``_handle_session_data`` 同口径）。

    明文日志（DSH 的 ``compression: none``）不需要 zstd 后端；检测路径原先直接走
    ``_decompress_all``，对明文会抛异常并被吞掉，导致整份日志被静默跳过。

    :raises ValueError: 是 zstd 日志但缺少 zstd 后端。
    """
    with open(_longpath(log_file), "rb") as fh:
        buf = fh.read()
    if len(buf) >= 4 and int.from_bytes(buf[:4], "little") == ZSTD_MAGIC:
        if decompress is None:
            raise ValueError(_ZSTD_MISSING_DETAIL)
        return _decompress_all(buf, decompress).decode("utf-8-sig", "replace")
    return buf.decode("utf-8-sig", "replace")


def _read_log_head(log_file: str, decompress: "Callable[[bytes], bytes] | None") -> str:
    """只读取会话日志开头（zstd 仅解压首帧；明文读整份），用于取 header 行。

    单独一份是为了避免为读一行 header 就解压整个大日志。
    """
    with open(_longpath(log_file), "rb") as fh:
        buf = fh.read()
    if len(buf) >= 4 and int.from_bytes(buf[:4], "little") == ZSTD_MAGIC:
        if decompress is None:
            raise ValueError(_ZSTD_MISSING_DETAIL)
        frames = _scan_zstd_frames(buf)
        if not frames:
            raise ValueError("空文件或无法切分 zstd 帧")
        return decompress(buf[frames[0][0] : frames[0][1]]).decode("utf-8-sig", "replace")
    return buf.decode("utf-8-sig", "replace")


def _read_session_header(log_file: str, decompress: "Callable[[bytes], bytes] | None") -> "tuple[dict | None, str]":
    """读取会话日志首行的 header（zstd 帧或明文皆可）。返回 ``(header_dict, error)``。"""
    try:
        text = _read_log_head(log_file, decompress)
    except Exception as exc:  # 读取失败不致命，仅记录
        return None, str(exc)
    # 只按 \n 取首行：splitlines 会在 U+2028/U+0085 等字符处断行
    line = text.split("\n", 1)[0].strip()
    if not line:
        return None, "空文件"
    try:
        parsed = json.loads(line)
    except (ValueError, TypeError) as exc:
        return None, "header 行不是 JSON：%s" % exc
    if not isinstance(parsed, dict):
        return None, "header 行不是 JSON 对象"
    return parsed, ""


def _is_flat_replay_state(value) -> bool:
    """是否为包络拆分前已发布的扁平 ``replayState``（有 ``kind``、无 ``response``）。"""
    return isinstance(value, dict) and "kind" in value and "response" not in value


def _scan_file_content(log_file: str, decompress: "Callable[[bytes], bytes]") -> "tuple[bool, bool, bool]":
    """扫描整份会话日志。

    :return: ``(是否含扁平 replayState, 是否含同 step 重复 tool-call id,
        是否含官方迁移不支持的 subagent/descriptor 版本)``。
    扁平 replayState 判定为「有 ``kind``、无 ``response``」（与升级器的信封
    转换条件完全一致，故检测到的命中数与升级器实际改写的数量一致）；
    重复 tool-call id 按 ``turn/start`` / ``step/end`` 重置；
    subagent/descriptor 依据官方 v0→v1 迁移校验（``assertReleasedEventPayload``）：
    该事件的 ``version`` 不是 3 时迁移直接拒绝，会话无法加载（本工具只检测、不修复）。
    与格式版本无关（v0/v1 数据都可能带扁平形态）。
    """
    try:
        text = _read_log_text(log_file, decompress)
    except Exception:
        return False, False, False
    events: list[dict] = []
    legacy = False
    descriptor_bad = False
    # 只能按 "\n" 切行：JSONL 的行边界是 \n，而 str.splitlines() 还会在
    # U+2028/U+2029/U+0085 等处断行——这些字符 JSON.stringify 不转义、会原样
    # 出现在正文里，会让整行 JSON 解析失败而被静默跳过（检测漏报，修复却会改写）。
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except (ValueError, TypeError):
            continue
        if not isinstance(entry, dict):
            continue
        events.append(entry)
        data = entry.get("data")
        if not isinstance(data, dict):
            continue
        if entry.get("type") == "subagent/descriptor":
            version = data.get("version")
            # 官方要求 version === 3；缺失或其它值（含非法类型）都会被迁移拒绝
            if not (isinstance(version, (int, float)) and not isinstance(version, bool) and version == 3):
                descriptor_bad = True
        chunk = data.get("chunk")
        if isinstance(chunk, dict) and _is_flat_replay_state(chunk.get("replayState")):
            legacy = True
        message = data.get("message")
        if (
            isinstance(message, dict)
            and isinstance(message.get("source"), dict)
            and _is_flat_replay_state(message["source"].get("replayState"))
        ):
            legacy = True
    return legacy, bool(_duplicate_advertised_ids(events)), descriptor_bad


def scan_sessions(dsh_home: str) -> list[SessionOnDisk]:
    """扫描 ``sessions/`` 下全部会话目录（不读内容，纯目录级）。

    每个会话目录按 DSH 的代际规则取**最高代际**的日志文件作为生效文件
    （``session.v2.jsonl.zstd`` 优先于 ``session.jsonl.zstd``），记在
    ``log_file`` / ``log_version``。
    """
    sessions_root = os.path.join(resolve_dsh_home(dsh_home), "sessions")
    out: list[SessionOnDisk] = []
    if not os.path.isdir(_longpath(sessions_root)):
        return out
    for project_name in sorted(os.listdir(_longpath(sessions_root))):
        project_path = os.path.join(sessions_root, project_name)
        if not os.path.isdir(_longpath(project_path)) or project_name.startswith("."):
            continue
        for session_name in sorted(os.listdir(_longpath(project_path))):
            session_path = os.path.join(project_path, session_name)
            if not os.path.isdir(_longpath(session_path)):
                continue
            log_file, log_version = find_generation_log(session_path)
            if log_file is None:
                continue
            out.append(
                SessionOnDisk(
                    project_dir=project_name,
                    session_dir=session_name,
                    session_id=session_name,
                    path=session_path,
                    log_file=log_file,
                    log_version=log_version,
                )
            )
    return out


# --------------------------------------------------------------------------- #
# workspace.json 索引
# --------------------------------------------------------------------------- #
def load_workspace_index(dsh_home: str) -> "dict | None":
    """读取 ``storages/workspace.json``；缺失或解析失败返回 None。"""
    file = os.path.join(resolve_dsh_home(dsh_home), "storages", "workspace.json")
    try:
        # utf-8-sig：容忍带 UTF-8 BOM 的文件（部分编辑器/脚本写入），
        # 否则 json.load 会把 BOM 当成非法首字符。
        with open(_longpath(file), "r", encoding="utf-8-sig") as fh:
            parsed = json.load(fh)
    except (OSError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def validate_workspace_index(idx: dict) -> list[str]:
    """校验索引结构，返回问题列表（空列表=健康）。"""
    problems: list[str] = []
    if "unit" not in idx:
        problems.append("缺少 unit 元数据")
    tables = idx.get("tables")
    if not isinstance(tables, dict) or not isinstance(tables.get("workspaces"), dict):
        problems.append("缺少 tables.workspaces 表")
        return problems
    workspaces = tables["workspaces"]
    global_state = idx.get("global")
    if not isinstance(global_state, dict):
        problems.append("缺少 global 状态")
        global_state = {}
    order = global_state.get("workspaceIds")
    if not isinstance(order, list):
        problems.append("global.workspaceIds 缺失或非数组")
        order = []
    order_set = set(order)
    for wid in order:
        if wid not in workspaces:
            problems.append("workspaceIds 引用了不存在的记录: %s" % wid)
    for wid, record in workspaces.items():
        if not isinstance(record, dict):
            problems.append("工作区记录 %s 不是对象" % wid)
            continue
        # path/title/sessionIds/createdAt/updatedAt 是 DSH 存储边界的 zod 必填字段
        # （workspaceRecord，均无默认值）。**任一条记录缺字段 ⇒ 整个 workspace 域
        # 解析失败 ⇒ 桌面端一个工作区、一个会话都不显示**（连本机原有会话一起消失）。
        if not isinstance(record.get("path"), str):
            problems.append("工作区 %s 缺少 path" % wid)
        if not isinstance(record.get("title"), str):
            problems.append("工作区 %s 缺少 title" % wid)
        if not isinstance(record.get("sessionIds"), list):
            problems.append("工作区 %s 的 sessionIds 非数组" % wid)
        if not isinstance(record.get("createdAt"), str):
            problems.append("工作区 %s 缺少 createdAt（必填，缺失会导致 DSH 整库解析失败）" % wid)
        if not isinstance(record.get("updatedAt"), str):
            problems.append("工作区 %s 缺少 updatedAt（必填，缺失会导致 DSH 整库解析失败）" % wid)
        if wid not in order_set:
            problems.append("工作区 %s 未登记进 global.workspaceIds" % wid)
    return problems


def _collect_index_sessions(idx: dict) -> set[str]:
    """索引里登记的全部会话 id。"""
    sessions: set[str] = set()
    tables = idx.get("tables") if isinstance(idx, dict) else None
    workspaces = tables.get("workspaces") if isinstance(tables, dict) else None
    if isinstance(workspaces, dict):
        for record in workspaces.values():
            if isinstance(record, dict) and isinstance(record.get("sessionIds"), list):
                sessions.update(s for s in record["sessionIds"] if isinstance(s, str))
    return sessions


# --------------------------------------------------------------------------- #
# 检测
# --------------------------------------------------------------------------- #
@dataclass
class DetectResult:
    """一次「检测会话健康」的结果。"""

    sessions_total: int = 0
    ungrouped: list[SessionOnDisk] = field(default_factory=list)
    ungrouped_attachable: int = 0          # 能自动归属（有确定工作区）的数量
    ungrouped_unattachable: int = 0        # 无法确定归属（需人工/zstd）的数量
    index_problems: list[str] = field(default_factory=list)
    index_exists: bool = True
    legacy_replay_sessions: list[str] = field(default_factory=list)
    dup_id_sessions: list[str] = field(default_factory=list)  # 含同 step 重复 tool-call id 的会话
    descriptor_bad_sessions: list[str] = field(default_factory=list)  # 子代理描述符版本不受官方迁移支持、无法加载的会话
    projcache_stale: list = field(default_factory=list)  # 投影缓存结论与日志矛盾的记录（StaleProjcache）
    shadowed_shells: list = field(default_factory=list)  # 空壳高代际遮挡正文的会话（ShadowedShell）
    zstd_name: str = ""
    zstd_missing: bool = False
    #: 子代理关系修复的检出情况（见 :mod:`ai_env_clone.subagent_relink`）：
    #: 可改造的旧顶层子代理会话数、判定不出来被跳过的条数、以及**判定所用的来源**说明。
    relink_targets: list = field(default_factory=list)
    relink_skipped: list = field(default_factory=list)
    relink_source_note: str = ""
    #: 索引里 path 合法、但该目录在本机不存在的**工作区 id**（信息项：记录本身没坏，
    #: 会话仍按该路径归属；不属于「未分组」，也无法从数据推断出正确路径）。
    workspace_paths_missing: list = field(default_factory=list)

    @property
    def healthy(self) -> bool:
        """无未分组、索引无问题、无旧格式 / 重复调用 id / 描述符不兼容 / 投影缓存陈旧 /
        无待改造的旧顶层子代理会话。"""
        return (
            not self.ungrouped
            and not self.index_problems
            and not self.legacy_replay_sessions
            and not self.dup_id_sessions
            and not self.descriptor_bad_sessions
            and not self.projcache_stale
            and not self.shadowed_shells
            and not self.relink_targets
        )

    def summary_lines(self) -> list[str]:
        """面向用户的摘要文本行：**每类可修复问题都给出数量**（为 0 也列出来）。

        为什么每类都列：只列非零项时，用户看到「未分组 0 个」以外一片空白，会以为
        别的检查没跑；列全了才能一眼看清「哪一类有问题、各有多少条」。按用户要求，
        这里**只给数量**，不展开具体是哪几条会话（要细节看修复计划的 dry-run 清单）。
        """
        lines = []
        if not self.index_exists:
            lines.append("⚠ 未找到 storages/workspace.json（索引缺失，会话可能全部显示为未分组）")
        lines.append("会话总数：%d 个" % self.sessions_total)
        if self.ungrouped:
            lines.append(
                "未分组会话：%d 个（其中 %d 个可自动修复归属，%d 个需人工确认）"
                % (len(self.ungrouped), self.ungrouped_attachable, self.ungrouped_unattachable)
            )
        else:
            lines.append("未分组会话：0 个")
        if self.index_problems:
            lines.append("索引问题：%d 处" % len(self.index_problems))
            lines.extend("　- %s" % p for p in self.index_problems)
        else:
            lines.append("索引问题：0 处")
        lines.append("旧格式会话（官方未打补丁的构建无法加载）：%d 个（修复＝把扁平 replayState 升级为信封）"
                     % len(self.legacy_replay_sessions))
        lines.append("重复调用 id 会话（v0→v1 迁移会拒绝）：%d 个（勾选「同时修复重复调用 ID」才处理）"
                     % len(self.dup_id_sessions))
        if self.descriptor_bad_sessions:
            # 与未分组会话取交集：描述符不兼容的会话很可能就是未分组的那批，
            # 标注重叠避免用户误以为「可修复 6 + 不兼容 6 = 12 个」而数量对不上。
            db_ids = set(self.descriptor_bad_sessions)
            overlap = len({s.session_id for s in self.ungrouped} & db_ids)
            same = "" if overlap == 0 else "（其中 %d 个与未分组会话为同一批）" % overlap
            lines.append("子代理描述符版本不合规会话（v0→v1 迁移会拒绝，会话加载报错）："
                         "%d 个%s（修复＝把 version 升到官方要求的 3；载荷不满足 v3 语义的"
                         "会跳过并上报）" % (len(self.descriptor_bad_sessions), same))
        else:
            lines.append("子代理描述符版本不合规会话（会话加载报错）：0 个")
        hidden = sum(1 for s in self.projcache_stale if s.hidden)
        lines.append("投影缓存陈旧会话（缓存映射与日志不符，侧边栏可能看不到 / 标题错）：%d 个"
                     "（其中 %d 个会被侧边栏隐藏；修复＝移走陈旧缓存，由 DSH 冷读时按最新日志重算）"
                     % (len(self.projcache_stale), hidden))
        if self.shadowed_shells:
            lines.append("被空壳遮挡的会话（导入时格式不合规被 DSH 静默重写为空壳，正文仍在低代际日志里）：%d 个"
                         "（修复＝按低代际正文重生成合法日志并移走空壳）" % len(self.shadowed_shells))
        else:
            lines.append("被空壳遮挡的会话：0 个")
        if self.relink_targets:
            lines.append("旧版导入的子代理会话（当前是顶层会话、会被误当用户会话）：%d 个可自动改造为"
                         "原生子代理会话" % len(self.relink_targets))
        else:
            lines.append("旧版导入的子代理会话：0 个可自动改造%s" % self._relink_skip_tail())
        if self.relink_source_note:
            lines.append("外部导入会话的来源（用于判定子代理父子关系）：%s"
                         % self.relink_source_note)
        if self.workspace_paths_missing:
            lines.append("工作区 path 在本机不存在的工作区：%d 个（记录本身完好、会话仍按该"
                         "路径归属；修复＝补建空目录）" % len(self.workspace_paths_missing))
        lines.append("需注意项合计：%d 处（点「修复会话数据」可自动修复其中的可自动项）"
                     % self.attention_total)
        if self.zstd_missing:
            lines.append(
                "注：未安装 zstd 解压支持（zstandard/pyzstd/zstd 命令），"
                "仅做目录级检测；旧格式与精确工作区路径判定受限。"
            )
        return lines

    def _relink_skip_tail(self) -> str:
        """「0 个可自动改造」后面的补充说明。

        跳过分两种，措辞要分清，别让用户以为自己的会话出了问题：
        - **来源里本来就是用户会话**（不是子代理记录）→ 无需改造，属正常；
        - **真的判不出来**（来源里找不到同指纹、父会话不在本机等）→ 保持原样，需要人工看看。
        """
        if not self.relink_skipped:
            return ""
        nothing_to_do = sum(1 for _sid, reason in self.relink_skipped
                            if "无需改造" in reason)
        rest = len(self.relink_skipped) - nothing_to_do
        parts = []
        if nothing_to_do:
            parts.append("其余 %d 条是用户会话，无需改造" % nothing_to_do)
        if rest:
            parts.append("%d 条无法判定父子关系，保持原样" % rest)
        return "（%s）" % "；".join(parts)

    @property
    def attention_total(self) -> int:
        """需要用户注意的问题条目合计（未分组 + 索引问题 + 旧格式 + 重复 id +
        描述符不兼容 + 投影缓存陈旧 + 空壳遮挡 + 旧导入子代理 + 工作区目录缺失）。"""
        return (len(self.ungrouped) + len(self.index_problems)
                + len(self.legacy_replay_sessions) + len(self.dup_id_sessions)
                + len(self.descriptor_bad_sessions) + len(self.projcache_stale)
                + len(self.shadowed_shells) + len(self.relink_targets)
                + len(self.workspace_paths_missing))


def _find_workspace_for_session(
    session: SessionOnDisk,
    workspaces: dict,
    decompress: "Callable[[bytes], bytes] | None",
) -> "tuple[str | None, list[str]]":
    """为未分组会话找归属工作区。

    :param session: 磁盘会话。
    :param workspaces: ``tables.workspaces`` 字典。
    :param decompress: zstd 解压后端（可无）。
    :return: ``(workspace_id | None, 说明列表)``。None 表示无法确定归属。
    """
    reasons: list[str] = []
    if session.cwd:
        # 有 header cwd：按规范化路径精确匹配
        want = _normalize_path(session.cwd)
        for wid, record in workspaces.items():
            if isinstance(record, dict) and _normalize_path(str(record.get("path", ""))) == want:
                return wid, ["cwd 精确匹配工作区 %s" % wid]
        reasons.append("无 path 匹配 cwd=%s 的工作区记录" % session.cwd)
        return None, reasons
    # 无 header：按项目目录名（projectKey）匹配
    matches = [
        wid
        for wid, record in workspaces.items()
        if isinstance(record, dict)
        and isinstance(record.get("path"), str)
        and project_key(record["path"]) == session.project_dir
    ]
    if len(matches) == 1:
        return matches[0], ["目录名匹配工作区 %s" % matches[0]]
    if len(matches) > 1:
        reasons.append("目录名 %s 命中多个工作区（%s），需 header cwd 精确判定" % (session.project_dir, ",".join(matches)))
        return None, reasons
    reasons.append("无 header 且目录名 %s 未命中任何工作区（需 zstd 读取 cwd 才能补建工作区）" % session.project_dir)
    return None, reasons


def detect_ungrouped(
    dsh_home: str,
    decompress: "Callable[[bytes], bytes] | None" = None,
    with_content_check: bool = True,
    relink_source: str = "",
) -> DetectResult:
    """检测未分组会话与索引健康。

    :param with_content_check: 是否顺带做旧格式 replayState 内容扫描
        （需要 zstd；目录级检测始终执行）。
    :param relink_source: 子代理关系修复的来源（留空 = 自动查找，见
        :func:`ai_env_clone.subagent_relink.discover_sources`）；检测结果里会给出
        「可改造几条 + 判定所用的来源」，便于用户在点修复前就知道会改什么。
    """
    home = resolve_dsh_home(dsh_home)
    result = DetectResult()
    if decompress is None:
        decompress, _compress, zstd_name = zstd_backend()
    else:
        zstd_name = "custom"
    result.zstd_name = zstd_name
    result.zstd_missing = not zstd_name

    sessions = scan_sessions(home)
    result.sessions_total = len(sessions)
    # 投影缓存陈旧 / 空壳遮挡都与索引无关：索引缺失（早退分支）时同样要报出来
    if decompress and with_content_check:
        result.projcache_stale = detect_stale_projcache(home, decompress=decompress, sessions=sessions)
        result.shadowed_shells = detect_shadowed_shells(home, decompress=decompress)

    # 子代理关系检出与索引也无关：放在早退分支之前，索引缺失时同样给出。
    from . import subagent_relink

    relink = subagent_relink.plan_subagent_relink(home, relink_source)
    result.relink_targets = relink.targets
    result.relink_skipped = relink.skipped
    result.relink_source_note = relink.source_note or relink.zstd_note

    idx = load_workspace_index(home)
    if idx is None:
        result.index_exists = False
        result.ungrouped = sessions
        result.ungrouped_unattachable = len(sessions)
        return result
    result.index_problems = validate_workspace_index(idx)

    # 信息项：path 合法但目录在本机不存在的工作区（记录没坏、无法从数据推断正确路径）
    result.workspace_paths_missing = [
        wid for wid, rec in (idx.get("tables", {}).get("workspaces") or {}).items()
        if isinstance(rec, dict) and isinstance(rec.get("path"), str) and rec["path"].strip()
        and not os.path.isdir(rec["path"])
    ]

    workspaces = idx.get("tables", {}).get("workspaces", {})
    known = _collect_index_sessions(idx)

    for session in sessions:
        # 内容扫描覆盖全部会话（不只未分组）：已登记的历史会话同样可能带
        # 扁平 replayState，或含官方迁移不支持的子代理描述符版本
        if decompress and with_content_check:
            legacy, dup, descriptor_bad = _scan_file_content(session.log_file, decompress)
            session.legacy_replay = legacy
            session.dup_call_ids = dup
            session.descriptor_bad = descriptor_bad
        if session.descriptor_bad:
            result.descriptor_bad_sessions.append(session.session_id)
        if session.session_id in known:
            continue
        # 尝试读 header（仅当有 zstd）
        if decompress:
            header, err = _read_session_header(session.log_file, decompress)
            session.header = header
            session.header_error = err
        wid, _ = _find_workspace_for_session(session, workspaces, decompress)
        result.ungrouped.append(session)
        if wid is not None:
            result.ungrouped_attachable += 1
        else:
            result.ungrouped_unattachable += 1
        if session.legacy_replay:
            result.legacy_replay_sessions.append(session.session_id)
        if session.dup_call_ids:
            result.dup_id_sessions.append(session.session_id)
    return result


def detect_legacy_replay_state(dsh_home: str) -> list[str]:
    """仅做旧格式检测（需要 zstd），返回受影响会话 id 列表。"""
    decompress, _compress, name = zstd_backend()
    if not decompress:
        return []
    home = resolve_dsh_home(dsh_home)
    out: list[str] = []
    for session in scan_sessions(home):
        header, _ = _read_session_header(session.log_file, decompress)
        if header is None:
            continue
        if _scan_file_content(session.log_file, decompress)[0]:
            out.append(session.session_id)
    return out


def detect_duplicate_call_ids(dsh_home: str) -> list[str]:
    """仅做重复 tool-call id 检测（需要 zstd），返回受影响会话 id 列表。"""
    decompress, _compress, _name = zstd_backend()
    if not decompress:
        return []
    home = resolve_dsh_home(dsh_home)
    out: list[str] = []
    for session in scan_sessions(home):
        if _scan_file_content(session.log_file, decompress)[1]:
            out.append(session.session_id)
    return out


def detect_incompatible_descriptors(dsh_home: str) -> list[str]:
    """仅做子代理描述符兼容性检测（需要 zstd），返回无法加载的会话 id 列表。

    官方 v0→v1 迁移要求 ``subagent/descriptor.version === 3``，旧版本描述符会被
    ``SessionFormatUnsupportedMigrationError`` 拒绝。会话文件修复现在会**顺带把它修好**
    （载荷满足 v3 语义时只改 version；见 :func:`_fix_descriptor_version`），故这里
    在修复后应回到空列表。
    """
    decompress, _compress, _name = zstd_backend()
    if not decompress:
        return []
    home = resolve_dsh_home(dsh_home)
    out: list[str] = []
    for session in scan_sessions(home):
        if _scan_file_content(session.log_file, decompress)[2]:
            out.append(session.session_id)
    return out


# --------------------------------------------------------------------------- #
# 投影缓存陈旧检测（结论与日志矛盾）
#
# 判据只认「缓存结论与日志矛盾」，**不认** seq 落后：DSH 的正常检查点常落在
# 会话中途，`sessionListMetadata.seq` 偏低是常态，据此判定会大面积误报。
#   a) 日志含 `turn/start`，缓存却标 `blank: true`  ⇒ 侧边栏会隐藏该会话；
#   b) 缓存 `title` 与日志推导出的标题不一致           ⇒ 界面显示旧标题 / 未命名。
# 缓存 `identity` 与日志不符时该记录本就被 DSH 读作「不存在」（会话仍可见），
# 不算可见性故障，故不处理。
# --------------------------------------------------------------------------- #
@dataclass
class StaleProjcache:
    """一条「结论与日志矛盾」的投影缓存记录。"""

    session_id: str
    path: str          # 陈旧的缓存记录文件 <sid>.json
    reason: str
    hidden: bool = False  # True：会使侧边栏隐藏该会话（blank 判据）


@dataclass
class ShadowedShell:
    """一个「空壳高代际遮挡正文」的会话（关系校验类受损，方案见从备份包导入方案 ④）。

    DSH 对**不满足关系校验**（seq 不连续 / 缺系统头 / turn-step 不配对）的日志会
    **静默丢弃**并改写成一个只有几行 seed 的空壳 v4；原低代际日志的正文还在磁盘上，
    但加载器按**最高代际**取用 ⇒ 界面看到的就是空会话。修复 = 把低代际正文经
    ``session_migration._dsh_v3_text`` 重生成合法 v3，并把空壳高代际文件移走。
    """

    session_id: str
    session_dir: str
    shadow_file: str        # 空壳高代际日志（最高代际，无 turn/start）
    shadow_version: int
    body_file: str          # 被遮挡的含正文低代际日志
    body_version: int


def _iter_generation_logs(session_dir: str) -> "list[tuple[str, int]]":
    """目录内全部 canonical 代际日志，按代际**降序**（与 ``find_generation_log``
    同一套文件名判定，临时/备份文件天然不匹配）。"""
    try:
        entries = os.listdir(_longpath(session_dir))
    except OSError:
        return []
    out = []
    for name in entries:
        version = parse_generation_log_filename(name)
        if version is None:
            continue
        out.append((os.path.join(session_dir, name), version))
    out.sort(key=lambda kv: -kv[1])
    return out


def _log_has_turn_start(log_file: str, decompress) -> bool:
    """日志里是否含 ``turn/start`` 事件（空壳 seed 没有；正文一定有）。"""
    try:
        text = _read_log_text(log_file, decompress)
    except Exception:  # noqa: BLE001 - 读不了按「没有正文」处理，宁可漏报不误伤
        return False
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except (ValueError, TypeError):
            continue
        if isinstance(entry, dict) and entry.get("type") == "turn/start":
            return True
    return False


def detect_shadowed_shells(
    dsh_home: str, decompress: "Callable[[bytes], bytes] | None" = None
) -> "list[ShadowedShell]":
    """找「最高代际是空壳 seed、低代际里还压着正文」的会话。

    只认**可证明的遮挡**：最高代际日志无 ``turn/start``（空壳），且**低代际**某份
    日志含 ``turn/start``（正文确在）。只有一份日志、或低代际也没有正文的
    「真空会话」**不算**——前者无需修复，后者没有可恢复的内容，标出来只会吓人。
    """
    home = resolve_dsh_home(dsh_home)
    if decompress is None:
        decompress, _compress, _name = zstd_backend()
    if decompress is None:
        return []               # 无 zstd 读不了日志内容，目录级检测不判这一类
    out: list = []
    for s in scan_sessions(home):
        if not s.log_file:
            continue
        logs = _iter_generation_logs(s.path)
        if len(logs) < 2:
            continue
        # scan_sessions 已把 log_file 指到最高代际；这里再对一遍防漂移
        if logs[0][0] != s.log_file:
            continue
        if _log_has_turn_start(s.log_file, decompress):
            continue            # 最高代际有正文：没有遮挡
        for body_file, body_version in logs[1:]:
            if _log_has_turn_start(body_file, decompress):
                out.append(ShadowedShell(
                    session_id=s.session_id, session_dir=s.path,
                    shadow_file=s.log_file, shadow_version=s.log_version,
                    body_file=body_file, body_version=body_version,
                ))
                break           # 取「代际最高的含正文低代际」即可（逐会话一份）
    return out


def _apply_shell_fix(fix: ShadowedShell, decompress, home: str,
                     backup: bool = True) -> dict:
    """执行一份空壳遮挡修复：重生成正文 → 移走空壳 → 移走其投影缓存。

    全程可回退：写盘前把**两份**日志都备份成 ``<文件>.bak.<UTC>``（与
    ``repair_session_file`` 同风格）；重生成直接复用导入写出器
    ``session_migration._dsh_v3_text``（同 sid、保留原 ``createdAt``），它的产物
    已用官方迁移+校验链验证过。缓存记录**移走不代写**（与陈旧投影缓存同一修法）。
    """
    from .session_migration import _dsh_v3_text, _dsh_write_text, _parse_dsh_text

    report = {"session_id": fix.session_id, "ok": False, "backups": [], "error": ""}
    try:
        header, _err = _read_session_header(fix.body_file, decompress)
        created_ms = 0
        cwd_val = ""
        if isinstance(header, dict):
            created_ms = int(header.get("createdAt") or 0)
            cwd_val = header.get("cwd") if isinstance(header.get("cwd"), str) else ""
        # ★ 用本模块的 _read_log_text（跟随调用方注入的解压后端）拿明文，再交给
        # _parse_dsh_text 解析；不走 SessionParser.parse_dsh（它固定走 session_migration
        # 的压缩层，明文日志 / 测试打桩都过不去）。
        session = _parse_dsh_text(_read_log_text(fix.body_file, decompress))
        text, _first_ms, _title = _dsh_v3_text(
            fix.session_id, session, cwd_val,
            parent_session_id=session.parent_source_id,
            created_ms=created_ms,
        )
    except Exception as exc:  # noqa: BLE001 - 单个会话失败不挡其余修复
        report["error"] = "重生成失败：%r" % (exc,)
        return report

    if backup:
        for path in (fix.body_file, fix.shadow_file):
            backup_path = _backup_path(path)
            try:
                shutil.copy2(_longpath(path), _longpath(backup_path))
                report["backups"].append(backup_path)
            except OSError as exc:
                report["error"] = "备份失败（%s），不写盘：%s" % (path, exc)
                return report

    try:
        _dsh_write_text(fix.body_file, text)
    except Exception as exc:  # noqa: BLE001
        report["error"] = "写回正文失败：%r" % (exc,)
        return report
    stamp = _now_iso().replace(":", "").replace(".", "-")
    try:
        os.replace(_longpath(fix.shadow_file),
                   _longpath("%s.bak-%s" % (fix.shadow_file, stamp)))
    except OSError as exc:
        report["error"] = ("正文已重生成，但空壳移走失败（%s）；请手动把 %s 移出会话目录"
                           % (exc, fix.shadow_file))
        return report
    # 该会话若有投影缓存记录，一并移走：重生成后缓存里的 title/blank 都可能过期，
    # 让 DSH 冷读按新日志重算（与陈旧投影缓存同一修法，绝不代写缓存值）。
    cache_record = os.path.join(_projcache_dir(home), fix.session_id + ".json")
    if os.path.isfile(cache_record):
        try:
            os.replace(_longpath(cache_record),
                       _longpath("%s.bak-%s" % (cache_record, stamp)))
        except OSError:
            pass                # 移不走只影响侧边栏首次显示，不算修复失败
    report["ok"] = True
    return report


def _projcache_dir(dsh_home: str) -> str:
    """投影缓存记录目录：``<root>/storages/session_projcache/sessions``。"""
    return os.path.join(
        resolve_dsh_home(dsh_home), "storages", "session_projcache", "sessions"
    )


def _session_log_projection(
    log_file: str, decompress: "Callable[[bytes], bytes] | None"
) -> "tuple[bool, str | None] | None":
    """从日志推导投影事实 ``(是否含 turn/start, 标题)``；日志不可读时返回 None。

    返回 None 表示「判不了」——调用方必须据此跳过该会话，绝不据此判陈旧。
    """
    try:
        text = _read_log_text(log_file, decompress)
    except Exception:  # 缺 zstd / 帧损坏等：判不了，跳过
        return None
    has_turn = False
    title = None
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except (ValueError, TypeError):
            continue
        if not isinstance(event, dict):
            continue
        etype = event.get("type")
        if etype == "turn/start":
            has_turn = True
        elif etype == "session/title":
            title = (event.get("data") or {}).get("title")
    return has_turn, title


def _stale_projcache_reason(
    record, has_turn: bool, title: "str | None"
) -> "tuple[str, bool]":
    """判断缓存记录是否与日志矛盾，返回 ``(原因, 是否隐藏会话)``；不矛盾返回 ``("", False)``。"""
    rows = record.get("rows") if isinstance(record, dict) else None
    if not isinstance(rows, dict):
        return "", False
    meta = rows.get("sessionListMetadata")
    cached_blank = False
    if isinstance(meta, dict) and isinstance(meta.get("val"), dict):
        cached_blank = bool(meta["val"].get("blank"))
    if has_turn and cached_blank:
        return "缓存标记为空会话，但日志含 turn/start（侧边栏会隐藏该会话）", True
    if title is not None:
        title_row = rows.get("title")
        cached_title = title_row.get("val") if isinstance(title_row, dict) else None
        if cached_title != title:
            return (
                "缓存标题 %r 与日志 %r 不一致（界面显示旧标题或未命名）"
                % (cached_title, title),
                False,
            )
    return "", False


def detect_stale_projcache(
    dsh_home: str,
    decompress: "Callable[[bytes], bytes] | None" = None,
    sessions: "list[SessionOnDisk] | None" = None,
) -> list[StaleProjcache]:
    """检测「结论与日志矛盾」的投影缓存记录（只读，不写盘）。

    :param sessions: 预扫的会话列表（避免重复扫盘）；``None`` 则自行扫描。
    :return: 陈旧记录列表；无 zstd 后端或日志不可读者一律跳过（不误报）。
    """
    home = resolve_dsh_home(dsh_home)
    if decompress is None:
        decompress, _compress, _name = zstd_backend()
    if decompress is None:
        return []
    cache_dir = _projcache_dir(home)
    if not os.path.isdir(_longpath(cache_dir)):
        return []
    out: list[StaleProjcache] = []
    for session in scan_sessions(home) if sessions is None else sessions:
        cache_fp = os.path.join(cache_dir, session.session_id + ".json")
        if not os.path.isfile(_longpath(cache_fp)):
            continue
        try:
            with open(_longpath(cache_fp), "r", encoding="utf-8-sig") as fh:
                doc = json.load(fh)
        except (OSError, ValueError):
            continue  # 读不出就当「无缓存」（DSH 亦然），不判定
        record = doc.get("record") if isinstance(doc, dict) else None
        facts = _session_log_projection(session.log_file, decompress)
        if facts is None:
            continue
        reason, hidden = _stale_projcache_reason(record, facts[0], facts[1])
        if not reason:
            continue
        out.append(
            StaleProjcache(
                session_id=session.session_id, path=cache_fp, reason=reason, hidden=hidden
            )
        )
    return out


# --------------------------------------------------------------------------- #
# 修复计划与应用
# --------------------------------------------------------------------------- #
@dataclass
class RepairMutation:
    """一条索引修复动作（均为纯增量，绝不删除）。"""

    action: str          # attach | create-workspace | fix-record
    session_id: str
    workspace_id: str
    path: str = ""       # create-workspace 时的新工作区 path
    title: str = ""      # create-workspace 时的新工作区标题
    detail: str = ""


@dataclass
class RepairPlan:
    """未分组会话的修复计划（dry-run 产物）。"""

    mutations: list[RepairMutation] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (session_id, 原因)

    @property
    def empty(self) -> bool:
        return not self.mutations and not self.skipped

    def describe(self) -> list[str]:
        lines = []
        for m in self.mutations:
            if m.action == "create-workspace":
                lines.append("补建工作区 %s（path=%s）并登记会话 %s" % (m.workspace_id, m.path, m.session_id))
            elif m.action == "mkdir":
                lines.append("补建工作区 %s 缺失的目录（记录里 path 指向、本机不存在）：%s"
                             % (m.workspace_id, m.path))
            elif m.action == "fix-record":
                if m.path:
                    lines.append("补齐工作区记录 %s 缺失的 path（从该工作区会话的 cwd 推断）：%s"
                                 % (m.workspace_id, m.path))
                else:
                    lines.append("补齐工作区记录 %s 的必填字段：%s" % (m.workspace_id, m.detail))
            else:
                lines.append("把会话 %s 加入工作区 %s 的 sessionIds" % (m.session_id, m.workspace_id))
        for sid, reason in self.skipped:
            lines.append("跳过 %s：%s" % (sid, reason))
        return lines


def _cwd_for_workspace(record: dict, sessions_by_id: dict,
                       decompress) -> "tuple[str, str]":
    """从某工作区名下会话的 header ``cwd`` 推断工作区路径。

    :return: ``(推断出的路径, 说明)``；推断不出时路径为空串、说明给出原因。
    """
    ids = record.get("sessionIds") if isinstance(record, dict) else None
    if not isinstance(ids, list) or not ids:
        return "", "该工作区名下没有会话，无从推断"
    found: dict = {}
    for sid in ids:
        session = sessions_by_id.get(sid)
        if session is None or not session.log_file or decompress is None:
            continue
        header, _err = (session.header, "") if session.header else \
            _read_session_header(session.log_file, decompress)
        if not isinstance(header, dict):
            continue
        cwd = header.get("cwd")
        if isinstance(cwd, str) and cwd.strip():
            found[cwd] = found.get(cwd, 0) + 1
    if not found:
        return "", "名下会话都没有可读的 header cwd"
    best = sorted(found.items(), key=lambda kv: (-kv[1], kv[0]))
    if len(found) > 1:
        return "", ("名下会话的 cwd 不一致（%s），不猜"
                    % "、".join("%s×%d" % (p, n) for p, n in best[:3]))
    path, count = best[0]
    return path, "名下 %d 个会话的 cwd 一致：%s" % (count, path)


def _missing_workspace_paths(workspaces: dict) -> list:
    """找「记录里 path 合法、但目录在本机不存在」的工作区，返回 ``[(wid, path)]``。

    为什么可以补建：这类记录本身完好（会话也按该路径归属），只是目录不在了；DSH 在
    侧边栏里对这类工作区会提示路径不可用。补建空目录是**纯增量**动作，不碰任何会话
    数据，也不要权限就能回退（把空目录删掉即可）。只处理**绝对路径**：记录里若出现
    相对路径，含义不确定，宁可不动。
    """
    out = []
    for wid, record in (workspaces or {}).items():
        if not isinstance(record, dict):
            continue
        path = record.get("path")
        if not isinstance(path, str) or not path.strip():
            continue
        if not os.path.isabs(path):
            continue
        if os.path.isdir(path):
            continue
        out.append((wid, path))
    return out


def plan_workspace_index_repair(
    dsh_home: str, decompress: "Callable[[bytes], bytes] | None" = None
) -> "tuple[RepairPlan, dict | None]":
    """生成未分组会话的修复计划（不写盘）。

    :return: ``(plan, idx)``；idx 为读入的 workspace.json（无则 None）。
    """
    home = resolve_dsh_home(dsh_home)
    plan = RepairPlan()
    idx = load_workspace_index(home)
    if idx is None:
        plan.skipped.append(("（全部）", "workspace.json 缺失，无法修复索引"))
        return plan, None
    workspaces = idx.get("tables", {}).get("workspaces", {})
    if not isinstance(workspaces, dict):
        plan.skipped.append(("（全部）", "tables.workspaces 缺失，无法修复索引"))
        return plan, idx
    known = _collect_index_sessions(idx)
    if decompress is None:
        decompress, _compress, _name = zstd_backend()
    # 会话清单要在「记录字段完整性」之前就备好：工作区 path 缺失时要从名下会话的
    # header cwd 推断（见 _cwd_for_workspace）。
    sessions = scan_sessions(home)
    sessions_by_id = {s.session_id: s for s in sessions}

    # 「记录里的 path 指向的目录不存在」→ 计划里补建该目录（纯增量，不碰会话数据）
    for wid, path in _missing_workspace_paths(workspaces):
        plan.mutations.append(
            RepairMutation(action="mkdir", session_id="", workspace_id=wid, path=path,
                           detail="记录里 path 指向的目录在本机不存在，补建空目录")
        )

    # 记录字段完整性：path/title/sessionIds/createdAt/updatedAt 是 DSH 存储边界的
    # zod 必填字段（均无默认值），缺任一字段会让整个 workspace 域解析失败、桌面端
    # 零工作区零会话可见。该修复与「未分组会话」无关，故独立于下面的会话遍历。
    global_state = idx.get("global")
    order = global_state.get("workspaceIds") if isinstance(global_state, dict) else None
    order_set = set(order) if isinstance(order, list) else set()
    for wid, record in workspaces.items():
        if not isinstance(record, dict):
            plan.mutations.append(
                RepairMutation(
                    action="fix-record", session_id="", workspace_id=wid,
                    detail="记录不是对象，重建为最小合法记录",
                )
            )
            continue
        missing = []
        path_bad = not isinstance(record.get("path"), str)
        if path_bad:
            missing.append("path")
        if not isinstance(record.get("title"), str):
            missing.append("title")
        if not isinstance(record.get("sessionIds"), list):
            missing.append("sessionIds")
        if not isinstance(record.get("createdAt"), str):
            missing.append("createdAt")
        if not isinstance(record.get("updatedAt"), str):
            missing.append("updatedAt")
        if wid not in order_set:
            missing.append("global.workspaceIds 登记")
        if missing:
            # path 缺失/为空是「工作区路径缺失」里唯一能**推断**出来的情形：记录里
            # sessionIds 指的会话 header 都带 cwd，一致即可作为该工作区的路径。
            # （path 已有合法值但目录不在本机，不属于索引损坏，不去改它。）
            inferred, reason = ("", "")
            if path_bad:
                inferred, reason = _cwd_for_workspace(record, sessions_by_id, decompress)
                if not inferred:
                    plan.skipped.append(
                        (wid, "工作区 path 缺失且推断不出（%s），只补空串占位" % reason))
            plan.mutations.append(
                RepairMutation(
                    action="fix-record", session_id="", workspace_id=wid,
                    detail=("补齐 " + "、".join(missing)) if not inferred else
                           ("补齐 path：" + reason),
                    path=inferred,
                )
            )

    for session in sessions:
        if session.session_id in known:
            continue
        if session.project_dir == NO_CWD_PROJECT:
            plan.skipped.append((session.session_id, "无 cwd 会话（_no-cwd），无法确定归属，需人工处理"))
            continue
        if decompress and session.header is None:
            session.header, session.header_error = _read_session_header(session.log_file, decompress)
        wid, reasons = _find_workspace_for_session(session, workspaces, decompress)
        if wid is not None:
            record = workspaces[wid]
            if session.session_id in record.get("sessionIds", []):
                continue
            plan.mutations.append(
                RepairMutation(
                    action="attach",
                    session_id=session.session_id,
                    workspace_id=wid,
                    detail="; ".join(reasons),
                )
            )
            continue
        if session.cwd:
            # 有 header cwd 但无匹配记录 → 补建工作区（等价官方 bootstrap）
            title = os.path.basename(os.path.normpath(session.cwd)) or session.cwd
            plan.mutations.append(
                RepairMutation(
                    action="create-workspace",
                    session_id=session.session_id,
                    workspace_id=str(uuid.uuid4()),
                    path=session.cwd,
                    title=title,
                    detail="; ".join(reasons),
                )
            )
            continue
        plan.skipped.append((session.session_id, "; ".join(reasons) or "无法确定归属"))
    return plan, idx


def apply_workspace_index_repair(
    dsh_home: str,
    plan: RepairPlan,
    dry_run: bool = True,
    backup: bool = True,
    idx: "dict | None" = None,
) -> "ApplyResult":
    """执行修复计划（默认 dry-run；``backup=True`` 时先备份原文件）。

    :param dry_run: True 时只计算不写盘。
    :param backup: 写盘前把原文件复制为 ``workspace.json.bak-<utc>``。
    :param idx: 预先读入的索引（避免重复读盘）。
    """
    home = resolve_dsh_home(dsh_home)
    file = os.path.join(home, "storages", "workspace.json")
    if idx is None:
        idx = load_workspace_index(home)
    backup_path = ""
    # 只有「跳过」没有实际修改时同样不写盘、不备份（避免无谓重写文件）
    if not plan.mutations:
        return ApplyResult(file=file, backup_path="", applied=0, dry_run=dry_run, ok=True)

    if dry_run:
        return ApplyResult(file=file, backup_path="", applied=len(plan.mutations), dry_run=True, ok=True)

    if idx is None:
        return ApplyResult(file=file, backup_path="", applied=0, dry_run=False, ok=False, error="workspace.json 缺失")

    # 先做备份（同一目录，UTC 时间戳，与 dsh-session-surgeon 的 .bak.<utc> 同风格）
    if backup:
        backup_path = "%s.bak-%s" % (file, _now_iso().replace(":", "").replace(".", "-"))
        try:
            with open(_longpath(file), "rb") as fh:
                data = fh.read()
            with open(_longpath(backup_path), "wb") as fh:
                fh.write(data)
        except OSError as exc:
            return ApplyResult(file=file, backup_path="", applied=0, dry_run=False, ok=False, error="备份失败: %s" % exc)

    workspaces = idx.setdefault("tables", {}).setdefault("workspaces", {})
    if not isinstance(workspaces, dict):
        return ApplyResult(file=file, backup_path=backup_path, applied=0, dry_run=False, ok=False, error="tables.workspaces 缺失")

    global_state = idx.setdefault("global", {})
    if not isinstance(global_state, dict):
        global_state = {}
        idx["global"] = global_state
    if not isinstance(global_state.get("workspaceIds"), list):
        global_state["workspaceIds"] = []

    now = _now_iso()
    applied = 0
    created: list = []
    for m in plan.mutations:
        if m.action == "mkdir":
            # 补建工作区目录：与索引内容无关的纯增量动作；失败只记录、不中断
            try:
                os.makedirs(_longpath(m.path), exist_ok=True)
                created.append({"workspace_id": m.workspace_id, "path": m.path,
                                "ok": True, "error": ""})
            except OSError as exc:
                created.append({"workspace_id": m.workspace_id, "path": m.path,
                                "ok": False, "error": str(exc)})
            continue
        if m.action == "fix-record":
            # 补齐记录必填字段（只增不改：已有合法值一律保留）
            record = workspaces.get(m.workspace_id)
            if not isinstance(record, dict):
                record = workspaces[m.workspace_id] = {}
            if not isinstance(record.get("path"), str):
                # 计划里推断出了真实路径就用它，否则只能补空串占位（见计划里的跳过说明）
                record["path"] = m.path or ""
            if not isinstance(record.get("title"), str) or not record.get("title"):
                record["title"] = os.path.basename(record["path"].rstrip("\\/")) or "workspace"
            if not isinstance(record.get("sessionIds"), list):
                record["sessionIds"] = []
            if not isinstance(record.get("createdAt"), str):
                record["createdAt"] = now
            if not isinstance(record.get("updatedAt"), str):
                record["updatedAt"] = now
            if m.workspace_id not in global_state["workspaceIds"]:
                global_state["workspaceIds"].append(m.workspace_id)
            applied += 1
            continue
        if m.action == "create-workspace":
            if m.workspace_id not in workspaces:
                workspaces[m.workspace_id] = {
                    "path": m.path,
                    "title": m.title,
                    "sessionIds": [m.session_id],
                    "createdAt": now,
                    "updatedAt": now,
                }
            else:
                # 已存在则按 attach 处理（理论上不会发生）
                workspaces[m.workspace_id].setdefault("sessionIds", [])
                if m.session_id not in workspaces[m.workspace_id]["sessionIds"]:
                    workspaces[m.workspace_id]["sessionIds"].insert(0, m.session_id)
                workspaces[m.workspace_id]["updatedAt"] = now
            if m.workspace_id not in global_state["workspaceIds"]:
                global_state["workspaceIds"].insert(0, m.workspace_id)
            applied += 1
            continue
        # attach
        record = workspaces.get(m.workspace_id)
        if not isinstance(record, dict):
            continue
        record.setdefault("sessionIds", [])
        if m.session_id not in record["sessionIds"]:
            record["sessionIds"].insert(0, m.session_id)  # 官方 attach 语义：置顶
        record["updatedAt"] = now
        applied += 1

    # 原子写：临时文件 + os.replace（Windows 同目录原子替换）
    try:
        os.makedirs(os.path.dirname(_longpath(file)), exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            prefix="workspace.json.", suffix=".tmp", dir=os.path.dirname(_longpath(file))
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(idx, fh, ensure_ascii=False, indent=2)
                fh.write("\n")
            os.replace(_longpath(tmp), _longpath(file))
        except BaseException:
            try:
                os.unlink(_longpath(tmp))
            except OSError:
                pass
            raise
    except OSError as exc:
        return ApplyResult(file=file, backup_path=backup_path, applied=0, dry_run=False, ok=False, error="写盘失败: %s" % exc)

    return ApplyResult(file=file, backup_path=backup_path, applied=applied, dry_run=False, ok=True,
                       created=created)


@dataclass
class ApplyResult:
    """修复应用结果。"""

    file: str
    backup_path: str
    applied: int
    dry_run: bool
    ok: bool
    error: str = ""
    #: ``mkdir`` 动作逐条结果：``[{"workspace_id","path","ok","error"}]``
    created: list = field(default_factory=list)


# --------------------------------------------------------------------------- #
# 会话文件内容修复（扁平 replayState 升级为信封 / 重复 tool-call id 去重）
#
# replayState 升级依据的是 DSH 官方校验器的两道不变式（见「规则一」注释），
# 并以「未打补丁的官方 DSH 真实加载」为验收标准（``.verify/`` 验证台），
# 不再以 ``第三方修复脚本/tools/会话数据修复.mjs`` 的行为为准——该脚本
# 只升级 message.source 一侧、漏掉内嵌 stream 的 finish 块，正是它把
# 3 个会话改坏成「replay state disagrees with its embedded stream」的原因。
#
# 重复 tool-call id 去重会改写数据内容（id 被加 ``#n`` 后缀），因此默认关闭，
# 需显式传入 ``fix_dup_call_ids=True``（CLI 为 ``--fix-dup-call-ids``）。
# --------------------------------------------------------------------------- #
#: 官方拒绝扁平 replayState 时抛的校验错误特征（用户可读说明用）
FLAT_REPLAY_REFUSAL_HINT = 'chunk replayState has unexpected member "kind"'

#: v2 紧凑行：同一行内含多 chunk，此时序号必然不连续（不算问题）
_PACKED_ROW_TYPES = ("text-chunks", "reasoning-chunks", "tool-call-chunks")

#: 会话日志文件名形态：``session.jsonl.zstd`` / ``session.vN.jsonl.zstd`` / 明文
_SESSION_FILE_NAME_RE = re.compile(r"^session(?:\.v\d+)?\.jsonl(?:\.zstd)?$")

_ZSTD_MISSING_DETAIL = "解压会话文件需要 zstd 后端（zstandard / pyzstd / zstd 命令），当前环境不可用"


def _is_zstd_missing(report: dict) -> bool:
    """报告是否仅因「缺少 zstd 后端」而无法处理（区别于真正的数据损坏）。"""
    problems = report.get("problems") or []
    return report.get("status") == "拒绝" and bool(problems) and all(
        p.get("code") == "缺少zstd" for p in problems
    )


# --------------------------------------------------------------------------- #
# 规则一：扁平 replayState 升级为信封（双侧一致）
#
# DSH 对 replayState 有两道独立校验，升级必须同时满足：
#   a) 格式校验（session-format 的 replayEnvelopeValue）：finish 块携带的
#      replayState 若不是「含 response 成员的信封」，未打补丁的官方构建直接
#      拒绝 —— 这就是旧会话「无法加载」的真正机制；
#   b) 镜像校验（core/session 与 v1→v2 校验）：message.source.replayState
#      必须与「从内嵌 stream 重组出来的 replayState」完全相等，而后者就是
#      finish 块里那份的原样回显（assembler.ts ③ 分支）。
# 因此同一事件里出现几处，就必须升级成**同一个值**：只改 message.source 一侧、
# 不改内嵌 stream 的 finish 块，必然触发
# 「replay state disagrees with its embedded stream」。
#
# v0 形态没有内嵌 stream（chunk 是独立事件），只有 source 一侧；迁移器会把
# source 的值原样复制进合成 stream 的 finish 块，故单侧升级即可。
# --------------------------------------------------------------------------- #
def _wrap_flat_replay_state(value) -> "tuple[object, bool]":
    """把包络拆分前的扁平 replayState 升级为 ``{response, blocks}`` 信封。

    除 ``blocks`` 外的字段整体挪进 ``response`` 半区（适配器私有值，官方校验
    不检查其内部形态）；``blocks`` 原样保留在共享半区。不修改入参。
    已含 ``response`` 的信封直接跳过（幂等）。
    """
    if not _is_flat_replay_state(value):
        return value, False
    response = {k: v for k, v in value.items() if k != "blocks"}
    if "blocks" in value:
        return {"response": response, "blocks": value["blocks"]}, True
    return {"response": response}, True


def _finish_reason_kind(chunk) -> "str | None":
    """finish 块的 ``reason.kind``（非 finish 块或缺 reason 返回 None）。"""
    if not isinstance(chunk, dict) or chunk.get("type") != "finish":
        return None
    reason = chunk.get("reason")
    if isinstance(reason, dict) and isinstance(reason.get("kind"), str):
        return reason["kind"]
    return None


def _pruning_would_drop_block(value) -> bool:
    """max-tokens 剪枝是否会真的丢块（replay 元数据里是否有 tool-call 项）。"""
    blocks = value.get("blocks") if isinstance(value, dict) else None
    if not isinstance(blocks, list):
        return False
    return any(isinstance(b, dict) and b.get("type") == "tool-call" for b in blocks)


def _wrap_holder(holder: dict, key: str, hits: list[dict], path: str, rewrite: bool) -> dict:
    """在持有者对象的某个键上升级 replayState；命中则记录，``rewrite`` 才改。"""
    if not isinstance(holder, dict) or key not in holder:
        return holder
    current = holder[key]
    if not _is_flat_replay_state(current):
        return holder
    hits.append({"path": path, "fields": list(current.keys())})
    if not rewrite:
        return holder
    new_value, _ = _wrap_flat_replay_state(current)
    return {**holder, key: new_value}


def _wrap_stream_record(record, hits: list[dict], skipped: list[dict], rewrite: bool) -> "tuple[object, bool]":
    """升级内嵌 stream 里单个记录的 finish 块 replayState。

    :return: ``(新记录或原记录, 是否有变更)``。
    max-tokens 剪枝场景（reason 为 max-tokens 且元数据含 tool-call 项）需要
    stream 侧与 source 侧取**不同**的值（前者保留全量、后者随内容剪枝），
    单靠本工具无法安全判定，跳过并上报，绝不猜测改写。
    """
    if not isinstance(record, dict) or not isinstance(record.get("chunk"), dict):
        return record, False
    chunk = record["chunk"]
    current = chunk.get("replayState")
    if not _is_flat_replay_state(current):
        return record, False
    if _finish_reason_kind(chunk) == "max-tokens" and _pruning_would_drop_block(current):
        skipped.append(
            {
                "path": "data.stream[].chunk.replayState",
                "reason": "max-tokens 剪枝需要 stream 侧与 source 侧取不同值，暂不支持自动升级",
                "fields": list(current.keys()),
            }
        )
        return record, False
    new_chunk = _wrap_holder(chunk, "replayState", hits, "data.stream[].chunk.replayState", rewrite)
    if new_chunk is not chunk:
        return {**record, "chunk": new_chunk}, True
    return record, False


#: 子代理描述符版本规则的命中标记：汇总时据此与 replayState 命中区分开。
DESCRIPTOR_RULE = "subagent-descriptor"


def _descriptor_v3_problem(data: dict) -> str:
    """按官方 v3 语义检查子代理描述符载荷；可安全升级时返回空串。

    与 DSH 安装包内的 ``subagentDescriptorValue`` 同规则：``version`` 必须为 **3**；
    ``provider`` 必须非空字符串；``mode`` 为 ``one-shot`` 时 ``label`` 可选，为
    ``continuable`` 时要求非空 ``label``、且 ``agentProvider`` 与 ``agentModel``
    必须成对出现；``toolFilter`` 只能含 ``allow`` / ``deny``（各自为非空字符串数组）。
    """
    provider = data.get("provider")
    if not isinstance(provider, str) or not provider:
        return "provider 不是非空字符串"
    mode = data.get("mode")
    if mode == "one-shot":
        label = data.get("label")
        return "" if label is None or isinstance(label, str) else "label 类型不是字符串"
    if mode != "continuable":
        return "mode=%r 既不是 one-shot 也不是 continuable" % (mode,)
    label = data.get("label")
    if not isinstance(label, str) or not label:
        return "continuable 模式要求非空 label"
    for key in ("agentProvider", "agentModel", "agentReasoningEffort", "persona"):
        if key in data and (not isinstance(data[key], str) or not data[key]):
            return "%s 不是非空字符串" % key
    if ("agentProvider" in data) != ("agentModel" in data):
        return "agentProvider 与 agentModel 必须成对出现"
    tool_filter = data.get("toolFilter")
    if tool_filter is not None:
        if not isinstance(tool_filter, dict) or not tool_filter \
                or set(tool_filter) - {"allow", "deny"}:
            return "toolFilter 形态不符合官方要求（只能含 allow / deny）"
        for key, value in tool_filter.items():
            if not isinstance(value, list) or not value \
                    or any(not isinstance(x, str) or not x for x in value):
                return "toolFilter.%s 必须是非空字符串数组" % key
    return ""


def _fix_descriptor_version(event, data: dict, rewrite: bool,
                            hits: list, skipped: list) -> "dict | None":
    """规则三：把 ``subagent/descriptor`` 的 ``version`` 升级为 3（官方硬要求）。

    旧构建写过 ``version: 2``（实测 TeaVision 的 6 个 v0 子代理会话）：released v0→v1
    迁移对描述符版本是**硬校验**——不等于 3 就直接拒绝整份日志，界面表现就是「会话
    加载错误」。载荷本身与 v3 语义一致时，只改这一个字段即可让会话恢复加载；载荷
    不满足 v3 语义时**跳过并上报**，绝不拼凑一个语义不同的描述符。
    """
    if not isinstance(event, dict) or event.get("type") != "subagent/descriptor":
        return None
    version = data.get("version")
    if isinstance(version, bool) or not isinstance(version, (int, float)) or version == 3:
        return None                      # 已是 3（或类型非法，交给官方校验报错，不猜）
    problem = _descriptor_v3_problem(data)
    if problem:
        skipped.append({
            "rule": DESCRIPTOR_RULE, "path": "subagent/descriptor.data",
            "reason": "描述符载荷不满足 version 3 的官方语义（%s），未改动" % problem,
            "fields": sorted(data.keys()),
        })
        return None
    hits.append({
        "rule": DESCRIPTOR_RULE, "path": "subagent/descriptor.data.version",
        "from": version, "to": 3, "fields": sorted(data.keys()),
    })
    if not rewrite:
        return None
    return {**data, "version": 3}


def wrap_line_event(event, rewrite: bool, finish_kind: "str | None" = None) -> "tuple[object, bool, list[dict], list[dict]]":
    """处理单个事件行的 replayState 升级（无跨行状态）。

    :param finish_kind: v0 形态（无内嵌 stream）事件对应 step 的 finish 原因，
        由调用方从相邻的 ``assistant/chunk`` finish 事件追踪而来；仅用于
        max-tokens 守卫。v2 形态从本行内嵌 stream 自取，无需传入。
    :return: ``(新事件或原事件, 是否有变更, 命中列表, 跳过列表)``。
    作用位置（同一事件内有几处就升级成同一个值）：
      ``data.stream[].chunk.replayState``（v2：assistant/message 与
      assistant/attempt）、``assistant/chunk.data.chunk.replayState``（v0）、
      ``assistant/message.data.message.source.replayState``。
    """
    hits: list[dict] = []
    skipped: list[dict] = []
    if not isinstance(event, dict) or not isinstance(event.get("data"), dict):
        return event, False, hits, skipped
    data = event["data"]
    changed = False

    # ⓪ 子代理描述符版本升级（与 replayState 无关，独立判定）
    new_descriptor = _fix_descriptor_version(event, data, rewrite, hits, skipped)
    if new_descriptor is not None:
        data = new_descriptor
        changed = True

    # ① 内嵌 stream 的 finish 块（v2 形态；assistant/message 与 assistant/attempt 通用）
    stream = data.get("stream")
    if isinstance(stream, list):
        new_stream = []
        stream_changed = False
        for record in stream:
            new_record, record_changed = _wrap_stream_record(record, hits, skipped, rewrite)
            new_stream.append(new_record)
            stream_changed = stream_changed or record_changed
        if stream_changed:
            data = {**data, "stream": new_stream}
            changed = True

    # ② assistant/chunk 的 finish 块（v0 形态）
    if event.get("type") == "assistant/chunk" and isinstance(data.get("chunk"), dict):
        chunk = data["chunk"]
        current = chunk.get("replayState")
        if (
            _finish_reason_kind(chunk) == "max-tokens"
            and _is_flat_replay_state(current)
            and _pruning_would_drop_block(current)
        ):
            skipped.append(
                {
                    "path": "assistant/chunk.data.chunk.replayState",
                    "reason": "max-tokens 剪枝场景暂不支持自动升级",
                    "fields": list(current.keys()),
                }
            )
        else:
            new_chunk = _wrap_holder(
                data["chunk"], "replayState", hits, "assistant/chunk.data.chunk.replayState", rewrite
            )
            if new_chunk is not data["chunk"]:
                data = {**data, "chunk": new_chunk}
                changed = True

    # ③ assistant/message 的 message.source.replayState
    if (
        event.get("type") == "assistant/message"
        and isinstance(data.get("message"), dict)
        and isinstance(data["message"].get("source"), dict)
    ):
        source = data["message"]["source"]
        current = source.get("replayState")
        if _is_flat_replay_state(current) and _pruning_would_drop_block(current) and (
            finish_kind == "max-tokens" or _event_finish_kinds(data) == ["max-tokens"]
        ):
            skipped.append(
                {
                    "path": "assistant/message.data.message.source.replayState",
                    "reason": "max-tokens 剪枝需要 source 侧随内容剪枝、stream 侧保留全量，暂不支持自动升级",
                    "fields": list(current.keys()),
                }
            )
        else:
            new_source = _wrap_holder(
                source,
                "replayState",
                hits,
                "assistant/message.data.message.source.replayState",
                rewrite,
            )
            if new_source is not source:
                data = {**data, "message": {**data["message"], "source": new_source}}
                changed = True

    for hit in hits:
        hit["seq"] = event.get("seq")
    for item in skipped:
        item["seq"] = event.get("seq")
    if not changed:
        return event, False, hits, skipped
    return {**event, "data": data}, True, hits, skipped


def _event_finish_kinds(data: dict) -> list:
    """收集本事件内嵌 stream 里全部 finish 块的 reason.kind（v2 形态用）。"""
    kinds = []
    stream = data.get("stream")
    if isinstance(stream, list):
        for record in stream:
            if isinstance(record, dict):
                kind = _finish_reason_kind(record.get("chunk"))
                if kind is not None:
                    kinds.append(kind)
    return kinds


# --------------------------------------------------------------------------- #
# 规则二：同一 step 内重复 tool-call id 去重（有语义改动，默认关闭）
# --------------------------------------------------------------------------- #
def _tool_call_block_id(block) -> "str | None":
    """取 tool-call 块的调用 id（``id`` 优先，兼容 ``callId``）。"""
    if not isinstance(block, dict):
        return None
    for key in ("id", "callId"):
        if isinstance(block.get(key), str):
            return block[key]
    return None


def _assistant_tool_call_blocks(event) -> list:
    """取 assistant/message 的 content 列表（非 assistant/message 返回空）。"""
    if not isinstance(event, dict) or event.get("type") != "assistant/message":
        return []
    data = event.get("data")
    message = data.get("message") if isinstance(data, dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    return content if isinstance(content, list) else []


def _is_lifecycle_reset(event_type) -> bool:
    """生命周期重置事件：清空「同一步」判定状态。"""
    return event_type in ("turn/start", "step/end")


def _duplicate_advertised_ids(events: list) -> list[dict]:
    """检测同一 step 内重复宣告的 tool-call id（Set 语义，只报检测）。"""
    hits: list[dict] = []
    advertised: set[str] = set()
    for event in events:
        if not isinstance(event, dict):
            continue
        event_type = event.get("type")
        if _is_lifecycle_reset(event_type):
            advertised.clear()
        for block in _assistant_tool_call_blocks(event):
            cid = _tool_call_block_id(block)
            if not isinstance(cid, str) or cid == "" or not isinstance(block, dict):
                continue
            if block.get("type") != "tool-call":
                continue
            if cid in advertised:
                hits.append({"seq": event.get("seq"), "callId": cid})
            advertised.add(cid)
    return hits


def disambiguate_duplicate_call_ids(events: list) -> "tuple[dict[int, dict], list[dict], int]":
    """同一 step 内重复宣告的 tool-call id 去重修复（**有语义改动**）。

    规则（与 dsh-session-surgeon 的 ``disambiguateDuplicateToolCallIds`` 同语义）：

    - 保留首个宣告的 id，同一步内后续重复的改写为 ``<id>#<n>``（n 从 2 起）；
    - 按出现顺序把 ``tool/call.callId``、``tool/result.message.source.callId``
      重映射到改写后的 id（游标消费宣告列表）；
    - ``turn/start`` / ``step/end`` 视为新一步，清空状态重新计数；
    - 只有确实重复才改写，空 id 从不凭空生成。

    :return: ``(改写{事件下标: 新事件}, 命中列表, 改写事件数)``。
    """
    rewrites: dict[int, dict] = {}
    hits: list[dict] = []
    advertised: dict[str, int] = {}
    orig_order: dict[str, list[str]] = {}
    call_cursor: dict[str, int] = {}
    result_cursor: dict[str, int] = {}

    def _reset() -> None:
        nonlocal advertised, orig_order, call_cursor, result_cursor
        advertised = {}
        orig_order = {}
        call_cursor = {}
        result_cursor = {}

    for index, event in enumerate(events):
        if not isinstance(event, dict):
            continue
        event_type = event.get("type")
        if _is_lifecycle_reset(event_type):
            _reset()

        if event_type == "assistant/message":
            content = _assistant_tool_call_blocks(event)
            if not content:
                continue
            new_content: list | None = None
            for pos, block in enumerate(content):
                if not isinstance(block, dict) or block.get("type") != "tool-call":
                    continue
                cid = _tool_call_block_id(block)
                if not isinstance(cid, str) or cid == "":
                    continue
                final_id = cid
                if cid in advertised:
                    number = advertised[cid] + 1
                    advertised[cid] = number
                    final_id = "%s#%d" % (cid, number)
                    if new_content is None:
                        new_content = list(content)
                    new_content[pos] = {**block, "id": final_id}
                    hits.append({"seq": event.get("seq"), "callId": cid, "new_id": final_id})
                else:
                    advertised[cid] = 1
                orig_order.setdefault(cid, []).append(final_id)
            if new_content is not None:
                data = event["data"]
                message = data.get("message")
                rewrites[index] = {
                    **event,
                    "data": {**data, "message": {**message, "content": new_content}},
                }
            continue

        if event_type == "tool/call":
            data = event.get("data")
            cid = data.get("callId") if isinstance(data, dict) else None
            if not isinstance(cid, str) or cid == "" or cid not in orig_order:
                continue
            entries = orig_order[cid]
            cursor = call_cursor.get(cid, 0)
            if cursor >= len(entries):
                continue
            final_id = entries[cursor]
            call_cursor[cid] = cursor + 1
            if final_id != cid:
                rewrites[index] = {**event, "data": {**data, "callId": final_id}}
            continue

        if event_type == "tool/result":
            data = event.get("data")
            message = data.get("message") if isinstance(data, dict) else None
            source = message.get("source") if isinstance(message, dict) else None
            cid = source.get("callId") if isinstance(source, dict) else None
            if not isinstance(cid, str) or cid == "" or cid not in orig_order:
                continue
            entries = orig_order[cid]
            cursor = result_cursor.get(cid, 0)
            if cursor >= len(entries):
                continue
            final_id = entries[cursor]
            result_cursor[cid] = cursor + 1
            if final_id != cid:
                rewrites[index] = {
                    **event,
                    "data": {
                        **data,
                        "message": {**message, "source": {**source, "callId": final_id}},
                    },
                }

    return rewrites, hits, len(rewrites)


# --------------------------------------------------------------------------- #
# 其他已知会让加载失败的形态（仅报告，不自动修）
# --------------------------------------------------------------------------- #
def detect_other_problems(events: list) -> list[dict]:
    """汇总会话数据的其他已知问题（只报告，不自动修）。

    - ``重复的tool-call id``：同一步内重复宣告（开启去重修复后不再重复报告）；
    - ``seq 不连续``：序号缺口（存在紧凑行时跳过判定，因为紧凑行必然跳号）。
    """
    problems: list[dict] = []
    dup_hits = _duplicate_advertised_ids(events)
    if dup_hits:
        detail = ", ".join("%s@seq%s" % (h["callId"], h["seq"]) for h in dup_hits)
        problems.append(
            {
                "code": "重复的tool-call id",
                "detail": "同一 step 内重复宣告 tool-call id（v0→v1 迁移会拒绝）：%s" % detail,
                "entries": dup_hits,
            }
        )
    has_packed = any(
        isinstance(e, dict) and e.get("type") in _PACKED_ROW_TYPES for e in events
    )
    if not has_packed:
        seqs = [
            e.get("seq")
            for e in events
            if isinstance(e, dict) and isinstance(e.get("seq"), int) and not isinstance(e.get("seq"), bool)
        ]
        gaps: list[str] = []
        for i in range(1, len(seqs)):
            if seqs[i] != seqs[i - 1] + 1:
                gaps.append("%s→%s" % (seqs[i - 1], seqs[i]))
        if gaps:
            shown = gaps[:8]
            problems.append(
                {
                    "code": "seq 不连续",
                    "detail": "序号缺口：%s%s" % (", ".join(shown), " …" if len(gaps) > 8 else ""),
                }
            )
    return problems


# --------------------------------------------------------------------------- #
# 行级处理与文件级修复
# --------------------------------------------------------------------------- #
def _split_jsonl_lines(text: str) -> "tuple[list[str], bool]":
    """按物理行拆分（保留尾部换行标记，便于回写时逐字节还原）。"""
    if text == "":
        return [], False
    has_trailing = text.endswith("\n")
    body = text[:-1] if has_trailing else text
    return body.split("\n"), has_trailing


def _new_session_report() -> dict:
    """新建一份会话文件修复报告（字段与 第三方修复脚本 的返回结构一致）。"""
    return {
        "file": "",
        "status": "正常",          # 正常 | 需要修复 | 已修复 | 拒绝
        "plain": False,            # 是否为明文 JSONL（非 zstd）
        "frames": 0,               # zstd 帧数
        "torn_tail": False,        # 尾部存在写断的残留字节
        "format_version": None,    # header 的 version
        "session_id": None,        # header 的 id
        "hits": [],                # replayState 升级命中 [{seq, path, fields}]
        "skipped": [],             # 主动跳过未升级的命中 [{seq, path, reason, fields}]
        "descriptor_hits": [],     # 子代理描述符版本升级命中 [{seq, path, from, to}]
        "descriptor_skipped": [],  # 描述符载荷不满足 v3 语义、跳过未改的命中
        "actions": [],             # 已识别的修复动作 [{rule, count, detail}]
        "problems": [],            # 其他问题（仅报告）
        "dup_fixes": [],           # 重复调用 id 去重命中 [{seq, callId, new_id}]
        "dup_rewrite_fields": 0,   # 去重实际改写的事件数
        "changed": False,          # 是否有内容被改写
        "backup": "",              # 备份文件路径（仅写盘时）
    }


def _process_lines(lines: list[str], rewrite: bool, report: dict, all_events: list, fix_dup_ids: bool):
    """处理一个会话的全部物理行（帧内行拼接后统一处理，便于跨帧判定重复 id）。

    :return: ``(新行列表, 是否有变更)``。
    """
    entries: list[object | None] = []
    for line in lines:
        if line == "":
            entries.append(None)
            continue
        try:
            event = json.loads(line)
            if isinstance(event, dict):
                entries.append(event)
                all_events.append(event)
                continue
        except (ValueError, TypeError):
            pass
        entries.append(None)

    changed_rows: dict[int, dict] = {}
    skipped: list[dict] = []
    # v0 形态没有内嵌 stream，其 finish 原因在相邻的 assistant/chunk 事件里，
    # 按 (turn, step) 追踪，供其后的 assistant/message 做 max-tokens 守卫。
    finish_kinds: dict[tuple, str] = {}
    # ① 扁平 replayState 升级（逐行为主；v0 形态借助跨行 finish 追踪）
    for i, event in enumerate(entries):
        if event is None:
            continue
        event_data = event.get("data") if isinstance(event, dict) else None
        if event.get("type") == "assistant/chunk" and isinstance(event_data, dict):
            kind = _finish_reason_kind(event_data.get("chunk"))
            if kind is not None:
                finish_kinds[(event_data.get("turn"), event_data.get("step"))] = kind
        fk = None
        if (
            event.get("type") == "assistant/message"
            and isinstance(event_data, dict)
            and not isinstance(event_data.get("stream"), list)
        ):
            fk = finish_kinds.get((event_data.get("turn"), event_data.get("step")))
        new_event, row_changed, line_hits, line_skipped = wrap_line_event(event, rewrite, finish_kind=fk)
        # 按规则分流命中：描述符版本升级与 replayState 升级是两件事，汇总时要分开说
        for hit in line_hits:
            target = report["descriptor_hits"] if hit.get("rule") == DESCRIPTOR_RULE \
                else report["hits"]
            target.append(hit)
        for item in line_skipped:
            target = report["descriptor_skipped"] if item.get("rule") == DESCRIPTOR_RULE \
                else skipped
            target.append(item)
        if rewrite and row_changed:
            changed_rows[i] = new_event
    if skipped:
        report["skipped"] = skipped

    # ② 重复 tool-call id 去重（需跨行状态机，仅在显式开启时执行）
    if fix_dup_ids:
        event_idxs = [i for i, event in enumerate(entries) if event is not None]
        current_events = [changed_rows.get(i, entries[i]) for i in event_idxs]
        rewrites, dup_hits, rewritten = disambiguate_duplicate_call_ids(current_events)
        if rewrite:
            for event_pos, new_event in rewrites.items():
                changed_rows[event_idxs[event_pos]] = new_event
        if dup_hits:
            report["dup_fixes"] = dup_hits
            report["dup_rewrite_fields"] = rewritten
            preview = ", ".join(
                "%s→%s@seq%s" % (h["callId"], h["new_id"], h["seq"]) for h in dup_hits[:6]
            )
            report["actions"].append(
                {
                    "rule": "重复tool-call id去重",
                    "count": len(dup_hits),
                    "detail": (
                        "同一 step 内重复宣告的 tool-call id 加后缀区分"
                        "（宣告 %d 处，共改写 %d 个事件）：%s%s"
                        % (len(dup_hits), rewritten, preview, " …" if len(dup_hits) > 6 else "")
                    ),
                }
            )

    if not rewrite or not changed_rows:
        return lines, False
    # 必须用紧凑分隔符：DSH 写入的 JSONL 是 ``JSON.stringify`` 风格
    # （无空格），默认 ``json.dumps`` 会插入 ``": "`` / `", "``，
    # 实测使单行体积膨胀 12%+。
    dump = lambda ev: json.dumps(ev, ensure_ascii=False, separators=(",", ":"))  # noqa: E731
    new_lines = [dump(changed_rows[i]) if i in changed_rows else line for i, line in enumerate(lines)]
    return new_lines, True


def _record_header(report: dict, first_line: str) -> None:
    """记录 header 的格式版本与会话 id（用于报告）。"""
    try:
        header = json.loads(first_line.strip())
    except (ValueError, TypeError):
        return
    if isinstance(header, dict) and header.get("type") == "session":
        report["format_version"] = header.get("version")
        report["session_id"] = header.get("id")


def _summarize(report: dict, all_events: list, rewrite: bool, changed: bool) -> None:
    """汇总命中动作与其他检测问题，并给出最终状态。"""
    if report["hits"]:
        report["actions"].insert(
            0,
            {
                "rule": "replayState升级为信封",
                "count": len(report["hits"]),
                "detail": (
                    "把包络拆分前的扁平 replayState 升级为 {response, blocks} 信封；"
                    "同一事件的内嵌 stream finish 块与 message.source 同值升级，"
                    "保证与内嵌 stream 重组结果完全一致"
                ),
            },
        )
    if report["skipped"]:
        report["actions"].append(
            {
                "rule": "max-tokens剪枝场景跳过",
                "count": len(report["skipped"]),
                "detail": "该场景需要 stream 侧保留全量、source 侧随内容剪枝，本工具暂不自动升级，已原样保留",
            }
        )
    if report["descriptor_hits"]:
        pairs = ", ".join("%s→%s" % (h.get("from"), h.get("to"))
                          for h in report["descriptor_hits"][:6])
        report["actions"].append(
            {
                "rule": "子代理描述符版本升级",
                "count": len(report["descriptor_hits"]),
                "detail": (
                    "把 subagent/descriptor 的 version 升到官方要求的 3"
                    "（v0→v1 迁移对版本是硬校验，旧版本会让会话加载报错）：%s%s"
                    % (pairs, " …" if len(report["descriptor_hits"]) > 6 else "")
                ),
            }
        )
    if report["descriptor_skipped"]:
        report["actions"].append(
            {
                "rule": "子代理描述符未升级（已保留原样）",
                "count": len(report["descriptor_skipped"]),
                "detail": "；".join(sorted({s.get("reason", "") for s in report["descriptor_skipped"]})),
            }
        )
    dup_fixed = bool(report["dup_fixes"])
    report["problems"].extend(
        p
        for p in detect_other_problems(all_events)
        if not (dup_fixed and p["code"] == "重复的tool-call id")
    )
    if report["actions"]:
        report["status"] = "已修复" if (rewrite and changed) else "需要修复"


def _repair_zstd_buffer(
    buf: bytes, rewrite: bool, fix_dup_ids: bool, decompress, compress
) -> "tuple[bytes | None, dict]":
    """修复一份 zstd 拼接帧的会话日志。

    只对**命中的帧**重新压缩，其余帧保持原字节（最小扰动）；尾部写断的残留
    字节原样追加在末尾，不参与重写。
    """
    report = _new_session_report()
    try:
        frames, tail = _scan_zstd_frames_with_tail(buf)
    except ValueError as exc:
        report["status"] = "拒绝"
        report["problems"].append({"code": "帧损坏", "detail": str(exc)})
        return None, report

    report["frames"] = len(frames)
    report["torn_tail"] = tail is not None
    if not frames:
        report["status"] = "拒绝"
        report["problems"].append({"code": "无有效帧", "detail": "文件不含完整 zstd 帧"})
        return None, report

    # 先解压全部帧并记录各自的行结构
    frame_infos: list[dict] = []
    for index, (start, end) in enumerate(frames):
        try:
            text = decompress(buf[start:end]).decode("utf-8", "replace")
        except Exception as exc:
            report["status"] = "拒绝"
            report["problems"].append({"code": "帧解压失败", "detail": "第 %d 帧：%s" % (index, exc)})
            return None, report
        if index == 0:
            _record_header(report, text)
        lines, has_trailing = _split_jsonl_lines(text)
        frame_infos.append(
            {"lines": lines, "has_trailing": has_trailing, "start": start, "end": end}
        )

    # 各帧的行拼接后统一处理（真实日志每行一帧，重复 id 判定必须跨帧），
    # 处理完再按原帧边界回填
    all_lines: list[str] = []
    frame_starts: list[int] = []
    for info in frame_infos:
        frame_starts.append(len(all_lines))
        all_lines.extend(info["lines"])

    all_events: list[dict] = []
    new_lines, any_changed = _process_lines(all_lines, rewrite, report, all_events, fix_dup_ids)
    report["changed"] = any_changed
    _summarize(report, all_events, rewrite, any_changed)
    if not rewrite:
        return None, report

    out_frames: list[bytes] = []
    for index, info in enumerate(frame_infos):
        start_off = frame_starts[index]
        frame_lines = new_lines[start_off : start_off + len(info["lines"])]
        changed_here = any(
            frame_lines[j] != info["lines"][j] for j in range(len(info["lines"]))
        )
        if changed_here:
            text = "\n".join(frame_lines) + ("\n" if info["has_trailing"] else "")
            out_frames.append(compress(text.encode("utf-8")))
        else:
            out_frames.append(buf[info["start"] : info["end"]])

    output = b"".join(out_frames)
    if tail is not None:
        output += buf[tail:]
    return output, report


def _handle_session_data(
    data: bytes, rewrite: bool, fix_dup_ids: bool, decompress, compress
) -> "tuple[bytes | None, dict]":
    """统一处理一份会话日志（明文 JSONL 或 zstd 拼接帧）。

    :return: ``(新字节或 None, 报告)``。``rewrite=False`` 时新字节恒为 None。
    """
    report = _new_session_report()

    # 明文 JSONL（非 zstd）：整体当作一段文本处理
    if len(data) < 4 or int.from_bytes(data[:4], "little") != ZSTD_MAGIC:
        report["plain"] = True
        text = data.decode("utf-8", "replace")
        _record_header(report, text.split("\n")[0] if text else "")
        has_trailing = text.endswith("\n")
        lines = [] if text == "" else (text[:-1].split("\n") if has_trailing else text.split("\n"))
        all_events: list[dict] = []
        new_lines, changed = _process_lines(lines, rewrite, report, all_events, fix_dup_ids)
        report["changed"] = changed
        _summarize(report, all_events, rewrite, changed)
        if not rewrite:
            return None, report
        return ("\n".join(new_lines) + ("\n" if has_trailing else "")).encode("utf-8"), report

    if decompress is None:
        report["status"] = "拒绝"
        report["problems"].append({"code": "缺少zstd", "detail": _ZSTD_MISSING_DETAIL})
        return None, report
    if rewrite and compress is None:
        report["status"] = "拒绝"
        report["problems"].append(
            {"code": "缺少zstd压缩", "detail": "修复会话文件内容需重压缩帧，当前环境缺少 zstd 压缩后端"}
        )
        return None, report
    return _repair_zstd_buffer(data, rewrite, fix_dup_ids, decompress, compress)


def detect_session_data(data: bytes, fix_dup_ids: bool = False) -> dict:
    """检测会话数据（不写盘）。返回报告 dict（字段见 ``_new_session_report``）。"""
    decompress, compress, _name = zstd_backend()
    _, report = _handle_session_data(data, False, fix_dup_ids, decompress, compress)
    return report


def repair_session_data(data: bytes, fix_dup_ids: bool = False) -> "tuple[bytes | None, dict]":
    """修复会话数据（内存操作，不改原文件）。

    :return: ``(新字节或 None, 报告)``。
    """
    decompress, compress, _name = zstd_backend()
    return _handle_session_data(data, True, fix_dup_ids, decompress, compress)


def _atomic_write(data: bytes, path: str) -> None:
    """原子写：临时文件 + ``os.replace``（Windows 同目录原子替换）。"""
    directory = os.path.dirname(_longpath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".dsh-repair.", dir=directory)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(_longpath(tmp), _longpath(path))
    except BaseException:
        try:
            os.unlink(_longpath(tmp))
        except OSError:
            pass
        raise


def _backup_path(path: str) -> str:
    """备份文件名：``<原文件>.bak.<UTC>``（与 第三方修复脚本 同风格）。"""
    return "%s.bak.%s" % (path, _now_iso().replace(":", "-").replace(".", "-"))


def repair_session_file(path: str, apply: bool = False, fix_dup_ids: bool = False) -> dict:
    """检测/修复单个会话文件。

    :param apply: ``True`` 才写盘，且先写 ``<文件>.bak.<UTC>`` 备份。
    :param fix_dup_ids: 是否一并修复同 step 内重复 tool-call id（有语义改动）。
    """
    try:
        with open(_longpath(path), "rb") as fh:
            raw = fh.read()
    except OSError as exc:
        report = _new_session_report()
        report["file"] = path
        report["status"] = "拒绝"
        report["problems"].append({"code": "读取失败", "detail": str(exc)})
        return report

    if apply:
        new_data, report = repair_session_data(raw, fix_dup_ids)
    else:
        report = detect_session_data(raw, fix_dup_ids)
    report["file"] = path

    if apply and report["changed"] and report["status"] != "拒绝":
        try:
            backup = _backup_path(path)
            with open(_longpath(backup), "wb") as fh:
                fh.write(raw)
        except OSError as exc:
            report["status"] = "拒绝"
            report["problems"].append({"code": "备份失败", "detail": str(exc)})
            return report
        try:
            _atomic_write(new_data, path)
            report["backup"] = backup
        except OSError as exc:
            report["status"] = "拒绝"
            report["problems"].append({"code": "写盘失败", "detail": str(exc)})
            report["backup"] = backup
    return report


def repair_session_root(
    root: str, apply: bool = False, fix_dup_ids: bool = False, max_depth: int = 4
) -> dict:
    """批量处理会话文件（文件或目录；目录按深度 ≤ ``max_depth`` 递归）。

    支持单文件（``.jsonl.zstd`` / 明文 ``.jsonl``）、会话目录、``sessions``
    根目录（自动递归找 ``session.jsonl.zstd`` / ``session.vN.jsonl.zstd``）。

    :return: 汇总 dict（``file_total`` / ``need_repair`` / ``repaired`` /
        ``rejected`` / ``results``）。
    """
    root = os.path.abspath(os.path.expanduser(root))
    files: list[str] = []

    def _walk(directory: str, depth: int) -> None:
        if depth > max_depth:
            return
        try:
            entries = os.listdir(_longpath(directory))
        except OSError:
            return
        for name in sorted(entries):
            sub = os.path.join(directory, name)
            if os.path.isdir(_longpath(sub)):
                _walk(sub, depth + 1)
            elif _SESSION_FILE_NAME_RE.match(name):
                files.append(sub)

    if os.path.isfile(_longpath(root)):
        files.append(root)
    else:
        _walk(root, 0)

    results: list[dict] = []
    for file in files:
        try:
            results.append(repair_session_file(file, apply=apply, fix_dup_ids=fix_dup_ids))
        except Exception as exc:  # 单文件失败不阻断批量
            report = _new_session_report()
            report["file"] = file
            report["status"] = "拒绝"
            report["problems"].append({"code": "处理失败", "detail": str(exc)})
            results.append(report)

    return {
        "root": root,
        "file_total": len(results),
        "need_repair": sum(1 for r in results if r["status"] == "需要修复"),
        "repaired": sum(1 for r in results if r["status"] == "已修复"),
        "rejected": sum(1 for r in results if r["status"] == "拒绝" and not _is_zstd_missing(r)),
        "skipped_no_zstd": sum(1 for r in results if _is_zstd_missing(r)),
        "results": results,
    }


# --------------------------------------------------------------------------- #
# 联合修复：会话文件内容 + workspace 索引（GUI / 导入流程一次调用）
# --------------------------------------------------------------------------- #
@dataclass
class DshRepairPlan:
    """一次「修复」操作覆盖的完整范围（dry-run 产物）。"""

    index_plan: RepairPlan = field(default_factory=RepairPlan)  # workspace.json 索引
    index_idx: "dict | None" = None                              # 读入的索引
    data_reports: list[dict] = field(default_factory=list)       # 需修的会话文件报告
    projcache_stale: list[StaleProjcache] = field(default_factory=list)  # 需移走的陈旧投影缓存
    shell_fixes: list[ShadowedShell] = field(default_factory=list)  # 空壳遮挡待修复会话
    zstd_note: str = ""                                           # zstd 不可用时的说明
    #: 子代理关系修复计划（``None`` = 本次没有要求处理，例如调用方没给来源）
    relink: "object | None" = None

    @property
    def empty(self) -> bool:
        relink_empty = True
        if self.relink is not None:
            relink_empty = bool(getattr(self.relink, "empty", True))
        return (
            not self.index_plan.mutations
            and not self.data_reports
            and not self.projcache_stale
            and not self.shell_fixes
            and relink_empty
        )

    def describe(self) -> list[str]:
        lines = self.index_plan.describe()
        for report in self.data_reports:
            rules = "、".join("%s×%d" % (a["rule"], a["count"]) for a in report["actions"])
            lines.append("修复会话文件 %s：%s" % (report["file"], rules))
        for stale in self.projcache_stale:
            lines.append("移走陈旧投影缓存 %s：%s" % (stale.session_id, stale.reason))
        for fix in self.shell_fixes:
            lines.append(
                "重生成会话 %s 的正文（正文在低代际 v%d 日志里，被 DSH 拒后写的空壳 v%d 遮挡），"
                "并把空壳日志与投影缓存移走"
                % (fix.session_id, fix.body_version, fix.shadow_version))
        if self.relink is not None:
            lines.extend(getattr(self.relink, "describe", lambda: [])())
        return lines


def plan_dsh_repair(dsh_home: str, fix_dup_ids: bool = False,
                    relink_source: str = "") -> DshRepairPlan:
    """生成联合修复计划（不写盘）。

    范围：① workspace.json 索引归属修复（含 path 字段缺失时按会话 cwd 推断补齐、
    path 指向的目录不在本机时补建空目录）；② 会话文件内容（默认把扁平
    ``replayState`` 升级为信封、把 ``subagent/descriptor`` 的版本升到 3；
    ``fix_dup_ids=True`` 才连重复 tool-call id 一起修）；
    ③ 移走结论与日志矛盾的陈旧投影缓存记录；④ **子代理关系修复**（把早期版本导入
    留下的顶层子代理会话改成原生子代理会话，见 :mod:`ai_env_clone.subagent_relink`）。

    ④ 的来源**自动查找**（本机 ZCode / DSH 会话数据，以及本工具备份目录与上次用过的
    目录里的 ``zcode_backup_*.zip`` / ``dsh_backup_*.zip``）；``relink_source`` 只是
    可选的显式覆盖。检测不到「旧版导入的子代理会话」时这一步几乎不花时间（先扫 DSH
    会话，没有候选就直接返回，不去解包）。
    """
    home = resolve_dsh_home(dsh_home)
    index_plan, index_idx = plan_workspace_index_repair(home)
    decompress, compress, zstd_name = zstd_backend()
    plan = DshRepairPlan(index_plan=index_plan, index_idx=index_idx)
    summary = repair_session_root(
        os.path.join(home, "sessions"), apply=False, fix_dup_ids=fix_dup_ids
    )
    plan.data_reports = [
        r for r in summary["results"] if r["status"] in ("需要修复", "已修复")
    ]
    plan.projcache_stale = detect_stale_projcache(home, decompress=decompress)
    plan.shell_fixes = detect_shadowed_shells(home, decompress=decompress)
    from . import subagent_relink

    plan.relink = subagent_relink.plan_subagent_relink(home, relink_source)
    if not decompress:
        plan.zstd_note = (
            "缺少 zstd 解压支持（%s）：zstd 会话文件内容无法检测/修复，仅处理了明文 JSONL"
            % (zstd_name or "未安装")
        )
    elif not compress:
        plan.zstd_note = "缺少 zstd 压缩后端：会话文件内容只能检测、不能修复"
    return plan


def _move_stale_projcache(stale: StaleProjcache) -> dict:
    """把一条陈旧投影缓存记录改名移走（同目录 ``<sid>.json.bak-<ts>``）。

    不写值、不删除：DSH 冷读时按最新日志自行重算；``.bak-`` 文件不匹配
    ``<sid>.json``，不会被 DSH 读作记录。失败不抛出，交由调用方汇总上报。
    """
    stamp = _now_iso().replace(":", "").replace(".", "-")
    backup = "%s.bak-%s" % (stale.path, stamp)
    try:
        os.replace(_longpath(stale.path), _longpath(backup))
    except OSError as exc:
        return {"session_id": stale.session_id, "ok": False, "backup": "", "error": str(exc)}
    return {"session_id": stale.session_id, "ok": True, "backup": backup, "error": ""}


def apply_dsh_repair(
    dsh_home: str,
    plan: DshRepairPlan,
    fix_dup_ids: bool = False,
    backup: bool = True,
) -> dict:
    """执行联合修复：会话文件内容 → 空壳遮挡 → 陈旧投影缓存 → workspace 索引 → 子代理关系。

    ⚠️ 子代理关系修复**放在最后**：它会把旧顶层子代理会话的 id 从 ``sessionIds`` 里摘掉，
    而索引修复按计划期的快照写盘（快照里那些 id 还在），先做索引才不会把它加回来。
    空壳遮挡修复放在投影缓存之前：它自己会顺带移走该会话的缓存记录。
    """
    home = resolve_dsh_home(dsh_home)
    decompress, _compress, _name = zstd_backend()
    results: list[dict] = []
    for report in plan.data_reports:
        file = report.get("file")
        if file:
            results.append(repair_session_file(file, apply=True, fix_dup_ids=fix_dup_ids))
    shells = [_apply_shell_fix(fix, decompress, home, backup=backup)
              for fix in plan.shell_fixes]
    projcache = [_move_stale_projcache(stale) for stale in plan.projcache_stale]
    index_result = apply_workspace_index_repair(
        home, plan.index_plan, dry_run=False, backup=backup, idx=plan.index_idx
    )
    relink_result: dict = {}
    if plan.relink is not None and getattr(plan.relink, "targets", None):
        from . import subagent_relink

        relink_result = subagent_relink.apply_subagent_relink(home, plan.relink,
                                                              backup=backup)
    return {"files": results, "shells": shells, "projcache": projcache,
            "index": index_result, "relink": relink_result}


# --------------------------------------------------------------------------- #
# CLI（自动化脚本入口）
# --------------------------------------------------------------------------- #
def _print_plan(plan) -> None:
    """打印修复计划（支持索引计划与联合计划）。"""
    if isinstance(plan, DshRepairPlan):
        if plan.empty:
            if plan.zstd_note:
                print("无可自动修复项；会话文件内容检测未执行（%s）。" % plan.zstd_note)
            else:
                print("无需修复：未发现可自动归属的未分组会话，会话文件内容与投影缓存也无需修复。")
            for sid, reason in plan.index_plan.skipped:
                print("  - 跳过 %s：%s" % (sid, reason))
            return
        print("修复计划（dry-run，未写盘）：")
        for line in plan.describe():
            print("  - %s" % line)
        if plan.zstd_note:
            print("  注：%s" % plan.zstd_note)
        return
    if not plan.mutations:
        print("无需修复：未发现可自动归属的未分组会话。")
        for sid, reason in plan.skipped:
            print("  - 跳过 %s：%s" % (sid, reason))
        return
    print("修复计划（dry-run，未写盘）：")
    for line in plan.describe():
        print("  - %s" % line)


def main(argv: "list[str] | None" = None) -> int:
    """命令行入口：``scan`` / ``plan`` / ``fix`` / ``repair-data``。

    - ``scan``：检测并输出摘要（``--json`` 输出结构化结果）。
    - ``plan``：输出将执行的修复动作（索引归属 / 会话文件内容 / 陈旧投影缓存）。
    - ``fix``：执行索引与会话文件修复，并移走陈旧投影缓存；默认 dry-run，
      加 ``--apply`` 才写盘（写盘前自动备份原文件，``--no-backup`` 可关）。
    - ``repair-data <文件或目录>``：检测/修复会话文件内容（扁平 replayState
      升级为信封，可选重复 tool-call id 去重）；默认 dry-run，加 ``--apply`` 写盘
      并自动备份。
    """
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m ai_env_clone.dsh_repair",
        description="DSH 旧会话数据「未分组 / 无法加载」检测与修复",
    )
    parser.add_argument("command", choices=["scan", "plan", "fix", "repair-data"])
    parser.add_argument("target", nargs="?", default=None, help="repair-data 的目标文件或目录")
    parser.add_argument("--dsh-home", default=None, help="DSH 数据根目录（默认 $DSH_HOME 或 ~/.dsh）")
    parser.add_argument("--json", action="store_true", help="scan 时输出 JSON")
    parser.add_argument("--apply", action="store_true", help="fix / repair-data 时真正写盘（默认 dry-run）")
    parser.add_argument("--no-backup", action="store_true", help="fix 写盘前不备份原文件")
    parser.add_argument(
        "--fix-dup-call-ids",
        dest="fix_dup_call_ids",
        action="store_true",
        help="同时修复同 step 内重复的 tool-call id（有语义改动，默认关闭）",
    )
    parser.add_argument(
        "--relink-source",
        dest="relink_source",
        default="",
        help="子代理关系修复的来源（数据根目录 / db.sqlite / 备份包 zip）；"
             "默认自动查找本机数据与本工具备份目录里的 zcode/dsh 备份包，本项仅作覆盖",
    )
    args = parser.parse_args(argv)

    # Windows 旧控制台（GBK/cp936）无法编码 ⚠ 等符号：把标准流切到 UTF-8，
    # 宁可显示替换符也不让 print 崩溃。
    if sys.platform == "win32":
        for _stream in (sys.stdout, sys.stderr):
            try:
                _stream.reconfigure(encoding="utf-8", errors="replace")
            except (AttributeError, ValueError):
                pass

    home = resolve_dsh_home(args.dsh_home)
    if args.command == "repair-data":
        if not args.target:
            print("用法：python -m ai_env_clone.dsh_repair repair-data <文件或目录> [--apply] "
                  "[--fix-dup-call-ids] [--json]", file=sys.stderr)
            return 2
        summary = repair_session_root(
            args.target, apply=args.apply, fix_dup_ids=args.fix_dup_call_ids
        )
        if args.json:
            print(json.dumps(summary, ensure_ascii=False, indent=2))
        else:
            print("会话文件：%d 个（需修复 %d，已修复 %d，损坏/无法处理 %d）"
                  % (summary["file_total"], summary["need_repair"], summary["repaired"], summary["rejected"]))
            for report in summary["results"]:
                if report["status"] == "正常":
                    continue
                if _is_zstd_missing(report):
                    continue
                print("  [%s] %s" % (report["status"], report["file"]))
                for action in report["actions"]:
                    print("      %s（%d 处）：%s" % (action["rule"], action["count"], action["detail"]))
                for problem in report["problems"]:
                    print("      问题：%s：%s" % (problem["code"], problem["detail"]))
                if report["backup"]:
                    print("      备份：%s" % report["backup"])
                if not report["actions"] and not report["problems"]:
                    print("      （无可识别问题）")
            if summary["skipped_no_zstd"]:
                print("  其中 %d 个会话文件因缺少 zstd 后端无法读取/修复（%s）"
                      % (summary["skipped_no_zstd"], _ZSTD_MISSING_DETAIL))
            if not args.apply and summary["need_repair"] > 0:
                print("（dry-run，未写盘；加 --apply 执行修复并备份）")
        return 1 if summary["rejected"] > 0 else 0

    if args.command == "scan":
        decompress, _compress, name = zstd_backend()
        result = detect_ungrouped(home, decompress=decompress)
        if args.json:
            print(
                json.dumps(
                    {
                        "dsh_home": home,
                        "zstd": name or None,
                        "sessions_total": result.sessions_total,
                        "ungrouped": [
                            {
                                "session_id": s.session_id,
                                "project_dir": s.project_dir,
                                "cwd": s.cwd,
                                "legacy_replay": s.legacy_replay,
                                "dup_call_ids": s.dup_call_ids,
                                "descriptor_bad": s.descriptor_bad,
                            }
                            for s in result.ungrouped
                        ],
                        "ungrouped_attachable": result.ungrouped_attachable,
                        "ungrouped_unattachable": result.ungrouped_unattachable,
                        "index_exists": result.index_exists,
                        "index_problems": result.index_problems,
                        "legacy_replay_sessions": result.legacy_replay_sessions,
                        "dup_id_sessions": result.dup_id_sessions,
                        "descriptor_bad_sessions": result.descriptor_bad_sessions,
                        "projcache_stale": [
                            {
                                "session_id": s.session_id,
                                "reason": s.reason,
                                "hidden": s.hidden,
                            }
                            for s in result.projcache_stale
                        ],
                        "shadowed_shells": [
                            {
                                "session_id": f.session_id,
                                "shadow_file": f.shadow_file,
                                "shadow_version": f.shadow_version,
                                "body_file": f.body_file,
                                "body_version": f.body_version,
                            }
                            for f in result.shadowed_shells
                        ],
                        "relink_targets": [
                            {"child_id": t.child_id, "parent_id": t.parent_id,
                             "source_session_id": t.source_session_id}
                            for t in result.relink_targets
                        ],
                        "relink_skipped": [
                            {"session_id": sid, "reason": reason}
                            for sid, reason in result.relink_skipped
                        ],
                        "relink_source_note": result.relink_source_note,
                        "workspace_paths_missing": result.workspace_paths_missing,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
        else:
            print("DSH 数据根：%s" % home)
            for line in result.summary_lines():
                print(line)
        return 0

    if args.command == "plan":
        _print_plan(plan_dsh_repair(home, fix_dup_ids=args.fix_dup_call_ids,
                                    relink_source=args.relink_source))
        return 0

    # fix：workspace.json 索引归属 + 会话文件内容（默认只修扁平 replayState）
    #      + 可选子代理关系修复（--relink-source 给出 ZCode 来源时才做）
    combined = plan_dsh_repair(home, fix_dup_ids=args.fix_dup_call_ids,
                               relink_source=args.relink_source)
    if combined.empty:
        _print_plan(combined)
        return 0
    if not args.apply:
        _print_plan(combined)
        print("（dry-run：加 --apply 执行写盘，写盘前自动备份原文件）")
        return 0
    outcome = apply_dsh_repair(
        home, combined, fix_dup_ids=args.fix_dup_call_ids, backup=not args.no_backup
    )
    index_result = outcome["index"]
    if not index_result.ok:
        print("索引修复失败：%s" % index_result.error)
        return 1
    if index_result.applied:
        print(
            "索引修复 %d 处；备份：%s"
            % (index_result.applied, index_result.backup_path or "（未生成）")
        )
    for item in getattr(index_result, "created", None) or []:
        if item["ok"]:
            print("补建工作区目录 %s" % item["path"])
        else:
            print("补建工作区目录 %s 失败：%s" % (item["path"], item["error"]))
    for item in outcome["projcache"]:
        if item["ok"]:
            print("移走陈旧投影缓存 %s；备份：%s" % (item["session_id"], item["backup"]))
        else:
            print("移走陈旧投影缓存 %s 失败：%s" % (item["session_id"], item["error"]))
    for report in outcome["files"]:
        if report["status"] not in ("已修复", "拒绝"):
            continue
        print(
            "会话文件 %s：%s；备份：%s"
            % (os.path.basename(report["file"]), report["status"], report["backup"] or "（未生成）")
        )
        for action in report["actions"]:
            print("      %s（%d 处）：%s" % (action["rule"], action["count"], action["detail"]))
        for problem in report["problems"]:
            print("      问题：%s" % problem["detail"])
    relink_res = outcome.get("relink") or {}
    relink_targets = relink_res.get("targets") or []
    if relink_targets:
        good = [r for r in relink_targets if r["ok"]]
        print("子代理关系修复 %d 条（旧目录已移到 sessions/.removed/，可回退）" % len(good))
        for r in relink_targets:
            if not r["ok"]:
                print("      %s 失败：%s" % (r["child_id"], r["error"]))
        for p in relink_res.get("parents") or []:
            print("      父会话 %s 已发布的 v4 已改名移走（DSH 会重新迁移并补 catalog）"
                  % p["parent_id"])
    if any(r["status"] == "拒绝" for r in outcome["files"]) or any(
        not item["ok"] for item in outcome["projcache"]
    ) or any(not r["ok"] for r in relink_targets):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
