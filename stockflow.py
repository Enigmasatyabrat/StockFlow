#!/usr/bin/env python
"""Launcher shim kept so ``python stockflow.py FOLDER`` and run_stockflow.bat
keep working after the move to the ``stockflow/`` package.

This file is a launcher, not a compatibility surface: it re-exports nothing.
A regular package always wins over a same-named module, so ``import stockflow``
resolves to the package and this file can only ever run as a script -- which
makes it structurally impossible to end up with half-migrated legacy code.

Prefer ``stockflow FOLDER`` or ``python -m stockflow FOLDER``.
"""

import sys

if __name__ == "__main__":
    from stockflow.cli import main

    raise SystemExit(main(sys.argv[1:]))
