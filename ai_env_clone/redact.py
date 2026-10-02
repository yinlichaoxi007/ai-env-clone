"""
备份导出脱敏的**共享文本工具**：把配置文件里「键名像凭证」的值替换为占位符。

为什么不放在各适配器里：DSH 的 ``settings.yaml``、Reasonix 的 ``config.toml``
都是「设置类」条目，自 2026-10-01 起**默认勾选**（用户定策：设置属「还原后立刻能
开工」类），而它们都可能带明文 apiKey / token（前者是 LLM 提供商配置，后者含
MCP 服务器 ``[[plugins]]`` 段）。备份包常被同步到网盘或转发他人 ⇒ 默认备份**绝
不能**带明文密钥，故导出前统一脱敏。

设计取舍（为什么是「行内正则」而不是 YAML/TOML 解析器）：

- 不引入 ``pyyaml`` / ``tomllib`` 依赖：本工具是**单文件 exe**，加依赖等于加体积
  与打包风险；而 ``tomllib`` 只能读不能写，改写还得自己序列化、会打乱用户注释与格式。
- 只改写**值**，键名、缩进、引号风格、行尾注释全部原样保留 ⇒ 恢复后文件结构与
  用户原稿一致，不产生「格式被重排」的副作用。
- 命中判据只看**键名**（不扫值内容）⇒ 不会误伤 ``base_url`` / ``model`` 这类正常配置。
- **引用型键名一律跳过**（见 :data:`REFERENCE_KEY_HINTS`）：值若是「环境变量名 / 文件名 /
  路径」，它本身**不是密钥**，抹掉等于把配置改坏。典型来自 DSH ``settings.yaml``——
  那里只有 ``apiKeyEnv: SENSENOVA_API_KEY``，真密钥在同目录 ``.credentials.yaml``。
- 但键名含 ``token`` 时 ``max_tokens: 4096`` 也会命中 ⇒ 对**纯数字 / 布尔 / 空值**跳过
  （凭证几乎不会是裸数字，而把整数换成字符串会让目标产品解析配置失败 —— 宁可少脱敏，
  也不弄坏用户的配置）。
- 未命中、空值、``${ENV_VAR}`` 引用（本身不含明文）、已脱敏值一律**原样返回**。
- 宁可**少脱敏**也不破坏文件：非 UTF-8 直接原样返回。

覆盖两种常见写法（同一条正则即可兼顾）：

.. code-block:: yaml

    # YAML（DSH settings.yaml）
    apiKeyEnv: MY_KEY_ENV    # 引用型 ⇒ 原样保留（DSH 实测只存引用，真密钥在 .credentials.yaml）
    api_key: sk-xxx          # 明文 ⇒ 脱敏
    providers:
      - token: sk-yyy        # 列表项

.. code-block:: toml

    # TOML（Reasonix config.toml）
    api_key = "sk-xxx"
    env = { API_KEY = "sk-zzz" }   # 内联表
"""

from __future__ import annotations

import re

__all__ = [
    "REDACTED",
    "SENSITIVE_HINTS",
    "REFERENCE_KEY_HINTS",
    "key_looks_sensitive",
    "key_is_reference",
    "redact_config_text",
    "redact_config_bytes",
]

#: 写入配置文件的凭证占位符。与各适配器 JSON 脱敏用的串**保持一致**，
#: 便于用户与排查脚本一眼认出「这里原本是凭证、已被抹掉」。
REDACTED = "***REDACTED***"

#: 键名（小写后）含这些片段即视为凭证。刻意**不含**宽泛的 ``auth``
#: （会误伤 ``author``），改用更具体的 ``authorization``。
#: 下划线 / 连字符 / 无分隔三种写法都收录（实测配置文件里 ``api_key`` /
#: ``api-key`` / ``apiKey`` 都出现过，只收一种会漏掉另外两种）。
SENSITIVE_HINTS: tuple[str, ...] = (
    "apikey", "api_key", "api-key", "apisecret", "api_secret", "api-secret",
    "token", "secret", "password", "passwd",
    "accesskey", "access_key", "access-key",
    "privatekey", "private_key", "private-key",
    "credential", "authorization", "cookie", "session_key", "session-key",
)

#: 键名含这些片段 ⇒ 该值是**引用**（环境变量名 / 文件名 / 路径 / 变量名），
#: 不是密钥本身 ⇒ **必须原样保留**。抹掉引用等于把用户的配置改坏：
#: 还原后 provider 会指向一个不存在的变量名，模型静默失效，且用户完全看不出原因。
#:
#: 实测来源（本机 DSH）：``settings.yaml`` 里是 ``apiKeyEnv: SENSENOVA_API_KEY``
#: —— DSH **只把引用写进设置**，真密钥在同目录 ``.credentials.yaml`` 的 ``refs`` 段。
#: 若按「键名含 apikey 就脱敏」一刀切，就会把 ``apiKeyEnv`` 的值换成占位符（已实测复现）。
#:
#: ⚠️ 刻意**不含** ``ref``：``refresh_token`` / ``refreshToken`` 是**真凭证**，
#: 收录 ``ref`` 会让它们被误当作引用而漏脱敏（宁可不识别 ``secretRef`` 这种冷僻写法）。
REFERENCE_KEY_HINTS: tuple[str, ...] = (
    "env", "var", "file", "path", "dir", "name",
)

#: ``key: value`` / ``key = value`` 一行（兼容 YAML 与 TOML、内联表、列表项）。
#:
#: ⚠️ 分隔符里的空白**必须用 ``[^\S\n]``（水平空白）而不是 ``\s``**：``\s`` 含换行，
#: 会让 ``empty_secret:`` 这种空值行的分隔符「吃掉换行」、把**下一行**的内容当成
#: 自己的值（实测：``empty_secret:\nnote: token…`` 会把 ``note:`` 误判成值），
#: 既破坏文件结构、又把本该保留的行整段吞掉。
#:
#: 值的第一个候选 ``\$\{[^{}]*\}`` 是环境变量引用（``${MY_KEY}``）——必须先整体吃掉，
#: 否则会被通用字符类切成 ``$`` 一个字符，脱敏后残留 ``***REDACTED***{MY_KEY}``（已实测）。
#: 通用字符类排除 ``\s , { } [ ] #``：即在空白、逗号、内联表/数组括号、注释处收尾。
_KV_RX = re.compile(
    r"(?P<key>[\"']?[A-Za-z0-9_.\-]+[\"']?)"
    r"(?P<sep>[^\S\n]*[:=][^\S\n]*)"
    r"(?P<val>\$\{[^{}]*\}|\"[^\"]*\"|'[^']*'|[^\s,{}\[\]#]+)"
)

#: 显然不是凭证的标量（纯数字 / 布尔 / 空值）。键名含 ``token`` 时 ``max_tokens: 4096``
#: 会被命中，若替换成 ``***REDACTED***`` 就成了「本该是整数的字符串」，
#: 可能让目标产品解析配置失败 ⇒ 这类值跳过，宁可少脱敏也不弄坏配置。
_NON_SECRET_SCALAR_RX = re.compile(
    r"^(?:[-+]?\d+(?:\.\d+)?|true|false|null|none|~|nil)$", re.IGNORECASE
)


def key_looks_sensitive(key: str) -> bool:
    """键名（自动去掉包裹引号、忽略大小写）是否像凭证。

    >>> key_looks_sensitive("apiKey")
    True
    >>> key_looks_sensitive("MODEL_NAME")
    False
    """
    k = key.strip().strip("\"'").lower()
    return any(hint in k for hint in SENSITIVE_HINTS)


def key_is_reference(key: str) -> bool:
    """键名是否表示「**引用**」而非密钥本身（如 ``apiKeyEnv`` / ``keyFile``）。

    引用型键的值是环境变量名、文件名或路径 —— 它必须原样保留，脱敏会把配置改坏。

    >>> key_is_reference("apiKeyEnv")
    True
    >>> key_is_reference("apiKeyPath")
    True
    >>> key_is_reference("apiKey")
    False
    >>> key_is_reference("refresh_token")
    False
    """
    k = key.strip().strip("\"'").lower()
    return any(hint in k for hint in REFERENCE_KEY_HINTS)


def _redacted_value(value: str) -> "str | None":
    """给出「该值应替换成什么」；无需处理时返回 ``None``。

    保留原引号风格（TOML 的裸 ``***REDACTED***`` 是非法值，必须带引号）。
    """
    core = value.strip()
    if not core:
        return None
    if _NON_SECRET_SCALAR_RX.match(core):
        return None  # 纯数字/布尔/空值，不可能是凭证，且替换会弄坏类型
    quote = core[0] if core[0] in "\"'" else ""
    if quote:
        if len(core) < 2 or core[-1] != quote:
            return None  # 引号不成对，形态不明，不动它
        inner = core[1:-1]
    else:
        inner = core
    # 空值 / 环境变量引用（不含明文）/ 已脱敏：无需处理
    if not inner or inner == REDACTED:
        return None
    if inner.startswith("${") and inner.endswith("}"):
        return None
    return "%s%s%s" % (quote, REDACTED, quote)


def redact_config_text(text: str) -> "tuple[str, bool]":
    """脱敏后的文本与「是否真的改动过」。

    :param text: YAML / TOML 文本。
    :return: ``(新文本, 是否有改动)``；未改动时第二个值为 ``False``。
    """
    changed = False

    def _sub(m: "re.Match[str]") -> str:
        nonlocal changed
        key = m.group("key")
        # 引用型键（apiKeyEnv / keyFile / …）的值是名称或路径，不是密钥 ⇒ 原样保留
        if key_is_reference(key) or not key_looks_sensitive(key):
            return m.group(0)
        repl = _redacted_value(m.group("val"))
        if repl is None:
            return m.group(0)
        changed = True
        return m.group("key") + m.group("sep") + repl

    return _KV_RX.sub(_sub, text), changed


def redact_config_bytes(source: bytes) -> bytes:
    """字节版 :func:`redact_config_text`，供适配器的导出脱敏回调直接返回。

    非 UTF-8 内容**原样返回**（宁可少脱敏，也不破坏用户文件）。
    """
    try:
        text = source.decode("utf-8")
    except UnicodeDecodeError:
        return source
    new_text, changed = redact_config_text(text)
    return new_text.encode("utf-8") if changed else source
