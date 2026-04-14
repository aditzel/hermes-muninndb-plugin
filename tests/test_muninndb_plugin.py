import importlib.util
import io
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch
import sqlite3
import time

import pytest


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "__init__.py"


@pytest.fixture
def plugin_module():
    spec = importlib.util.spec_from_file_location("muninndb_plugin", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["muninndb_plugin"] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class FakeHTTPResponse:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


@pytest.fixture(autouse=True)
def isolate_env(tmp_path, monkeypatch):
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.delenv("MUNINN_MCP_URL", raising=False)
    monkeypatch.delenv("MUNINN_ENDPOINT", raising=False)
    monkeypatch.delenv("MUNINN_MCP_TOKEN", raising=False)
    monkeypatch.delenv("MUNINN_VAULT", raising=False)
    return hermes_home


def test_provider_name_and_availability_from_saved_config(plugin_module, isolate_env):
    config_path = isolate_env / "muninndb.json"
    config_path.write_text(json.dumps({"endpoint": "http://127.0.0.1:8750/mcp", "vault": "hermes"}))

    provider = plugin_module.MuninnDBMemoryProvider()

    assert provider.name == "muninndb"
    assert provider.is_available() is True


def test_provider_loads_endpoint_and_token_from_existing_mcp_config(plugin_module, isolate_env):
    (isolate_env / "config.yaml").write_text(
        """
mcp_servers:
  muninndb:
    url: https://mcp.example.test/mcp
    headers:
      Authorization: Bearer test-token-123
""".strip()
        + "\n"
    )

    provider = plugin_module.MuninnDBMemoryProvider()

    assert provider.is_available() is True
    provider.initialize("sess-mcp", hermes_home=str(isolate_env), platform="cli")
    assert provider._config["endpoint"] == "https://mcp.example.test/mcp"
    assert provider._client is not None
    assert provider._client.token == "test-token-123"


def test_save_config_merges_existing_values(plugin_module, isolate_env):
    config_path = isolate_env / "muninndb.json"
    config_path.write_text(json.dumps({"auto_capture": False}))

    provider = plugin_module.MuninnDBMemoryProvider()
    provider.save_config({"endpoint": "https://muninn.example/mcp", "vault": "team"}, str(isolate_env))

    saved = json.loads(config_path.read_text())
    assert saved["endpoint"] == "https://muninn.example/mcp"
    assert saved["vault"] == "team"
    assert saved["auto_capture"] is False


def test_client_tool_call_builds_json_rpc_request_and_parses_text_json(plugin_module):
    client = plugin_module._MCPClient(
        endpoint="https://muninn.example/mcp",
        token="secret-token",
        timeout=9.0,
    )

    captured = {}

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["timeout"] = timeout
        captured["headers"] = dict(request.header_items())
        captured["payload"] = json.loads(request.data.decode("utf-8"))
        return FakeHTTPResponse(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "result": {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps({"items": [{"concept": "ADR", "content": "Use MuninnDB"}]})
                        }
                    ]
                },
            }
        )

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        result = client.call_tool("muninn_recall", {"context": ["memory"]})

    assert captured["url"] == "https://muninn.example/mcp"
    assert captured["timeout"] == 9.0
    assert captured["headers"]["Authorization"] == "Bearer secret-token"
    assert captured["payload"]["method"] == "tools/call"
    assert captured["payload"]["params"]["name"] == "muninn_recall"
    assert result == {"items": [{"concept": "ADR", "content": "Use MuninnDB"}]}


def test_write_queue_persists_rows_before_delivery(plugin_module, tmp_path):
    client = MagicMock()
    client.call_tool.side_effect = lambda *_args, **_kwargs: time.sleep(5)
    queue_db = tmp_path / "muninndb_queue.sqlite3"
    writer = plugin_module._WriteQueue(client, queue_db)

    writer.enqueue("muninn_remember", {"content": "remember this"})
    time.sleep(0.2)

    conn = sqlite3.connect(str(queue_db))
    rows = conn.execute("SELECT tool_name, arguments_json FROM pending").fetchall()
    conn.close()

    assert len(rows) >= 1
    assert rows[0][0] == "muninn_remember"
    assert json.loads(rows[0][1])["content"] == "remember this"

    writer.shutdown(timeout=0.1)


def test_write_queue_replays_pending_rows_on_restart(plugin_module, tmp_path):
    queue_db = tmp_path / "muninndb_queue.sqlite3"

    failing_client = MagicMock()
    failing_client.call_tool.side_effect = RuntimeError("network down")
    writer1 = plugin_module._WriteQueue(failing_client, queue_db)
    writer1.enqueue("muninn_remember", {"content": "recover me"})
    time.sleep(0.3)
    writer1.shutdown(timeout=0.2)

    conn = sqlite3.connect(str(queue_db))
    count_before = conn.execute("SELECT COUNT(*) FROM pending").fetchone()[0]
    conn.close()
    assert count_before >= 1

    success_client = MagicMock()
    success_client.call_tool.return_value = {"ok": True}
    writer2 = plugin_module._WriteQueue(success_client, queue_db)
    writer2.join(timeout=2.0)
    writer2.shutdown(timeout=0.2)

    success_client.call_tool.assert_called()
    conn = sqlite3.connect(str(queue_db))
    count_after = conn.execute("SELECT COUNT(*) FROM pending").fetchone()[0]
    conn.close()
    assert count_after == 0


def test_prefetch_formats_context_from_muninn_recall(plugin_module, isolate_env):
    (isolate_env / "muninndb.json").write_text(
        json.dumps({
            "endpoint": "https://muninn.example/mcp",
            "vault": "hermes",
            "auto_recall": True,
            "recall_limit": 4,
        })
    )

    provider = plugin_module.MuninnDBMemoryProvider()
    provider.initialize(
        "sess-123",
        hermes_home=str(isolate_env),
        platform="cli",
        agent_identity="athena",
        user_id="allan",
    )

    provider._client = MagicMock()
    provider._client.call_tool.return_value = {
        "items": [
            {
                "id": "01A",
                "concept": "Deployment decision",
                "summary": "Vercel is the default deploy target.",
                "score": 0.91,
                "why": "recent, reinforced",
            },
            {
                "id": "01B",
                "content": "Allan prefers terse bullet-first replies.",
                "score": 0.88,
            },
        ]
    }

    text = provider.prefetch("How should I deploy this?")

    assert "MuninnDB recall" in text
    assert "Deployment decision" in text
    assert "Vercel is the default deploy target." in text
    assert "Allan prefers terse bullet-first replies." in text
    provider._client.call_tool.assert_called_once()
    call_name, call_args = provider._client.call_tool.call_args[0]
    assert call_name == "muninn_recall"
    assert "How should I deploy this?" in call_args["context"]
    assert call_args["vault"] == "hermes"


def test_sync_turn_enqueues_turn_memory_with_scope_tags(plugin_module, isolate_env):
    (isolate_env / "muninndb.json").write_text(
        json.dumps({
            "endpoint": "https://muninn.example/mcp",
            "vault": "hermes",
            "auto_capture": True,
            "capture_assistant_turns": True,
        })
    )

    provider = plugin_module.MuninnDBMemoryProvider()
    provider.initialize(
        "sess-abc",
        hermes_home=str(isolate_env),
        platform="telegram",
        agent_identity="athena",
        user_id="user-42",
    )
    provider._writer = MagicMock()

    provider.sync_turn("Remember the deployment plan", "Use Vercel and keep the PR small.")

    provider._writer.enqueue.assert_called_once()
    tool_name, arguments = provider._writer.enqueue.call_args[0]
    assert tool_name == "muninn_remember"
    assert arguments["vault"] == "hermes"
    assert arguments["type"] == "event"
    assert "telegram" in arguments["tags"]
    assert "identity:athena" in arguments["tags"]
    assert "user:user-42" in arguments["tags"]
    assert "Use Vercel" in arguments["content"]


def test_handle_tool_call_routes_namespaced_tools(plugin_module, isolate_env):
    (isolate_env / "muninndb.json").write_text(json.dumps({"endpoint": "https://muninn.example/mcp", "vault": "hermes"}))

    provider = plugin_module.MuninnDBMemoryProvider()
    provider.initialize("sess-tools", hermes_home=str(isolate_env), platform="cli")
    provider._client = MagicMock()
    provider._client.call_tool.return_value = {"id": "01XYZ", "status": "stored"}

    payload = json.loads(
        provider.handle_tool_call(
            "muninndb_remember",
            {"content": "Allan prefers MuninnDB as source of truth", "type": "decision"},
        )
    )

    assert payload["status"] == "stored"
    provider._client.call_tool.assert_called_once()
    tool_name, tool_args = provider._client.call_tool.call_args[0]
    assert tool_name == "muninn_remember"
    assert tool_args["vault"] == "hermes"
    assert tool_args["content"] == "Allan prefers MuninnDB as source of truth"


def test_handle_tool_call_supports_forget(plugin_module, isolate_env):
    (isolate_env / "muninndb.json").write_text(json.dumps({"endpoint": "https://muninn.example/mcp", "vault": "hermes"}))

    provider = plugin_module.MuninnDBMemoryProvider()
    provider.initialize("sess-forget", hermes_home=str(isolate_env), platform="cli")
    provider._client = MagicMock()
    provider._client.call_tool.return_value = {"id": "01FORGET", "status": "forgotten"}

    payload = json.loads(provider.handle_tool_call("muninndb_forget", {"id": "01FORGET"}))

    assert payload["status"] == "forgotten"
    provider._client.call_tool.assert_called_once_with(
        "muninn_forget",
        {"vault": "hermes", "id": "01FORGET"},
    )


def test_register_exposes_memory_provider(plugin_module):
    ctx = MagicMock()
    plugin_module.register(ctx)
    ctx.register_memory_provider.assert_called_once()
    registered = ctx.register_memory_provider.call_args[0][0]
    assert isinstance(registered, plugin_module.MuninnDBMemoryProvider)
