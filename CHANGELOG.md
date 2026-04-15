# Changelog

All notable changes to this project will be documented here.

## [0.3.0] - 2026-04-14

Added
- `hermes-muninndb-install` / `hermes-muninndb-materialize` console scripts that materialize a self-contained Hermes plugin tree from the installed Python package
- Automatic creation of `~/.hermes/plugins/muninndb` with Hermes entrypoint shims plus a copied `src/hermes_muninndb_plugin/` runtime payload
- Automatic linking of `~/.hermes/hermes-agent/plugins/memory/muninndb` back to the materialized plugin tree

Changed
- README quick start now prefers PyPI install plus the materializer command instead of a repo clone
- CI now smoke-tests the installed wheel materializer path in a fresh virtual environment
- Publish workflow now opts into explicit PyPI attestations and README includes release badges

## [0.2.1] - 2026-04-14

Added
- Short, exact Hermes install/update instructions near the top of the README

Changed
- Pinned GitHub Actions to immutable SHAs and upgraded to current Node 24-safe action releases
- Pinned the PyPI publish action to an immutable release SHA

## [0.2.0] - 2026-04-14

Added
- Durable SQLite-backed write queue for MuninnDB write-behind persistence
- Replay of pending writes after restart/crash
- `muninndb_forget` tool for cleanup and debugging
- Automatic fallback to the existing Muninn MCP server config in `~/.hermes/config.yaml`
- `hermes muninndb status` and `hermes muninndb ping` CLI helpers
- `pyproject.toml`, CI workflow, manifest, and release metadata for a release-ready repo

Changed
- License set to Apache-2.0 for a public integration repo with explicit patent grant
- Moved implementation into `src/hermes_muninndb_plugin/` with thin Hermes entrypoint wrappers at the repo root
- Improved README with installation, auth, and operational notes

## [0.1.0] - 2026-04-14

Added
- Initial Hermes memory provider for MuninnDB using the MCP endpoint
- Automatic recall, explicit remember/entity/status tools, and basic async persistence
