"""Top-level provider settings loader.

Resolution order (Track P0 of the v1.5 stabilization plan):

1. If ``settings.json`` exists → load and return it.
2. Else if ``spacerouter.env`` exists → migrate it to ``settings.json``,
   rename the env file to a ``.migrated.bak`` so we never re-migrate.
3. Else → fall back to env-var resolution via the legacy ``app.config``
   path, then **save** the resolved values as ``settings.json`` so the
   next launch is JSON-driven.

This is deliberately a thin wrapper. The full env-var sweep across
``app/main.py``, ``gui/api.py``, etc. is a follow-up track (P5/P10);
this module only adds the new entry point + the migration glue.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from app.settings_v2 import Settings

logger = logging.getLogger(__name__)


def _spacerouter_dir() -> Path:
    """Return the user's ``~/.spacerouter`` directory.

    We use ``Path.home() / ".spacerouter"`` regardless of platform — this
    matches the schema's ``settlement_key_path`` default. The GUI uses a
    different (platform-native) location for its existing
    ``spacerouter.env``; that path is passed in explicitly via
    :py:func:`load_provider_settings_from`.
    """
    return Path.home() / ".spacerouter"


def settings_path(directory: Path | None = None) -> Path:
    return (directory or _spacerouter_dir()) / "settings.json"


def env_path(directory: Path | None = None) -> Path:
    return (directory or _spacerouter_dir()) / "spacerouter.env"


def load_provider_settings(directory: Path | None = None) -> Settings:
    """Resolve provider settings using the full Track P0 chain.

    *directory* defaults to ``~/.spacerouter``. With v1.5's path
    unification both GUI and CLI now agree on this location, so callers
    rarely need to override.
    """
    directory = directory or _spacerouter_dir()

    # Step 0 — one-shot copy of legacy macOS Application Support data.
    # No-op on Linux/Windows or when the sentinel says we already did it.
    # Done BEFORE we look for settings.json so the migrated file ends up
    # exactly where load() expects it.
    try:
        from app.legacy_migration import maybe_migrate_legacy_macos
        moved = maybe_migrate_legacy_macos(directory)
        if moved:
            logger.info("legacy macOS migration: migrated to %s", directory)
        else:
            logger.debug("legacy macOS migration: skipped (not applicable)")
    except Exception:  # noqa: BLE001
        # Best-effort: never let a migration glitch block startup.
        logger.warning("legacy macOS migration skipped due to error", exc_info=True)

    s_path = settings_path(directory)
    e_path = env_path(directory)

    # Step 1 — JSON exists, just load it.
    if s_path.exists():
        s = Settings.load(s_path)
        logger.info("settings loaded from: %s", s_path)
        return s

    # Step 2 — legacy env file exists, migrate.
    if e_path.exists():
        s = Settings.migrate_from_env_file(e_path, s_path)
        logger.info("settings loaded from: %s (migrated from %s)", s_path, e_path)
        return s

    # Step 3 — last-resort env-var resolution. Build a Settings from
    # whatever ``SR_*`` vars are in os.environ, persist it, and use it.
    env_vars = {k: v for k, v in os.environ.items() if k.startswith("SR_")}
    if env_vars:
        s = Settings.from_env_mapping(env_vars)
        directory.mkdir(parents=True, exist_ok=True)
        s.save(s_path)
        logger.info("settings loaded from: %s (seeded from environment)", s_path)
        return s

    # Step 4 — no config anywhere. Persist a defaults-only ``settings.json``
    # so the next launch is JSON-driven (and so the daemon's first-run
    # log clearly shows where canonical config lives). The pre-Phase-1
    # behaviour was to return defaults without writing — that left the
    # macOS test build's ``~/.spacerouter/`` empty after a cold start
    # (only ``daemon.lock`` was created). See v1.5.0-test.80 E2E report.
    s = Settings()
    try:
        directory.mkdir(parents=True, exist_ok=True)
        s.save(s_path)
        logger.info("settings loaded from: %s (cold-start defaults persisted)", s_path)
    except OSError as e:
        # Best-effort: if disk is read-only or perms refuse, fall back
        # to in-memory defaults rather than blocking startup.
        logger.warning(
            "could not persist cold-start settings.json at %s: %s — using defaults in memory",
            s_path,
            e,
        )
    return s
