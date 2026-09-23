#!/usr/bin/env python
"""SniffOut entry-point shim: imports from src/ and launches the app."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from src.webui import main  # noqa: E402

main()
