"""Disk-backed PollerCursor — Track P4 / loopholes L2 + L8.

The cursor lives at ``<config_dir>/poller_cursor.json`` so the
signed-receipt poller can resume across daemon restarts. A 24h floor
applies on top — the gateway's signed-receipt retention window is 24h,
so reading further back than that wastes a request.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from app.payment.poller_cursor import (
    CURSOR_FILENAME,
    PollerCursor,
    SCHEMA_VERSION,
)


def test_load_returns_none_when_file_missing(tmp_path):
    cursor = PollerCursor(tmp_path)
    assert cursor.load() is None


def test_save_then_load_roundtrip(tmp_path):
    cursor = PollerCursor(tmp_path)
    when = datetime(2026, 4, 26, 12, 30, 0, tzinfo=timezone.utc)
    cursor.save(when)

    # File exists at the documented path with the documented schema.
    assert (tmp_path / CURSOR_FILENAME).is_file()
    raw = json.loads((tmp_path / CURSOR_FILENAME).read_text())
    assert raw["schema_version"] == SCHEMA_VERSION
    assert raw["timestamp"].startswith("2026-04-26T12:30:00")

    loaded = cursor.load()
    assert loaded == when
    assert loaded.tzinfo is not None  # always aware


def test_save_naive_datetime_is_normalised_to_utc(tmp_path):
    """Saving a naive datetime must not crash and must round-trip as UTC."""
    cursor = PollerCursor(tmp_path)
    naive = datetime(2026, 4, 26, 12, 30, 0)  # no tzinfo
    cursor.save(naive)
    loaded = cursor.load()
    assert loaded.tzinfo is not None
    # Same wall-clock value, just labelled UTC.
    assert loaded.replace(tzinfo=None) == naive


def test_save_creates_config_dir_if_missing(tmp_path):
    nested = tmp_path / "fresh-install" / ".spacerouter"
    cursor = PollerCursor(nested)
    when = datetime.now(timezone.utc)
    cursor.save(when)
    assert (nested / CURSOR_FILENAME).is_file()


def test_durable_cursor_survives_restart(tmp_path):
    """Spec test: write cursor, reload via fresh instance, value matches."""
    a = PollerCursor(tmp_path)
    when = datetime(2026, 4, 25, 9, 0, 0, tzinfo=timezone.utc)
    a.save(when)

    b = PollerCursor(tmp_path)
    assert b.load() == when


def test_load_handles_corrupt_file(tmp_path):
    """Garbage on disk must not crash the daemon; load returns None."""
    cursor = PollerCursor(tmp_path)
    (tmp_path / CURSOR_FILENAME).write_text("not json {{{")
    assert cursor.load() is None


def test_load_handles_missing_timestamp_field(tmp_path):
    cursor = PollerCursor(tmp_path)
    (tmp_path / CURSOR_FILENAME).write_text(
        json.dumps({"schema_version": SCHEMA_VERSION})
    )
    assert cursor.load() is None


def test_load_handles_invalid_iso_timestamp(tmp_path):
    cursor = PollerCursor(tmp_path)
    (tmp_path / CURSOR_FILENAME).write_text(
        json.dumps({"schema_version": SCHEMA_VERSION,
                    "timestamp": "not-a-date"})
    )
    assert cursor.load() is None


def test_save_is_atomic(tmp_path):
    """Atomic-write contract: an in-progress save must never leave the
    main file in a partial state. We simulate by checking that no
    .tmp-prefixed file is left behind after a successful save."""
    cursor = PollerCursor(tmp_path)
    cursor.save(datetime.now(timezone.utc))
    leftovers = list(tmp_path.glob(".poller_cursor.*.tmp"))
    assert leftovers == []


def test_save_overwrites_previous_value(tmp_path):
    cursor = PollerCursor(tmp_path)
    first = datetime(2026, 4, 24, 10, 0, 0, tzinfo=timezone.utc)
    second = datetime(2026, 4, 25, 11, 0, 0, tzinfo=timezone.utc)
    cursor.save(first)
    cursor.save(second)
    assert cursor.load() == second


def test_load_accepts_z_suffix(tmp_path):
    """ISO format with Z (Zulu) suffix should round-trip as UTC."""
    cursor = PollerCursor(tmp_path)
    (tmp_path / CURSOR_FILENAME).write_text(
        json.dumps({"schema_version": SCHEMA_VERSION,
                    "timestamp": "2026-04-26T12:30:00Z"})
    )
    loaded = cursor.load()
    assert loaded == datetime(2026, 4, 26, 12, 30, 0, tzinfo=timezone.utc)
