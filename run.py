#!/usr/bin/env python3
"""Точка входа: python3 run.py --config config.json"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from benderadio.app import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
