"""StockFlow -- AI-assisted preparation of stock photography for marketplaces.

A regular package (this ``__init__.py`` is what makes it one) always wins over
a same-named top-level module, so the legacy ``stockflow.py`` beside it can
only ever run as a script and can never be imported by mistake.
"""

from __future__ import annotations

from .config import VERSION

__version__ = VERSION
__all__ = ["VERSION", "__version__"]
