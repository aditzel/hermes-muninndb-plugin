.PHONY: test build clean smoke

test:
	python3 -m pytest tests/test_muninndb_plugin.py -q

smoke:
	python3 -m py_compile __init__.py cli.py src/hermes_muninndb_plugin/__init__.py src/hermes_muninndb_plugin/cli.py

build:
	python3 -m build

clean:
	rm -rf build dist .pytest_cache __pycache__ tests/__pycache__ src/hermes_muninndb_plugin.egg-info
