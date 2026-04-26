"""Optional background auto-claim monitor (P10 of v1.5 plan).

Polls the local receipt store every ``_AUTO_CLAIM_POLL_INTERVAL``
seconds. When the claimable rollup crosses EITHER configured threshold
— total wei >= ``settings.AUTO_CLAIM_THRESHOLD_SPACE_WEI`` OR count >=
``settings.AUTO_CLAIM_THRESHOLD_COUNT`` — the monitor fires a single
``claim_all()`` run under the same ``claim.lock`` the manual CLI / GUI
paths use, so there's no risk of double-submitting nonces.

Failure policy is **S3-c** from the v1.5 plan: if the claim raises
(RPC down, batch revert, anything else), we log ERROR once and sit
idle. The next firing only happens if thresholds are tripped again,
which they will be — the unclaimed rows are still there. There is no
exponential backoff, no automatic retry storm. Operators retry by
running ``--claim`` (or eventually ``/claim`` from the TUI).

Lifecycle mirrors :class:`app.payment.reaper.ClaimReaper` — same
``start`` / ``stop`` pair, started from ``app/main.py`` once daemon
init reaches the post-registration phase, cancelled on shutdown.

The monitor is a strict no-op when ``settings.AUTO_CLAIM_ENABLED`` is
false (the default). ``start()`` returns without scheduling a task,
keeping disabled installs free of background work.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Optional

from app.config import Settings
from app.payment.claim_lock import ClaimLockHeld, acquire_claim_lock
from app.payment.receipt_store import get_store

logger = logging.getLogger(__name__)


# Polling cadence. Constant — the interval is short enough that the
# monitor reacts quickly to threshold trips, and long enough that the
# disabled-but-mistakenly-started case is harmless. Manual claims still
# run instantly via the lock-protected CLI/GUI path.
_AUTO_CLAIM_POLL_INTERVAL = 30.0


def _now_iso() -> str:
    """ISO-8601 UTC timestamp, second-precision — matches existing
    last-attempt fields elsewhere in the schema (e.g. settings synced_at).
    """
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _coerce_threshold_wei(raw: object) -> int:
    """Threshold is stored as a string in settings.json (wei amounts can
    exceed JS Number.MAX_SAFE_INTEGER). Coerce defensively — empty/missing
    falls back to ``0`` which simply means "never triggered by balance".
    """
    if raw is None or raw == "":
        return 0
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


class AutoClaimMonitor:
    """Background monitor that fires ``claim_all()`` on threshold trip.

    Cheap when disabled (``start()`` is a no-op). When enabled, runs a
    single coroutine that polls every ``_AUTO_CLAIM_POLL_INTERVAL``
    seconds and dispatches at most one claim per tick.
    """

    def __init__(self, settings: Settings, settlement_key_hex: str | None = None) -> None:
        self._settings = settings
        # Resolved at construction time. The monitor never re-reads
        # settings; if the operator flips the flag in settings.json,
        # they restart the daemon. (Settings hot-reload is S8 in the
        # v1.5 plan and explicitly deferred.)
        self._enabled = bool(getattr(settings, "AUTO_CLAIM_ENABLED", False))
        self._threshold_wei = _coerce_threshold_wei(
            getattr(settings, "AUTO_CLAIM_THRESHOLD_SPACE_WEI", 0),
        )
        self._threshold_count = int(
            getattr(settings, "AUTO_CLAIM_THRESHOLD_COUNT", 0) or 0,
        )

        # Settlement key — same source the CLI uses. May be None if the
        # caller didn't resolve it; in that case the monitor refuses to
        # fire and the operator gets a one-time WARN. We don't call
        # ``load_or_create_identity`` ourselves to avoid surprising the
        # operator with a passphrase prompt from a background task.
        self._settlement_key_hex = settlement_key_hex

        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

        # Status surface — read by ``get_status()`` for future TUI/GUI.
        self._current_claimable_wei: int = 0
        self._current_claimable_count: int = 0
        self._last_attempt_at: str | None = None
        self._last_attempt_outcome: str = "none"  # "success" | "failed" | "none"
        self._last_error: str | None = None

        # Suppress repeated "lock held" log lines when a manual claim is
        # running for a long time. We log once per contention burst; the
        # flag clears the moment we get the lock again.
        self._lock_log_suppressed = False

    @property
    def enabled(self) -> bool:
        return self._enabled

    async def start(self) -> None:
        """Schedule the polling task. No-op if disabled."""
        if not self._enabled:
            logger.debug(
                "Auto-claim monitor disabled (AUTO_CLAIM_ENABLED=False) — "
                "not starting.",
            )
            return
        if self._task is not None:
            return

        if not self._settlement_key_hex:
            # Disabled at runtime — without a key we can't sign txs.
            # Better to log loudly than to silently never fire.
            logger.warning(
                "Auto-claim monitor: no settlement key resolved at startup; "
                "auto-claim will not run. Restart the daemon with "
                "SR_SETTLEMENT_KEY set or with the identity key readable.",
            )
            return

        if not (self._settings.ESCROW_CONTRACT_ADDRESS and self._settings.ESCROW_CHAIN_RPC):
            logger.warning(
                "Auto-claim monitor: escrow contract / RPC not configured; "
                "auto-claim will not run.",
            )
            return

        self._stop.clear()
        self._task = asyncio.create_task(self._loop())
        logger.info(
            "Auto-claim monitor started (interval=%.0fs, threshold=%s wei OR %d receipts)",
            _AUTO_CLAIM_POLL_INTERVAL,
            self._threshold_wei,
            self._threshold_count,
        )

    async def stop(self) -> None:
        """Cancel and await the polling task."""
        self._stop.set()
        if self._task is not None:
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _loop(self) -> None:
        # First tick runs after a short delay so daemon startup has
        # time to settle (registration, poller boot, etc.) before we
        # start poking the receipt store. Symmetric with the reaper's
        # one-shot-at-startup pattern but without the risk of firing
        # an immediate claim against a half-initialised system.
        try:
            await asyncio.wait_for(
                self._stop.wait(), timeout=_AUTO_CLAIM_POLL_INTERVAL,
            )
            return
        except asyncio.TimeoutError:
            pass

        while not self._stop.is_set():
            try:
                await self.tick()
            except Exception:
                # Defensive — tick() owns its own try/except for the
                # claim path; any uncaught exception here is a bug we
                # want logged but never want to crash the daemon for.
                logger.exception("Auto-claim monitor tick crashed")

            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=_AUTO_CLAIM_POLL_INTERVAL,
                )
                return
            except asyncio.TimeoutError:
                continue

    async def tick(self) -> dict:
        """One poll → maybe-claim cycle. Returns a small status dict
        (used by tests; the daemon ignores the return value).
        """
        store = get_store(self._settings.RECEIPT_STORE_PATH)
        await store.initialize()
        summary = await store.summary()

        self._current_claimable_wei = int(summary.get("claimable_total_price", 0) or 0)
        self._current_claimable_count = int(summary.get("claimable", 0) or 0)

        balance_trip = (
            self._threshold_wei > 0
            and self._current_claimable_wei >= self._threshold_wei
        )
        count_trip = (
            self._threshold_count > 0
            and self._current_claimable_count >= self._threshold_count
        )
        if not (balance_trip or count_trip):
            return {
                "fired": False,
                "reason": "below_threshold",
                "claimable_wei": self._current_claimable_wei,
                "claimable_count": self._current_claimable_count,
            }

        # Above threshold → attempt one claim. Acquire the same lock
        # the manual CLI/GUI paths take. ClaimLockHeld means a manual
        # run is in flight; skip and try next tick (the receipts will
        # still be claimable if the manual run failed, and gone if it
        # succeeded — either way a polite back-off is correct).
        try:
            with acquire_claim_lock(self._settings):
                self._lock_log_suppressed = False
                return await self._fire_claim_locked()
        except ClaimLockHeld:
            if not self._lock_log_suppressed:
                logger.info(
                    "Auto-claim: claim.lock held by another process "
                    "(manual claim in progress?). Skipping this tick.",
                )
                self._lock_log_suppressed = True
            return {
                "fired": False,
                "reason": "lock_held",
                "claimable_wei": self._current_claimable_wei,
                "claimable_count": self._current_claimable_count,
            }

    async def _fire_claim_locked(self) -> dict:
        """Inside the claim_lock — run claim_all() and record outcome.

        S3-c: on any exception, log ERROR + record failure + return.
        Do not retry, do not back off. The next tick re-evaluates the
        thresholds; if the receipts didn't get claimed, the monitor
        will trip again — but only because the operator's situation
        actually warrants another attempt.
        """
        from app.payment.settlement import claim_all

        logger.info(
            "Auto-claim: thresholds tripped (claimable=%d wei / %d receipts) "
            "— firing claim.",
            self._current_claimable_wei,
            self._current_claimable_count,
        )
        self._last_attempt_at = _now_iso()

        try:
            results = await claim_all(
                self._settings,
                self._settlement_key_hex,  # type: ignore[arg-type]
                include_retryable=False,
            )
        except Exception as e:  # noqa: BLE001
            self._last_attempt_outcome = "failed"
            self._last_error = f"{type(e).__name__}: {e}"
            logger.error(
                "Auto-claim: claim_all() raised — %s. Sitting idle until "
                "thresholds trip again. Operator should investigate via "
                "`--receipts` and retry with `--claim` if appropriate.",
                self._last_error,
            )
            return {"fired": True, "outcome": "failed", "error": self._last_error}

        # Success path: claim_all() returned a list of ClaimResult.
        # Even per-batch reverts (e.g. one bad sig in a batch of 50)
        # are reflected in the results' ``error`` fields rather than
        # raising — those don't count as "auto-claim failed", they
        # count as "auto-claim ran". Only an outright raise (RPC down
        # at submit-time, etc.) is treated as failure.
        self._last_attempt_outcome = "success"
        self._last_error = None
        submitted = sum(r.submitted for r in results)
        reverted_batches = sum(1 for r in results if r.error)
        logger.info(
            "Auto-claim: claim_all() complete — %d batch(es), %d receipt(s) "
            "submitted, %d reverted batch(es).",
            len(results), submitted, reverted_batches,
        )
        return {
            "fired": True,
            "outcome": "success",
            "batches": len(results),
            "submitted": submitted,
            "reverted_batches": reverted_batches,
        }

    def get_status(self) -> dict:
        """Snapshot of current monitor state for TUI/GUI consumption.

        No locking — fields are written by a single coroutine in this
        same event loop, so a simple read is consistent. The TUI in
        Track P8 will poll this every second or two; cheap.
        """
        return {
            "enabled": self._enabled,
            "next_threshold_space_wei": str(self._threshold_wei),
            "next_threshold_count": int(self._threshold_count),
            "current_claimable_wei": str(self._current_claimable_wei),
            "current_claimable_count": int(self._current_claimable_count),
            "last_attempt_at": self._last_attempt_at,
            "last_attempt_outcome": self._last_attempt_outcome,
            "last_error": self._last_error,
        }
