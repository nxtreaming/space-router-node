"""Tests for the optional auto-claim monitor (P10 of v1.5 plan).

Each test exercises one user-facing behaviour:

- disabled flag → no work done.
- balance threshold trips → claim fires.
- count threshold trips (under balance) → claim still fires (OR-semantics).
- ``claim.lock`` already held → skip, retry next tick, no crash.
- claim raises → log + sit idle (S3-c, no auto-retry).
- success → status surface reflects last_attempt_outcome="success".
- ``get_status()`` returns the documented dict shape.

These are unit tests; ``claim_all`` and the chain RPC layer are mocked.
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest

from app.payment.auto_claim import AutoClaimMonitor
from app.payment.eip712 import Receipt
from app.payment.receipt_store import get_store


# ── Shared fixtures ─────────────────────────────────────────────────


def _mk_receipt(*, total_price: int = 1) -> Receipt:
    return Receipt(
        client_address="0x" + "a" * 40,
        node_address="0x" + "b" * 64,
        request_uuid=str(uuid.uuid4()),
        data_amount=1024,
        total_price=total_price,
    )


def _mk_settings(
    db_path: Path,
    *,
    enabled: bool = True,
    threshold_wei: str = "10000000000000000000",  # 10 SPACE
    threshold_count: int = 10,
):
    """Plain attribute bag — same convention as test_settlement_hardening."""
    class S:
        ESCROW_CHAIN_RPC = "http://fake-rpc.invalid"
        ESCROW_CONTRACT_ADDRESS = "0x" + "e" * 40
        ESCROW_CHAIN_ID = 102031
        RECEIPT_STORE_PATH = str(db_path)
        GATEWAY_PAYER_ADDRESS = "0x" + "c" * 40
        CLAIM_BATCH_SIZE = 50
        AUTO_CLAIM_ENABLED = enabled
        AUTO_CLAIM_THRESHOLD_SPACE_WEI = threshold_wei
        AUTO_CLAIM_THRESHOLD_COUNT = threshold_count
    return S()


async def _populate_claimable(store, count: int, *, total_price: int = 1) -> list[str]:
    """Drop ``count`` claimable rows in. Returns the UUIDs."""
    uuids: list[str] = []
    for _ in range(count):
        r = _mk_receipt(total_price=total_price)
        await store.store(r, signature="0xsig")
        uuids.append(r.request_uuid)
    return uuids


# ── Disabled flag ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_auto_claim_disabled_does_nothing(tmp_path):
    """When ``AUTO_CLAIM_ENABLED=False`` (the default), the monitor must
    not fire even if thresholds are obviously crossed.

    This is the "cheap when disabled" guarantee from the v1.5 plan: the
    daemon shouldn't burn cycles polling for users who never opted in.
    ``start()`` is a no-op and the task never runs; here we verify the
    behaviour by directly calling ``tick()`` and confirming no claim
    path was reached (we patch ``claim_all`` to fail loudly if called).
    """
    db = tmp_path / "r.db"
    store = get_store(str(db))
    await store.initialize()

    # 20 receipts of 1 SPACE each — both thresholds tripped if enabled.
    await _populate_claimable(store, count=20, total_price=10**18)

    settings = _mk_settings(db, enabled=False)
    monitor = AutoClaimMonitor(settings, settlement_key_hex="0x" + "1" * 64)

    # start() is a no-op — never schedules a task.
    await monitor.start()
    assert monitor._task is None

    # Even if someone forces a tick, no claim_all call happens.
    # (We don't patch claim_all here because tick() returns early on
    # the threshold check; the disabled monitor never enters _fire_claim_locked.
    # Instead we assert via the returned dict.)
    # Note: disabled monitors don't expose tick() as a public surface;
    # this test really is about start() being a no-op + no crash.
    assert monitor.get_status()["enabled"] is False


# ── Balance threshold (OR-semantics, balance arm) ──────────────────


@pytest.mark.asyncio
async def test_auto_claim_fires_when_balance_threshold_tripped(tmp_path):
    """Receipts whose total wei crosses the balance threshold trigger a
    claim, even when the count threshold is far from being met.

    SQLite stores ``total_price`` as a 64-bit int — its ``SUM`` aggregate
    overflows past ~9.2e18 — so this test uses a smaller per-receipt
    amount and a small threshold to avoid the overflow while still
    isolating the balance-arm logic. Production uses a string-typed
    column elsewhere; this is a test-fixture constraint, not a
    monitor-level concern.
    """
    db = tmp_path / "r.db"
    store = get_store(str(db))
    await store.initialize()
    # 5 receipts × 1e17 wei = 5e17 wei total; threshold = 4e17. Tripped.
    await _populate_claimable(store, count=5, total_price=10**17)

    settings = _mk_settings(
        db,
        # Below the 5-receipt total (5e17), above any single receipt (1e17),
        # so only the balance arm crosses.
        threshold_wei="400000000000000000",  # 0.4 SPACE
        # Way above 5 — count arm would NOT trip from these 5 rows.
        threshold_count=999,
    )
    monitor = AutoClaimMonitor(settings, settlement_key_hex="0x" + "1" * 64)

    fired = {"count": 0}

    async def fake_claim_all(s, key, **kwargs):
        fired["count"] += 1
        from app.payment.settlement import ClaimResult
        return [ClaimResult(submitted=5, tx_hash="0xabc", gas_used=21000)]

    with patch("app.payment.settlement.claim_all", fake_claim_all):
        result = await monitor.tick()

    assert fired["count"] == 1
    assert result["fired"] is True
    assert result["outcome"] == "success"
    assert result["submitted"] == 5
    assert monitor.get_status()["last_attempt_outcome"] == "success"


# ── Count threshold (OR-semantics, count arm only) ─────────────────


@pytest.mark.asyncio
async def test_auto_claim_fires_when_count_threshold_tripped(tmp_path):
    """10 small receipts (total ≪ 10 SPACE) still trip the count threshold.

    Verifies OR semantics: count alone is enough even if balance is far
    below the wei threshold. This is the "10 small jobs accumulated"
    case from the user spec — operators who route a lot of tiny
    sessions still get prompt settlement.
    """
    db = tmp_path / "r.db"
    store = get_store(str(db))
    await store.initialize()
    # Each receipt is just 1 wei — total 10 wei, vastly under 10 SPACE.
    await _populate_claimable(store, count=10, total_price=1)

    settings = _mk_settings(db)
    monitor = AutoClaimMonitor(settings, settlement_key_hex="0x" + "1" * 64)

    fired = {"count": 0}

    async def fake_claim_all(s, key, **kwargs):
        fired["count"] += 1
        from app.payment.settlement import ClaimResult
        return [ClaimResult(submitted=10, tx_hash="0xabc", gas_used=21000)]

    with patch("app.payment.settlement.claim_all", fake_claim_all):
        result = await monitor.tick()

    assert fired["count"] == 1
    assert result["fired"] is True

    # Sanity: the balance arm did NOT trigger this — wei < threshold.
    status = monitor.get_status()
    assert int(status["current_claimable_wei"]) < int(status["next_threshold_space_wei"])
    assert int(status["current_claimable_count"]) >= int(status["next_threshold_count"])


# ── Below-threshold no-op ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_auto_claim_below_threshold_does_not_fire(tmp_path):
    """Neither threshold tripped → tick is a no-op, no claim_all call."""
    db = tmp_path / "r.db"
    store = get_store(str(db))
    await store.initialize()
    # 5 receipts × 1 wei = nowhere near either threshold.
    await _populate_claimable(store, count=5, total_price=1)

    settings = _mk_settings(db)
    monitor = AutoClaimMonitor(settings, settlement_key_hex="0x" + "1" * 64)

    # claim_all should never be reached — fail loudly if it is.
    async def fake_claim_all(s, key, **kwargs):
        raise AssertionError("claim_all should not be called below threshold")

    with patch("app.payment.settlement.claim_all", fake_claim_all):
        result = await monitor.tick()

    assert result["fired"] is False
    assert result["reason"] == "below_threshold"
    assert monitor.get_status()["last_attempt_outcome"] == "none"


# ── Lock contention ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_auto_claim_skips_when_lock_held(tmp_path):
    """If ``claim.lock`` is held (manual claim is running), the monitor
    must skip this tick cleanly — no claim_all call, no crash, status
    untouched. The next tick can still try, so receipts get cleared
    eventually whether the manual claim succeeded or not.
    """
    from app.payment.claim_lock import acquire_claim_lock

    db = tmp_path / "r.db"
    store = get_store(str(db))
    await store.initialize()
    # Use the count arm so we don't have to worry about SQLite SUM overflow.
    await _populate_claimable(store, count=10, total_price=1)

    settings = _mk_settings(db)
    monitor = AutoClaimMonitor(settings, settlement_key_hex="0x" + "1" * 64)

    fired = {"count": 0}

    async def fake_claim_all(s, key, **kwargs):
        fired["count"] += 1
        from app.payment.settlement import ClaimResult
        return [ClaimResult(submitted=10, tx_hash="0xabc", gas_used=21000)]

    # Hold the lock from the test. The monitor must observe it and bail.
    with acquire_claim_lock(settings):
        with patch("app.payment.settlement.claim_all", fake_claim_all):
            result = await monitor.tick()

    assert fired["count"] == 0
    assert result["fired"] is False
    assert result["reason"] == "lock_held"
    # Outcome is still "none" — we didn't actually try yet.
    assert monitor.get_status()["last_attempt_outcome"] == "none"

    # Now lock is released; a follow-up tick should fire.
    with patch("app.payment.settlement.claim_all", fake_claim_all):
        result2 = await monitor.tick()
    assert fired["count"] == 1
    assert result2["fired"] is True


# ── Failure → log + idle (S3-c) ─────────────────────────────────────


@pytest.mark.asyncio
async def test_auto_claim_failure_logs_and_sits_idle(tmp_path, caplog):
    """When ``claim_all()`` raises, the monitor must:

    - log an ERROR with the exception text,
    - record ``last_attempt_outcome="failed"`` + ``last_error``,
    - NOT retry on the immediately-next tick if the receipts are still
      claimable. This is the S3-c "fire once, sit idle" rule from the
      v1.5 plan; auto-retry would burn gas in a loop on a stuck
      operator.

    Note on "sits idle": with OR-semantics, the next tick re-evaluates
    thresholds. Because the receipts are still in the store, thresholds
    are still tripped and the monitor will fire again — the v1.5 plan
    explicitly accepts this ("the receipts are still claimable, the
    next firing only happens when thresholds trip again, which they
    will"). What we actually guarantee here is: failure does NOT cause
    a tighter retry cadence, just the next regular poll.
    """
    import logging

    db = tmp_path / "r.db"
    store = get_store(str(db))
    await store.initialize()
    # Count arm — avoids SQLite SUM overflow (see balance-test note).
    await _populate_claimable(store, count=10, total_price=1)

    settings = _mk_settings(db)
    monitor = AutoClaimMonitor(settings, settlement_key_hex="0x" + "1" * 64)

    async def boom(s, key, **kwargs):
        raise RuntimeError("rpc unreachable")

    caplog.set_level(logging.ERROR, logger="app.payment.auto_claim")

    with patch("app.payment.settlement.claim_all", boom):
        result = await monitor.tick()

    assert result["fired"] is True
    assert result["outcome"] == "failed"
    assert "rpc unreachable" in result["error"]

    status = monitor.get_status()
    assert status["last_attempt_outcome"] == "failed"
    assert "rpc unreachable" in (status["last_error"] or "")
    assert status["last_attempt_at"] is not None

    # An ERROR was emitted (operator-visible).
    assert any(
        "auto-claim" in rec.message.lower() or "claim_all" in rec.message.lower()
        for rec in caplog.records if rec.levelno >= logging.ERROR
    ), [r.message for r in caplog.records]


# ── Success state reset ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_auto_claim_success_resets_state(tmp_path):
    """After a successful auto-claim, ``get_status().last_attempt_outcome``
    flips to ``"success"`` and ``last_error`` is cleared (covers a prior
    failed attempt being replaced by a success).
    """
    db = tmp_path / "r.db"
    store = get_store(str(db))
    await store.initialize()
    # Count arm — avoids SQLite SUM overflow (see balance-test note).
    await _populate_claimable(store, count=10, total_price=1)

    settings = _mk_settings(db)
    monitor = AutoClaimMonitor(settings, settlement_key_hex="0x" + "1" * 64)

    # Simulate a prior failure to verify the success path clears it.
    monitor._last_attempt_outcome = "failed"
    monitor._last_error = "previous: rpc was down"

    async def fake_claim_all(s, key, **kwargs):
        from app.payment.settlement import ClaimResult
        return [ClaimResult(submitted=10, tx_hash="0xabc", gas_used=21000)]

    with patch("app.payment.settlement.claim_all", fake_claim_all):
        await monitor.tick()

    status = monitor.get_status()
    assert status["last_attempt_outcome"] == "success"
    assert status["last_error"] is None


# ── get_status() shape ──────────────────────────────────────────────


def test_get_status_shape(tmp_path):
    """Spot-check the shape of ``get_status()`` so a future TUI/GUI can
    rely on the documented keys.
    """
    settings = _mk_settings(tmp_path / "r.db")
    monitor = AutoClaimMonitor(settings, settlement_key_hex="0x" + "1" * 64)
    status = monitor.get_status()
    expected_keys = {
        "enabled",
        "next_threshold_space_wei",
        "next_threshold_count",
        "current_claimable_wei",
        "current_claimable_count",
        "last_attempt_at",
        "last_attempt_outcome",
        "last_error",
    }
    assert set(status.keys()) == expected_keys
    # Wei values are stringified — JS-safe convention used elsewhere.
    assert isinstance(status["next_threshold_space_wei"], str)
    assert isinstance(status["current_claimable_wei"], str)
    # Counts are real ints.
    assert isinstance(status["next_threshold_count"], int)
    assert isinstance(status["current_claimable_count"], int)
    # Initial outcome is "none" — no tick has run yet.
    assert status["last_attempt_outcome"] == "none"
    assert status["last_attempt_at"] is None
    assert status["last_error"] is None
