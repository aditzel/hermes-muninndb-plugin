# Contributing

## Development

Prerequisites
- Python 3.11+
- Hermes Agent available locally if you want to test the live plugin wiring

Setup
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .[dev]
```

Run tests
```bash
python3 -m pytest
```

Smoke-check the plugin entrypoints
```bash
python3 -m py_compile __init__.py cli.py src/hermes_muninndb_plugin/__init__.py src/hermes_muninndb_plugin/cli.py
```

Package build
```bash
python3 -m build
```

## Project layout
- `__init__.py` — Hermes plugin entrypoint wrapper
- `cli.py` — Hermes CLI entrypoint wrapper
- `src/hermes_muninndb_plugin/` — actual implementation
- `plugin.yaml` — Hermes memory-provider manifest
- `tests/` — pytest suite

## Release checklist
- Update `CHANGELOG.md`
- Run tests
- Run `python3 -m build`
- Verify live canary against MuninnDB if credentials are available
- Tag the release

## Commit style
Use conventional commits where possible:
- `feat:` new functionality
- `fix:` bug fix
- `docs:` documentation only
- `chore:` repo maintenance
