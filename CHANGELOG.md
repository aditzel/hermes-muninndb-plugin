# Changelog

All notable changes to this project will be documented here.

## [0.2.0] - 2026-04-14

Added
- Durable SQLite-backed write queue for MuninnDB write-behind persistence
- Replay of pending writes after restart/crash
- `muninndb_forget` tool for cleanup and debugging
- Automatic fallback to the existing Muninn MCP server config in `~/.hermes/config.yaml`
- `hermes muninndb status` and `hermes muninndb ping` CLI helpers
- `pyproject.toml`, CI workflow, manifest, and release metadata for a release-ready repo

Changed
- Moved implementation into `src/hermes_muninndb_plugin/` with thin Hermes entrypoint wrappers at the repo root
- Improved README with installation, auth, and operational notes

## [0.1.0] - 2026-04-14

Added
- Initial Hermes memory provider for MuninnDB using the MCP endpoint
- Automatic recall, explicit remember/entity/status tools, and basic async persistence
