"""Materialize a pip-installed package into the Hermes plugin directory layout."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path
from typing import Any, Dict

from . import __version__

PLUGIN_NAME = "muninndb"
PACKAGE_NAME = "hermes_muninndb_plugin"
PACKAGE_VERSION = __version__
PLUGIN_DESCRIPTION = (
    "MuninnDB-backed Hermes memory provider using the Muninn MCP endpoint "
    "for recall and durable memory writes."
)
MATERIALIZED_MARKER = ".hermes-muninndb-materialized.json"
MANAGED_TOP_LEVEL = {
    "__init__.py",
    "cli.py",
    "plugin.yaml",
    "README.md",
    MATERIALIZED_MARKER,
    "src",
}
PACKAGE_SOURCE_FILES = ("__init__.py", "cli.py")

ROOT_INIT_TEMPLATE = '''"""Hermes plugin entrypoint wrapper for the packaged implementation."""

from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from hermes_muninndb_plugin import *  # noqa: F401,F403
'''

ROOT_CLI_TEMPLATE = '''"""Hermes CLI entrypoint wrapper for the packaged implementation."""

from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from hermes_muninndb_plugin.cli import *  # noqa: F401,F403
'''

RUNTIME_README_TEMPLATE = f'''# MuninnDB memory provider for Hermes Agent

This directory was materialized by the `hermes-muninndb-plugin` Python package.

What it contains
- `plugin.yaml`
- Hermes entrypoint shims (`__init__.py`, `cli.py`)
- a self-contained copy of `src/hermes_muninndb_plugin/`

Why this exists
- Hermes currently discovers memory-provider plugins from the plugin directory layout,
  not directly from `site-packages`.
- Python wheels do not support clean, standard post-install hooks, so the package ships
  an explicit materializer command instead of trying something cursed at install time.

If you need to rebuild this tree after upgrading the package:

```bash
hermes-muninndb-install
```
'''


def _resolve_hermes_home(explicit: Any = None) -> Path:
    raw = explicit or os.environ.get("HERMES_HOME") or str(Path.home() / ".hermes")
    return Path(str(raw)).expanduser()


def _render_plugin_yaml() -> str:
    return (
        f"name: {PLUGIN_NAME}\n"
        f"version: {PACKAGE_VERSION}\n"
        f'description: "{PLUGIN_DESCRIPTION}"\n'
        "hooks:\n"
        "  - on_session_end\n"
        "  - on_memory_write\n"
    )


def _render_marker() -> str:
    payload = {
        "package": "hermes-muninndb-plugin",
        "version": PACKAGE_VERSION,
        "materialized_at": datetime.now(timezone.utc).isoformat(),
    }
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def _read_package_source_files() -> Dict[str, str]:
    package_root = resources.files(PACKAGE_NAME)
    return {
        relative_name: package_root.joinpath(relative_name).read_text(encoding="utf-8")
        for relative_name in PACKAGE_SOURCE_FILES
    }


def _safe_remove(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)


def _looks_like_legacy_repo_clone(plugin_dir: Path) -> bool:
    return (
        (plugin_dir / "plugin.yaml").exists()
        and (plugin_dir / "src" / PACKAGE_NAME / "__init__.py").exists()
        and ((plugin_dir / ".git").exists() or (plugin_dir / "CHANGELOG.md").exists() or (plugin_dir / "LICENSE").exists())
    )


def _reserve_backup_path(plugin_dir: Path) -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    candidate = plugin_dir.with_name(f"{plugin_dir.name}-legacy-backup-{timestamp}")
    counter = 1
    while candidate.exists():
        candidate = plugin_dir.with_name(f"{plugin_dir.name}-legacy-backup-{timestamp}-{counter}")
        counter += 1
    return candidate


def _prepare_plugin_dir(plugin_dir: Path, force: bool) -> Path | None:
    plugin_dir = plugin_dir.expanduser()
    if plugin_dir.exists() and not plugin_dir.is_dir():
        raise RuntimeError(f"Target plugin path is not a directory: {plugin_dir}")

    backup_path: Path | None = None

    if plugin_dir.exists():
        existing_names = {entry.name for entry in plugin_dir.iterdir()}
        has_marker = (plugin_dir / MATERIALIZED_MARKER).exists()
        unmanaged = sorted(existing_names - MANAGED_TOP_LEVEL)
        if not has_marker and not force:
            if _looks_like_legacy_repo_clone(plugin_dir):
                backup_path = _reserve_backup_path(plugin_dir)
                plugin_dir.rename(backup_path)
            else:
                detail = f" Unmanaged entries: {', '.join(unmanaged)}." if unmanaged else ""
                raise RuntimeError(
                    f"Refusing to overwrite existing unmarked plugin directory {plugin_dir}.{detail} "
                    "Re-run with --force if you really mean it."
                )
        if force:
            shutil.rmtree(plugin_dir)

    plugin_dir.mkdir(parents=True, exist_ok=True)
    return backup_path


def _ensure_source_tree_link(link_path: Path, plugin_dir: Path, force: bool) -> Dict[str, str]:
    link_path = link_path.expanduser()
    link_path.parent.mkdir(parents=True, exist_ok=True)
    desired = plugin_dir.resolve()

    if link_path.is_symlink() and link_path.resolve() == desired:
        return {"path": str(link_path), "mode": "symlink", "status": "already-correct"}

    if link_path.exists() or link_path.is_symlink():
        managed_dir = link_path.is_dir() and (link_path / MATERIALIZED_MARKER).exists()
        if not force and not managed_dir:
            raise RuntimeError(
                f"Refusing to replace existing Hermes source-tree plugin path {link_path}. "
                "Re-run with --force if you want this installer to take over that path."
            )
        _safe_remove(link_path)

    try:
        link_path.symlink_to(plugin_dir, target_is_directory=True)
        return {"path": str(link_path), "mode": "symlink", "status": "created"}
    except OSError:
        shutil.copytree(plugin_dir, link_path)
        return {"path": str(link_path), "mode": "copy", "status": "created"}


def _activate_provider(hermes_home: Path) -> Dict[str, Any]:
    hermes_cli = shutil.which("hermes")
    if not hermes_cli:
        return {
            "attempted": True,
            "activated": False,
            "reason": "hermes CLI not found on PATH",
        }

    command = [hermes_cli, "config", "set", "memory.provider", PLUGIN_NAME]
    env = os.environ.copy()
    env["HERMES_HOME"] = str(hermes_home)
    proc = subprocess.run(command, capture_output=True, text=True, env=env)
    if proc.returncode != 0:
        return {
            "attempted": True,
            "activated": False,
            "command": command,
            "stdout": proc.stdout.strip(),
            "stderr": proc.stderr.strip(),
            "reason": "hermes config set failed",
        }
    return {
        "attempted": True,
        "activated": True,
        "command": command,
        "stdout": proc.stdout.strip(),
        "stderr": proc.stderr.strip(),
    }


def materialize_install(
    hermes_home: Any = None,
    *,
    plugin_dir: Any = None,
    source_tree_link: bool = True,
    activate: bool = False,
    force: bool = False,
) -> Dict[str, Any]:
    resolved_hermes_home = _resolve_hermes_home(hermes_home)
    target_plugin_dir = Path(str(plugin_dir)).expanduser() if plugin_dir else resolved_hermes_home / "plugins" / PLUGIN_NAME
    if not target_plugin_dir.is_absolute():
        target_plugin_dir = resolved_hermes_home / target_plugin_dir

    legacy_backup = _prepare_plugin_dir(target_plugin_dir, force=force)

    source_dir = target_plugin_dir / "src" / PACKAGE_NAME
    if source_dir.parent.exists():
        shutil.rmtree(source_dir.parent)
    source_dir.mkdir(parents=True, exist_ok=True)

    (target_plugin_dir / "__init__.py").write_text(ROOT_INIT_TEMPLATE, encoding="utf-8")
    (target_plugin_dir / "cli.py").write_text(ROOT_CLI_TEMPLATE, encoding="utf-8")
    (target_plugin_dir / "plugin.yaml").write_text(_render_plugin_yaml(), encoding="utf-8")
    (target_plugin_dir / "README.md").write_text(RUNTIME_README_TEMPLATE, encoding="utf-8")
    (target_plugin_dir / MATERIALIZED_MARKER).write_text(_render_marker(), encoding="utf-8")

    for relative_name, content in _read_package_source_files().items():
        (source_dir / relative_name).write_text(content, encoding="utf-8")

    link_result: Dict[str, Any] = {"status": "skipped"}
    if source_tree_link:
        link_path = resolved_hermes_home / "hermes-agent" / "plugins" / "memory" / PLUGIN_NAME
        link_result = _ensure_source_tree_link(link_path, target_plugin_dir, force=force)

    activation_result: Dict[str, Any] = {"attempted": False, "activated": False}
    if activate:
        activation_result = _activate_provider(resolved_hermes_home)

    return {
        "plugin_name": PLUGIN_NAME,
        "package_name": "hermes-muninndb-plugin",
        "version": PACKAGE_VERSION,
        "hermes_home": str(resolved_hermes_home),
        "plugin_dir": str(target_plugin_dir),
        "legacy_backup": str(legacy_backup) if legacy_backup else None,
        "source_tree_link": link_result,
        "activated": bool(activation_result.get("activated")),
        "activation": activation_result,
        "next_steps": ["hermes memory setup"],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hermes-muninndb-install",
        description="Materialize the MuninnDB Hermes plugin tree from the installed Python package.",
    )
    parser.add_argument("--hermes-home", help="Override HERMES_HOME (defaults to $HERMES_HOME or ~/.hermes).")
    parser.add_argument("--plugin-dir", help="Override the target plugin directory.")
    parser.add_argument("--force", action="store_true", help="Replace existing managed paths if necessary.")
    parser.add_argument(
        "--no-source-tree-link",
        action="store_true",
        help="Do not create/update ~/.hermes/hermes-agent/plugins/memory/muninndb.",
    )
    parser.add_argument(
        "--activate",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Run `hermes config set memory.provider muninndb` after materializing (default: true).",
    )
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON instead of human output.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = materialize_install(
            hermes_home=args.hermes_home,
            plugin_dir=args.plugin_dir,
            source_tree_link=not args.no_source_tree_link,
            activate=bool(args.activate),
            force=bool(args.force),
        )
    except Exception as exc:
        if args.json:
            print(json.dumps({"error": str(exc)}, indent=2))
        else:
            print(f"Materialization failed: {exc}")
        return 1

    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
        if result["activation"].get("attempted") and not result["activation"].get("activated"):
            return 1
        return 0

    print("Materialized MuninnDB Hermes plugin")
    print(f"  HERMES_HOME: {result['hermes_home']}")
    print(f"  Plugin dir:  {result['plugin_dir']}")
    if result.get("legacy_backup"):
        print(f"  Legacy repo backup: {result['legacy_backup']}")
    link = result["source_tree_link"]
    if link.get("status") != "skipped":
        print(f"  Source-tree path: {link['path']} ({link['mode']}, {link['status']})")
    activation = result["activation"]
    if activation.get("attempted"):
        state = "yes" if activation.get("activated") else "no"
        print(f"  Activated provider: {state}")
        if activation.get("reason") and not activation.get("activated"):
            print(f"  Activation note: {activation['reason']}")
    print("Next step:")
    print("  hermes memory setup")
    if activation.get("attempted") and not activation.get("activated"):
        return 1
    return 0


__all__ = [
    "MATERIALIZED_MARKER",
    "PACKAGE_VERSION",
    "PLUGIN_NAME",
    "build_parser",
    "main",
    "materialize_install",
]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
