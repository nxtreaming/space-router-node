"""Build variant for Space Router Home Node.

The build variant determines which platform config dir is used (e.g.
``SpaceRouter`` vs ``SpaceRouter-Test`` on macOS) and which coordination
URL is the default.

Resolution order (Track P0 — see Section 13 of the v1.5 plan):

1. If ``app/_build_variant.py`` exists (frozen production binary), use
   that. CI stamps this file at build time.
2. Else, read from the persisted ``settings.json`` via
   :py:func:`app.settings_loader.load_provider_settings`. The lazy
   import avoids a circular-import risk and keeps CLI startup fast.
3. Final fallback: ``"production"``. We deliberately do NOT consult
   ``os.environ['SR_BUILD_VARIANT']`` — that env-var lookup was the
   root cause of the macOS Node ID rotation bug (PR #68 / Section 13).
   The env var is unstable across Finder vs shell launches, leading
   to two different config dirs, two different identity keys.

The module-level ``BUILD_VARIANT`` constant is computed lazily on first
attribute access via ``__getattr__`` so that simply importing the module
during early test bootstrap does not pay the settings.json round-trip
cost (and does not crash if settings.json is malformed).

Variants:

  - ``production``: standard release build (settings UI hidden)
  - ``test``: test build with advanced settings (env selection, mTLS toggle)
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

_cached: str | None = None


def get_build_variant() -> str:
    """Return the resolved build variant, caching the result.

    Tests can reset the cache via :py:func:`reset_cached_build_variant`.
    """
    global _cached
    if _cached is not None:
        return _cached

    # Priority 1: frozen production binaries.
    try:
        from app._build_variant import BUILD_VARIANT as _FROZEN  # type: ignore[import-not-found]
        _cached = _FROZEN
        return _cached
    except ImportError:
        pass

    # Priority 2: persisted settings.json. Lazy import avoids the
    # circular ``app.config`` ↔ ``app.variant`` chain that exists today.
    try:
        from app.settings_loader import load_provider_settings
        _cached = load_provider_settings().build_variant
        return _cached
    except Exception as e:  # noqa: BLE001
        # Settings load may fail during very early bootstrap (e.g. tests
        # importing ``app.variant`` with a malformed settings.json). Fall
        # through with a debug log — production code always has either
        # _build_variant.py or a valid settings.json.
        logger.debug("variant: falling back to default (settings load failed: %s)", e)
        _cached = "production"
        return _cached


def reset_cached_build_variant() -> None:
    """Clear the cached variant. Tests use this between fixtures."""
    global _cached
    _cached = None


def __getattr__(name: str) -> Any:
    # PEP 562 module __getattr__: lets ``from app.variant import BUILD_VARIANT``
    # keep working without paying the resolution cost at module import time.
    if name == "BUILD_VARIANT":
        return get_build_variant()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
