"""
跨软件会话数据迁移 —— 原生复刻写入。

目标：在**明文可读**的 AI 工具之间无损迁移「历史会话」。
- 解析各来源工具原生会话文件为统一中间模型 ``Session`` / ``SessionMessage``。
- 以目标工具**原生格式**重新写出，使其能被目标工具像原生会话一样打开。
- 全程不覆盖目标工具已有会话（新会话使用全新生成的 id，避免碰撞）。

已支持的来源 / 目标（能力矩阵见 ``ai_env_clone/import_matrix.py``）：

Reasonix（明文 JSONL）
    会话目录：``<roam>/projects/<scope>/sessions/<id>-session.jsonl``
              ``<roam>/projects/<scope>/sessions/<id>.jsonl.meta``
    session.jsonl：每行一条 ``{"role","content","tool_calls","reasoning_content",
                                "createdAt",...}``（标准 JSONL，含推理过程）。
    jsonl.meta：``{"id","created_at","topic_title","scope",...}``

CodeBuddy（明文，分片 JSON + 索引）
    ``<data>/<workspaceId>/<sessionId>/index.json``
        -> ``{"messages":[{id,type,role,isComplete}], "requests":[{id,type,
            messages:[msgId...], state, startedAt, usage}]}``
    ``<data>/<workspaceId>/<sessionId>/messages/<msgId>.json``
        -> ``{"role","message"(内层 JSON 字符串),"id","references","extra","createdAt"}``

WorkBuddy（明文 JSONL 事件流 + SQLite 索引）
    会话文件：``~/.workbuddy/projects/<工作区编码>/<会话 id>.jsonl``
    每行一条事件 ``{"id","timestamp","type",...}``，``type`` 取值：
    ``message``（role + content[{type:input_text|output_text,text}]）、
    ``reasoning``（rawContent[{type:reasoning_text,text}]）、
    ``function_call``（callId/name/arguments）、``function_call_result``
    （callId/name/status/output{type,text}）、``ai-title``（aiTitle）。
    会话索引：``~/.workbuddy/workbuddy.db`` 的 ``sessions`` 表（id/cwd/user_id/
    title/status/created_at/updated_at/...）。写入新会话需**同时**落 jsonl 与
    注册 sessions 行，否则界面列表看不到。

ZCode（明文 SQLite）
    ``<zcode home>/cli/db/db.sqlite``
    ``session``（id/title/time_created/time_updated/directory/…）、
    ``message``（id/session_id/sequence/data(JSON 文本)）、
    ``part``（id/message_id/session_id/sequence/data(JSON 文本)）。
    ``message.data``：``{"role":"user"|"assistant", "time":{...}}``；
    ``part.data``：``{"type":"text","text":…}`` / ``{"type":"reasoning","text":…}``
    / ``{"type":"tool",…}``。

DeepSeek Harness（DSH，Zstandard 压缩 JSONL）
    ``<dsh home>/sessions/<workspace_dir>/session-<uuid>/session.v3.jsonl.zstd``
    （同族还有 ``session.jsonl.zstd`` / ``session.v2.jsonl.zstd``，取代际最高者）
    首行 ``{"type":"session","version":3,"id":"session-<uuid>","createdAt","cwd",…}``；
    事件行 ``{"type":"<事件>","seq","time","data":{…}}``，关键事件
    ``user/message``、``assistant/message``、``session/title``。
    会话登记：``storages/workspace.json``（工作区 → sessionIds）与
    ``storages/session_projcache.json``（会话 → identity/title）。
    读 / 写均依赖 zstd 后端（``zstandard`` / ``pyzstd`` / 系统 ``zstd`` 命令，
    探测逻辑复用 :func:`ai_env_clone.dsh_repair.zstd_backend`）；后端缺失时
    相关操作会明确报错而非静默失败。

说明：CodeBuddy 路径嵌套 UUID 极易超过 Windows MAX_PATH(260)，统一用 ``\\\\?\\``
长路径前缀读写。

注意（**不支持**会话迁移的工具）：Qoder 与 Trae 家族
（TraeCode CN / TraeWork CN，``ModularData/ai-agent/database.db``）的会话正文**取不出**，
本模块无法解析，只支持整库备份 / 还原（见 import_matrix）。但两者的成因不同，勿混为一谈：

- **Trae 家族**：``database.db`` 非标准 SQLite（**产品侧加密**），整库无法读取。
- **Qoder**：旧版 ``local.db`` 的**容器是标准 SQLite**（``sqlite3`` 可打开、表可枚举、
  ``chat_session`` / ``agent_memory`` / ``lingma_memory`` 均为明文），密文只出现在
  **会话正文列** —— ``chat_message.content``（含 ``summary`` / ``tool_result``）与
  ``chat_record.question|answer``。故其会话无法按条融合、无法跨工具复刻。
  例外：``cache/projects/<项目>/conversation-history/<id>/<id>.jsonl`` 是**明文纯文字稿**
  （``{"role","message":{"content":[{"type":"text",...}]}}``，实测无 ``tool_use`` /
  ``tool_result`` / ``thinking``，且不覆盖全部会话），只够做**有损导入**（仅文字、无工具
  过程），不能当无损迁移。

落点（工作区）的**自动判定**见 :mod:`ai_env_clone.workspace_plan`：本模块只负责
「给定工作区标识 -> 按目标工具的原生布局写出」，不负责决定写到哪个工作区。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Optional

from .adapters.codebuddy import detect_current_uid, detect_session_root


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def _ms_to_iso(ms) -> str:
    """毫秒时间戳（int/float/数字串）-> ISO8601 UTC 字符串；非法输入返回空串。"""
    try:
        if isinstance(ms, str):
            ms = float(ms)
        if not ms:
            return ""
        return datetime.fromtimestamp(float(ms) / 1000.0, timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%S.%fZ"
        )
    except (TypeError, ValueError, OSError, OverflowError):
        return ""


def _iso_to_ms(iso: str) -> int:
    """ISO8601 字符串 -> 毫秒时间戳；解析失败时返回「现在」。"""
    if isinstance(iso, str) and iso:
        for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ",
                    "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
            try:
                dt = datetime.strptime(iso, fmt).replace(tzinfo=timezone.utc)
                return int(dt.timestamp() * 1000)
            except ValueError:
                continue
    return _now_ms()



def _longpath(path: str) -> str:
    """为 Windows 提供 ``\\\\?\\`` 长路径前缀；其他平台原样返回。"""
    if os.name == "nt" and not path.startswith("\\\\?\\"):
        # 网络路径 UNC 前缀不同，这里仅处理本地绝对路径
        if os.path.isabs(path):
            return "\\\\?\\" + os.path.abspath(path)
    return path


def _read_json(path: str):
    with open(_longpath(path), "r", encoding="utf-8") as f:
        return json.load(f)


def _read_text(path: str) -> str:
    with open(_longpath(path), "r", encoding="utf-8") as f:
        return f.read()


def _write_json(path: str, obj) -> None:
    lp = _longpath(path)
    parent = os.path.dirname(lp)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(lp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def _write_text(path: str, text: str) -> None:
    lp = _longpath(path)
    parent = os.path.dirname(lp)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(lp, "w", encoding="utf-8") as f:
        f.write(text)


# --------------------------------------------------------------------------- #
# 格式无关小工具（各解析/写出器共用）
# --------------------------------------------------------------------------- #
def _join_block(existing: str, add: str) -> str:
    """把一段文本追加到已有文本后（空串安全，避免残留多余换行）。"""
    if not add:
        return existing
    if not existing:
        return add
    return existing + "\n" + add


def _flatten_any(content) -> str:
    """把任意形态的「内容」（字符串 / 列表 / 字典）拍平成可读文本。"""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                if isinstance(item.get("text"), str):
                    parts.append(item["text"])
                elif isinstance(item.get("content"), (list, str)):
                    parts.append(_flatten_any(item.get("content")))
                else:
                    parts.append(json.dumps(item, ensure_ascii=False))
            else:
                parts.append(str(item))
        return "\n".join(p for p in parts if p)
    if isinstance(content, dict):
        if isinstance(content.get("text"), str):
            return content["text"]
        if isinstance(content.get("content"), (list, str)):
            return _flatten_any(content.get("content"))
        return json.dumps(content, ensure_ascii=False)
    return str(content)


def _flatten_wb_content(content) -> str:
    """WorkBuddy ``message.content``（``[{type:input_text|output_text,text}]``）-> 文本。"""
    return _flatten_any(content)


def _flatten_wb_reasoning(raw_content) -> str:
    """WorkBuddy ``reasoning.rawContent``（``[{type:reasoning_text,text}]``）-> 文本。"""
    return _flatten_any(raw_content)


def _open_sqlite_ro(db_path: str) -> sqlite3.Connection:
    """以只读方式打开 SQLite（URI mode=ro，不修改源库）。"""
    uri = "file:%s?mode=ro" % db_path.replace("\\", "/")
    return sqlite3.connect(uri, uri=True)


def _open_sqlite_rw(db_path: str) -> sqlite3.Connection:
    """读写方式打开 SQLite（用于把新会话写入目标工具的库）。"""
    con = sqlite3.connect(_longpath(db_path))
    try:
        con.execute("pragma busy_timeout=3000")
    except sqlite3.Error:
        pass
    return con


def _loads_maybe(raw):
    """把可能是「JSON 字符串」或「已是对象」的值统一为对象；失败返回原值/None。"""
    if raw is None:
        return None
    if isinstance(raw, (dict, list)):
        return raw
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", "replace")
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return raw
    return raw


# --------------------------------------------------------------------------- #
# DSH（zstd）读写辅助
# --------------------------------------------------------------------------- #
def _dsh_zstd_backend():
    """探测可用 zstd 后端，复用 dsh_repair 的探测逻辑（避免重复实现）。

    :return: ``(decompress, compress, name)``；无可用后端时 ``(None, None, "")``。
    """
    try:
        from .dsh_repair import zstd_backend
        return zstd_backend()
    except Exception:
        return None, None, ""


def _dsh_latest_session_file(session_dir: str) -> "str | None":
    """在会话目录中挑出代际最高的会话日志文件（``session.vN.jsonl.zstd`` 优先）。

    DSH 加载器按代际取最高者；本函数与之保持一致，避免读到旧代际的残缺内容。
    """
    if not os.path.isdir(session_dir):
        return None
    best = None
    best_rank = -1
    for fn in os.listdir(session_dir):
        low = fn.lower()
        if not low.endswith(".jsonl.zstd"):
            continue
        m = re.match(r"^session\.v(\d+)\.jsonl\.zstd$", low)
        if m:
            rank = int(m.group(1))
        elif low == "session.jsonl.zstd":
            rank = 0
        else:
            rank = 1
        if rank > best_rank:
            best_rank = rank
            best = fn
    return os.path.join(session_dir, best) if best else None


def _dsh_decompress_bytes(raw: bytes) -> bytes:
    """**多帧安全**的 zstd 解压。

    ⚠️ 实测坑：``ZstdDecompressor().decompressobj().decompress(raw)`` 只解**第一帧**，
    对多帧拼接的 DSH 会话文件会截断——本机一个 4.3MB 的 ``session.v3.jsonl.zstd``
    只解出 205 字节（首行 session 头），导致会话被误判为「无消息」。故这里改用
    ``stream_reader``（可跨帧）/ ``pyzstd.decompress`` / 命令行 ``zstd -d``。
    """
    errors: list = []
    try:
        import zstandard  # type: ignore
        import io
        with zstandard.ZstdDecompressor().stream_reader(io.BytesIO(raw)) as reader:
            return reader.read()
    except ImportError:
        errors.append("zstandard 未安装")
    except Exception as exc:  # 格式异常：继续尝试其它后端
        errors.append("zstandard: %s" % exc)
    try:
        import pyzstd  # type: ignore
        return pyzstd.decompress(raw)
    except ImportError:
        errors.append("pyzstd 未安装")
    except Exception as exc:
        errors.append("pyzstd: %s" % exc)
    exe = shutil.which("zstd")
    if exe:
        import subprocess
        proc = subprocess.run([exe, "-d", "-c"], input=raw,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if proc.returncode == 0:
            return proc.stdout
        errors.append("zstd 命令: %s" % proc.stderr.decode("utf-8", "replace")[:200])
    raise RuntimeError(
        "未检测到可用的 zstd 后端，无法读取 DSH 会话文件。"
        "请安装 zstandard / pyzstd 模块或系统 zstd 命令后重试。（尝试：%s）"
        % "；".join(errors)
    )


def _dsh_read_text(session_file: str) -> str:
    """解压 DSH 会话文件为明文 JSONL 文本；后端缺失时抛 RuntimeError。"""
    with open(_longpath(session_file), "rb") as f:
        raw = f.read()
    return _dsh_decompress_bytes(raw).decode("utf-8", "replace")


def _dsh_write_text(session_file: str, text: str) -> None:
    """把明文 JSONL 以 zstd 压缩写入 DSH 会话文件；后端缺失时抛 RuntimeError。"""
    _decompress, compress, _name = _dsh_zstd_backend()
    if compress is None:
        raise RuntimeError(
            "未检测到可用的 zstd 压缩后端，无法写出 DSH 会话文件。"
            "请安装 zstandard / pyzstd 模块或系统 zstd 命令后重试。"
        )
    payload = compress(text.encode("utf-8"))
    lp = _longpath(session_file)
    os.makedirs(os.path.dirname(lp), exist_ok=True)
    with open(lp, "wb") as f:
        f.write(payload)


def _register_workbuddy_session(wb_home: str, sid: str, cwd: str, title: str,
                                created_ms: int, updated_ms: int, warn) -> None:
    """把新会话登记进 ``workbuddy.db`` 的 ``sessions``（+ ``workspaces``）表。

    仅做**新增**（``insert or ignore``），绝不改写既有行；缺库或写失败只提示不中断
    （会话事件流 jsonl 已落盘，保留现场便于排查）。
    """
    db = os.path.join(wb_home, "workbuddy.db")
    if not os.path.isfile(db):
        warn("未找到 WorkBuddy 会话索引库 %s：已写出会话事件流，但界面列表可能看不到该会话。" % db)
        return
    try:
        con = _open_sqlite_rw(db)
    except sqlite3.Error as exc:
        warn("打开 WorkBuddy 会话索引库失败：%s（会话事件流仍已写出）" % exc)
        return
    try:
        row = con.execute(
            "select user_id from sessions where user_id is not null and user_id <> '' limit 1"
        ).fetchone()
        user_id = row[0] if row else "imported"
        con.execute(
            "insert or ignore into sessions(id, cwd, user_id, title, status, created_at,"
            " updated_at, last_activity_at, is_playground, source_mode, mode)"
            " values(?,?,?,?,?,?,?,?,?,?,?)",
            (sid, cwd, user_id, title, "completed", created_ms, updated_ms, updated_ms,
             0, "import", "craft"),
        )
        con.execute(
            "insert into workspaces(path, last_opened_at) values(?,?)"
            " on conflict(path) do update set last_opened_at=max(last_opened_at, excluded.last_opened_at)",
            (cwd, updated_ms),
        )
        con.commit()
    except sqlite3.Error as exc:
        warn("写入 WorkBuddy 会话索引失败：%s（会话事件流仍已写出，可稍后手动重试）" % exc)
    finally:
        con.close()


def _dsh_workspace_dirname(cwd: str) -> str:
    """由工作区路径推导 DSH 会话目录名。

    规则由本机 ``~/.dsh/sessions/`` 的**真实目录名**反推，并用
    ``workspace.json`` 里登记的全部工作区路径回归验证（6/6 命中）：

    - 路径分隔符 ``\\`` / ``/`` -> ``-``
    - 盘符冒号 ``:`` -> 丢弃（``D:\\...`` -> ``D-...``，不是 ``D--...``）
    - ASCII 字母 / 数字 / ``-`` / ``_`` / ``.`` -> 原样保留（大小写不折叠）
    - 其余字符（含中文等非 ASCII）-> ``~`` + 码点大写十六进制（至少 4 位）

    例：``D:\\project\\demo\\子项目\\demo_common``
    -> ``--D-project-demo-~5B50~9879~76EE-demo_common--``
    """
    out: list[str] = []
    for ch in cwd or "":
        if ch in "\\/":
            out.append("-")
        elif ch == ":":
            continue
        elif ch.isascii() and (ch.isalnum() or ch in "-_."):
            out.append(ch)
        else:
            out.append("~%04X" % ord(ch))
    s = "".join(out).strip("-")
    return "--%s--" % (s or "imported-workspace")


def workbuddy_project_slug(workspace: str) -> str:
    """由工作区路径（或已编码串）推导 WorkBuddy ``projects/<slug>/`` 目录名。

    规则由本机 ``~/.workbuddy/projects/`` 的**真实目录名**反推，并用
    ``workbuddy.db`` 的 sessions.cwd 全量回归验证（11/11 命中）：

    - 盘符字母 -> 小写；冒号 ``:`` -> 丢弃
    - 路径分隔符 ``\\`` / ``/`` -> ``-``
    - 其余字符**原样保留**（含中文、空格、``【】``、``▶︎``、``.``、``_``）

    例：``D:\\Desktop\\示例资料`` -> ``d-Desktop-示例资料``

    ⚠️ 旧实现对非 ``[\\w-]`` 字符一律压成 ``-``，会把中文工作区写成
    ``d-Desktop-----``、把盘符写成大写 ``D-...``，与产品真实落点不一致，
    导致导入的会话在 WorkBuddy 里散落到「另一个工作区」。
    """
    p = (workspace or "").strip()
    if len(p) >= 2 and p[1] == ":":
        p = p[0].lower() + p[2:]
    p = p.replace("\\", "-").replace("/", "-")
    return p.strip("-") or "imported-workspace"


def _register_dsh_session(dsh_home: str, sid: str, workspace_dir: str, cwd: str,
                          title: str, created_ms: int, warn) -> None:
    """把新会话登记进 DSH 的 ``storages/workspace.json``（+ ``session_projcache.json``）。

    DSH 会话列表按 ``workspace.json`` 的「工作区 → sessionIds」索引，未登记则界面看不到。
    仅在既有结构上**追加**，不改动其它工作区与会话。
    """
    storages = os.path.join(dsh_home, "storages")
    ws_file = os.path.join(storages, "workspace.json")
    if not os.path.isfile(ws_file):
        warn("未找到 DSH 工作区索引 %s：会话文件已写出，但界面列表可能看不到该会话。" % ws_file)
        return
    try:
        data = _read_json(ws_file)
        if not isinstance(data, dict):
            raise ValueError("workspace.json 结构异常")
        tables = data.setdefault("tables", {})
        workspaces = tables.setdefault("workspaces", {})
        norm = os.path.normcase(os.path.normpath(cwd)) if cwd else ""
        ws_id = None
        for key, val in workspaces.items():
            if isinstance(val, dict) and norm and \
                    os.path.normcase(os.path.normpath(val.get("path") or "")) == norm:
                ws_id = key
                break
        if ws_id is None:
            ws_id = str(uuid.uuid4())
            base = os.path.basename((cwd or "").rstrip("\\/")) or workspace_dir
            workspaces[ws_id] = {"path": cwd, "title": base, "sessionIds": []}
        entry = workspaces[ws_id]
        if not isinstance(entry, dict):
            entry = workspaces[ws_id] = {"path": cwd, "title": workspace_dir, "sessionIds": []}
        ids = entry.setdefault("sessionIds", [])
        if sid not in ids:
            ids.append(sid)
        glob = data.setdefault("global", {})
        if isinstance(glob, dict):
            wids = glob.setdefault("workspaceIds", [])
            if ws_id not in wids:
                wids.append(ws_id)
        _write_json(ws_file, data)
    except (OSError, ValueError, TypeError) as exc:
        warn("登记 DSH 工作区索引失败：%s（会话文件已写出）" % exc)
        return

    # 会话统计缓存：尽力更新，失败不影响主流程（DSH 会自行重建）
    cache_file = os.path.join(storages, "session_projcache.json")
    try:
        cdata = _read_json(cache_file)
        if isinstance(cdata, dict):
            tbl = cdata.setdefault("tables", {}).setdefault("sessions", {})
            item = tbl.setdefault(sid, {})
            item["identity"] = {"createdAt": created_ms, "cwd": cwd}
            _write_json(cache_file, cdata)
    except (OSError, ValueError, TypeError):
        pass




# --------------------------------------------------------------------------- #
# 中间模型
# --------------------------------------------------------------------------- #
@dataclass
class SessionMessage:
    role: str                       # "user" / "assistant" / "system"
    content: str                   # 纯文本正文（已把多模态/工具调用拍平为可见文本）
    reasoning_content: str = ""    # 推理过程（Reasonix 有；CodeBuddy 无）
    tool_calls: list = field(default_factory=list)
    created_at: str = ""           # ISO 时间戳


@dataclass
class Session:
    source_tool: str               # "reasonix" / "codebuddy"
    title: str = ""                # 会话标题
    scope: str = ""                # 项目/作用域标识
    messages: list = field(default_factory=list)  # List[SessionMessage]


# --------------------------------------------------------------------------- #
# 解析器
# --------------------------------------------------------------------------- #
class SessionParser:
    """从原生格式解析为 ``Session`` 中间模型。"""

    # ---- Reasonix ----
    @staticmethod
    def parse_reasonix(session_jsonl: str, meta_json: Optional[str] = None) -> Session:
        """解析 Reasonix 会话。

        :param session_jsonl: ``<id>-session.jsonl`` 路径
        :param meta_json: 可选 ``<id>.jsonl.meta`` 路径（取标题/作用域）
        """
        title, scope = "", ""
        # meta 文件名推导：``<id>-session.jsonl`` -> ``<id>.jsonl.meta``
        if meta_json is None and session_jsonl.endswith("-session.jsonl"):
            candidate = session_jsonl[: -len("-session.jsonl")] + ".jsonl.meta"
            if os.path.exists(candidate):
                meta_json = candidate
        if meta_json and os.path.exists(meta_json):
            meta = _read_json(meta_json)
            title = meta.get("topic_title") or meta.get("title") or ""
            scope = meta.get("scope") or ""

        msgs: list = []
        raw = _read_text(session_jsonl)
        # 只按 "\n" 切行：JSONL 的行边界是 \n；str.splitlines() 还会在
        # U+2028/U+2029/U+0085 等处断行，这些字符会原样出现在正文里，
        # 用 splitlines() 会把整行 JSON 切碎而静默丢弃该条消息。
        for line in raw.split("\n"):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            role = obj.get("role", "user")
            content = obj.get("content", "")
            if not isinstance(content, str):
                # content 可能是列表（多模态），拍平为文本
                content = json.dumps(content, ensure_ascii=False)
            msgs.append(SessionMessage(
                role=role,
                content=content,
                reasoning_content=obj.get("reasoning_content", "") or "",
                tool_calls=obj.get("tool_calls") or [],
                created_at=obj.get("createdAt") or obj.get("created_at") or "",
            ))
        return Session(source_tool="reasonix", title=title, scope=scope, messages=msgs)

    # ---- CodeBuddy ----
    @staticmethod
    def parse_codebuddy(session_dir: str) -> Session:
        """解析 CodeBuddy 单体会话目录（含 index.json 与 messages/）。"""
        idx = _read_json(os.path.join(session_dir, "index.json"))
        msgs_meta = {m.get("id"): m for m in idx.get("messages", [])}

        msgs: list = []
        messages_dir = os.path.join(session_dir, "messages")
        # 优先按 index.json 的 messages 列表顺序
        order = idx.get("messages", [])
        # 若 messages 目录另有 index.json（聚合索引），也尊重其顺序
        agg = os.path.join(messages_dir, "index.json")
        if os.path.exists(agg):
            agg_data = _read_json(agg)
            if isinstance(agg_data, dict) and "messages" in agg_data:
                order = agg_data["messages"]

        for m in order:
            mid = m.get("id") if isinstance(m, dict) else m
            if not mid:
                continue
            mpath = os.path.join(messages_dir, f"{mid}.json")
            if not os.path.exists(mpath):
                continue
            mobj = _read_json(mpath)
            role = mobj.get("role", "user")
            inner = mobj.get("message", "")
            # message 字段是内层 JSON 字符串（双重编码）
            text = ""
            reasoning = ""
            try:
                inner_obj = json.loads(inner) if isinstance(inner, str) else inner
                if isinstance(inner_obj, dict):
                    # 常见结构：{"role","content":[{"type":"text","text":...}]}
                    content = inner_obj.get("content")
                    if isinstance(content, list):
                        parts = []
                        for part in content:
                            if isinstance(part, dict):
                                if part.get("type") == "text":
                                    parts.append(part.get("text", ""))
                                elif "text" in part:
                                    parts.append(str(part.get("text", "")))
                                else:
                                    parts.append(json.dumps(part, ensure_ascii=False))
                            else:
                                parts.append(str(part))
                        text = "\n".join(p for p in parts if p)
                    elif isinstance(content, str):
                        text = content
                    else:
                        text = json.dumps(inner_obj, ensure_ascii=False)
                    reasoning = inner_obj.get("reasoning_content", "") or ""
            except (json.JSONDecodeError, TypeError):
                text = inner if isinstance(inner, str) else json.dumps(inner, ensure_ascii=False)
            msgs.append(SessionMessage(
                role=role,
                content=text,
                reasoning_content=reasoning,
                tool_calls=mobj.get("tool_calls") or [],
                created_at=mobj.get("createdAt") or mobj.get("created_at") or "",
            ))
        title = idx.get("title") or idx.get("name") or ""
        return Session(source_tool="codebuddy", title=title, scope="", messages=msgs)

    # ---- WorkBuddy ----
    @staticmethod
    def parse_workbuddy(session_jsonl: str) -> Session:
        """解析 WorkBuddy ``projects/<工作区编码>/<会话 id>.jsonl`` 事件流。

        事件流按时间顺序给出 user/assistant 消息、推理、工具调用与结果。本方法把
        「一段用户消息到下一段用户消息之间」的全部 assistant 产出（推理 + 工具调用 +
        工具结果 + 正文）**聚合为一条 assistant 消息**，得到与其它工具一致的用户/
        助手交替序列，便于跨软件往返而不丢内容。
        """
        title = ""
        msgs: list = []
        cur: "SessionMessage | None" = None

        def _ensure_assistant() -> "SessionMessage":
            nonlocal cur
            if cur is None:
                cur = SessionMessage(role="assistant", content="")
            return cur

        raw = _read_text(session_jsonl)
        for line in raw.split("\n"):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            etype = obj.get("type")
            ts = _ms_to_iso(obj.get("timestamp"))
            if etype == "ai-title":
                if not title:
                    title = obj.get("aiTitle") or obj.get("title") or ""
            elif etype == "message":
                role = obj.get("role") or "user"
                text = _flatten_wb_content(obj.get("content"))
                if role == "assistant":
                    a = _ensure_assistant()
                    if not a.created_at:
                        a.created_at = ts
                    a.content = _join_block(a.content, text)
                else:
                    if cur is not None:
                        msgs.append(cur)
                        cur = None
                    msgs.append(SessionMessage(role=role, content=text, created_at=ts))
            elif etype == "reasoning":
                a = _ensure_assistant()
                if not a.created_at:
                    a.created_at = ts
                a.reasoning_content = _join_block(
                    a.reasoning_content, _flatten_wb_reasoning(obj.get("rawContent"))
                )
            elif etype == "function_call":
                a = _ensure_assistant()
                if not a.created_at:
                    a.created_at = ts
                a.tool_calls.append({
                    "id": obj.get("callId") or "",
                    "name": obj.get("name") or "",
                    "arguments": obj.get("arguments") or "",
                })
            elif etype == "function_call_result":
                a = _ensure_assistant()
                out = obj.get("output")
                if isinstance(out, dict):
                    out_text = out.get("text") or ""
                elif isinstance(out, str):
                    out_text = out
                else:
                    out_text = ""
                if out_text:
                    a.content = _join_block(
                        a.content, "[%s] %s" % (obj.get("name") or "tool", out_text)
                    )
        if cur is not None:
            msgs.append(cur)
        return Session(source_tool="workbuddy", title=title, scope="", messages=msgs)

    # ---- ZCode ----
    @staticmethod
    def parse_zcode(db_path: str, session_id: str) -> Session:
        """解析 ZCode ``cli/db/db.sqlite`` 中的单个会话（只读打开，不写入）。"""
        con = _open_sqlite_ro(db_path)
        try:
            row = con.execute(
                "select id, title, directory, time_created from session where id = ?",
                (session_id,),
            ).fetchone()
            title = (row[1] if row else "") or ""
            scope = (row[2] if row else "") or ""
            msgs: list = []
            for mid, seq, mdata in con.execute(
                "select id, sequence, data from message where session_id = ? "
                "order by sequence asc, time_created asc",
                (session_id,),
            ):
                mobj = _loads_maybe(mdata)
                role = (mobj or {}).get("role") or "user"
                text_parts: list = []
                reasoning_parts: list = []
                tool_calls: list = []
                for (pdata,) in con.execute(
                    "select data from part where message_id = ? "
                    "order by sequence asc, time_created asc",
                    (mid,),
                ):
                    pobj = _loads_maybe(pdata)
                    if not isinstance(pobj, dict):
                        continue
                    ptype = pobj.get("type")
                    if ptype == "text":
                        text_parts.append(pobj.get("text") or "")
                    elif ptype == "reasoning":
                        reasoning_parts.append(pobj.get("text") or "")
                    elif ptype in ("tool", "tool-call", "tool_call"):
                        tool_calls.append({
                            "id": pobj.get("callID") or pobj.get("id") or "",
                            "name": pobj.get("tool") or pobj.get("name") or "",
                            "arguments": json.dumps(
                                pobj.get("state", pobj.get("arguments", {})),
                                ensure_ascii=False,
                            ),
                        })
                    elif ptype == "tool-result" or ptype == "tool_result":
                        txt = _flatten_any(pobj.get("output") or pobj.get("content"))
                        if txt:
                            text_parts.append("[%s] %s" % (pobj.get("tool") or "tool", txt))
                    else:
                        # 未知 part 类型：尽量保留可读文本，避免静默丢内容
                        extra = pobj.get("text")
                        if isinstance(extra, str) and extra:
                            text_parts.append(extra)
                msgs.append(SessionMessage(
                    role=role,
                    content="\n".join(p for p in text_parts if p),
                    reasoning_content="\n".join(p for p in reasoning_parts if p),
                    tool_calls=tool_calls,
                    created_at=_ms_to_iso((mobj or {}).get("time", {}).get("created")
                                          if isinstance((mobj or {}).get("time"), dict) else None),
                ))
            return Session(source_tool="zcode", title=title, scope=scope, messages=msgs)
        finally:
            con.close()

    # ---- DeepSeek Harness ----
    @staticmethod
    def parse_dsh(session_file: str) -> Session:
        """解析 DSH 会话文件（``session*.jsonl.zstd``，自动按最高代际取用）。

        需要可用的 zstd 后端；缺失时抛 :class:`RuntimeError`。

        ``session_file`` 也可传**会话目录**，此时自动取代际最高的
        ``session*.jsonl.zstd``。
        """
        if os.path.isdir(session_file):
            resolved = _dsh_latest_session_file(session_file)
            if not resolved:
                raise ValueError("指定的 DSH 会话目录内没有 session*.jsonl.zstd 文件")
            session_file = resolved
        text = _dsh_read_text(session_file)
        title = ""
        scope = ""
        msgs: list = []
        for line in text.split("\n"):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            etype = obj.get("type")
            data = obj.get("data") or {}
            if etype == "session":
                scope = obj.get("cwd") or ""
            elif etype == "session/title":
                title = title or (data.get("title") or "")
            elif etype == "user/message":
                msgs.append(SessionMessage(
                    role="user",
                    content=_flatten_any(data.get("content")),
                    created_at=_ms_to_iso(obj.get("time")),
                ))
            elif etype == "assistant/message":
                msg = data.get("message") or {}
                text_parts: list = []
                tool_calls: list = []
                for part in msg.get("content") or []:
                    if not isinstance(part, dict):
                        continue
                    if part.get("type") == "text":
                        text_parts.append(part.get("text") or "")
                    elif part.get("type") == "tool-call":
                        tool_calls.append({
                            "id": part.get("id") or "",
                            "name": part.get("name") or "",
                            "arguments": part.get("arguments") or "",
                        })
                m = SessionMessage(
                    role="assistant",
                    content="\n".join(p for p in text_parts if p),
                    tool_calls=tool_calls,
                    created_at=_ms_to_iso(obj.get("time")),
                )
                # 相邻助手消息（同一步多次输出）合并，保持用户名下内容完整
                if msgs and msgs[-1].role == "assistant" and tool_calls and not text_parts:
                    msgs[-1].tool_calls.extend(tool_calls)
                else:
                    msgs.append(m)
        return Session(source_tool="dsh", title=title, scope=scope, messages=msgs)



# --------------------------------------------------------------------------- #
# 原生复刻写入器
# --------------------------------------------------------------------------- #
class SessionWriter:
    """把 ``Session`` 中间模型以目标工具原生格式写出。"""

    # ---- 写为 Reasonix 原生会话 ----
    @staticmethod
    def write_reasonix(session: Session, sessions_dir: str, scope: str = "") -> str:
        """写出为 Reasonix 会话，返回新会话 id。

        不覆盖目标已有会话：生成全新 id。
        """
        sid = _new_reasonix_id()
        scope = scope or session.scope or "global-workspace"
        safe_scope = re.sub(r"[^\w\-]+", "-", scope).strip("-") or "global-workspace"
        # 实测 Reasonix 会话位于 projects/<scope>/sessions/ 下
        target_dir = _longpath(os.path.join(sessions_dir, safe_scope, "sessions"))
        os.makedirs(target_dir, exist_ok=True)

        # session.jsonl
        lines = []
        for m in session.messages:
            rec = {
                "role": m.role,
                "content": m.content,
                "createdAt": m.created_at or _now_iso(),
            }
            if m.reasoning_content:
                rec["reasoning_content"] = m.reasoning_content
            if m.tool_calls:
                rec["tool_calls"] = m.tool_calls
            lines.append(json.dumps(rec, ensure_ascii=False))
        _write_text(os.path.join(target_dir, f"{sid}-session.jsonl"), "\n".join(lines) + "\n")

        # jsonl.meta
        meta = {
            "id": sid,
            "created_at": session.messages[0].created_at if session.messages else _now_iso(),
            "updated_at": session.messages[-1].created_at if session.messages else _now_iso(),
            "topic_title": session.title or "导入会话（来自 %s）" % session.source_tool,
            "scope": safe_scope,
        }
        _write_json(os.path.join(target_dir, f"{sid}.jsonl.meta"), meta)
        return sid

    # ---- 写为 CodeBuddy 原生会话 ----
    @staticmethod
    def write_codebuddy(session: Session, history_root: str, workspace_id: str) -> str:
        """写出为 CodeBuddy 原生会话，返回新 sessionId。

        不覆盖目标已有会话：生成全新 sessionId。
        """
        session_id = _new_uuid()
        session_dir = _longpath(os.path.join(history_root, workspace_id, session_id))
        messages_dir = os.path.join(session_dir, "messages")
        os.makedirs(messages_dir, exist_ok=True)

        msg_ids: list = []
        for m in session.messages:
            mid = _new_uuid()
            msg_ids.append(mid)
            inner = {
                "role": m.role,
                "content": [{"type": "text", "text": m.content}],
            }
            if m.reasoning_content:
                inner["reasoning_content"] = m.reasoning_content
            outer = {
                "role": m.role,
                "message": json.dumps(inner, ensure_ascii=False),
                "id": mid,
                "references": [],
                "extra": {},
                "createdAt": m.created_at or _now_iso(),
            }
            _write_json(os.path.join(messages_dir, f"{mid}.json"), outer)

        # messages 聚合索引
        agg = {
            "messages": [
                {"id": mid, "type": "message", "role": session.messages[i].role,
                 "isComplete": True}
                for i, mid in enumerate(msg_ids)
            ]
        }
        _write_json(os.path.join(messages_dir, "index.json"), agg)

        # 会话级 index.json
        session_index = {
            "messages": [
                {"id": mid, "type": "message", "role": session.messages[i].role,
                 "isComplete": True}
                for i, mid in enumerate(msg_ids)
            ],
            "requests": [
                {
                    "id": _new_uuid(),
                    "type": "request",
                    "messages": msg_ids,
                    "state": "completed",
                    "startedAt": session.messages[0].created_at if session.messages else _now_iso(),
                    "usage": [],
                }
            ],
            "title": session.title or f"导入会话（来自 {session.source_tool}）",
        }
        _write_json(os.path.join(session_dir, "index.json"), session_index)
        return session_id

    # ---- 写为 WorkBuddy 原生会话 ----
    @staticmethod
    def write_workbuddy(session: Session, wb_home: str, workspace_slug: str = "",
                        cwd: str = "", warn: "Callable[[str], None] | None" = None) -> str:
        """写出为 WorkBuddy 原生会话，返回新会话 id。

        需要**同时**落两份，否则界面读不到：

        1. ``projects/<工作区编码>/<会话 id>.jsonl`` 事件流（真正的内容）；
        2. ``workbuddy.db`` 的 ``sessions`` 行（界面列表的索引）。

        任一步失败都会通过 ``warn`` 提示，但不中断（jsonl 仍保留，便于手工排查）。
        """
        def _warn(msg: str) -> None:
            if warn:
                warn(msg)

        sid = str(uuid.uuid4())
        # 落点目录名必须与 WorkBuddy 产品自身的推导规则一致（见 workbuddy_project_slug），
        # 否则会话会被归到「另一个工作区」下。
        slug = workbuddy_project_slug(workspace_slug or cwd or "imported-workspace")
        proj_dir = os.path.join(wb_home, "projects", slug)
        os.makedirs(_longpath(proj_dir), exist_ok=True)

        events: list = []
        first_ms = last_ms = _now_ms()
        for i, m in enumerate(session.messages):
            ms = _iso_to_ms(m.created_at) if m.created_at else _now_ms()
            if i == 0:
                first_ms = ms
            last_ms = ms
            if m.role == "assistant":
                if m.reasoning_content:
                    events.append({
                        "id": str(uuid.uuid4()), "timestamp": ms, "type": "reasoning",
                        "content": [],
                        "rawContent": [{"type": "reasoning_text", "text": m.reasoning_content}],
                        "sessionId": sid,
                    })
                for tc in (m.tool_calls or []):
                    events.append({
                        "id": str(uuid.uuid4()), "timestamp": ms, "type": "function_call",
                        "callId": tc.get("id") or str(uuid.uuid4()),
                        "name": tc.get("name") or "",
                        "arguments": tc.get("arguments") or "",
                    })
                events.append({
                    "id": str(uuid.uuid4()), "timestamp": ms, "type": "message",
                    "role": "assistant", "status": "completed",
                    "content": [{"type": "output_text", "text": m.content}],
                    "sessionId": sid, "cwd": cwd,
                })
            else:
                events.append({
                    "id": str(uuid.uuid4()), "timestamp": ms, "type": "message",
                    "role": m.role or "user",
                    "content": [{"type": "input_text", "text": m.content}],
                    "sessionId": sid, "cwd": cwd,
                })

        title = session.title or "导入会话（来自 %s）" % session.source_tool
        events.append({
            "timestamp": last_ms, "type": "ai-title", "aiTitle": title,
            "sessionId": sid, "cwd": cwd,
        })

        jsonl_path = os.path.join(proj_dir, sid + ".jsonl")
        _write_text(jsonl_path, "\n".join(
            json.dumps(e, ensure_ascii=False) for e in events
        ) + "\n")

        _register_workbuddy_session(wb_home, sid, cwd or slug, title, first_ms, last_ms, _warn)
        return sid

    # ---- 写为 DeepSeek Harness 原生会话 ----
    @staticmethod
    def write_dsh(session: Session, dsh_home: str, cwd: str = "",
                  warn: "Callable[[str], None] | None" = None) -> str:
        """写出为 DSH 原生会话，返回新会话 id（``session-<uuid>``）。

        需要**同时**落：

        1. ``sessions/<workspace_dir>/<sid>/session.v3.jsonl.zstd``（内容）；
        2. ``storages/workspace.json`` 的工作区登记（会话列表按此索引）。

        依赖可用 zstd 后端；缺失时抛 :class:`RuntimeError`。
        """
        def _warn(msg: str) -> None:
            if warn:
                warn(msg)

        sid = _new_dsh_id()
        workspace_dir = _dsh_workspace_dirname(cwd or session.scope or "imported-workspace")
        session_dir = os.path.join(dsh_home, "sessions", workspace_dir, sid)

        first_ms = _now_ms()
        lines: list = [json.dumps({
            "type": "session", "version": 3, "id": sid,
            "createdAt": first_ms, "cwd": cwd or session.scope or "",
            "isSeeded": False, "delegationDepth": 0, "agentPreset": "standard",
        }, ensure_ascii=False)]

        seq = 0
        for i, m in enumerate(session.messages):
            ms = _iso_to_ms(m.created_at) if m.created_at else _now_ms()
            if i == 0:
                first_ms = ms
            if m.role == "assistant":
                content: list = []
                if m.content:
                    content.append({"type": "text", "text": m.content})
                for tc in (m.tool_calls or []):
                    content.append({
                        "type": "tool-call",
                        "id": tc.get("id") or "call_%d" % seq,
                        "name": tc.get("name") or "",
                        "arguments": tc.get("arguments") or "",
                    })
                seq += 1
                lines.append(json.dumps({
                    "type": "assistant/message", "seq": seq, "time": ms,
                    "data": {
                        "turn": 1, "step": seq,
                        "message": {"role": "assistant", "content": content,
                                    "source": {"kind": "model"}},
                    },
                }, ensure_ascii=False))
            else:
                seq += 1
                lines.append(json.dumps({
                    "type": "user/message", "seq": seq, "time": ms,
                    "data": {
                        "content": [{"type": "text", "text": m.content}],
                        "role": m.role or "user", "id": str(uuid.uuid4()),
                    },
                    "surfaceOp": "append",
                }, ensure_ascii=False))

        title = session.title or "导入会话（来自 %s）" % session.source_tool
        seq += 1
        lines.append(json.dumps({
            "type": "session/title", "seq": seq, "time": _now_ms(),
            "data": {"title": title, "messageSeqs": [], "source": {"kind": "fallback"}},
        }, ensure_ascii=False))

        _dsh_write_text(os.path.join(session_dir, "session.v3.jsonl.zstd"),
                        "\n".join(lines) + "\n")

        _register_dsh_session(dsh_home, sid, workspace_dir, cwd or session.scope or "",
                              title, first_ms, _warn)
        return sid



# --------------------------------------------------------------------------- #
# 辅助
# --------------------------------------------------------------------------- #
def _new_uuid() -> str:
    return uuid.uuid4().hex


def _new_reasonix_id() -> str:
    # Reasonix id 形如 20260804-023341.599017100-deepseek-v4-flash
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    frac = f".{uuid.uuid4().hex[:9]}"
    return f"{ts}{frac}-imported"


def _new_dsh_id() -> str:
    """DSH 会话 id / 目录名：``session-<uuid>``。"""
    return "session-%s" % uuid.uuid4()


def _sanitize_scope(scope: str) -> str:
    """把工作区路径编码为可作目录名的安全串（与其他适配器一致的正/反斜杠归一）。"""
    s = re.sub(r"[^\w\-]+", "-", scope or "").strip("-")
    return s or "imported-workspace"



def migrate_session(source_tool: str, source_path: str,
                     target_tool: str, target_root: str,
                     scope: str = "", workspace_id: str = "",
                     source_session_id: str = "",
                     warn: "Callable[[str], None] | None" = None) -> str:
    """统一入口：从 source 解析并以 target 原生格式写出，返回新会话 id。

    :param source_tool: ``"reasonix"`` / ``"codebuddy"`` / ``"workbuddy"`` /
        ``"zcode"`` / ``"dsh"``
    :param source_path:
        - reasonix：``<id>-session.jsonl``（或 ``.jsonl.meta``）路径
        - codebuddy：会话目录
        - workbuddy：``<会话 id>.jsonl`` 路径
        - zcode：``db.sqlite`` 路径（须同时给 ``source_session_id``）
        - dsh：``session*.jsonl.zstd`` 文件或会话目录
    :param target_tool: ``"reasonix"`` / ``"codebuddy"`` / ``"workbuddy"`` / ``"dsh"``
    :param target_root:
        - reasonix：``projects`` 父目录
        - codebuddy：history 根
        - workbuddy：``~/.workbuddy`` 数据根
        - dsh：``~/.dsh`` 数据根
    :param scope: reasonix 目标作用域 / workbuddy 工作区编码
    :param workspace_id: codebuddy 目标 workspaceId / workbuddy 与 dsh 的目标工作区路径（cwd）
    :param source_session_id: zcode 源会话 id
    :param warn: 可选警告回调（落点异常时调用，仅提示不阻断）。
    """
    def _warn(msg: str) -> None:
        if warn:
            warn(msg)

    # ---------------- 解析 ----------------
    if source_tool == "reasonix":
        if source_path.endswith(".jsonl.meta"):
            jsonl = source_path[:-len(".meta")]
            meta = source_path
        else:
            jsonl = source_path
            meta = source_path + ".meta" if os.path.exists(source_path + ".meta") else None
        session = SessionParser.parse_reasonix(jsonl, meta)
    elif source_tool == "codebuddy":
        session = SessionParser.parse_codebuddy(source_path)
    elif source_tool == "workbuddy":
        session = SessionParser.parse_workbuddy(source_path)
    elif source_tool == "zcode":
        if not source_session_id:
            raise ValueError("从 ZCode 导入必须指定 source_session_id（源会话 id）")
        session = SessionParser.parse_zcode(source_path, source_session_id)
    elif source_tool == "dsh":
        src = source_path
        if os.path.isdir(src):
            src = _dsh_latest_session_file(src) or ""
            if not src:
                raise ValueError("指定的 DSH 会话目录内没有 session*.jsonl.zstd 文件")
        session = SessionParser.parse_dsh(src)
    else:
        raise ValueError(f"不支持的源工具: {source_tool}")

    if not session.messages:
        _warn("源会话未解析出任何消息（可能文件为空或格式不符），仍将写出空会话。")

    # ---------------- 写出 ----------------
    if target_tool == "reasonix":
        # Reasonix 无登录用户 UUID 概念，按项目路径编码隔离（scope）。
        expected = detect_session_root(detect_current_uid())
        if expected and os.path.realpath(target_root) != os.path.realpath(expected):
            _warn(
                "迁移目标根目录 %s 不是当前登录用户 Reasonix 数据根（%s）。\n"
                "请确认目标机器上该项目路径与源机器一致，否则会话可能不被索引显示。"
                % (target_root, expected)
            )
        return SessionWriter.write_reasonix(session, target_root, scope=scope)
    elif target_tool == "codebuddy":
        # CodeBuddy 会话按「项目路径派生的 workspaceId」索引，且外层 Data/<uuid>
        # 为登录用户标识。落点必须落在当前登录用户的 detect_session_root() 之下，
        # 否则会话会写进「读不到」的孤立工作区。
        expected = detect_session_root(detect_current_uid())
        if expected and os.path.realpath(target_root) != os.path.realpath(expected):
            _warn(
                "迁移目标根目录 %s 不是当前登录用户 CodeBuddy 数据根（%s）。\n"
                "会话将落在其他用户读不到的位置，请确认目标机器项目路径同源。"
                % (target_root, expected)
            )
        wid = workspace_id
        if not wid:
            wid = _new_uuid()
            _warn(
                "未指定目标 workspaceId，已生成新的随机 workspaceId。\n"
                "CodeBuddy 按项目路径派生 workspaceId 索引会话，若目标机器不存在该"
                "随机工作区，会话可能不被索引显示。请确认目标机器项目路径一致，"
                "或显式传入与源机器对应的 workspaceId。"
            )
        else:
            ws_dir = os.path.join(target_root, wid)
            if not os.path.exists(ws_dir):
                _warn(
                    "目标工作区 %s 在目标机器上不存在，已照常写入。\n"
                    "CodeBuddy 按项目路径派生 workspaceId 索引会话，若该工作区在目标"
                    "机器未被打开过 / 项目路径不一致，会话可能不被索引显示。\n"
                    "补救方法：把目标机器项目放到与源机器相同路径，或先打开该工程再重启 IDE。"
                    % wid
                )
        return SessionWriter.write_codebuddy(session, target_root, wid)
    elif target_tool == "workbuddy":
        # WorkBuddy 会话按「projects/<工作区编码>/」存放，且在 sessions 表登记。
        # workspace_id 复用为「目标工作区路径（cwd）」。
        return SessionWriter.write_workbuddy(
            session, target_root, workspace_slug=scope, cwd=workspace_id, warn=_warn
        )
    elif target_tool == "dsh":
        return SessionWriter.write_dsh(
            session, target_root, cwd=workspace_id, warn=_warn
        )
    else:
        raise ValueError(f"不支持的目标工具: {target_tool}")


# --------------------------------------------------------------------------- #
# 来源扫描（供「导入」对话框列出可导入会话）
# --------------------------------------------------------------------------- #
def default_source_root(tool: str) -> str:
    """各来源工具的默认数据根（导入对话框中「来源目录」的初始值）。"""
    home = os.path.expanduser("~")
    if tool == "reasonix":
        from .adapters.reasonix import _roaming_root
        return _roaming_root()
    if tool == "codebuddy":
        # 会话位于 <CodeBuddyIDE/<uid>>/history/<workspaceId>/<sessionId>/，
        # 与本模块 migrate_session(target="codebuddy") 约定一致：target_root = history 根。
        return os.path.join(detect_session_root(detect_current_uid()) or home, "history")
    if tool == "workbuddy":
        return os.path.join(home, ".workbuddy")
    if tool == "zcode":
        from .adapters.zcode import existing_zcode_homes
        homes = existing_zcode_homes()
        return homes[0] if homes else os.path.join(home, ".zcode")
    if tool == "dsh":
        return os.path.join(os.environ.get("DSH_HOME") or os.path.join(home, ".dsh"))
    return home


def default_target_root(tool: str) -> str:
    """各目标工具 ``migrate_session(target_root=...)`` 的默认值。"""
    home = os.path.expanduser("~")
    if tool == "reasonix":
        from .adapters.reasonix import _roaming_root
        return os.path.join(_roaming_root(), "projects")
    if tool == "codebuddy":
        # 与写出口径一致：target_root 传 history 根
        return os.path.join(detect_session_root(detect_current_uid()) or home, "history")
    if tool == "workbuddy":
        return os.path.join(home, ".workbuddy")
    if tool == "dsh":
        return os.environ.get("DSH_HOME") or os.path.join(home, ".dsh")
    return home


def list_target_workspaces(tool: str, root: str) -> list:
    """列出目标工具 ``root`` 下已存在的工作区标识（供用户选择，保证路径同源）。

    - codebuddy：返回 history 下的 workspaceId 列表；
    - 其它工具返回空列表（无「工作区 id」概念，直接给 cwd 即可）。
    """
    if tool != "codebuddy" or not os.path.isdir(root):
        return []
    try:
        return sorted(d for d in os.listdir(root) if os.path.isdir(os.path.join(root, d)))
    except OSError:
        return []


def list_source_sessions(tool: str, root: str) -> list:
    """列出来源工具 ``root`` 下可导入的会话。

    :return: 列表，每项 ``{"id","title","path","detail","cwd","workspace_id","scope"}``：

        - ``id``    ：migrate_session 所需的源标识（zcode 为会话 id，其余为路径）
        - ``title`` ：会话标题（尽力而为，取不到为空串）
        - ``path``  ：供 migrate_session 的 ``source_path``（zcode 为 db 路径）
        - ``detail``：附加信息（如所属工作区 / 时间），供界面副标题展示
        - ``cwd``   ：会话所属工作区路径（能取到即用于自动判定目标工作区）。
                      CodeBuddy 的目录名是 ``md5(路径)``，由 IDE 的「已打开文件夹」
                      记录反查（反查不到则留空，退回目标工具的默认落点）
        - ``workspace_id``：CodeBuddy 的 workspaceId（仅 codebuddy 有值）
        - ``scope`` ：Reasonix 的项目名（仅 reasonix 有值）

    以上三项「工作区线索」由 ``workspace_plan`` 消费，用于**自动**确定导入落点
    （见 ``ai_env_clone/workspace_plan.py``）。
    """
    if not root or not os.path.isdir(root):
        return []
    if tool == "reasonix":
        return _scan_reasonix(root)
    if tool == "codebuddy":
        return _scan_codebuddy(root)
    if tool == "workbuddy":
        return _scan_workbuddy(root)
    if tool == "zcode":
        return _scan_zcode(root)
    if tool == "dsh":
        return _scan_dsh(root)
    return []


def _scan_reasonix(root: str) -> list:
    """扫描 Reasonix ``<root>/projects/<scope>/sessions/<id>-session.jsonl``。"""
    out = []
    proj = root if os.path.basename(os.path.normpath(root)) == "projects" \
        else os.path.join(root, "projects")
    if not os.path.isdir(proj):
        return out
    for scope in sorted(os.listdir(proj)):
        sdir = os.path.join(proj, scope, "sessions")
        if not os.path.isdir(sdir):
            continue
        for fn in sorted(os.listdir(sdir)):
            if not fn.endswith("-session.jsonl"):
                continue
            p = os.path.join(sdir, fn)
            title = ""
            meta = p[: -len("-session.jsonl")] + ".jsonl.meta"
            if os.path.isfile(meta):
                try:
                    title = _read_json(meta).get("topic_title") or ""
                except (OSError, ValueError):
                    title = ""
            out.append({"id": p, "title": title, "path": p,
                        "detail": "项目 %s" % scope, "cwd": "",
                        "workspace_id": "", "scope": scope})
    return out


def _scan_codebuddy(root: str) -> list:
    """扫描 CodeBuddy ``<root>/<workspaceId>/<sessionId>/index.json``（``root`` = history 根）。

    兼容两种传参：直接给 ``history`` 根，或给其上层的 ``CodeBuddyIDE/<uid>``
    （自动下钻到 ``history``）。

    **工作区路径还原**：目录名 ``<workspaceId>`` 是 ``md5(项目路径)``（不可逆），会话的
    ``index.json`` 也没有结构化的工作区字段，所以必须从外部把 id 反查回路径，填入条目的
    ``cwd``。否则迁移到 WorkBuddy / DSH 时，判定会误以为「源会话没有工作区」，把会话扔进
    工具默认落点（``~/WorkBuddy/<时间戳>``）而不是原本的工程工作区。

    反查按可靠性排序：

    1. **备份包携带的映射**（``<会话根>/session-workspaces.json``，导出时生成、随包走）
       —— 跨机仍有效，也不依赖「先还原、后导入」的顺序；
    2. 本机 IDE 的「已打开文件夹」记录（:func:`~ai_env_clone.workspace_plan.codebuddy_workspace_path_index`）
       —— **只覆盖本机打开过的工程**，且必须先还原再导入才生效；
    3. 都没有时，条目会带上 ``workspace_candidates``（导出时记下的**正文候选路径**，
       只是线索），供界面在退默认落点前提醒用户「它原本可能属于哪里」。

    三者都还原不出来时 ``cwd`` 保持空串，由界面明确交代成因（而不是静默换落点）。
    """
    hist = root
    if not os.path.isdir(hist):
        return []

    def _has_session_index(base: str) -> bool:
        try:
            for d in os.listdir(base):
                dd = os.path.join(base, d)
                if not os.path.isdir(dd):
                    continue
                for s in os.listdir(dd):
                    if os.path.isfile(os.path.join(dd, s, "index.json")):
                        return True
        except OSError:
            return False
        return False

    # 若给的是 CodeBuddyIDE/<uid> 层（含 history 子目录），下钻到 history
    if not _has_session_index(hist) and os.path.isdir(os.path.join(hist, "history")):
        hist = os.path.join(hist, "history")

    try:
        from .workspace_plan import (codebuddy_backup_workspace_map,
                                     codebuddy_workspace_path_index)
        wid_to_path = codebuddy_workspace_path_index()
        # ``hist`` 此刻已是 history 根，映射文件在它上一层（会话根）下。
        backup_map = codebuddy_backup_workspace_map(
            os.path.dirname(hist.rstrip("\\/")) if hist else "") or {}
    except Exception:  # noqa: BLE001 - 反查是尽力而为，失败只是退回默认落点
        wid_to_path, backup_map = {}, {}
    # 备份包携带的映射**优先**：它是导出时落进包的，跨机有效、也不依赖
    # 「先还原、后导入」的操作顺序（IDE 记录则两者都依赖）。
    merged = dict(wid_to_path)
    for w, entry in (backup_map.get("workspaces") or {}).items():
        if isinstance(entry, dict) and (entry.get("path") or "").strip():
            merged[w] = entry["path"].strip()
    wid_to_path = merged

    out = []
    try:
        ws_ids = sorted(os.listdir(hist))
    except OSError:
        return out
    for wid in ws_ids:
        wdir = os.path.join(hist, wid)
        if not os.path.isdir(wdir):
            continue
        try:
            sid_list = sorted(os.listdir(wdir))
        except OSError:
            continue
        for sid in sid_list:
            sdir = os.path.join(wdir, sid)
            idx = os.path.join(sdir, "index.json")
            if not os.path.isfile(idx):
                continue
            title = ""
            try:
                data = _read_json(idx)
                if isinstance(data, dict):
                    title = data.get("title") or data.get("name") or ""
                    if not title:
                        # 部分版本把标题放在 conversations[].name
                        convs = data.get("conversations") or []
                        if convs and isinstance(convs[0], dict):
                            title = convs[0].get("name") or ""
            except (OSError, ValueError):
                title = ""
            if not title:
                title = _codebuddy_title_from_first_message(sdir, idx)
            cwd = wid_to_path.get(wid, "")
            detail = "工作区 %s" % wid[:12]
            if cwd:
                detail += "（%s）" % cwd
            item = {"id": sdir, "title": title, "path": sdir,
                    "detail": detail, "cwd": cwd, "workspace_id": wid}
            if not cwd:
                # 推不出路径时，把「导出时记下的正文候选」带出来 —— 至少能提醒用户
                # 这条会话原本属于哪里，而不是只显示一个不可逆的 id。
                entry = (backup_map.get("workspaces") or {}).get(wid) or {}
                cands = [p for p in (entry.get("candidates") or []) if p]
                if cands:
                    item["workspace_candidates"] = cands
            out.append(item)
    return out


def _codebuddy_title_from_first_message(session_dir: str, index_path: str) -> str:
    """CodeBuddy 新版 index.json 无 title 字段时，用首条用户消息前 40 字兜底。"""
    try:
        idx = _read_json(index_path)
        msgs = idx.get("messages") or []
        mid = None
        for m in msgs:
            if isinstance(m, dict) and m.get("role") == "user":
                mid = m.get("id")
                break
        if not mid and msgs and isinstance(msgs[0], dict):
            mid = msgs[0].get("id")
        if not mid:
            return ""
        mobj = _read_json(os.path.join(session_dir, "messages", "%s.json" % mid))
        text = _flatten_any(_loads_maybe(mobj.get("message")))
        text = " ".join((text or "").split())
        return text[:40]
    except (OSError, ValueError, TypeError, AttributeError):
        return ""


def _scan_workbuddy(root: str) -> list:
    """扫描 WorkBuddy ``<root>/projects/<工作区编码>/<会话 id>.jsonl``。

    标题优先取 ``workbuddy.db`` 的 sessions 表（快），取不到时留空。
    """
    out = []
    titles: dict = {}
    cwds: dict = {}
    db = os.path.join(root, "workbuddy.db")
    if os.path.isfile(db):
        try:
            con = _open_sqlite_ro(db)
            try:
                for sid, title, cwd in con.execute("select id, title, cwd from sessions"):
                    titles[sid] = title or ""
                    cwds[sid] = cwd or ""
            finally:
                con.close()
        except sqlite3.Error:
            pass
    proj = os.path.join(root, "projects")
    if not os.path.isdir(proj):
        return out
    for slug in sorted(os.listdir(proj)):
        sdir = os.path.join(proj, slug)
        if not os.path.isdir(sdir):
            continue
        for fn in sorted(os.listdir(sdir)):
            if not fn.endswith(".jsonl"):
                continue
            sid = fn[: -len(".jsonl")]
            p = os.path.join(sdir, fn)
            out.append({"id": p, "title": titles.get(sid, ""), "path": p,
                        "detail": "工作区 %s" % slug,
                        "cwd": cwds.get(sid, ""), "workspace_id": ""})
    return out


def _scan_zcode(root: str) -> list:
    """扫描 ZCode ``<root>/cli/db/db.sqlite`` 的 ``session`` 表。"""
    db = os.path.join(root, "cli", "db", "db.sqlite")
    if not os.path.isfile(db):
        # 允许直接传 db 路径
        db = root if root.endswith(".sqlite") or root.endswith(".db") else db
    if not os.path.isfile(db):
        return []
    out = []
    try:
        con = _open_sqlite_ro(db)
        try:
            cols = [r[1] for r in con.execute('pragma table_info("session")')]
            has_dir = "directory" in cols
            sql = "select id, title%s from session" % (", directory" if has_dir else "")
            rows = list(con.execute(sql))
        finally:
            con.close()
    except sqlite3.Error:
        return []
    for row in rows:
        sid, title = row[0], row[1]
        detail = row[2] if len(row) > 2 and row[2] else ""
        out.append({"id": sid, "title": title or "", "path": db, "detail": detail,
                    "cwd": detail, "workspace_id": ""})
    return out


def _scan_dsh(root: str) -> list:
    """扫描 DSH ``<root>/sessions/<workspace_dir>/<session-…>/``。

    标题取 ``storages/session_projcache.json``（避免解压大文件）。
    """
    out = []
    titles: dict = {}
    ws_paths: dict = {}   # workspace_dir 名 -> 工作区真实路径
    cache = os.path.join(root, "storages", "session_projcache.json")
    if os.path.isfile(cache):
        try:
            data = _read_json(cache)
            tbl = (data.get("tables") or {}).get("sessions") or {}
            for sid, item in tbl.items():
                try:
                    titles[sid] = (item.get("rows") or {}).get("title", {}).get("val") or ""
                except AttributeError:
                    titles[sid] = ""
        except (OSError, ValueError, AttributeError):
            pass
    ws_file = os.path.join(root, "storages", "workspace.json")
    if os.path.isfile(ws_file):
        try:
            data = _read_json(ws_file)
            for _wid, ent in ((data.get("tables") or {}).get("workspaces") or {}).items():
                if isinstance(ent, dict) and ent.get("path"):
                    ws_paths[_dsh_workspace_dirname(ent["path"])] = ent["path"]
        except (OSError, ValueError, AttributeError):
            pass
    sroot = os.path.join(root, "sessions")
    if not os.path.isdir(sroot):
        return out
    for wsdir in sorted(os.listdir(sroot)):
        wd = os.path.join(sroot, wsdir)
        if not os.path.isdir(wd):
            continue
        for sess in sorted(os.listdir(wd)):
            sd = os.path.join(wd, sess)
            if not os.path.isdir(sd):
                continue
            if not _dsh_latest_session_file(sd):
                continue
            out.append({"id": sd, "title": titles.get(sess, ""), "path": sd,
                        "detail": "工作区 %s" % wsdir,
                        "cwd": ws_paths.get(wsdir, ""), "workspace_id": ""})
    return out


