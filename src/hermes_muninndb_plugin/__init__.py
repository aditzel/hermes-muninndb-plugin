"""MuninnDB memory provider plugin for Hermes Agent.

A standalone memory-provider plugin that uses MuninnDB's MCP endpoint as the
backend for cross-session recall and write-behind persistence.
"""

from __future__ import annotations

__version__ = "0.2.1"

import json
import logging
import os
import queue
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional
import urllib.error
import urllib.request

try:  # Hermes runtime
    from agent.memory_provider import MemoryProvider
except Exception:  # pragma: no cover - standalone/unit-test fallback
    class MemoryProvider:  # type: ignore[override]
        pass

try:  # Hermes runtime
    from tools.registry import tool_error
except Exception:  # pragma: no cover - standalone/unit-test fallback
    def tool_error(message: str) -> str:
        return json.dumps({"error": message})


logger = logging.getLogger(__name__)

_CONFIG_FILENAME = "muninndb.json"
_DEFAULT_ENDPOINT = "http://127.0.0.1:8750/mcp"
_SENTINEL = object()


RECALL_SCHEMA = {
    "name": "muninndb_recall",
    "description": (
        "Search MuninnDB for context relevant to the current task. "
        "Use when you want deliberate long-term recall beyond the automatic memory injection."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "What to recall from MuninnDB."},
            "limit": {"type": "integer", "description": "Maximum memories to return (default from plugin config)."},
            "mode": {
                "type": "string",
                "enum": ["semantic", "recent", "balanced", "deep"],
                "description": "Recall mode. Defaults to the provider config.",
            },
            "threshold": {"type": "number", "description": "Minimum relevance score 0.0-1.0."},
        },
        "required": ["query"],
    },
}

REMEMBER_SCHEMA = {
    "name": "muninndb_remember",
    "description": (
        "Persist a durable memory to MuninnDB. Use for decisions, preferences, facts, constraints, or events "
        "that should survive across sessions."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "content": {"type": "string", "description": "The memory content to store."},
            "concept": {"type": "string", "description": "Short label for the memory."},
            "type": {"type": "string", "description": "Memory type, e.g. fact, decision, preference, event."},
            "summary": {"type": "string", "description": "Optional one-line summary."},
            "confidence": {"type": "number", "description": "Confidence score 0.0-1.0."},
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional extra tags to attach.",
            },
        },
        "required": ["content"],
    },
}

ENTITY_SCHEMA = {
    "name": "muninndb_entity",
    "description": (
        "Inspect a named entity in MuninnDB — metadata, related memories, and relationships. "
        "Useful when a person, project, service, or concept matters repeatedly."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Entity name to inspect."},
            "limit": {"type": "integer", "description": "Maximum related memories to include."},
        },
        "required": ["name"],
    },
}

STATUS_SCHEMA = {
    "name": "muninndb_status",
    "description": "Check MuninnDB vault health and capacity information.",
    "parameters": {
        "type": "object",
        "properties": {},
        "required": [],
    },
}

FORGET_SCHEMA = {
    "name": "muninndb_forget",
    "description": "Delete a specific MuninnDB memory by its ID. Use for cleanup, test canaries, or correcting bad writes.",
    "parameters": {
        "type": "object",
        "properties": {
            "id": {"type": "string", "description": "MuninnDB memory ID to remove."},
        },
        "required": ["id"],
    },
}


class _MCPClient:
    """Tiny JSON-RPC client for MuninnDB's MCP endpoint."""

    def __init__(self, endpoint: str, token: str = "", timeout: float = 10.0):
        self.endpoint = endpoint.strip()
        self.token = token.replace("Bearer ", "").strip()
        self.timeout = float(timeout)

    def call_tool(self, tool_name: str, arguments: Dict[str, Any]) -> Any:
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": tool_name,
                "arguments": arguments or {},
            },
        }
        body = json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"

        request = urllib.request.Request(self.endpoint, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"MuninnDB MCP call failed ({exc.code}): {detail or exc.reason}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"MuninnDB MCP call failed: {exc}") from exc

        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"MuninnDB returned non-JSON response: {raw[:200]}") from exc

        if decoded.get("error"):
            error = decoded["error"]
            raise RuntimeError(error.get("message") or str(error))

        return self._unwrap_result(decoded.get("result"))

    def health(self) -> Dict[str, Any]:
        health_url = self.endpoint.rstrip("/") + "/health"
        request = urllib.request.Request(health_url, method="GET")
        if self.token:
            request.add_header("Authorization", f"Bearer {self.token}")
        try:
            with urllib.request.urlopen(request, timeout=min(self.timeout, 5.0)) as response:
                return json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            raise RuntimeError(f"MuninnDB health check failed: {exc}") from exc

    @staticmethod
    def _unwrap_result(result: Any) -> Any:
        if not isinstance(result, dict):
            return result

        if "structuredContent" in result:
            return result["structuredContent"]

        content = result.get("content")
        if isinstance(content, list):
            texts: List[str] = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    texts.append(str(item.get("text", "")))
            joined = "\n".join(t for t in texts if t).strip()
            if not joined:
                return result
            try:
                return json.loads(joined)
            except json.JSONDecodeError:
                return {"text": joined}

        return result


class _WriteQueue:
    """SQLite-backed async write queue. Pending rows replay on startup."""

    def __init__(self, client: _MCPClient, db_path: Path, retry_delay: float = 2.0):
        self._client = client
        self._db_path = Path(db_path)
        self._retry_delay = max(float(retry_delay), 0.1)
        self._queue: "queue.Queue[Any]" = queue.Queue()
        self._thread = threading.Thread(target=self._run, name="muninndb-writer", daemon=True)
        self._local = threading.local()
        self._lock = threading.Lock()
        self._inflight = 0
        self._stopping = threading.Event()
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()
        self._thread.start()
        for row_id, tool_name, arguments_json in self._pending_rows():
            self._queue.put((row_id, tool_name, json.loads(arguments_json)))

    def _get_conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(str(self._db_path), timeout=30, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            self._local.conn = conn
        return conn

    def _init_db(self) -> None:
        conn = self._get_conn()
        conn.execute(
            """CREATE TABLE IF NOT EXISTS pending (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tool_name TEXT NOT NULL,
                arguments_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT
            )"""
        )
        conn.commit()

    def _pending_rows(self) -> list:
        conn = self._get_conn()
        return conn.execute(
            "SELECT id, tool_name, arguments_json FROM pending ORDER BY id ASC LIMIT 500"
        ).fetchall()

    def enqueue(self, tool_name: str, arguments: Dict[str, Any]) -> None:
        conn = self._get_conn()
        cur = conn.execute(
            "INSERT INTO pending (tool_name, arguments_json, created_at) VALUES (?, ?, ?)",
            (
                tool_name,
                json.dumps(arguments or {}, ensure_ascii=False),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        row_id = cur.lastrowid
        conn.commit()
        self._queue.put((row_id, tool_name, arguments or {}))

    def join(self, timeout: float = 5.0) -> None:
        deadline = time.time() + max(timeout, 0.0)
        while time.time() < deadline:
            with self._lock:
                idle = self._queue.empty() and self._inflight == 0
            if idle:
                return
            time.sleep(0.05)

    def shutdown(self, timeout: float = 5.0) -> None:
        self._stopping.set()
        self.join(timeout=timeout)
        self._queue.put(_SENTINEL)
        self._thread.join(timeout=max(timeout, 0.1))

    def _run(self) -> None:
        while True:
            try:
                item = self._queue.get(timeout=5)
            except queue.Empty:
                continue
            if item is _SENTINEL:
                return
            row_id, tool_name, arguments = item
            with self._lock:
                self._inflight += 1
            try:
                self._flush_row(row_id, tool_name, arguments)
            finally:
                with self._lock:
                    self._inflight = max(0, self._inflight - 1)

    def _flush_row(self, row_id: int, tool_name: str, arguments: Dict[str, Any]) -> None:
        try:
            self._client.call_tool(tool_name, arguments)
            conn = self._get_conn()
            conn.execute("DELETE FROM pending WHERE id = ?", (row_id,))
            conn.commit()
        except Exception as exc:
            logger.warning("MuninnDB async write failed", exc_info=True)
            conn = self._get_conn()
            conn.execute(
                "UPDATE pending SET attempts = attempts + 1, last_error = ? WHERE id = ?",
                (str(exc), row_id),
            )
            conn.commit()
            if not self._stopping.is_set():
                def _retry() -> None:
                    time.sleep(self._retry_delay)
                    if not self._stopping.is_set():
                        self._queue.put((row_id, tool_name, arguments))
                threading.Thread(target=_retry, name="muninndb-writer-retry", daemon=True).start()


_AsyncWriter = _WriteQueue


class MuninnDBMemoryProvider(MemoryProvider):
    """Hermes MemoryProvider backed by MuninnDB's MCP tool surface."""

    def __init__(self) -> None:
        self._config: Dict[str, Any] = {}
        self._client: Optional[_MCPClient] = None
        self._writer: Optional[_AsyncWriter] = None
        self._session_id = ""
        self._platform = "cli"
        self._agent_identity = "hermes"
        self._agent_workspace = ""
        self._user_id = "local"
        self._agent_context = "primary"
        self._active = False
        self._write_enabled = True
        self._prefetch_lock = threading.Lock()
        self._prefetch_thread: Optional[threading.Thread] = None
        self._prefetch_query = ""
        self._prefetch_text = ""

    @property
    def name(self) -> str:
        return "muninndb"

    def is_available(self) -> bool:
        config = self._load_config(self._resolve_hermes_home())
        endpoint = str(config.get("endpoint") or "").strip()
        return bool(endpoint)

    def initialize(self, session_id: str, **kwargs) -> None:
        hermes_home = self._resolve_hermes_home(kwargs.get("hermes_home"))
        self._config = self._load_config(hermes_home)
        self._session_id = session_id or ""
        self._platform = kwargs.get("platform") or "cli"
        self._agent_identity = kwargs.get("agent_identity") or "hermes"
        self._agent_workspace = kwargs.get("agent_workspace") or ""
        self._user_id = kwargs.get("user_id") or "local"
        self._agent_context = kwargs.get("agent_context") or "primary"
        self._active = bool(self._config.get("endpoint"))
        self._write_enabled = self._agent_context in {"", "primary", None}
        if not self._active:
            return

        self._client = _MCPClient(
            endpoint=str(self._config["endpoint"]),
            token=str(self._config.get("token") or ""),
            timeout=float(self._config.get("timeout") or 10.0),
        )
        if self._write_enabled:
            queue_path = hermes_home / "muninndb_queue.sqlite3"
            self._writer = _WriteQueue(
                self._client,
                queue_path,
                retry_delay=float(self._config.get("retry_delay") or 2.0),
            )

    def get_config_schema(self) -> List[Dict[str, Any]]:
        return [
            {
                "key": "endpoint",
                "description": "MuninnDB MCP endpoint URL",
                "default": _DEFAULT_ENDPOINT,
                "required": True,
            },
            {
                "key": "token",
                "description": "MuninnDB MCP bearer token (leave blank for unsecured/default-vault setups)",
                "secret": True,
                "required": False,
                "env_var": "MUNINN_MCP_TOKEN",
                "url": "https://muninndb.com/",
            },
            {
                "key": "vault",
                "description": "MuninnDB vault name",
                "default": "default",
                "required": True,
            },
        ]

    def save_config(self, values: Dict[str, Any], hermes_home: str) -> None:
        config_path = Path(hermes_home) / _CONFIG_FILENAME
        existing: Dict[str, Any] = {}
        if config_path.exists():
            try:
                existing = json.loads(config_path.read_text())
            except Exception:
                existing = {}
        merged = {**existing, **(values or {})}
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(json.dumps(merged, indent=2, sort_keys=True))

    def system_prompt_block(self) -> str:
        if not self._active:
            return ""
        lines = [
            "# MuninnDB Memory",
            f"Active. Vault: {self._config.get('vault', 'default')}.",
            "Use muninndb_recall for deliberate retrieval, muninndb_remember for durable writes, and muninndb_entity when an entity matters.",
        ]
        if not self._write_enabled:
            lines.append("Write-back is disabled for this agent context, so this session reads memory without mutating it.")
        return "\n".join(lines)

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        if not self._active or not self._config.get("auto_recall", True) or not self._client:
            return ""
        query = (query or "").strip()
        if not query:
            return ""
        with self._prefetch_lock:
            if self._prefetch_query == query and self._prefetch_text:
                return self._prefetch_text
        text = self._recall_text(query)
        with self._prefetch_lock:
            self._prefetch_query = query
            self._prefetch_text = text
        return text

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        if not self._active or not self._config.get("auto_recall", True) or not self._client:
            return
        query = (query or "").strip()
        if not query:
            return

        def _run() -> None:
            text = self._recall_text(query)
            with self._prefetch_lock:
                self._prefetch_query = query
                self._prefetch_text = text

        thread = threading.Thread(target=_run, name="muninndb-prefetch", daemon=True)
        self._prefetch_thread = thread
        thread.start()

    def sync_turn(self, user_content: str, assistant_content: str, *, session_id: str = "") -> None:
        if not self._active or not self._write_enabled or not self._config.get("auto_capture", True):
            return
        if not self._writer:
            return
        memory = self._build_turn_memory(user_content, assistant_content)
        self._writer.enqueue("muninn_remember", memory)

    def on_session_end(self, messages: List[Dict[str, Any]]) -> None:
        if self._writer:
            self._writer.join(timeout=float(self._config.get("flush_timeout") or 5.0))

    def on_memory_write(self, action: str, target: str, content: str) -> None:
        if action not in {"add", "replace"}:
            return
        if not self._active or not self._write_enabled or not self._config.get("mirror_builtin_memory", True):
            return
        if not self._writer or not (content or "").strip():
            return
        tags = self._base_tags(prefix_target=True)
        tags.extend(["builtin-memory", f"target:{target}"])
        args = {
            "vault": self._config.get("vault", "default"),
            "concept": f"Hermes {target} memory",
            "content": content.strip(),
            "type": "preference" if target == "user" else "fact",
            "confidence": 0.95,
            "tags": _dedupe(tags),
        }
        self._writer.enqueue("muninn_remember", args)

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return [RECALL_SCHEMA, REMEMBER_SCHEMA, ENTITY_SCHEMA, STATUS_SCHEMA, FORGET_SCHEMA]

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs) -> str:
        if not self._active or not self._client:
            return tool_error("MuninnDB is not configured")
        try:
            if tool_name == "muninndb_recall":
                query = str(args.get("query") or "").strip()
                if not query:
                    return tool_error("query is required")
                payload = self._build_recall_args(
                    query,
                    limit=args.get("limit"),
                    mode=args.get("mode"),
                    threshold=args.get("threshold"),
                )
                result = self._client.call_tool("muninn_recall", payload)
                memories = self._extract_memories(result)
                return json.dumps({"query": query, "results": memories, "formatted": self._format_recall(memories)})

            if tool_name == "muninndb_remember":
                content = str(args.get("content") or "").strip()
                if not content:
                    return tool_error("content is required")
                payload = {
                    "vault": self._config.get("vault", "default"),
                    "content": content,
                    "concept": args.get("concept") or _safe_concept(content),
                    "type": args.get("type") or "fact",
                    "summary": args.get("summary") or _shorten(content, 140),
                    "confidence": _as_float(args.get("confidence"), default=float(self._config.get("remember_confidence", 0.8))),
                    "tags": _dedupe(self._base_tags(prefix_target=True) + list(args.get("tags") or [])),
                }
                result = self._client.call_tool("muninn_remember", payload)
                return json.dumps(result)

            if tool_name == "muninndb_entity":
                entity_name = str(args.get("name") or "").strip()
                if not entity_name:
                    return tool_error("name is required")
                payload = {
                    "vault": self._config.get("vault", "default"),
                    "name": entity_name,
                    "limit": _as_int(args.get("limit"), default=8),
                }
                result = self._client.call_tool("muninn_entity", payload)
                return json.dumps(result)

            if tool_name == "muninndb_status":
                result = self._client.call_tool("muninn_status", {"vault": self._config.get("vault", "default")})
                return json.dumps(result)

            if tool_name == "muninndb_forget":
                memory_id = str(args.get("id") or "").strip()
                if not memory_id:
                    return tool_error("id is required")
                result = self._client.call_tool(
                    "muninn_forget",
                    {"vault": self._config.get("vault", "default"), "id": memory_id},
                )
                return json.dumps(result)

            return tool_error(f"Unknown MuninnDB tool: {tool_name}")
        except Exception as exc:
            return tool_error(str(exc))

    def shutdown(self) -> None:
        if self._writer:
            self._writer.shutdown(timeout=float(self._config.get("flush_timeout") or 5.0))
            self._writer = None

    def _recall_text(self, query: str) -> str:
        if not self._client:
            return ""
        try:
            result = self._client.call_tool("muninn_recall", self._build_recall_args(query))
        except Exception:
            logger.debug("MuninnDB recall failed", exc_info=True)
            return ""
        memories = self._extract_memories(result)
        return self._format_recall(memories)

    def _build_recall_args(
        self,
        query: str,
        *,
        limit: Any = None,
        mode: Optional[str] = None,
        threshold: Any = None,
    ) -> Dict[str, Any]:
        context = [query]
        scope_hints = [
            f"Agent identity: {self._agent_identity}",
            f"Platform: {self._platform}",
            f"User id: {self._user_id}",
        ]
        if self._agent_workspace:
            scope_hints.append(f"Workspace: {self._agent_workspace}")
        context.extend(scope_hints)
        return {
            "vault": self._config.get("vault", "default"),
            "context": context,
            "limit": _as_int(limit, default=int(self._config.get("recall_limit", 6))),
            "mode": (mode or self._config.get("recall_mode") or "balanced"),
            "threshold": _as_float(threshold, default=float(self._config.get("recall_threshold", 0.45))),
        }

    def _build_turn_memory(self, user_content: str, assistant_content: str) -> Dict[str, Any]:
        max_chars = int(self._config.get("max_turn_chars", 4000))
        content_parts = [
            f"Hermes conversation turn.",
            f"Session: {self._session_id or 'unknown'}",
            f"Agent identity: {self._agent_identity}",
            f"Platform: {self._platform}",
            f"User id: {self._user_id}",
            f"User: {_shorten((user_content or '').strip(), max_chars // 2)}",
        ]
        if self._config.get("capture_assistant_turns", True) and (assistant_content or "").strip():
            content_parts.append(f"Assistant: {_shorten(assistant_content.strip(), max_chars // 2)}")
        body = "\n".join(part for part in content_parts if part)
        return {
            "vault": self._config.get("vault", "default"),
            "concept": _safe_concept(user_content or assistant_content or "Hermes turn"),
            "content": _shorten(body, max_chars),
            "type": "event",
            "summary": _shorten(user_content or assistant_content or "Hermes turn", 140),
            "confidence": _as_float(self._config.get("turn_confidence"), default=0.65),
            "tags": self._base_tags(prefix_target=True),
        }

    def _extract_memories(self, payload: Any) -> List[Dict[str, Any]]:
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        if not isinstance(payload, dict):
            return []
        for key in ("items", "results", "memories", "page", "engrams"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        if all(k in payload for k in ("content", "concept")):
            return [payload]
        return []

    def _format_recall(self, memories: Iterable[Dict[str, Any]]) -> str:
        memories = list(memories)
        if not memories:
            return ""
        lines = ["# MuninnDB recall"]
        for index, item in enumerate(memories, start=1):
            label = str(item.get("concept") or item.get("summary") or item.get("id") or f"Memory {index}")
            detail = str(item.get("summary") or item.get("content") or "").strip()
            score = item.get("score")
            prefix = f"- [{score:.2f}]" if isinstance(score, (int, float)) else "-"
            if detail:
                lines.append(f"{prefix} {label}: {detail}")
            else:
                lines.append(f"{prefix} {label}")
            why = item.get("why") or item.get("reason")
            if why:
                lines.append(f"  Why: {why}")
        return "\n".join(lines)

    def _load_config(self, hermes_home: Path) -> Dict[str, Any]:
        config_path = hermes_home / _CONFIG_FILENAME
        disk: Dict[str, Any] = {}
        if config_path.exists():
            try:
                disk = json.loads(config_path.read_text())
            except Exception:
                logger.debug("Failed to read %s", config_path, exc_info=True)
        mcp_fallback = _load_existing_muninn_mcp_config(hermes_home)
        endpoint = (
            os.environ.get("MUNINN_MCP_URL")
            or os.environ.get("MUNINN_ENDPOINT")
            or disk.get("endpoint")
            or mcp_fallback.get("endpoint")
            or ""
        )
        token = (
            os.environ.get("MUNINN_MCP_TOKEN")
            or disk.get("token")
            or mcp_fallback.get("token")
            or ""
        )
        vault = os.environ.get("MUNINN_VAULT") or disk.get("vault") or "default"
        return {
            "endpoint": str(endpoint).strip(),
            "token": str(token).strip(),
            "vault": str(vault).strip() or "default",
            "auto_recall": _as_bool(disk.get("auto_recall"), default=True),
            "auto_capture": _as_bool(disk.get("auto_capture"), default=True),
            "mirror_builtin_memory": _as_bool(disk.get("mirror_builtin_memory"), default=True),
            "capture_assistant_turns": _as_bool(disk.get("capture_assistant_turns"), default=True),
            "recall_mode": str(disk.get("recall_mode") or "balanced"),
            "recall_limit": _as_int(disk.get("recall_limit"), default=6),
            "recall_threshold": _as_float(disk.get("recall_threshold"), default=0.45),
            "remember_confidence": _as_float(disk.get("remember_confidence"), default=0.8),
            "turn_confidence": _as_float(disk.get("turn_confidence"), default=0.65),
            "timeout": _as_float(disk.get("timeout"), default=10.0),
            "flush_timeout": _as_float(disk.get("flush_timeout"), default=5.0),
            "retry_delay": _as_float(disk.get("retry_delay"), default=2.0),
            "max_turn_chars": _as_int(disk.get("max_turn_chars"), default=4000),
        }

    @staticmethod
    def _resolve_hermes_home(explicit: Any = None) -> Path:
        raw = explicit or os.environ.get("HERMES_HOME") or str(Path.home() / ".hermes")
        return Path(str(raw)).expanduser()

    def _base_tags(self, *, prefix_target: bool = False) -> List[str]:
        tags = ["hermes", self._platform, f"platform:{self._platform}", f"identity:{self._agent_identity}"]
        if self._session_id:
            tags.append(f"session:{self._session_id}")
        if self._user_id:
            tags.append(f"user:{self._user_id}")
        if self._agent_workspace:
            tags.append(f"workspace:{self._agent_workspace}")
        if prefix_target:
            tags.append("muninndb-provider")
        return _dedupe(tags)


def register(ctx) -> None:
    ctx.register_memory_provider(MuninnDBMemoryProvider())


def _load_existing_muninn_mcp_config(hermes_home: Path) -> Dict[str, str]:
    config_yaml = hermes_home / "config.yaml"
    if not config_yaml.exists():
        return {}
    try:
        import yaml  # type: ignore
    except Exception:
        return {}
    try:
        config = yaml.safe_load(config_yaml.read_text()) or {}
    except Exception:
        logger.debug("Failed to parse %s", config_yaml, exc_info=True)
        return {}

    block = _find_named_mapping(config, "muninndb")
    if not isinstance(block, dict):
        return {}
    endpoint = str(block.get("url") or "").strip()
    headers = block.get("headers") or {}
    auth = ""
    if isinstance(headers, dict):
        auth = str(headers.get("Authorization") or headers.get("authorization") or "").strip()
    token = auth.split(" ", 1)[1].strip() if auth.lower().startswith("bearer ") else ""
    result: Dict[str, str] = {}
    if endpoint:
        result["endpoint"] = endpoint
    if token:
        result["token"] = token
    return result


def _find_named_mapping(node: Any, key: str) -> Optional[Dict[str, Any]]:
    if isinstance(node, dict):
        direct = node.get(key)
        if isinstance(direct, dict):
            return direct
        for value in node.values():
            found = _find_named_mapping(value, key)
            if found:
                return found
    elif isinstance(node, list):
        for value in node:
            found = _find_named_mapping(value, key)
            if found:
                return found
    return None


def _as_bool(value: Any, *, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _as_int(value: Any, *, default: int) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _as_float(value: Any, *, default: float) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _shorten(text: str, limit: int) -> str:
    text = str(text or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def _safe_concept(text: str) -> str:
    cleaned = " ".join(str(text or "").strip().split())
    if not cleaned:
        return "Hermes memory"
    return _shorten(cleaned, 80)


def _dedupe(values: Iterable[str]) -> List[str]:
    seen = set()
    ordered: List[str] = []
    for value in values:
        item = str(value or "").strip()
        if not item or item in seen:
            continue
        ordered.append(item)
        seen.add(item)
    return ordered


__all__ = [
    "__version__",
    "MuninnDBMemoryProvider",
    "_MCPClient",
    "_WriteQueue",
    "_AsyncWriter",
    "register",
]
