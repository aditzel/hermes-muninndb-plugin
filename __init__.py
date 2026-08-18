"""Hermes plugin entrypoint wrapper for the packaged implementation.

Provides: MuninnDBMemoryProvider (MemoryProvider)
"""

from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from hermes_muninndb_plugin import MuninnDBMemoryProvider, register  # noqa: F401
