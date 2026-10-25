"""Temporary live acceptance probe. Never merge this branch."""

import sys


def test_python_lane_failure_isolation():
    assert sys.version_info[:2] != (3, 11), "Intentional Janus Python 3.11 failure probe"
