#!/usr/bin/env python3
"""Run a credential-free Janus CI lane without touching the user's Hermes home."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import tempfile
import time


ROOT = Path(__file__).resolve().parents[2]
MATERIALIZER_SMOKE = """
import importlib.util
import json
import pathlib
import sys

result = json.loads(pathlib.Path(sys.argv[1]).read_text())
hermes_home = pathlib.Path(sys.argv[2]).resolve()
plugin_dir = pathlib.Path(result['plugin_dir']).resolve()
assert plugin_dir.is_relative_to(hermes_home)
assert result['activation']['attempted'] is False
assert result['activated'] is False
assert plugin_dir.joinpath('plugin.yaml').exists()
assert plugin_dir.joinpath('__init__.py').exists()
assert plugin_dir.joinpath('cli.py').exists()
assert plugin_dir.joinpath('src', 'hermes_muninndb_plugin', '__init__.py').exists()

spec = importlib.util.spec_from_file_location('materialized_plugin', plugin_dir / '__init__.py')
module = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(module)
assert hasattr(module, 'register')
"""


def isolated_environment(work: Path) -> dict[str, str]:
    paths = {
        "HOME": work / "home",
        "HERMES_HOME": work / "hermes-home",
        "XDG_CONFIG_HOME": work / "config",
        "XDG_CACHE_HOME": work / "cache",
        "XDG_DATA_HOME": work / "data",
        "XDG_STATE_HOME": work / "state",
        "TMPDIR": work / "tmp",
    }
    for path in paths.values():
        path.mkdir(parents=True, exist_ok=True)
    return {
        **{key: str(value) for key, value in paths.items()},
        "PATH": os.environ.get("PATH", os.defpath),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "CI": "true",
        "PYTHONNOUSERSITE": "1",
        "PIP_CONFIG_FILE": os.devnull,
    }


_cancel_signal: int | None = None


class Cancelled(RuntimeError):
    """The first cancellation signal remains authoritative during cleanup."""


def request_cancellation(signum: int, _frame: object) -> None:
    global _cancel_signal
    if _cancel_signal is None:
        _cancel_signal = signum


def stop_process_group(process: subprocess.Popen) -> None:
    """Stop the command's session, including children left after its exit."""
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        process.wait(timeout=1)
        return
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        process.poll()  # Reap the direct child while waiting for its group.
        try:
            os.killpg(process.pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait(timeout=1)


def run(command: list[str], env: dict[str, str], **kwargs: object) -> None:
    if _cancel_signal is not None:
        raise Cancelled()
    print("+ " + shlex.join(command), flush=True)
    # The handler records intent without raising across Popen's launch window.
    # Each command owns a separate group so cleanup never signals this wrapper.
    process = subprocess.Popen(command, cwd=ROOT, env=env, start_new_session=True, **kwargs)
    try:
        while process.poll() is None:
            if _cancel_signal is not None:
                raise Cancelled()
            try:
                process.wait(timeout=0.05)
            except subprocess.TimeoutExpired:
                pass
        if _cancel_signal is not None:
            raise Cancelled()
        if process.returncode:
            raise subprocess.CalledProcessError(process.returncode, command)
    finally:
        stop_process_group(process)


def run_lane() -> None:
    with tempfile.TemporaryDirectory(prefix="hermes-janus-") as directory:
        work = Path(directory)
        env = isolated_environment(work)
        check_venv = work / "check-venv"
        run([sys.executable, "-m", "venv", str(check_venv)], env)
        env["VIRTUAL_ENV"] = str(check_venv)
        env["PATH"] = str(check_venv / "bin") + os.pathsep + env["PATH"]

        run(["python", "-m", "pip", "install", "--upgrade", "pip"], env)
        run(["python", "-m", "pip", "install", "-e", ".[dev]"], env)
        run(["python", "-m", "pytest"], env)
        run([
            "python", "-m", "py_compile", "__init__.py", "cli.py",
            "src/hermes_muninndb_plugin/__init__.py", "src/hermes_muninndb_plugin/cli.py",
        ], env)
        dist = work / "dist"
        run(["python", "-m", "build", "--outdir", str(dist)], env)

        wheel_venv = work / "wheel-venv"
        run(["python", "-m", "venv", str(wheel_venv)], env)
        wheel_env = {
            **env,
            "VIRTUAL_ENV": str(wheel_venv),
            "PATH": str(wheel_venv / "bin") + os.pathsep + env["PATH"],
        }
        wheel_python = str(wheel_venv / "bin" / "python")
        run([wheel_python, "-m", "pip", "install", "--upgrade", "pip"], wheel_env)
        wheels = sorted(dist.glob("*.whl"))
        if len(wheels) != 1:
            raise RuntimeError(f"Expected exactly one built wheel; found {len(wheels)}")
        run([str(wheel_venv / "bin" / "pip"), "install", str(wheels[0])], wheel_env)
        result = work / "result.json"
        with result.open("w") as output:
            run([
                str(wheel_venv / "bin" / "hermes-muninndb-install"),
                "--no-activate", "--json",
            ], wheel_env, stdout=output)
        run(["python", "-c", MATERIALIZER_SMOKE, str(result), env["HERMES_HOME"]], env)
        print("Wheel materializer passed with isolated HOME and activation disabled.", flush=True)


def runtime_matches(lane: str) -> bool:
    return (
        f"{sys.version_info.major}.{sys.version_info.minor}" == lane
        and sys.version_info.releaselevel == "final"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("lane", choices=("3.11", "3.12"))
    args = parser.parse_args(argv)
    if not runtime_matches(args.lane):
        print(f"Expected stable Python {args.lane}; got {sys.version.split()[0]}", file=sys.stderr)
        return 1
    print(f"Janus CI: Python {sys.version.split()[0]}; isolated wheel verification.", flush=True)
    global _cancel_signal
    _cancel_signal = None
    handled = (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)
    previous = {sig: signal.signal(sig, request_cancellation) for sig in handled}
    try:
        run_lane()
    except Cancelled:
        return 128 + _cancel_signal
    except subprocess.CalledProcessError as error:
        return (128 + _cancel_signal) if _cancel_signal is not None else (
            error.returncode if error.returncode >= 0 else 128 - error.returncode
        )
    except (RuntimeError, subprocess.TimeoutExpired) as error:
        print(str(error), file=sys.stderr)
        return 128 + _cancel_signal if _cancel_signal is not None else 1
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    return 128 + _cancel_signal if _cancel_signal is not None else 0



if __name__ == "__main__":
    raise SystemExit(main())
