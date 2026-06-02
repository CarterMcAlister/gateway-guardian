#!/usr/bin/env python3
"""Compatibility wrapper for running Gateway Guardian from a checkout."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ROOT_STR = str(ROOT)
sys.path = [ROOT_STR, *[entry for entry in sys.path if entry != ROOT_STR]]

from gateway_guardian import main


if __name__ == "__main__":
    main()
