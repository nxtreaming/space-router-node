"""R2 / P5 — GUI API surface for auto-claim, incidents, error catalog.

Tests the new methods added in fix/gui-sweep-p5:

- get_auto_claim_config / set_auto_claim_config — round-trip through
  settings.json
- get_auto_claim_status — fallback path (no live monitor ref)
- get_incidents / acknowledge_incident — round-trip through
  incidents.json
- _claim_runner attaches a stable error_code on web3-style failures
- classify_error_text mapping for the documented patterns
- stop_node blanks staking_status synchronously (G5)
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def patched_config_dir(tmp_path, monkeypatch):
    """Redirect ~/.spacerouter to a tmp path so tests don't pollute
    the real config dir."""
    monkeypatch.setattr("app.paths.config_dir", lambda variant=None: tmp_path)
    yield tmp_path


def _make_api(tmp_path):
    """Build an Api with a real ConfigStore-shaped MagicMock and a
    NodeManager that just exposes the .status namespace we need.
    """
    from gui.api import Api
    node = MagicMock()
    node.status.staking_status = "earning"
    api = Api(config=MagicMock(), node_manager=node)
    return api, node


def test_classify_error_text_known_patterns():
    from gui.api import classify_error_text

    # Insufficient gas — covers the canonical web3.py error.
    assert classify_error_text("ValueError: insufficient funds for gas") == "insufficient_gas"
    assert classify_error_text("intrinsic gas too low") == "insufficient_gas"

    # Coordination API
    assert classify_error_text("could not reach https://spacerouter-coordination-api.fly.dev/register") == "coord_unreachable"
    assert classify_error_text("Coordination API unreachable") == "coord_unreachable"

    # Chain RPC — must not be mistaken for coord_unreachable.
    assert classify_error_text("Max retries exceeded with rpc endpoint") == "chain_rpc_unreachable"

    # Identity key
    assert classify_error_text("identity key not found at /tmp/x") == "identity_key_missing"

    # Disk full
    assert classify_error_text("OSError: [Errno 28] No space left on device") == "disk_full"

    # SQLite
    assert classify_error_text("sqlite3.OperationalError: database is locked") == "receipt_db_locked"

    # Unknown falls through.
    assert classify_error_text("totally unrelated error") == "unknown"
    assert classify_error_text("") == "unknown"


def test_get_auto_claim_config_defaults_when_no_settings(patched_config_dir):
    api, _ = _make_api(patched_config_dir)
    cfg = api.get_auto_claim_config()
    assert cfg["ok"] is True
    assert cfg["enabled"] is False
    # Threshold returned as string so the JS side never loses precision.
    assert isinstance(cfg["threshold_space_wei"], str)
    assert int(cfg["threshold_space_wei"]) > 0


def test_set_auto_claim_config_round_trips(patched_config_dir):
    api, _ = _make_api(patched_config_dir)

    # Threshold higher than 2^53 to prove the wei-as-string contract.
    big_wei = "12345678901234567890123"
    resp = api.set_auto_claim_config(True, big_wei, 7)
    assert resp["ok"] is True
    assert resp["restart_required"] is True

    cfg = api.get_auto_claim_config()
    assert cfg["enabled"] is True
    assert cfg["threshold_space_wei"] == big_wei
    assert cfg["threshold_count"] == 7


def test_set_auto_claim_config_rejects_bad_input(patched_config_dir):
    api, _ = _make_api(patched_config_dir)
    resp = api.set_auto_claim_config(True, "not-a-number", 3)
    assert resp["ok"] is False
    assert "wei" in resp["error"].lower()


def test_incidents_round_trip(patched_config_dir):
    """Append-then-load + acknowledge wires up the sticky banner."""
    from gui.api import _incident_record

    api, _ = _make_api(patched_config_dir)

    _incident_record(
        "auto_claim_failed",
        code="auto_claim_failed",
        message="Test failure",
    )

    resp = api.get_incidents()
    assert resp["ok"] is True
    assert len(resp["incidents"]) == 1
    inc = resp["incidents"][0]
    assert inc["kind"] == "auto_claim_failed"
    assert inc["acknowledged"] is False

    ack = api.acknowledge_incident(inc["id"])
    assert ack["ok"] is True

    resp2 = api.get_incidents()
    assert resp2["incidents"][0]["acknowledged"] is True


def test_incidents_acknowledge_all_when_id_blank(patched_config_dir):
    from gui.api import _incident_record

    api, _ = _make_api(patched_config_dir)
    _incident_record("auto_claim_failed", message="a")
    _incident_record("auto_claim_failed", message="b")

    api.acknowledge_incident("")
    items = api.get_incidents()["incidents"]
    assert len(items) == 2
    assert all(i["acknowledged"] for i in items)


def test_incidents_capped_at_50(patched_config_dir):
    """incidents.json must stay bounded — no slow-leak across a year."""
    from gui.api import _incident_record

    api, _ = _make_api(patched_config_dir)
    for i in range(60):
        _incident_record("auto_claim_failed", message=f"fail-{i}")

    items = api.get_incidents()["incidents"]
    assert len(items) == 50
    # Newest preserved.
    assert items[-1]["message"] == "fail-59"


def test_get_auto_claim_status_fallback_reads_incident(patched_config_dir):
    """When no live monitor ref is available the status surface still
    reports the most recent failure so the banner can render."""
    from gui.api import _incident_record

    api, _ = _make_api(patched_config_dir)
    api.set_auto_claim_config(True, "1000000000000000000", 5)
    _incident_record(
        "auto_claim_failed",
        message="RPC unreachable",
        at_iso="2026-04-26T01:23:45+00:00",
    )

    st = api.get_auto_claim_status()
    assert st["ok"] is True
    assert st["enabled"] is True
    assert st["last_attempt_outcome"] == "failed"
    assert "RPC unreachable" in (st["last_error"] or "")


def test_stop_node_blanks_staking_status_synchronously(patched_config_dir):
    """G5 — staking_status must flip to '—' the moment stop is
    requested, not on the next coordination poll."""
    from gui.api import Api

    node = MagicMock()
    node.status.staking_status = "earning"
    # ``stop`` may take a long time; the synchronous blanking happens
    # *before* we call into it.
    captured = []
    def _stop(timeout=None):
        captured.append(node.status.staking_status)
    node.stop = _stop

    api = Api(config=MagicMock(), node_manager=node)
    api.stop_node()

    # By the time stop() is invoked the field is already blank.
    assert captured == ["—"]


def test_claim_runner_attaches_error_code_on_web3_failure(tmp_path, monkeypatch):
    """A7 — _claim_runner must attach a stable error_code so the GUI
    can render the friendly modal."""
    from gui import api as api_mod

    class FakeSettings:
        RECEIPT_STORE_PATH = str(tmp_path / "r.db")
        ESCROW_CHAIN_RPC = "http://fake"
        ESCROW_CONTRACT_ADDRESS = "0x" + "e" * 40
        ESCROW_CHAIN_ID = 102031
        CLAIM_BATCH_SIZE = 50
        IDENTITY_KEY_PATH = str(tmp_path / "id.key")
        IDENTITY_PASSPHRASE = ""

    async def boom(*a, **k):
        raise ValueError("execution reverted: insufficient funds for gas")

    with patch("app.main.load_settings", return_value=FakeSettings()), \
         patch("app.payment.settlement.claim_all", boom), \
         patch(
             "app.identity.load_or_create_identity",
             return_value=("0x" + "f" * 64, "0x" + "a" * 40),
         ):
        result = api_mod._claim_runner(None, False)

    assert result["ok"] is False
    assert result["error_code"] == "insufficient_gas"


def test_get_recent_logs_returns_tail(patched_config_dir):
    """get_recent_logs reads the daemon log tail for the log-viewer modal."""
    api, _ = _make_api(patched_config_dir)
    log = patched_config_dir / "spacerouter.log"
    log.write_text("\n".join(f"line {i}" for i in range(200)))

    resp = api.get_recent_logs(50)
    assert resp["ok"] is True
    assert len(resp["lines"]) == 50
    assert resp["lines"][-1] == "line 199"


def test_scan_logs_records_auto_claim_incident(patched_config_dir):
    """The log-scan fallback path turns auto-claim error log lines
    into incident-banner entries."""
    api, _ = _make_api(patched_config_dir)
    log = patched_config_dir / "spacerouter.log"
    log.write_text(
        "2026-04-26 01:00:00 INFO some other line\n"
        "2026-04-26 01:00:01 ERROR Auto-claim: claim_all() raised — "
        "RuntimeError: rpc unreachable. Sitting idle until thresholds...\n"
    )

    api._scan_logs_for_auto_claim_failure()
    items = api.get_incidents()["incidents"]
    assert len(items) == 1
    assert items[0]["kind"] == "auto_claim_failed"
    assert "rpc unreachable" in items[0]["message"]

    # Idempotent — scanning again does not duplicate.
    api._scan_logs_for_auto_claim_failure()
    items2 = api.get_incidents()["incidents"]
    assert len(items2) == 1
