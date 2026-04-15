# MuninnDB memory provider for Hermes Agent

Bottom line:
- This is a standalone Hermes memory-provider plugin backed by MuninnDB.
- It uses MuninnDB's MCP endpoint for recall, explicit writes, and health/status checks.
- It adds deliberate memory tools plus automatic recall/capture hooks.

Quick install for Hermes users
- PyPI project: https://pypi.org/project/hermes-muninndb-plugin/
- Working install path today is the plugin directory layout Hermes expects, not just `site-packages`.
- Fastest working install:

```bash
git clone https://github.com/aditzel/hermes-muninndb-plugin.git ~/.hermes/plugins/muninndb
git -C ~/.hermes/plugins/muninndb checkout <release-tag>  # for example: v0.2.1
mkdir -p ~/.hermes/hermes-agent/plugins/memory
ln -sfn ~/.hermes/plugins/muninndb ~/.hermes/hermes-agent/plugins/memory/muninndb
hermes config set memory.provider muninndb
hermes memory setup
```

- Updating an existing install:

```bash
git -C ~/.hermes/plugins/muninndb fetch --tags
git -C ~/.hermes/plugins/muninndb checkout <release-tag>  # or: git -C ~/.hermes/plugins/muninndb pull --ff-only
hermes config set memory.provider muninndb
```

- `pip install hermes-muninndb-plugin` publishes the Python package successfully, but Hermes still needs the plugin files in `~/.hermes/plugins/muninndb` until external memory-provider discovery is fully landed upstream.

What it does
- Automatic recall before turns via `muninn_recall`
- Durable SQLite write-behind turn capture via `muninn_remember`
- Replays pending writes after crashes or restarts
- Mirrors built-in Hermes memory writes into MuninnDB
- Reuses the existing Muninn MCP server config from `config.yaml` when available, including its bearer token
- Exposes namespaced tools to the model:
  - `muninndb_recall`
  - `muninndb_remember`
  - `muninndb_forget`
  - `muninndb_entity`
  - `muninndb_status`
- Adds CLI helpers:
  - `hermes muninndb status`
  - `hermes muninndb ping`

Files
- `__init__.py` — Hermes plugin entrypoint wrapper
- `cli.py` — Hermes CLI wrapper
- `src/hermes_muninndb_plugin/__init__.py` — actual provider implementation
- `src/hermes_muninndb_plugin/cli.py` — actual CLI implementation
- `plugin.yaml` — Hermes plugin manifest
- `pyproject.toml` — packaging and test metadata
- `.github/workflows/ci.yml` — CI for tests/build
- `tests/test_muninndb_plugin.py` — unit tests

Installation

1. Put the plugin where Hermes can discover it.

Preferred layout when your Hermes version supports user-installed memory providers:

```bash
mkdir -p ~/.hermes/plugins/muninndb
rsync -a --delete /path/to/hermes-muninndb-plugin/ ~/.hermes/plugins/muninndb/
```

If your Hermes build still only scans bundled `plugins/memory/` directories, symlink it into the Hermes source tree instead:

```bash
ln -s /path/to/hermes-muninndb-plugin ~/.hermes/hermes-agent/plugins/memory/muninndb
```

2. Configure Hermes to use the provider.

```bash
hermes config set memory.provider muninndb
hermes memory setup
```

3. Or write the config directly.

`$HERMES_HOME/muninndb.json`

```json
{
  "endpoint": "http://127.0.0.1:8750/mcp",
  "vault": "default",
  "auto_recall": true,
  "auto_capture": true,
  "mirror_builtin_memory": true,
  "capture_assistant_turns": true,
  "recall_mode": "balanced",
  "recall_limit": 6,
  "recall_threshold": 0.45,
  "timeout": 10.0,
  "flush_timeout": 5.0,
  "max_turn_chars": 4000
}
```

Optional secret in `$HERMES_HOME/.env`:

```bash
MUNINN_MCP_TOKEN=your-token-here
```

You can also avoid duplicating the token entirely if Hermes already has a Muninn MCP server configured in `config.yaml`; the plugin will reuse that `Authorization: Bearer ...` header automatically.

You can also override config with env vars:
- `MUNINN_MCP_URL`
- `MUNINN_ENDPOINT`
- `MUNINN_MCP_TOKEN`
- `MUNINN_VAULT`

Behaviour mapping

Hermes lifecycle -> MuninnDB action
- `prefetch(query)` -> `muninn_recall`
- `queue_prefetch(query)` -> background `muninn_recall`
- `sync_turn(user, assistant)` -> background `muninn_remember`
- `on_memory_write(add|replace, target, content)` -> background `muninn_remember`
- `on_session_end(messages)` -> drain queued writes

Tool behaviour

`muninndb_recall`
- Purpose: deliberate recall when automatic injection is not enough
- Input: `query`, optional `limit`, `mode`, `threshold`
- Backend call: `muninn_recall`

`muninndb_remember`
- Purpose: explicit durable write
- Input: `content`, optional `concept`, `type`, `summary`, `confidence`, `tags`
- Backend call: `muninn_remember`

`muninndb_forget`
- Purpose: explicit cleanup/delete by memory id
- Input: `id`
- Backend call: `muninn_forget`

`muninndb_entity`
- Purpose: inspect an entity and its connected memories
- Input: `name`, optional `limit`
- Backend call: `muninn_entity`

`muninndb_status`
- Purpose: health/capacity snapshot
- Backend call: `muninn_status`

CLI

```bash
hermes muninndb status
hermes muninndb ping
```

Testing

```bash
make test
make smoke
make build
```

Equivalent direct commands:

```bash
python3 -m pytest tests/test_muninndb_plugin.py -q
python3 -m py_compile __init__.py cli.py src/hermes_muninndb_plugin/__init__.py src/hermes_muninndb_plugin/cli.py
python3 -m build
```

Design notes
- Uses stdlib HTTP (`urllib`) rather than third-party dependencies.
- Uses a durable SQLite async queue for write-behind persistence and crash recovery.
- Scopes recall and writes with platform, identity, session, and user tags.
- Falls back to the existing Hermes Muninn MCP server config in `config.yaml` so one source of truth can supply the token.
- For subagents/cron/flush contexts, write-back is disabled; recall still works.

Caveats
- Recall is semantic, so immediate exact-string lookups are not guaranteed to rank first without enough signal. Use `muninndb_entity` or Muninn's direct `muninn_read`/`muninn_recall` tools when you need forensic precision.
- This plugin talks to MuninnDB's MCP endpoint. If you prefer REST for some operations, that is a sensible follow-up, but MCP keeps the surface aligned with Muninn's richest toolset.
- The Hermes IDE plugin registry at `hermes-hq/plugins` is a different plugin system. Useful reference for packaging style, not the runtime contract used here.

Why this shape
- Minimal moving parts
- Good enough defaults
- No dependence on Muninn-specific Python SDKs that may shift underfoot
- Easy to inspect, patch, and extend without ritual sacrifice
