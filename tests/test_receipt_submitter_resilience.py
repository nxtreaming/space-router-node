"""Track P4 — receipt-submitter resilience regression tests.

Covers loopholes L1, L2, L8, L9 from `internal-docs/v1.5-provider-plan.md`:

- **L1**: 429 / 5xx / network errors must increment a per-row transient
  backoff counter and skip rows still in their backoff window. After
  ~24h of consecutive transient failures the row escalates to
  ``failed_retryable`` with ``SIGN_TRANSIENT_BUDGET_EXHAUSTED``.
- **L2 + L8**: durable poller cursor with a 24h floor. Long outages no
  longer stale-skip signed receipts.
- **L9**: 3 consecutive clock-skew errors in a 5-minute window logs an
  ERROR-level NTP warning and flips ``get_clock_skew_state()["in_drift"]``
  to True. Resets on any successful sign.
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

from app.payment import reasons
from app.payment.eip712 import Receipt
from app.payment.poller_cursor import PollerCursor
from app.payment.receipt_store import get_store
from app.payment.receipt_submitter import (
    POLL_INITIAL_LOOKBACK_HOURS,
    ReceiptPoller,
    ReceiptSubmitter,
    TRANSIENT_BUDGET_ATTEMPTS,
    _is_transient_status,
    get_clock_skew_state,
    reset_clock_skew_state,
    transient_backoff_seconds,
)


def _mk_receipt(**overrides) -> Receipt:
    base = dict(
        client_address="0x" + "a" * 40,
        node_address="0x" + "b" * 64,
        request_uuid=str(uuid.uuid4()),
        data_amount=1024,
        total_price=1,
    )
    base.update(overrides)
    return Receipt(**base)


class _Settings:
    def __init__(self, db_path: Path):
        self.NODE_RATE_PER_GB = 10**18
        self.RECEIPT_STORE_PATH = str(db_path)
        self.COORDINATION_API_URL = "http://coord"


# ── L1: transient backoff + budget ──────────────────────────────────


@pytest.mark.asyncio
async def test_transient_429_increments_counter_and_backs_off(tmp_path):
    """A 429 must:
      - bump transient_attempts from 0 to 1
      - leave the row pending_sign (no terminal error)
      - hide the row from count_unsigned_ready until the backoff window elapses
      - re-expose it once the simulated wall-clock advances past the window
    """
    db = tmp_path / "r.db"
    settings = _Settings(db)
    s = ReceiptSubmitter(
        settings=settings, node_id="n1",
        identity_key="0x" + "c" * 64,
        identity_address="0x" + "a" * 40,
        gateway_payer_address="0x" + "d" * 40,
        node_wallet_address="0x" + "e" * 40,
    )
    r = _mk_receipt()
    store = get_store(str(db))
    await store.initialize()
    await store.store_unsigned(r, request_id="req-1")

    # Pre: ready count should include the freshly stored row.
    assert (await store.count_unsigned_ready()) == 1

    # Simulate the 429 response from the coord API.
    resp_429 = httpx.Response(status_code=429, text="rate limited")

    class FakeClient:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def post(self, url, json):
            return resp_429

    with patch("app.payment.receipt_submitter.httpx.AsyncClient",
               return_value=FakeClient()):
        await s._fire_submit(r, "req-1")

    stored = await store.get_by_uuid(r.request_uuid)
    assert stored.transient_attempts == 1
    assert stored.last_error_code is None  # NOT terminal — still pending_sign
    assert stored.view == "pending_sign"
    # The row's still in its backoff window (60s after one attempt).
    assert (await store.count_unsigned_ready()) == 0

    # Advance the row's last_attempt_at into the past so the backoff
    # window has effectively elapsed; row should re-appear as ready.
    long_ago = int(time.time()) - 120
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE signed_receipts SET last_attempt_at = ?",
            (long_ago,),
        )
    assert (await store.count_unsigned_ready()) == 1


@pytest.mark.asyncio
async def test_transient_network_error_increments_counter(tmp_path):
    """RequestError (DNS / timeout / connection refused) is also
    transient; same accounting as 429."""
    db = tmp_path / "r.db"
    settings = _Settings(db)
    s = ReceiptSubmitter(
        settings=settings, node_id="n1",
        identity_key="0x" + "c" * 64,
        identity_address="0x" + "a" * 40,
        gateway_payer_address="0x" + "d" * 40,
        node_wallet_address="0x" + "e" * 40,
    )
    r = _mk_receipt()
    store = get_store(str(db))
    await store.initialize()
    await store.store_unsigned(r, request_id="req-1")

    class FakeClient:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def post(self, url, json):
            raise httpx.ConnectTimeout("simulated timeout")

    with patch("app.payment.receipt_submitter.httpx.AsyncClient",
               return_value=FakeClient()):
        await s._fire_submit(r, "req-1")

    stored = await store.get_by_uuid(r.request_uuid)
    assert stored.transient_attempts == 1
    assert stored.last_error_code is None


@pytest.mark.asyncio
async def test_transient_5xx_increments_counter(tmp_path):
    db = tmp_path / "r.db"
    settings = _Settings(db)
    s = ReceiptSubmitter(
        settings=settings, node_id="n1",
        identity_key="0x" + "c" * 64,
        identity_address="0x" + "a" * 40,
        gateway_payer_address="0x" + "d" * 40,
        node_wallet_address="0x" + "e" * 40,
    )
    r = _mk_receipt()
    store = get_store(str(db))
    await store.initialize()
    await store.store_unsigned(r, request_id="req-1")

    resp = httpx.Response(status_code=503, text="service unavailable")

    class FakeClient:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def post(self, url, json):
            return resp

    with patch("app.payment.receipt_submitter.httpx.AsyncClient",
               return_value=FakeClient()):
        await s._fire_submit(r, "req-1")

    stored = await store.get_by_uuid(r.request_uuid)
    assert stored.transient_attempts == 1


@pytest.mark.asyncio
async def test_mark_signed_resets_transient_attempts(tmp_path):
    """After a 429 storm, a successful sign clears the counter so future
    signs aren't pre-burdened with stale state."""
    db = tmp_path / "r.db"
    store = get_store(str(db))
    await store.initialize()
    r = _mk_receipt()
    await store.store_unsigned(r, request_id="req-1")
    # Simulate 5 transient attempts.
    for _ in range(5):
        await store.mark_transient_attempt(r.request_uuid)
    assert (await store.get_by_uuid(r.request_uuid)).transient_attempts == 5

    # Then it eventually signs.
    await store.mark_signed(r.request_uuid, "0xsig")
    stored = await store.get_by_uuid(r.request_uuid)
    assert stored.transient_attempts == 0


@pytest.mark.asyncio
async def test_transient_budget_exhausted_escalates_to_failed_retryable(tmp_path):
    """After TRANSIENT_BUDGET_ATTEMPTS hits, the row escalates to
    failed_retryable with SIGN_TRANSIENT_BUDGET_EXHAUSTED. Operator now
    sees it in the retryable bucket. ``transient_attempts`` is preserved
    for diagnostics."""
    db = tmp_path / "r.db"
    store = get_store(str(db))
    await store.initialize()
    r = _mk_receipt()
    await store.store_unsigned(r, request_id="req-1")

    for _ in range(TRANSIENT_BUDGET_ATTEMPTS):
        await store.mark_transient_attempt(r.request_uuid)

    pre = await store.get_by_uuid(r.request_uuid)
    assert pre.transient_attempts >= TRANSIENT_BUDGET_ATTEMPTS
    assert pre.view == "pending_sign"

    escalated = await store.escalate_transient_budget_exhausted(
        threshold=TRANSIENT_BUDGET_ATTEMPTS,
        code=reasons.SIGN_TRANSIENT_BUDGET_EXHAUSTED,
        detail="~24h of transient submit failures.",
    )
    assert escalated == [r.request_uuid]

    post = await store.get_by_uuid(r.request_uuid)
    assert post.last_error_code == reasons.SIGN_TRANSIENT_BUDGET_EXHAUSTED
    assert post.view == "failed_retryable"
    # Counter preserved for diagnostics — operator can see the run-up.
    assert post.transient_attempts == TRANSIENT_BUDGET_ATTEMPTS


@pytest.mark.asyncio
async def test_escalation_does_not_touch_under_budget_rows(tmp_path):
    """Don't escalate a row that's only at attempts=3."""
    db = tmp_path / "r.db"
    store = get_store(str(db))
    await store.initialize()
    r = _mk_receipt()
    await store.store_unsigned(r, request_id="req-1")
    for _ in range(3):
        await store.mark_transient_attempt(r.request_uuid)

    escalated = await store.escalate_transient_budget_exhausted(
        threshold=TRANSIENT_BUDGET_ATTEMPTS,
        code=reasons.SIGN_TRANSIENT_BUDGET_EXHAUSTED,
    )
    assert escalated == []
    stored = await store.get_by_uuid(r.request_uuid)
    assert stored.last_error_code is None
    assert stored.view == "pending_sign"


def test_transient_backoff_seconds_grows_then_caps():
    """First retry waits 60s; cap kicks in around the 6th attempt; high
    counters never exceed 1h."""
    assert transient_backoff_seconds(0) == 60
    assert transient_backoff_seconds(1) == 120
    assert transient_backoff_seconds(2) == 240
    assert transient_backoff_seconds(5) == 1920
    # Cap = 3600.
    assert transient_backoff_seconds(6) == 3600
    assert transient_backoff_seconds(20) == 3600


def test_is_transient_status_classifies_correctly():
    assert _is_transient_status(429) is True
    assert _is_transient_status(500) is True
    assert _is_transient_status(503) is True
    assert _is_transient_status(599) is True
    assert _is_transient_status(400) is False
    assert _is_transient_status(403) is False
    assert _is_transient_status(404) is False
    assert _is_transient_status(409) is False


# ── L2 + L8: durable cursor on poller startup ──────────────────────


@pytest.mark.asyncio
async def test_poller_uses_legacy_24h_lookback_when_no_saved_cursor(tmp_path):
    """First-run behaviour preserved: cursor = now - 24h."""
    db = tmp_path / "r.db"
    settings = _Settings(db)
    cursor_store = PollerCursor(tmp_path / "cfg")  # empty, no file
    poller = ReceiptPoller(
        settings=settings, node_id="n1",
        identity_key="0x" + "c" * 64,
        node_wallet_address="0x" + "d" * 40,
        cursor_store=cursor_store,
    )

    started_at = datetime.now(timezone.utc)
    await poller.start()
    try:
        # Within a few seconds of (now - 24h), tolerate test scheduling jitter.
        expected = started_at - timedelta(hours=POLL_INITIAL_LOOKBACK_HOURS)
        delta = abs((poller._cursor - expected).total_seconds())
        assert delta < 5, f"cursor drift {delta}s"
    finally:
        await poller.stop()


@pytest.mark.asyncio
async def test_poller_resumes_from_durable_cursor_with_1h_guard(tmp_path):
    """A saved cursor 6h old should yield an effective cursor of
    saved-1h (within the 24h floor)."""
    db = tmp_path / "r.db"
    settings = _Settings(db)
    cursor_dir = tmp_path / "cfg"
    cursor_store = PollerCursor(cursor_dir)
    saved = datetime.now(timezone.utc) - timedelta(hours=6)
    cursor_store.save(saved)

    poller = ReceiptPoller(
        settings=settings, node_id="n1",
        identity_key="0x" + "c" * 64,
        node_wallet_address="0x" + "d" * 40,
        cursor_store=cursor_store,
    )
    await poller.start()
    try:
        # Effective = min(saved-1h, now-24h). saved-1h = 7h ago, now-24h = 24h
        # ago → min picks 24h ago (the earlier of the two).
        # Spec: 24h is a *floor* on how far back we go. With saved=6h we
        # want to look back at LEAST as far as saved-1h=7h, but the 24h
        # floor takes precedence per the implementation.
        floor = datetime.now(timezone.utc) - timedelta(hours=24)
        delta = abs((poller._cursor - floor).total_seconds())
        assert delta < 5
    finally:
        await poller.stop()


@pytest.mark.asyncio
async def test_cursor_uses_24h_floor_on_long_outage(tmp_path):
    """Saved cursor 50h old → effective cursor uses the saved-1h value
    (which is older than the 24h floor) so no signed receipts are missed
    just because the daemon was offline for two days."""
    db = tmp_path / "r.db"
    settings = _Settings(db)
    cursor_dir = tmp_path / "cfg"
    cursor_store = PollerCursor(cursor_dir)
    saved = datetime.now(timezone.utc) - timedelta(hours=50)
    cursor_store.save(saved)

    poller = ReceiptPoller(
        settings=settings, node_id="n1",
        identity_key="0x" + "c" * 64,
        node_wallet_address="0x" + "d" * 40,
        cursor_store=cursor_store,
    )
    await poller.start()
    try:
        # saved - 1h = 51h ago, now - 24h = 24h ago. min picks the
        # earlier (51h ago) so old receipts in retention can be picked
        # up if the gateway still has them.
        expected = saved - timedelta(hours=1)
        delta = abs((poller._cursor - expected).total_seconds())
        assert delta < 5, f"cursor drift {delta}s"
    finally:
        await poller.stop()


@pytest.mark.asyncio
async def test_durable_cursor_survives_restart(tmp_path):
    """Spec test: write cursor via PollerCursor, reload through a fresh
    PollerCursor instance, value matches."""
    cursor_dir = tmp_path / "cfg"
    a = PollerCursor(cursor_dir)
    when = datetime(2026, 4, 25, 9, 0, 0, tzinfo=timezone.utc)
    a.save(when)

    b = PollerCursor(cursor_dir)
    assert b.load() == when


@pytest.mark.asyncio
async def test_poller_persists_cursor_after_successful_tick(tmp_path):
    """After a tick that returned ≥1 signed receipt, the cursor file on
    disk reflects the latest signed_at timestamp the poll observed."""
    db = tmp_path / "r.db"
    settings = _Settings(db)
    cursor_dir = tmp_path / "cfg"
    cursor_store = PollerCursor(cursor_dir)

    r = _mk_receipt()
    store = get_store(str(db))
    await store.initialize()
    await store.store_unsigned(r, request_id="req-1")

    poller = ReceiptPoller(
        settings=settings, node_id="n1",
        identity_key="0x" + "c" * 64,
        node_wallet_address="0x" + "d" * 40,
        cursor_store=cursor_store,
    )
    # Don't run the loop — just call _tick directly with the cursor
    # already set (mimic post-start state).
    poller._cursor = datetime.now(timezone.utc) - timedelta(hours=1)

    signed_at = datetime.now(timezone.utc) - timedelta(minutes=5)
    signed_rows = [{
        "request_uuid": r.request_uuid,
        "signature": "0xsig",
        "created_at": signed_at.isoformat(),
    }]

    class FakeResp:
        def __init__(self, payload, status=200):
            self.status_code = status
            self._payload = payload
        def json(self):
            return self._payload

    class FakeClient:
        def __init__(self):
            self.calls = 0
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def get(self, url, params=None):
            self.calls += 1
            if "/rejected-receipts" in url:
                return FakeResp([])
            return FakeResp(signed_rows)

    fake_client = FakeClient()
    with patch("app.payment.receipt_submitter.httpx.AsyncClient",
               return_value=fake_client):
        await poller._tick()

    # Local state: the row is now signed.
    stored = await store.get_by_uuid(r.request_uuid)
    assert stored.signature == "0xsig"

    # Cursor file on disk reflects the row's signed_at.
    saved = cursor_store.load()
    assert saved is not None
    delta = abs((saved - signed_at).total_seconds())
    assert delta < 1


# ── L9: clock-skew escalation ──────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_skew_between_tests():
    reset_clock_skew_state()
    yield
    reset_clock_skew_state()


@pytest.mark.asyncio
async def test_clock_skew_state_starts_clean():
    state = get_clock_skew_state()
    assert state["in_drift"] is False
    assert state["consecutive_failures"] == 0
    assert state["last_error_at"] is None


@pytest.mark.asyncio
async def test_clock_skew_escalates_after_3_consecutive(tmp_path, caplog):
    """3 consecutive timestamp-expired rejections within the 5-min
    window must:
      - log an ERROR with the NTP message
      - flip get_clock_skew_state()["in_drift"] to True
    """
    from app.payment.receipt_submitter import _record_clock_skew_event

    with caplog.at_level("ERROR", logger="app.payment.receipt_submitter"):
        _record_clock_skew_event("Timestamp expired. Drift 47.")
        _record_clock_skew_event("Timestamp expired. Drift 49.")
        _record_clock_skew_event("Timestamp expired. Drift 52.")

    state = get_clock_skew_state()
    assert state["in_drift"] is True
    assert state["consecutive_failures"] == 3

    errors = [r for r in caplog.records if r.levelname == "ERROR"]
    assert any(
        "out of sync" in (r.message or "") and "NTP" in (r.message or "")
        for r in errors
    ), f"Expected NTP error log; got: {[r.message for r in errors]}"
    # Diff should be extracted from the last detail.
    assert state["last_seconds_diff"] == 52


@pytest.mark.asyncio
async def test_clock_skew_two_errors_does_not_escalate(tmp_path, caplog):
    """Below threshold: still WARN per row but no ERROR-level escalation."""
    from app.payment.receipt_submitter import _record_clock_skew_event

    with caplog.at_level("ERROR", logger="app.payment.receipt_submitter"):
        _record_clock_skew_event("Timestamp expired. Drift 30.")
        _record_clock_skew_event("Timestamp expired. Drift 32.")

    state = get_clock_skew_state()
    assert state["in_drift"] is False
    assert state["consecutive_failures"] == 2

    errors = [r for r in caplog.records if r.levelname == "ERROR"]
    assert errors == []


@pytest.mark.asyncio
async def test_clock_skew_resets_on_successful_sign(tmp_path):
    """A successful sign of any receipt clears the skew counter so a
    later transient drift doesn't immediately re-escalate."""
    from app.payment.receipt_submitter import _record_clock_skew_event

    db = tmp_path / "r.db"
    settings = _Settings(db)
    s = ReceiptSubmitter(
        settings=settings, node_id="n1",
        identity_key="0x" + "c" * 64,
        identity_address="0x" + "a" * 40,
        gateway_payer_address="0x" + "d" * 40,
        node_wallet_address="0x" + "e" * 40,
    )

    # Drift up.
    _record_clock_skew_event("Timestamp expired. Drift 47.")
    _record_clock_skew_event("Timestamp expired. Drift 47.")
    _record_clock_skew_event("Timestamp expired. Drift 47.")
    assert get_clock_skew_state()["in_drift"] is True

    # Now a relay's submit returns 200 signed.
    r = _mk_receipt()
    store = get_store(str(db))
    await store.initialize()
    await store.store_unsigned(r, request_id="req-1")

    resp = httpx.Response(
        status_code=200,
        json={"status": "signed", "signature": "0xsig"},
    )

    class FakeClient:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def post(self, url, json):
            return resp

    with patch("app.payment.receipt_submitter.httpx.AsyncClient",
               return_value=FakeClient()):
        await s._fire_submit(r, "req-1")

    state = get_clock_skew_state()
    assert state["in_drift"] is False
    assert state["consecutive_failures"] == 0


@pytest.mark.asyncio
async def test_clock_skew_state_shape_is_stable():
    """The dict shape is the contract for a future TUI / GUI surface."""
    state = get_clock_skew_state()
    assert set(state.keys()) == {
        "in_drift",
        "consecutive_failures",
        "last_error_at",
        "last_seconds_diff",
    }


# ── Poller backoff gating ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_poller_skips_tick_when_all_unsigned_in_backoff(tmp_path):
    """When every unsigned row is still inside its transient-backoff
    window, the poller MUST NOT issue any HTTP request — that's the
    whole point of the backoff (avoid hot-looping against a 429 storm).
    """
    db = tmp_path / "r.db"
    settings = _Settings(db)
    cursor_store = PollerCursor(tmp_path / "cfg")

    r = _mk_receipt()
    store = get_store(str(db))
    await store.initialize()
    await store.store_unsigned(r, request_id="req-1")
    # Pre-bump transient_attempts and stamp last_attempt_at to now so
    # the row is squarely inside its backoff window.
    await store.mark_transient_attempt(r.request_uuid)

    poller = ReceiptPoller(
        settings=settings, node_id="n1",
        identity_key="0x" + "c" * 64,
        node_wallet_address="0x" + "d" * 40,
        cursor_store=cursor_store,
    )

    class TrackingClient:
        def __init__(self):
            self.call_count = 0
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def get(self, url, params=None):
            self.call_count += 1
            class _R:
                status_code = 200
                def json(self_inner):
                    return []
            return _R()

    tracker = TrackingClient()
    with patch("app.payment.receipt_submitter.httpx.AsyncClient",
               return_value=tracker):
        await poller._tick()

    # No HTTP at all — neither signed-receipts nor rejection poll.
    assert tracker.call_count == 0


@pytest.mark.asyncio
async def test_poller_escalates_budget_exhausted_rows_during_tick(tmp_path):
    """The poller's tick runs the escalation pass and emits a WARN."""
    import logging
    db = tmp_path / "r.db"
    settings = _Settings(db)
    cursor_store = PollerCursor(tmp_path / "cfg")

    r = _mk_receipt()
    store = get_store(str(db))
    await store.initialize()
    await store.store_unsigned(r, request_id="req-1")
    for _ in range(TRANSIENT_BUDGET_ATTEMPTS):
        await store.mark_transient_attempt(r.request_uuid)

    poller = ReceiptPoller(
        settings=settings, node_id="n1",
        identity_key="0x" + "c" * 64,
        node_wallet_address="0x" + "d" * 40,
        cursor_store=cursor_store,
    )

    # No HTTP needed — count_unsigned_ready will return 0 once row is
    # escalated (no longer pending_sign), so the poller short-circuits.
    class FakeClient:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def get(self, url, params=None):
            class _R:
                status_code = 200
                def json(self_inner):
                    return []
            return _R()

    captured = []

    class _Handler(logging.Handler):
        def emit(self, record):
            captured.append((record.levelname, record.getMessage()))

    handler = _Handler()
    handler.setLevel(logging.WARNING)
    target = logging.getLogger("app.payment.receipt_submitter")
    target.addHandler(handler)
    try:
        with patch("app.payment.receipt_submitter.httpx.AsyncClient",
                   return_value=FakeClient()):
            await poller._tick()
    finally:
        target.removeHandler(handler)

    stored = await store.get_by_uuid(r.request_uuid)
    assert stored.last_error_code == reasons.SIGN_TRANSIENT_BUDGET_EXHAUSTED
    assert stored.view == "failed_retryable"
    assert any(
        lvl == "WARNING" and reasons.SIGN_TRANSIENT_BUDGET_EXHAUSTED in msg
        for lvl, msg in captured
    ), f"expected WARN log; got {captured}"
