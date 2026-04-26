"""Disk-backed cursor for the Leg 2 signed-receipt poller (P4 / L2 + L8).

The poller picks up gateway-signed receipts via
``GET /nodes/{id}/signed-receipts?since=<ts>``. The "since" cursor is
held in memory by the daemon — fine while it's running, but on cold
start the cursor reset to ``now() - 24h``. If the provider was offline
longer than 24h (laptop closed over a long weekend, hardware swap with
the same identity dir, etc.) any signed receipts older than that are
silently lost.

This module persists the cursor between runs at
``<config_dir>/poller_cursor.json`` (atomic tmp + rename). The 24h
window remains as a *floor* — even with a saved cursor, we never look
back further than 24h from "now" because the gateway's signed-receipt
retention is 24h. But we always look back AT LEAST as far as the saved
cursor (minus a 1h guard) so a sustained outage doesn't stale-skip.

Format: ``{"timestamp": "<iso8601 UTC>", "schema_version": 1}``.
Anything malformed → ``load()`` returns ``None``, the poller falls back
to the legacy "now - 24h" behaviour, and we'll save a fresh cursor on
the next successful tick.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

CURSOR_FILENAME = "poller_cursor.json"
SCHEMA_VERSION = 1


class PollerCursor:
    """Disk-backed timestamp cursor for the signed-receipt poller."""

    def __init__(self, config_dir: str | os.PathLike) -> None:
        self._dir = Path(config_dir).expanduser()
        self._path = self._dir / CURSOR_FILENAME

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> datetime | None:
        """Return the saved cursor, or None if missing/unreadable.

        Defensive: any I/O / parse error returns None and logs at DEBUG.
        Operators see the fallback path warn on its own; we don't double
        up.
        """
        try:
            raw = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as exc:
            logger.debug("poller_cursor read failed: %s", exc)
            return None

        try:
            data = json.loads(raw)
        except (ValueError, TypeError) as exc:
            logger.debug("poller_cursor parse failed: %s", exc)
            return None

        ts = data.get("timestamp") if isinstance(data, dict) else None
        if not isinstance(ts, str):
            return None
        try:
            parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            logger.debug("poller_cursor invalid iso timestamp: %r", ts)
            return None

        # Always normalise to aware UTC so comparisons elsewhere are safe.
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    def save(self, timestamp: datetime) -> None:
        """Atomically write the cursor file.

        Tempfile + rename ensures the file is either the previous value
        or the new value — never half-written. ``parents=True`` so the
        save works on a freshly-created config dir.
        """
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        else:
            timestamp = timestamp.astimezone(timezone.utc)

        try:
            self._dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.debug("poller_cursor mkdir failed: %s", exc)
            return

        payload = {
            "schema_version": SCHEMA_VERSION,
            "timestamp": timestamp.isoformat(),
        }

        try:
            # Same directory as the destination so rename is atomic on
            # POSIX and stays on the same volume on Windows.
            fd, tmp_path = tempfile.mkstemp(
                prefix=".poller_cursor.",
                suffix=".tmp",
                dir=str(self._dir),
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(payload, fh)
                    fh.flush()
                    try:
                        os.fsync(fh.fileno())
                    except OSError:
                        # Some filesystems (e.g. tmpfs in CI) don't support
                        # fsync; best-effort is fine here.
                        pass
                os.replace(tmp_path, self._path)
            except Exception:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
        except OSError as exc:
            logger.debug("poller_cursor save failed: %s", exc)
