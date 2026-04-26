"""Single canonical config directory for Space Router Node.

The v1.5 stabilization plan unified the provider config dir to
``~/.spacerouter/`` on every platform — Linux, macOS, and Windows. The
prior macOS-native ``~/Library/Application Support/SpaceRouter[-Test]/``
location and the Windows ``%APPDATA%\\SpaceRouter`` location are both
abandoned. ``Path.home()`` resolves to the user profile on every
platform Python supports, so this works uniformly.

Legacy macOS data is migrated by :py:mod:`app.legacy_migration`; that
module runs before settings.json is loaded so the file ends up in the
right place when we look for it.
"""

from __future__ import annotations

from pathlib import Path


def config_dir(variant: str | None = None) -> Path:
    """Return the canonical config directory: ``~/.spacerouter``.

    The *variant* argument is accepted for backward-compatibility with
    callers from before the unification, but it is intentionally
    ignored. There is no longer a per-variant directory — variant lives
    in ``settings.json`` instead.
    """
    # *variant* is kept in the signature for callers that haven't been
    # updated yet; intentionally unused.
    del variant
    return Path.home() / ".spacerouter"
