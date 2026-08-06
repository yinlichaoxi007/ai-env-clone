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
        ".md", ".txt", ".log", ".json", ".jsonl", ".yaml", ".yml", ".py", ".js", ".ts",
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
_COMPRESS_RATIO: dict[int, dict[str, float]] = {
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
    bytes_by_ext: Mapping[str, int], compresslevel: int = DEFAULT_COMPRESS_LEVEL
) -> int:
    """
    按扩展名分组、用各类别经验压缩率加权，估算压缩后总字节数。

    :param bytes_by_ext: 扩展名（小写，含点）-> 源字节数 的映射，
        通常由 ``core.scan_items`` 的 ``ScanResult.bytes_by_ext`` 提供。
    :param compresslevel: 压缩档位（1=快速 / 6=正常），未知档位回退到正常档。
    :return: 估算的压缩后总字节数（向下取整到整数）。
    """
    ratios = _COMPRESS_RATIO.get(compresslevel, _COMPRESS_RATIO[DEFAULT_COMPRESS_LEVEL])
    total = 0
    for ext, size in bytes_by_ext.items():
        total += int(size * ratios[category_of(ext)])
    return total
