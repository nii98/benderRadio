"""BendeRadio для Orange Pi One (Linux).

Пакет намеренно добавляет свою папку в sys.path: так и `python3 run.py`,
и `python3 -m benderadio` работают без установки пакета в систему.
"""

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

__version__ = "2.0.0"
