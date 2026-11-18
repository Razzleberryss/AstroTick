"""Point the trade log at a temp file before config is imported.

``config.TRADE_LOG_FILE`` defaults to ``trades.csv`` in the process working
directory. ``RiskManager()`` creates that file on init. Setting the env var
here runs before test modules import config, so the suite does not leave a
``trades.csv`` in the repo root.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

_LOG_DIR = Path(tempfile.mkdtemp(prefix="astrotick-test-logs-"))
os.environ["TRADE_LOG_FILE"] = str(_LOG_DIR / "trades.csv")
