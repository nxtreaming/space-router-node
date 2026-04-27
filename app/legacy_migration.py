"""One-shot migration of legacy per-platform config dirs to ``~/.spacerouter``.

Pre-v1.5 the GUI used platform-native config locations:

* macOS: ``~/Library/Application Support/SpaceRouter[-Test]/``
* Linux: ``~/.config/spacerouter/`` (XDG Base Dir default)
* Windows: ``%USERPROFILE%/.spacerouter/`` (already canonical — no
  migration needed)

v1.5 unifies the config dir at ``~/.spacerouter`` on all platforms (see
:py:mod:`app.paths`). Existing v1.4 users must NOT lose their data.

This module performs a best-effort copy from the legacy dir(s) to the
new canonical location on first launch. Highlights:

* Only runs when ``~/.spacerouter`` doesn't yet exist or is empty.
* If the new dir already has content, we abort the auto-migration and
  log a WARN — the operator gets to decide. Better to do nothing than
  destroy work.
* We never delete the legacy dir; the operator can clean up manually
  once they've verified the migration.
* Sentinel files record that a migration already ran so we never
  re-migrate the same source twice. macOS uses
  ``.migrated_from_appsupport``; Linux uses ``.migrated_from_xdg_config``
  so a hypothetical dual-OS cross-machine copy of ``~/.spacerouter``
  doesn't suppress a still-needed migration on the other platform.
* If both ``SpaceRouter`` and ``SpaceRouter-Test`` exist on macOS, we
  pick the one matching the persisted ``build_variant``; the other is
  skipped with a WARN.
* Linux v1.4 never shipped a separate ``-Test`` dir, so there's only
  one candidate to consider.

The migrator is invoked by :py:func:`app.settings_loader.load_provider_settings`
*before* it tries to read ``settings.json`` — that way the freshly
migrated file is in the right place when load() looks for it.
"""

from __future__ import annotations

import json
import logging
import shutil
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

_SENTINEL = ".migrated_from_appsupport"
_SENTINEL_LINUX = ".migrated_from_xdg_config"


def _legacy_macos_candidates() -> list[Path]:
    """Return the macOS legacy dirs in priority order.

    The ``-Test`` suffix is honored because pre-v1.5 test builds used
    a separate dir to avoid contaminating production state.
    """
    base = Path.home() / "Library" / "Application Support"
    return [base / "SpaceRouter", base / "SpaceRouter-Test"]


def _legacy_linux_candidates() -> list[Path]:
    """Return the Linux legacy dir(s) in priority order.

    Pre-v1.5 the Linux GUI used the XDG Base Dir default
    ``~/.config/spacerouter/``. There is no ``-Test`` variant — the v1.4
    Linux build never shipped one — so this list always has exactly one
    entry.
    """
    return [Path.home() / ".config" / "spacerouter"]


def _is_dir_empty(p: Path) -> bool:
    """True if *p* doesn't exist or contains no entries."""
    if not p.exists():
        return True
    try:
        return not any(p.iterdir())
    except OSError:
        return False


def _read_persisted_variant(legacy_dir: Path) -> str | None:
    """Best-effort read of build_variant from a legacy settings.json.

    We may have shipped a build that wrote settings.json into the legacy
    dir before the path-unification. If so, prefer that variant.
    """
    sj = legacy_dir / "settings.json"
    if not sj.is_file():
        return None
    try:
        return json.loads(sj.read_text()).get("build_variant")
    except (json.JSONDecodeError, OSError):
        return None


def _active_build_variant() -> str | None:
    """Return the active BUILD_VARIANT, or None if the variant module
    isn't importable (defensive — should never happen in production)."""
    try:
        from app.variant import BUILD_VARIANT
        return BUILD_VARIANT
    except Exception:  # noqa: BLE001
        return None


def _pick_legacy_dir(candidates: list[Path]) -> Path | None:
    """Pick which of the candidates we want to migrate from.

    Strategy (in order):

    1. Drop entries that don't exist or are empty.
    2. If exactly one remains, use it.
    3. If more than one, prefer the dir matching the active
       ``BUILD_VARIANT`` — ``SpaceRouter`` for production, ``SpaceRouter-Test``
       for test. This is the right call: a test build picking up a stale
       prod-variant App Support dir was the v1.5.0-test.85 footgun where
       the daemon migrated prod settings (with a prod coord URL!) into a
       fresh test install.
    4. Fall back to non-``-Test`` (production) if BUILD_VARIANT didn't
       help — preserves pre-fix behaviour as the safe default.

    The unchosen dir gets a WARN so the operator can resolve manually.
    """
    populated = [p for p in candidates if p.exists() and not _is_dir_empty(p)]
    if not populated:
        return None
    if len(populated) == 1:
        return populated[0]

    chosen: Path | None = None
    variant = _active_build_variant()
    if variant == "test":
        chosen = next((p for p in populated if p.name.endswith("-Test")), None)
    elif variant in ("production", "prod"):
        chosen = next((p for p in populated if not p.name.endswith("-Test")), None)

    # Fall back to "production wins" if the variant lookup didn't
    # produce a match (unknown variant, or only the wrong dir exists).
    if chosen is None:
        chosen = next((p for p in populated if not p.name.endswith("-Test")), None)
    if chosen is None:
        chosen = populated[0]

    others = [p for p in populated if p != chosen]
    for other in others:
        logger.warning(
            "Multiple legacy macOS config dirs found. Migrating %s "
            "(matches build_variant=%r); ignoring %s — re-run with that "
            "path manually if you want it.",
            chosen,
            variant,
            other,
        )
    return chosen


def _dir_summary(p: Path) -> tuple[int, int]:
    """Return (file_count, total_bytes) under *p*."""
    files = 0
    total = 0
    for entry in p.rglob("*"):
        if entry.is_file():
            files += 1
            try:
                total += entry.stat().st_size
            except OSError:
                pass
    return files, total


def maybe_migrate_legacy_macos(target: Path) -> bool:
    """Migrate a legacy macOS Application Support dir into *target*.

    *target* is the canonical ``~/.spacerouter`` directory.

    Returns True when a migration happened, False otherwise. Never
    raises for "expected" no-op cases (wrong platform, no legacy dir,
    sentinel already written) — those just return False with an INFO/
    DEBUG log. Truly unexpected I/O errors (e.g. permission denied on
    a file we expected to be readable) propagate.

    Safety: we only act when *target* is the canonical
    ``~/.spacerouter``. If the caller passes a different directory
    (e.g. a test temp dir, a packaging path) we no-op so unit tests
    don't accidentally drag real user data into a temp tree.
    """
    if sys.platform != "darwin":
        return False

    canonical = Path.home() / ".spacerouter"
    try:
        if target.resolve() != canonical.resolve():
            return False
    except OSError:
        # ``resolve()`` can fail for not-yet-created targets; fall back
        # to a plain equality compare.
        if target != canonical:
            return False

    # If we've already migrated, don't even look.
    if (target / _SENTINEL).exists():
        logger.debug("legacy macOS migration already done (%s present)", _SENTINEL)
        return False

    source = _pick_legacy_dir(_legacy_macos_candidates())
    if source is None:
        return False

    # Refuse to overwrite a non-empty target. Operator decides.
    if target.exists() and not _is_dir_empty(target):
        logger.warning(
            "Legacy macOS dir %s exists, but %s is already populated. "
            "Skipping auto-migration to avoid clobbering existing data — "
            "merge manually if needed.",
            source,
            target,
        )
        return False

    files, total_bytes = _dir_summary(source)
    logger.info(
        "Migrating legacy macOS config: %s -> %s (%d files, %d bytes)",
        source,
        target,
        files,
        total_bytes,
    )

    # ``dirs_exist_ok=False`` — we already verified target is empty/missing,
    # so a stricter copy here surfaces any race we didn't anticipate.
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and _is_dir_empty(target):
        # copytree refuses if the destination exists; remove the empty
        # placeholder so the copy can recreate it with proper perms.
        target.rmdir()
    shutil.copytree(source, target, dirs_exist_ok=False)

    # Drop the sentinel so subsequent launches skip this code path.
    try:
        (target / _SENTINEL).write_text(str(source) + "\n")
    except OSError as e:
        logger.warning(
            "Could not write migration sentinel %s: %s", target / _SENTINEL, e
        )

    logger.info(
        "Legacy macOS migration complete. Legacy dir kept at %s — "
        "remove manually after verifying the new dir.",
        source,
    )
    return True


def maybe_migrate_legacy_linux(target: Path) -> bool:
    """Migrate a legacy Linux XDG config dir into *target*.

    *target* is the canonical ``~/.spacerouter`` directory.

    Mirrors :py:func:`maybe_migrate_legacy_macos` but for the Linux v1.4
    XDG location ``~/.config/spacerouter/``. There's no variant suffix
    on Linux, so the candidate list is single-entry and there is no
    BUILD_VARIANT tie-break.

    Returns True when a migration happened, False otherwise. Never
    raises for "expected" no-op cases (wrong platform, no legacy dir,
    sentinel already written) — those just return False with an INFO/
    DEBUG log. Truly unexpected I/O errors propagate.

    Safety: we only act when *target* is the canonical
    ``~/.spacerouter``. If the caller passes a different directory
    (e.g. a test temp dir, a packaging path) we no-op so unit tests
    don't accidentally drag real user data into a temp tree.

    Sentinel filename is :py:data:`_SENTINEL_LINUX`
    (``.migrated_from_xdg_config``) — distinct from the macOS sentinel
    so a dual-OS copy of ``~/.spacerouter`` doesn't suppress a still-
    needed migration on the other platform.
    """
    if sys.platform != "linux":
        return False

    canonical = Path.home() / ".spacerouter"
    try:
        if target.resolve() != canonical.resolve():
            return False
    except OSError:
        # ``resolve()`` can fail for not-yet-created targets; fall back
        # to a plain equality compare.
        if target != canonical:
            return False

    # If we've already migrated, don't even look.
    if (target / _SENTINEL_LINUX).exists():
        logger.debug(
            "legacy Linux XDG migration already done (%s present)", _SENTINEL_LINUX
        )
        return False

    candidates = _legacy_linux_candidates()
    source = next(
        (p for p in candidates if p.exists() and not _is_dir_empty(p)), None
    )
    if source is None:
        return False

    # Refuse to overwrite a non-empty target. Operator decides.
    if target.exists() and not _is_dir_empty(target):
        logger.warning(
            "Legacy Linux dir %s exists, but %s is already populated. "
            "Skipping auto-migration to avoid clobbering existing data — "
            "merge manually if needed.",
            source,
            target,
        )
        return False

    files, total_bytes = _dir_summary(source)
    logger.info(
        "Migrating legacy Linux XDG config: %s -> %s (%d files, %d bytes)",
        source,
        target,
        files,
        total_bytes,
    )

    # ``dirs_exist_ok=False`` — we already verified target is empty/missing,
    # so a stricter copy here surfaces any race we didn't anticipate.
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and _is_dir_empty(target):
        # copytree refuses if the destination exists; remove the empty
        # placeholder so the copy can recreate it with proper perms.
        target.rmdir()
    shutil.copytree(source, target, dirs_exist_ok=False)

    # Drop the sentinel so subsequent launches skip this code path.
    try:
        (target / _SENTINEL_LINUX).write_text(str(source) + "\n")
    except OSError as e:
        logger.warning(
            "Could not write migration sentinel %s: %s",
            target / _SENTINEL_LINUX,
            e,
        )

    logger.info(
        "Legacy Linux XDG migration complete. Legacy dir kept at %s — "
        "remove manually after verifying the new dir.",
        source,
    )
    return True
