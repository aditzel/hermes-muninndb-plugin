"""CLI helpers for the MuninnDB Hermes memory provider."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

try:  # Loaded as a proper package inside Hermes
    from . import MuninnDBMemoryProvider, _MCPClient
except Exception:  # pragma: no cover - standalone import fallback
    _MODULE_PATH = Path(__file__).with_name("__init__.py")
    _SPEC = importlib.util.spec_from_file_location("muninndb_plugin", _MODULE_PATH)
    if _SPEC is None or _SPEC.loader is None:  # pragma: no cover
        raise ImportError("Unable to load MuninnDB plugin module")
    _MODULE = importlib.util.module_from_spec(_SPEC)
    _SPEC.loader.exec_module(_MODULE)
    MuninnDBMemoryProvider = _MODULE.MuninnDBMemoryProvider
    _MCPClient = _MODULE._MCPClient


def muninndb_command(args) -> None:
    sub = getattr(args, "muninndb_command", None) or "status"
    if sub == "status":
        cmd_status(args)
        return
    if sub == "ping":
        cmd_ping(args)
        return
    print(f"Unknown muninndb command: {sub}")
    raise SystemExit(2)


def cmd_status(args) -> None:
    provider = MuninnDBMemoryProvider()
    hermes_home = provider._resolve_hermes_home(getattr(args, "target_profile_home", None))
    config = provider._load_config(hermes_home)
    print("MuninnDB memory provider")
    print(f"  Available: {'yes' if provider.is_available() else 'no'}")
    print(f"  Endpoint:  {config.get('endpoint') or '(not configured)'}")
    print(f"  Vault:     {config.get('vault')}")
    print(f"  Auto recall:  {config.get('auto_recall')}")
    print(f"  Auto capture: {config.get('auto_capture')}")
    print(f"  Mirror built-in memory: {config.get('mirror_builtin_memory')}")
    print(f"  HERMES_HOME: {hermes_home}")


def cmd_ping(args) -> None:
    provider = MuninnDBMemoryProvider()
    hermes_home = provider._resolve_hermes_home(getattr(args, "target_profile_home", None))
    config = provider._load_config(hermes_home)
    endpoint = config.get("endpoint")
    if not endpoint:
        print("MuninnDB is not configured. Run `hermes memory setup` first.")
        raise SystemExit(1)
    client = _MCPClient(endpoint=endpoint, token=str(config.get("token") or ""), timeout=float(config.get("timeout") or 10.0))
    try:
        health = client.health()
    except Exception as exc:
        print(f"Ping failed: {exc}")
        raise SystemExit(1)
    print(json.dumps(health, indent=2))


def register_cli(subparser) -> None:
    subparser.add_argument(
        "--target-profile-home",
        dest="target_profile_home",
        help="Override HERMES_HOME for inspection without switching profiles.",
    )
    subs = subparser.add_subparsers(dest="muninndb_command")
    subs.add_parser("status", help="Show the active MuninnDB plugin configuration")
    subs.add_parser("ping", help="Call the MuninnDB MCP health endpoint")
    subparser.set_defaults(func=muninndb_command)


__all__ = ["register_cli", "muninndb_command"]
