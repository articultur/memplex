"""Memplex -- multi-agent memory system.

Primary entry point::

    from memplex import MemplexService

    svc = MemplexService()
    result = svc.query("登录函数在哪")

CLI usage::

    memplex query "search text"
    memplex write --text "content"
    memplex health
"""

from memplex.core import CoreEngine
from memplex.service import MemplexService

__all__ = ["CoreEngine", "MemplexService", "main"]


def main() -> None:
    """CLI entry point for ``memplex`` command.

    Delegates to :func:`memplex.adapters.cli.main`.
    """
    import sys

    from memplex.adapters.cli import main as cli_main

    sys.exit(cli_main())
