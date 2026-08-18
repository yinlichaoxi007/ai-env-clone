"""
跨工具会话数据迁移 —— 原生复刻写入。

目标：在 Reasonix 与 CodeBuddy 之间无损迁移「历史会话」。
- 解析两侧原生会话文件为统一中间模型 ``Session`` / ``SessionMessage``。
- 以目标工具**原生格式**重新写出，使其能被目标工具像原生会话一样打开。
- 全程不覆盖目标工具已有会话（新会话使用全新生成的 id，避免碰撞）。

格式契约（基于本机实测，2026-08-10）：

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

说明：CodeBuddy 路径嵌套 UUID 极易超过 Windows MAX_PATH(260)，统一用 ``\\\\?\\``
长路径前缀读写。

注意：Qoder 会话主库为加密 SQLite（local.db），无法实现无损迁移，本模块不支持。
"""

from __future__ import annotations

import json
import os
import re
import uuid
from dataclasses import dataclass, field
from typing import Callable, Optional

from .adapters.codebuddy import detect_current_uid, detect_session_root


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
        for line in raw.splitlines():
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


# --------------------------------------------------------------------------- #
# 辅助
# --------------------------------------------------------------------------- #
def _new_uuid() -> str:
    return uuid.uuid4().hex


def _new_reasonix_id() -> str:
    # Reasonix id 形如 20260804-023341.599017100-deepseek-v4-flash
    from datetime import datetime
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    frac = f".{uuid.uuid4().hex[:9]}"
    return f"{ts}{frac}-imported"


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def migrate_session(source_tool: str, source_path: str,
                     target_tool: str, target_root: str,
                     scope: str = "", workspace_id: str = "",
                     warn: "Callable[[str], None] | None" = None) -> str:
    """统一入口：从 source 解析并以 target 原生格式写出，返回新会话 id。

    :param source_tool: "reasonix" / "codebuddy"
    :param source_path: Reasonix 传 session.jsonl 路径；CodeBuddy 传会话目录
    :param target_tool: "reasonix" / "codebuddy"
    :param target_root: Reasonix 传 sessions 父目录（projects）；CodeBuddy 传 history 根
    :param scope: Reasonix 目标作用域（项目目录名）
    :param workspace_id: CodeBuddy 目标 workspaceId
    :param warn: 可选的警告回调。当写入位置可能落在目标机「读不到」的孤立
        工作区 / 根目录时调用（仍照常写入，仅提示，不阻断）。
    """
    def _warn(msg: str) -> None:
        if warn:
            warn(msg)

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
    else:
        raise ValueError(f"不支持的源工具: {source_tool}")

    if target_tool == "reasonix":
        # Reasonix 无登录用户 UUID 概念，按项目路径编码隔离（scope）。
        # 若 target_root 不是本机 Reasonix 当前 projects 根，仅提示落点确认。
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
            # 目标工作区在目标机是否存在：不存在则提示（仍写入）。
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
    else:
        raise ValueError(f"不支持的目标工具: {target_tool}")
