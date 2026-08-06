"""
按文件类型估算 DEFLATE 压缩后体积的通用模块。

本模块与具体 AI 工具无关，任何适配器（Qoder、以及未来支持的各类国内 AI 工具）
都可直接调用 :func:`estimate_compressed_bytes`，传入按扩展名分组的字节数即可获得
贴近实际的压缩后体积估算，无需真实跑一遍压缩。

设计要点
--------
1. 按扩展名把文件分为五类：text / db / struct / binary / other，各自 DEFFLATE 压缩率
   （即「压缩后 / 源」占比）差异很大，分类加权比单一固定系数更准。
2. 压缩档位只保留两档：**快速(1)** 与 **正常(6)**。

   - 「正常」档（lvl6）DEFLATE 已接近最优，lvl6→lvl9 压缩率差异极小（网络资料佐证，
     CPU 却多耗数倍），故不再单列「高压缩比」档，体积与正常档几乎相同，如实省略。
   - 「快速」档（lvl1）明显更大（打包快），如实反映。

压缩系数校准依据
----------------
**权威数据来源 = 真实全量导出 zip 反推**（最可信，非小样本）：
对本机 Qoder 真实备份包 ``qoder_backup_20260805_095519.zip`` 逐文件读取
``file_size`` / ``compress_size`` 反算（该包即核心 ``scan_items`` 入包内容的 DEFLATE 压缩）。
源 877MB，压缩后 469MB，**实际整体压缩率 0.535**。按扩展名占比与实测压缩率：

    .db   (581MB, 66%): 实际 0.594   ← SQLite 内已含熵编码/压缩 blob（如向量等），
                                DEFLATE 仅再压掉约 40%，远低于"可压到剩0.27"的早期误判
    .zap  (258MB, 29%): 实际 0.462   （zap/bolt 向量索引：非通用压缩格式，
                                DEFLATE 仍可压掉约一半）
    .txt  ( 33MB, 3.7%): 实际 0.100
    .jsonl(2.5MB)      : 实际 0.206
    .bolt (0.94MB)     : 实际 0.025（量极小）
    .json (0.58MB)     : 实际 0.828（量极小）

按上述占比加权校验：0.66×0.594 + 0.294×0.462 + 0.037×0.100 + 小项 ≈ 0.53，
与真实整体 0.535 吻合。故系数定稿（正常档 lvl6）：
db=0.59 / struct=0.46 / text=0.15 / binary=0.99 / other=0.13；
快速档 lvl1 在 lvl6 基础上略增（db 0.61 / text 0.17 / struct 0.48 等）。

- binary（图片/音视频/压缩包/可执行等通用已压缩或二进制）：DEFLATE 几乎无收益，≈0.99。
- 注意：db 系数随 SQLite 内部存储内容（是否含已压缩 blob）浮动，**本系数基于当前 Qoder
  数据校准**；未来其他工具的 db 若存储明文为主，可压到剩 0.27 左右，届时按实际微调。
- 佐证（网络资料）：DEFLATE 在 lvl6 已接近最优，lvl6→lvl9 压缩率差异极小。
"""

from __future__ import annotations

import json
import os

from typing import Mapping

__all__ = [
    "COMPRESS_LEVELS",
    "COMPRESS_LEVEL_NAMES",
    "CATEGORY_TEXT",
    "CATEGORY_DB",
    "CATEGORY_STRUCT",
    "CATEGORY_BINARY",
    "CATEGORY_OTHER",
    "TEXT_EXT",
    "DB_EXT",
    "STRUCT_EXT",
    "BINARY_EXT",
    "DEFAULT_COMPRESS_LEVEL",
    "EXT_CATEGORIES",
    "category_of",
    "estimate_compressed_bytes",
    "cache_dir",
    "load_calibration",
    "save_calibration",
]


#: 压缩档位可选值 -> 透传给 ZipFile.compresslevel 的整数
COMPRESS_LEVELS: dict[int, str] = {
    1: "快速",
    6: "正常",
}

#: 压缩档位显示名 -> 档位整数（供 GUI 下拉、CLI 选项复用）
COMPRESS_LEVEL_NAMES: dict[str, int] = {v: k for k, v in COMPRESS_LEVELS.items()}

#: 默认压缩档位（"正常"）
DEFAULT_COMPRESS_LEVEL: int = 6


#: 各分类标识
CATEGORY_TEXT = "text"
CATEGORY_DB = "db"
CATEGORY_STRUCT = "struct"
CATEGORY_BINARY = "binary"
CATEGORY_OTHER = "other"


#: 文本 / 源码类（高度可压缩）
TEXT_EXT: frozenset[str] = frozenset(
    {
        # 注意：`.json`、`.md` 不在此列。Qoder 真实数据中：
        #   - `.json` 实为向量索引快照（内含 base64 编码向量/UUID 等），DEFLATE 几乎压不动（实测 0.913）；
        #   - `.md` 含 base64 形式的会话附件/图片数据，DEFLATE 仅压掉约 1/3（实测 0.667）。
        # 二者均归入 binary 类（≈0.99/0.67），避免被误估为 0.15 造成大幅低估。
        ".txt", ".log", ".jsonl", ".yaml", ".yml", ".py", ".js", ".ts",
        ".toml", ".ini", ".cfg", ".csv", ".xml", ".html", ".css", ".rs", ".go",
        ".java", ".c", ".cpp", ".h", ".sh", ".bat", ".ps1", ".conf", ".env",
    }
)

#: 数据库类（SQLite 等：非通用压缩格式，DEFLATE 仍可压掉约 70%）
DB_EXT: frozenset[str] = frozenset({".db", ".sqlite", ".sqlite3"})

#: 结构化 / 索引类（如 Qoder 的 .zap / .bolt 向量索引）：非通用压缩格式，
#: 但 DEFLATE 仍可压掉约一半（实测≈0.46），并非"已压缩压不动"。
STRUCT_EXT: frozenset[str] = frozenset({".zap", ".bolt"})

#: 通用已压缩 / 二进制（图片/音视频/压缩包/可执行等）：DEFLATE 几乎无收益。
BINARY_EXT: frozenset[str] = frozenset(
    {
        ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico", ".zip", ".gz",
        ".tar", ".7z", ".rar", ".mp3", ".mp4", ".wav", ".pdf", ".ttf", ".woff",
        ".woff2", ".exe", ".dll", ".bin",
    }
)


# 压缩后占比（源字节 -> 压缩后字节，即"剩多少"）。仅保留两档：快速(1) / 正常(6)。
#
# 注意：这是**通用兜底**经验表。各 AI 工具的数据结构差异很大（如 SQLite 内是否含
# 已压缩 blob），真实系数应由对应适配器在自己的 ``COMPRESS_RATIO`` 中单独定义并维护
# （见 ``ai_env_clone/adapters/qoder.py``）。调用 :func:`estimate_compressed_bytes` 时
# 必须传入该工具的系数表，仅在未提供时才回退到此兜底表。
DEFAULT_COMPRESS_RATIO: dict[int, dict[str, float]] = {
    1: {  # 快速
        CATEGORY_TEXT: 0.17,
        CATEGORY_DB: 0.61,
        CATEGORY_STRUCT: 0.48,
        CATEGORY_BINARY: 0.99,
        CATEGORY_OTHER: 0.17,
    },
    6: {  # 正常（推荐）
        CATEGORY_TEXT: 0.15,
        CATEGORY_DB: 0.59,
        CATEGORY_STRUCT: 0.46,
        CATEGORY_BINARY: 0.99,
        CATEGORY_OTHER: 0.13,
    },
}


def category_of(ext: str) -> str:
    """返回扩展名（小写，含点，如 ``.md``；无扩展名为 ``""``）对应的文件类别。"""
    if ext in TEXT_EXT:
        return CATEGORY_TEXT
    if ext in DB_EXT:
        return CATEGORY_DB
    if ext in STRUCT_EXT:
        return CATEGORY_STRUCT
    if ext in BINARY_EXT:
        return CATEGORY_BINARY
    return CATEGORY_OTHER


#: 扩展名 -> 类别 的预构建映射（供需要整体遍历的场景使用）
EXT_CATEGORIES: Mapping[str, str] = {
    ext: cat
    for cat, exts in (
        (CATEGORY_TEXT, TEXT_EXT),
        (CATEGORY_DB, DB_EXT),
        (CATEGORY_STRUCT, STRUCT_EXT),
        (CATEGORY_BINARY, BINARY_EXT),
    )
    for ext in exts
}


def estimate_compressed_bytes(
    bytes_by_ext: Mapping[str, int],
    compresslevel: int = DEFAULT_COMPRESS_LEVEL,
    per_extension: Mapping[str, float] | None = None,
    base_ratios: Mapping[int, Mapping[str, float]] | None = None,
) -> int:
    """
    按扩展名分组、用各扩展名（类别）压缩率加权，估算压缩后总字节数。

    :param bytes_by_ext: 扩展名（小写，含点）-> 源字节数 的映射，
        通常由 ``core.scan_items`` 的 ``ScanResult.bytes_by_ext`` 提供。
    :param compresslevel: 压缩档位（1=快速 / 6=正常），未知档位回退到正常档。
    :param per_extension: 按扩展名实测的压缩率（``".db" -> 0.59`` 等），
        由最近一次真实备份按扩展名反算而来。**优先级**：
        1) 精确匹配的扩展名实测率 → 直接用；
        2) 否则回退到该扩展名所属类别的实测率（合并 ``.json``/``.txt`` 等同属一类的场景）；
        3) 都没有则回退到经验系数。
        提供时仅对出现过实测数据的扩展名/类别覆盖经验系数；为 ``None`` 时全走经验系数。
    :param base_ratios: 该工具的内置经验系数表（档位 -> 类别 -> 占比），
        **必须**由调用方传入对应适配器的 ``COMPRESS_RATIO``。未传时回退到
        通用兜底 ``DEFAULT_COMPRESS_RATIO``（不推荐，跨工具精度较差）。
    :return: 估算的压缩后总字节数（向下取整到整数）。

    注：实测率会按经验系数的合理范围做**异常值过滤**，避免历史损坏的校准文件
    （如 ``.db`` 被误存为 0.06）导致估算严重失真；越界值会被忽略并回退到经验系数。
    """
    ratios = (base_ratios or DEFAULT_COMPRESS_RATIO).get(
        compresslevel, (base_ratios or DEFAULT_COMPRESS_RATIO)[DEFAULT_COMPRESS_LEVEL]
    )
    total = 0
    for ext, size in bytes_by_ext.items():
        # 1) 精确匹配扩展名
        ratio = None
        if per_extension:
            r = per_extension.get(ext)
            if r is not None and _is_plausible_ratio(ext, r, ratios):
                ratio = r
            else:
                # 2) 回退到所属类别实测率（同一类别的其它扩展名的率）
                cat = category_of(ext)
                r = per_extension.get(cat)
                if r is not None and _is_plausible_ratio(cat, r, ratios):
                    ratio = r
        if ratio is None:
            ratio = ratios[category_of(ext)]
        total += int(size * ratio)
    return total


#: 各类别实测压缩率的合理区间（按经验系数上下浮动），
#: 用于过滤掉历史损坏的校准数据（如把 .db 误存为 0.06 这种）。
_RATIO_SANITY_RANGE: dict[str, tuple[float, float]] = {
    CATEGORY_TEXT: (0.05, 0.35),
    CATEGORY_DB: (0.40, 0.90),
    CATEGORY_STRUCT: (0.25, 0.75),
    CATEGORY_BINARY: (0.85, 1.0),
    CATEGORY_OTHER: (0.05, 0.99),
}


def _is_plausible_ratio(key: str, ratio: float, ratios: Mapping[str, float]) -> bool:
    """判断给定 key（扩展名带点或类别）的实测压缩率是否在合理区间。
    未知 key 一律放行，避免误伤冷启动数据。
    """
    if not (0 < ratio <= 1):
        return False
    # 若是类别名直接查表
    rng = _RATIO_SANITY_RANGE.get(key)
    if rng is None:
        # 若是扩展名（如 ".db"），按其类别查表
        if key.startswith("."):
            rng = _RATIO_SANITY_RANGE.get(category_of(key))
    if rng is None:
        return True  # 未知类型，放行
    lo, hi = rng
    return lo <= ratio <= hi


# --------------------------------------------------------------------------- #
# 运行时校准缓存（按工具分文件，存于系统用户缓存目录，不进仓库）
# --------------------------------------------------------------------------- #
def cache_dir() -> str:
    """返回本程序的用户级缓存根目录（零第三方依赖、跨平台）。

    - Windows: ``%LOCALAPPDATA%/ai_env_clone``
    - 其它（Linux / macOS）: ``~/.cache/ai_env_clone``

    该目录用于存放运行时自动校准等缓存数据，**不应纳入版本控制**，
    与工具自身的 ``.codebuddy`` 配置目录严格区分。
    """
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~\\AppData\\Local")
    else:
        base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    path = os.path.join(base, "ai_env_clone")
    try:
        os.makedirs(path, exist_ok=True)
    except OSError:
        pass
    return path


def _calibration_path(tool: str) -> str:
    """返回某工具的校准文件路径：``{cache_dir}/calibration/{tool}.json``。"""
    return os.path.join(cache_dir(), "calibration", "%s.json" % tool)


def load_calibration(tool: str, base_ratios: Mapping[int, Mapping[str, float]] | None = None) -> dict | None:
    """读取某工具最近一次真实备份按扩展名反算的实测压缩率。

    :param tool: 工具标识（如 ``"qoder"``），校准按工具分文件存储。
    :param base_ratios: 该工具的内置经验系数表，用于异常值过滤的参考区间；
        不传则回退到通用 ``DEFAULT_COMPRESS_RATIO``。
    :return: ``{"per_extension": {ext: ratio, ...}}`` 形式的实测率字典；
        无记录 / 损坏 / 全被过滤则返回 ``None``。
    """
    ratios = (base_ratios or DEFAULT_COMPRESS_RATIO)[DEFAULT_COMPRESS_LEVEL]
    try:
        with open(_calibration_path(tool), "r", encoding="utf-8") as f:
            data = json.load(f)
        per = data.get("per_extension")
        if isinstance(per, dict) and per:
            raw = {k: float(v) for k, v in per.items()}
            clean = {k: v for k, v in raw.items() if _is_plausible_ratio(k, v, ratios)}
            return clean or None
        # 兼容旧版 per_category 格式
        old = data.get("per_category")
        if isinstance(old, dict) and old:
            return {k: float(v) for k, v in old.items()}
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def save_calibration(
    tool: str,
    bytes_by_ext: Mapping[str, int],
    bytes_by_ext_compressed: Mapping[str, int],
    base_ratios: Mapping[int, Mapping[str, float]] | None = None,
) -> None:
    """备份成功后，按扩展名反算实测压缩率并保存到该工具的校准文件。

    比类别级更精细：同类别下不同扩展名（如 ``.json`` 不可压 vs ``.txt`` 高度可压）
    才能各自贴合实测值。仅统计本次确实出现的扩展名，并按经验区间过滤异常值，
    避免写入的校准文件本身有错（如历史 ``.db``=0.06 那样的污染数据）。
    """
    ratios = (base_ratios or DEFAULT_COMPRESS_RATIO)[DEFAULT_COMPRESS_LEVEL]
    per: dict[str, float] = {}
    for ext, src in bytes_by_ext.items():
        comp = bytes_by_ext_compressed.get(ext)
        if not src or comp is None:
            continue
        ratio = comp / src
        if _is_plausible_ratio(ext, ratio, ratios):
            per[ext] = ratio
    if not per:
        return
    try:
        os.makedirs(os.path.dirname(_calibration_path(tool)), exist_ok=True)
        with open(_calibration_path(tool), "w", encoding="utf-8") as f:
            json.dump({"per_extension": per}, f)
    except OSError:
        pass
