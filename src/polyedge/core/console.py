"""Shared Rich Console instance for the entire app.

Respects POLYEDGE_NO_COLOR=1 env var for clean logs on DO App Platform.
When set, outputs plain text with no ANSI escape codes — readable in
DO Runtime Logs and the mobile app. Locally, you get full Rich colors.

Usage:
    from polyedge.core.console import console
"""

import os
import sys

from rich.console import Console

_no_color = os.environ.get("POLYEDGE_NO_COLOR", "").strip() in ("1", "true", "yes")

console = Console(
    force_terminal=not _no_color,
    force_jupyter=False,
    no_color=_no_color,
    highlight=not _no_color,
    file=sys.stdout,
)
