"""Temporary live acceptance probe. Never merge this branch."""

import sys
import time


def test_python_lane_cancellation_isolation():
    if sys.version_info[:2] == (3, 11):
        time.sleep(120)
