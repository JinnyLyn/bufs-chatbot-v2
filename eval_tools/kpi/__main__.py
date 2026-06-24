"""``python -m eval_tools.kpi`` entrypoint — delegates to :func:`cli.main`.

Keeps the process-exit boundary thin: :func:`eval_tools.kpi.cli.main` returns
the gate exit code (0=GO / 1=NO-GO / 2=ERROR) and this module maps it to
``sys.exit`` so the shell sees the right status.
"""
from __future__ import annotations

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
