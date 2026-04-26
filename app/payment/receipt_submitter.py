"""Leg 2 receipt submission + background sync with the coord API.

By topology rule the provider never connects to the gateway. Interaction
goes through the coord API:

1. After a relay, generate a Receipt from the provider's own byte count
   and hand it to the submitter. Submitter records it locally as
   ``unsigned`` (signature=None) then fires a best-effort POST to the
   coord API.
2. Happy path: coord API returns 200 with the signed receipt immediately
   (gateway's pending_leg2 row already existed). Submitter stores the
   signature locally.
3. Slow path: coord API returns 202 (accepted for async signing). Nothing
   else to do — the provider's local record stays unsigned and will be
   filled in by the poller.
4. ``ReceiptPoller`` runs in the background every ``poll_interval_seconds``
   and does a single short GET to ``/nodes/{id}/signed-receipts?since=<ts>``
   to pick up any signed copies for locally-unsigned rows. Short HTTP
   timeouts throughout (5s).

All failures are non-fatal: local state is the source of truth, and the
poller will pick up signatures on its next tick.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
import uuid
from datetime import datetime, timedelta, timezone

import httpx
from eth_account import Account
from eth_account.messages import encode_defunct

from app.config import Settings
from app.paths import config_dir
from app.payment import reasons
from app.payment.eip712 import Receipt, address_to_bytes32
from app.payment.poller_cursor import PollerCursor
from app.payment.receipt_store import get_store

logger = logging.getLogger(__name__)

_GB = 1024 ** 3

# Short timeouts — no request should wait for another service's work.
SUBMIT_TIMEOUT_SECONDS = 5.0
POLL_TIMEOUT_SECONDS = 5.0
POLL_INTERVAL_SECONDS = 10.0

# Absorbs clock skew between coord API DB and provider host — after each
# poll, cursor is rolled back by this much so a row inserted with an
# older timestamp (e.g. cron-side sync path finishing after a fast GET)
# still gets picked up on the next tick. mark_signed is idempotent.
POLL_CURSOR_BUFFER_SECONDS = 60

# On start, back the cursor up by this much — catches anything signed
# between the node's previous shutdown and this startup.
POLL_INITIAL_LOOKBACK_HOURS = 24

# Per-row exponential backoff for transient sign-side failures (L1).
# Effective wait = min(60 * 2^transient_attempts, 3600). Cap at 1h so a
# row that hits a permanent-but-transient-looking storm still re-tries
# every hour rather than disappearing into a multi-day backoff. Kept in
# sync with the SQL expression in
# :func:`ReceiptStore.count_unsigned_ready` — change both together.
TRANSIENT_BACKOFF_BASE_SECONDS = 60
TRANSIENT_BACKOFF_CAP_SECONDS = 3600

# Total transient-failure budget before a row escalates from
# pending_sign → failed_retryable with SIGN_TRANSIENT_BUDGET_EXHAUSTED.
# 14 attempts at exponential backoff with the 1h cap covers ~24h:
# 60 + 120 + 240 + 480 + 960 + 1800(cap) + 3600 + 3600 + 3600 + 3600 +
# 3600 + 3600 + 3600 + 3600 ≈ 31000s ≈ 8.6h of pure backoff plus
# whatever wall-clock the operator was offline. Operators see one WARN
# per escalation.
TRANSIENT_BUDGET_ATTEMPTS = 14

# L9: a run of this many consecutive timestamp-expired rejections inside
# CLOCK_SKEW_ESCALATION_WINDOW_SECONDS escalates from quiet WARN-per-row
# (operator-friendly NTP nudge) to one ERROR-level "system clock out of
# sync" message. Reset on any successful sign.
CLOCK_SKEW_ESCALATION_THRESHOLD = 3
CLOCK_SKEW_ESCALATION_WINDOW_SECONDS = 300


def transient_backoff_seconds(transient_attempts: int) -> int:
    """Mirror of the SQL expression in
    :meth:`ReceiptStore.count_unsigned_ready`.

    For ``transient_attempts == 0`` this returns 60 (the first retry
    waits one minute), but the gate in ``count_unsigned_ready`` lets
    rows with 0 attempts retry immediately. The function is exposed
    here for tests and future TUI surfaces.
    """
    if transient_attempts <= 0:
        return TRANSIENT_BACKOFF_BASE_SECONDS
    # ``min(transient_attempts, 16)`` matches the SQL guard against
    # 1<<N overflowing at very high counters.
    shift = min(int(transient_attempts), 16)
    return min(
        TRANSIENT_BACKOFF_BASE_SECONDS * (1 << shift),
        TRANSIENT_BACKOFF_CAP_SECONDS,
    )


def _is_transient_status(status_code: int) -> bool:
    """429 + any 5xx are transient at the submitter layer."""
    return status_code == 429 or 500 <= status_code <= 599


# L9 — module-level clock-skew tracker. Module-level because the
# submitter is created per-relay and we want the counter to span
# instances. The poller's clock-skew arrivals (via
# /rejected-receipts) also feed this counter, so shared state is the
# simplest correct option. Tests use ``reset_clock_skew_state``.
_clock_skew_state: dict = {
    "consecutive_failures": 0,
    "last_error_at": None,
    "last_seconds_diff": None,
    "in_drift": False,
    "last_escalation_at": None,
}


def get_clock_skew_state() -> dict:
    """Return a copy of the current clock-skew state.

    Stable shape: ``{"in_drift": bool, "consecutive_failures": int,
    "last_error_at": iso str | None, "last_seconds_diff": int | None}``.
    Future TUI / GUI surfaces read this; no UI work in this PR.
    """
    return {
        "in_drift": bool(_clock_skew_state["in_drift"]),
        "consecutive_failures": int(_clock_skew_state["consecutive_failures"]),
        "last_error_at": (
            _clock_skew_state["last_error_at"].isoformat()
            if _clock_skew_state["last_error_at"] is not None
            else None
        ),
        "last_seconds_diff": _clock_skew_state["last_seconds_diff"],
    }


def reset_clock_skew_state() -> None:
    """Clear the module-level clock-skew counter.

    Called on any successful sign and from tests.
    """
    _clock_skew_state["consecutive_failures"] = 0
    _clock_skew_state["last_error_at"] = None
    _clock_skew_state["last_seconds_diff"] = None
    _clock_skew_state["in_drift"] = False
    _clock_skew_state["last_escalation_at"] = None


def _extract_seconds_diff(detail: str) -> int | None:
    """Best-effort parse of the seconds-of-drift from a coord API reply.

    Coord API formats the rejection as "Timestamp expired. Must be
    within Ns. Got drift Ms." — we look for the largest integer that
    looks like a seconds value. None on parse failure; the operator
    still sees the raw detail in the log.
    """
    if not detail:
        return None
    import re
    nums = re.findall(r"-?\d+", detail)
    if not nums:
        return None
    try:
        # Coord normally puts the drift as the last integer in the message.
        return int(nums[-1])
    except (TypeError, ValueError):
        return None


def _record_clock_skew_event(detail: str) -> None:
    """Bump the consecutive-failure counter and escalate if past threshold."""
    now = datetime.now(timezone.utc)
    state = _clock_skew_state
    state["consecutive_failures"] = int(state["consecutive_failures"]) + 1
    state["last_error_at"] = now
    diff = _extract_seconds_diff(detail or "")
    if diff is not None:
        state["last_seconds_diff"] = diff

    if state["consecutive_failures"] < CLOCK_SKEW_ESCALATION_THRESHOLD:
        return

    last_esc = state.get("last_escalation_at")
    window = timedelta(seconds=CLOCK_SKEW_ESCALATION_WINDOW_SECONDS)

    # First-time escalation: always log + flip in_drift on.
    # Subsequent escalations: only log again if we've left the window
    # since the last ERROR (avoids spamming the operator's log).
    fresh_window = (
        last_esc is None or (now - last_esc) > window
    )

    state["in_drift"] = True
    if fresh_window:
        diff_text = (
            f" Coord API timestamp differs by {state['last_seconds_diff']} seconds."
            if state["last_seconds_diff"] is not None
            else ""
        )
        logger.error(
            "System clock appears out of sync — check NTP.%s "
            "This will block all receipt submission until fixed.",
            diff_text,
        )
        state["last_escalation_at"] = now


def _build_receipt(
    gateway_payer_address: str,
    node_wallet_address: str,
    rate_per_gb: int,
    data_amount: int,
) -> Receipt:
    total_price = (data_amount * rate_per_gb) // _GB
    return Receipt(
        client_address=gateway_payer_address,
        node_address=address_to_bytes32(node_wallet_address),
        request_uuid=str(uuid.uuid4()),
        data_amount=int(data_amount),
        total_price=int(total_price),
    )


def _receipt_body_hash(receipt: Receipt) -> str:
    """Deterministic sha256 over the receipt body — used to bind the
    identity signature to the exact payload so a MITM can't tamper with
    ``dataAmount``/``totalPrice`` using a captured signature.
    """
    canonical = (
        f"{receipt.client_address.lower()}|{receipt.node_address.lower()}|"
        f"{receipt.request_uuid}|{int(receipt.data_amount)}|{int(receipt.total_price)}"
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _sign_submission(
    identity_key: str,
    node_id: str,
    request_id: str,
    timestamp: int,
    receipt_hash: str,
) -> str:
    msg = (
        f"space-router:submit-receipt:{node_id}:{request_id}"
        f":{receipt_hash}:{timestamp}"
    )
    account = Account.from_key(identity_key)
    signed = account.sign_message(encode_defunct(text=msg))
    return "0x" + signed.signature.hex()


def _sign_list_request(identity_key: str, node_id: str, timestamp: int) -> str:
    msg = f"space-router:list-signed-receipts:{node_id}:{timestamp}"
    account = Account.from_key(identity_key)
    signed = account.sign_message(encode_defunct(text=msg))
    return "0x" + signed.signature.hex()


def _sign_list_rejections_request(identity_key: str, node_id: str, timestamp: int) -> str:
    msg = f"space-router:list-rejected-receipts:{node_id}:{timestamp}"
    account = Account.from_key(identity_key)
    signed = account.sign_message(encode_defunct(text=msg))
    return "0x" + signed.signature.hex()


class ReceiptSubmitter:
    """Builds receipts, records them locally, and fires the best-effort POST."""

    def __init__(
        self,
        settings: Settings,
        node_id: str,
        identity_key: str,
        identity_address: str,
        gateway_payer_address: str,
        node_wallet_address: str,
    ) -> None:
        self._settings = settings
        self._node_id = node_id
        self._identity_key = identity_key if identity_key.startswith("0x") else "0x" + identity_key
        self._identity_address = identity_address.lower()
        self._gateway_payer_address = gateway_payer_address
        self._node_wallet_address = node_wallet_address

    @property
    def ready(self) -> bool:
        return bool(
            self._gateway_payer_address
            and self._node_wallet_address
            and self._identity_key
            and self._node_id
        )

    async def submit(self, request_id: str, data_amount: int) -> None:
        """Generate + store unsigned + fire async POST.

        Returns after the POST completes (or times out) but never
        raises — the poller will fill in the signature later if the POST
        didn't already.
        """
        if not self.ready or data_amount <= 0:
            return

        # Refuse to generate zero-value receipts — they cluster the local
        # DB and waste sign round-trips with no payout. Operators are
        # already warned at startup when NODE_RATE_PER_GB is 0.
        if self._settings.NODE_RATE_PER_GB <= 0:
            logger.debug(
                "Leg 2 submit skipped: NODE_RATE_PER_GB=0, receipt would be zero-value",
            )
            return

        receipt = _build_receipt(
            gateway_payer_address=self._gateway_payer_address,
            node_wallet_address=self._node_wallet_address,
            rate_per_gb=self._settings.NODE_RATE_PER_GB,
            data_amount=data_amount,
        )

        # Persist locally as unsigned *before* firing the POST, so a
        # crash/timeout doesn't lose the receipt.
        try:
            store = get_store(self._settings.RECEIPT_STORE_PATH)
            await store.initialize()
            await store.store_unsigned(receipt, request_id=request_id)
        except Exception:
            logger.exception("Failed to persist unsigned receipt uuid=%s", receipt.request_uuid)
            return

        # Best-effort submit.
        await self._fire_submit(receipt, request_id)

    async def _fire_submit(self, receipt: Receipt, request_id: str) -> None:
        timestamp = int(time.time())
        receipt_hash = _receipt_body_hash(receipt)
        signature = _sign_submission(
            self._identity_key, self._node_id, request_id, timestamp, receipt_hash,
        )
        url = self._settings.COORDINATION_API_URL.rstrip("/") + f"/nodes/{self._node_id}/receipts"
        payload = {
            "request_id": request_id,
            "receipt": receipt.to_json_dict(),
            "signature": signature,
            "timestamp": timestamp,
        }

        try:
            async with httpx.AsyncClient(timeout=SUBMIT_TIMEOUT_SECONDS) as client:
                resp = await client.post(url, json=payload)
        except httpx.RequestError as exc:
            # Network / timeout / DNS — pure transient. Bump the per-row
            # backoff counter so the poller's retry path doesn't busy-wait
            # against an outage.
            logger.debug("Leg 2 submit network error uuid=%s: %s — poller will retry",
                         receipt.request_uuid, exc)
            await _record_transient_attempt(
                self._settings, receipt.request_uuid,
            )
            return

        if resp.status_code == 200:
            try:
                body = resp.json()
            except Exception:
                return
            if body.get("status") == "signed" and body.get("signature"):
                try:
                    store = get_store(self._settings.RECEIPT_STORE_PATH)
                    await store.mark_signed(receipt.request_uuid, body["signature"])
                    # Successful sign clears clock-skew + transient state.
                    reset_clock_skew_state()
                    logger.info(
                        "Leg 2 receipt signed synchronously uuid=%s amount=%d",
                        receipt.request_uuid, receipt.data_amount,
                    )
                except Exception:
                    logger.exception("Failed to mark receipt signed uuid=%s",
                                     receipt.request_uuid)
        elif resp.status_code == 202:
            logger.debug(
                "Leg 2 receipt queued for async signing uuid=%s", receipt.request_uuid,
            )
        elif resp.status_code == 403:
            # Special-case the coord API's "Timestamp expired" anti-replay
            # rejection — it's a clock-drift diagnostic, not a permanent
            # failure. Transient so the counter doesn't increment; user
            # gets an NTP-friendly message.
            detail = ""
            try:
                detail = resp.json().get("detail", "")
            except Exception:
                detail = (resp.text or "")[:200]
            if "timestamp" in detail.lower() and "expire" in detail.lower():
                _record_clock_skew_event(detail)
                await _record_clock_skew(
                    self._settings, receipt.request_uuid, detail,
                )
            else:
                # 403 with a different reason → signature mismatch, node
                # not found — treat as permanent to surface in UI.
                await _record_sign_rejection(
                    self._settings, receipt.request_uuid, resp,
                )
        elif resp.status_code in (400, 409, 422):
            # Explicit rejection from the coord API / gateway with a
            # structured reason. Record it so the user sees "why" in the
            # retryable-failures UI.
            await _record_sign_rejection(
                self._settings, receipt.request_uuid, resp,
            )
        elif _is_transient_status(resp.status_code):
            # 429 / 5xx → transient. Increment the per-row backoff
            # counter so the poller doesn't hot-loop.
            logger.debug(
                "Leg 2 submit transient %d uuid=%s — backing off per-row",
                resp.status_code, receipt.request_uuid,
            )
            await _record_transient_attempt(
                self._settings, receipt.request_uuid,
            )
        else:
            logger.debug(
                "Leg 2 submit got %d uuid=%s — poller will retry",
                resp.status_code, receipt.request_uuid,
            )


async def _record_transient_attempt(
    settings: Settings, request_uuid: str,
) -> None:
    """Bump per-row transient backoff counter. No exception escapes."""
    try:
        store = get_store(settings.RECEIPT_STORE_PATH)
        await store.mark_transient_attempt(request_uuid)
    except Exception:
        logger.exception(
            "Failed to record transient sign attempt uuid=%s", request_uuid,
        )


async def _record_clock_skew(
    settings: Settings, request_uuid: str, detail: str,
) -> None:
    """Record a clock-drift diagnostic.

    Transient per ``reasons.TRANSIENT_CODES``, so the attempts counter
    doesn't increment — once the operator fixes NTP, the next poll tick
    re-submits naturally.
    """
    try:
        store = get_store(settings.RECEIPT_STORE_PATH)
        await store.mark_sign_failed(
            request_uuid, reasons.SIGN_REJECTED_CLOCK_SKEW, detail,
        )
        logger.warning(
            "Leg 2 submit rejected uuid=%s code=%s — enable NTP on this host "
            "(sudo timedatectl set-ntp true) and the receipt will retry.",
            request_uuid, reasons.SIGN_REJECTED_CLOCK_SKEW,
        )
    except Exception:
        logger.exception(
            "Failed to record clock-skew rejection uuid=%s", request_uuid,
        )


async def _record_sign_rejection(
    settings: Settings, request_uuid: str, resp: httpx.Response,
) -> None:
    """Parse a 4xx rejection response and persist it to the receipt store.

    Accepted body shape: ``{"reason": "<CODE>", "detail": "<text>"}``.
    Unknown codes fall back to ``SIGN_REJECTED_UNKNOWN_REQUEST`` so the
    row still surfaces in the failed-retryable bucket.
    """
    code = reasons.SIGN_REJECTED_UNKNOWN_REQUEST
    detail: str | None = None
    try:
        body = resp.json()
        raw_code = (body.get("reason") or "").strip().upper()
        if raw_code in reasons.SIGN_CODES:
            code = raw_code
        detail = body.get("detail")
    except Exception:
        detail = (resp.text or "")[:200] or None

    try:
        store = get_store(settings.RECEIPT_STORE_PATH)
        await store.mark_sign_failed(request_uuid, code, detail)
        logger.info(
            "Leg 2 submit rejected uuid=%s code=%s detail=%s",
            request_uuid, code, detail,
        )
    except Exception:
        logger.exception(
            "Failed to record sign rejection uuid=%s", request_uuid,
        )


class ReceiptPoller:
    """Background loop that fetches signed receipts and fills in local signatures.

    Runs every ``POLL_INTERVAL_SECONDS``. Each tick is a single short GET.
    Uses ``created_at`` as a cursor (persisted through the next tick via
    the store's last-seen-timestamp).
    """

    def __init__(
        self,
        settings: Settings,
        node_id: str,
        identity_key: str,
        node_wallet_address: str,
        cursor_store: PollerCursor | None = None,
    ) -> None:
        self._settings = settings
        self._node_id = node_id
        self._identity_key = identity_key if identity_key.startswith("0x") else "0x" + identity_key
        self._node_wallet_address = node_wallet_address
        self._cursor: datetime | None = None
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        # Tests inject a custom PollerCursor (e.g. pointed at tmp_path).
        # Production uses the canonical config dir.
        self._cursor_store = cursor_store or PollerCursor(config_dir())

    async def start(self) -> None:
        if self._task is not None:
            return
        # L2 + L8: durable cursor with 24h floor.
        #
        # If we have a saved cursor:
        #   effective = min(saved - 1h guard, now - 24h)
        # The guard absorbs in-flight rows the previous run's last tick
        # may have missed; the 24h floor protects against losing
        # signed-receipts retention if the saved cursor is older than
        # the gateway's 24h retention window.
        # If we don't have a saved cursor (true first-run): fall back to
        # the legacy "now - 24h" lookback.
        now_utc = datetime.now(timezone.utc)
        floor = now_utc - timedelta(hours=POLL_INITIAL_LOOKBACK_HOURS)
        try:
            saved = self._cursor_store.load()
        except Exception:
            logger.exception("poller cursor load failed; using 24h fallback")
            saved = None
        if saved is None:
            self._cursor = floor
            logger.info(
                "Leg 2 receipt poller started (interval=%ds, "
                "cursor=initial-24h-lookback)",
                POLL_INTERVAL_SECONDS,
            )
        else:
            # Plan says cursor = min(saved-1h, now-24h). ``min`` picks
            # the EARLIER timestamp:
            #   - saved older than 23h → saved-1h is earlier → use it
            #     (look back as far as the saved cursor + 1h guard)
            #   - saved within last 23h → now-24h is earlier → use it
            #     (the 24h floor still applies because we never want a
            #     cursor more recent than 24h ago, in case poller fell
            #     behind during the previous run).
            saved_with_guard = saved - timedelta(hours=1)
            self._cursor = min(saved_with_guard, floor)
            logger.info(
                "Leg 2 receipt poller started (interval=%ds, "
                "cursor=%s, source=durable)",
                POLL_INTERVAL_SECONDS, self._cursor.isoformat(),
            )
        self._stop.clear()
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            await self._task
            self._task = None

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self._tick()
            except Exception:
                logger.exception("Leg 2 poller tick failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=POLL_INTERVAL_SECONDS)
                break
            except asyncio.TimeoutError:
                continue

    async def _tick(self) -> None:
        store = get_store(self._settings.RECEIPT_STORE_PATH)
        await store.initialize()

        # L1 escalation pass: any row past the transient-failure budget
        # gets moved to failed_retryable so it surfaces in the UI rather
        # than retrying every tick forever.
        try:
            escalated = await store.escalate_transient_budget_exhausted(
                threshold=TRANSIENT_BUDGET_ATTEMPTS,
                code=reasons.SIGN_TRANSIENT_BUDGET_EXHAUSTED,
                detail=(
                    f"~24h of transient submit failures "
                    f"(>= {TRANSIENT_BUDGET_ATTEMPTS} attempts)."
                ),
            )
            for uuid_ in escalated:
                logger.warning(
                    "Leg 2 receipt uuid=%s escalated to failed_retryable: "
                    "%s — sustained transient errors talking to coord API.",
                    uuid_, reasons.SIGN_TRANSIENT_BUDGET_EXHAUSTED,
                )
        except Exception:
            logger.exception("Transient-budget escalation pass failed")

        # Only poll when we have unsigned receipts waiting AND at least
        # one is past its per-row backoff window. Saves API calls during
        # 429 / 5xx storms — every row in backoff means no work to do.
        unsigned_count = await store.count_unsigned()
        if unsigned_count == 0:
            return
        ready = await store.count_unsigned_ready()
        if ready == 0:
            logger.debug(
                "Leg 2 poller skipped — %d unsigned rows still in transient backoff",
                unsigned_count,
            )
            return

        # Also pull any async rejections the coord API has queued. Runs on
        # the same cadence as the signed-receipt poll so a rejected row
        # flips from pending_sign → failed_retryable within one tick.
        await self._tick_rejections(store)

        timestamp = int(time.time())
        sig = _sign_list_request(self._identity_key, self._node_id, timestamp)
        params = {"ts": timestamp, "sig": sig, "limit": 50}
        if self._cursor is not None:
            params["since"] = self._cursor.isoformat()

        url = self._settings.COORDINATION_API_URL.rstrip("/") + f"/nodes/{self._node_id}/signed-receipts"
        try:
            async with httpx.AsyncClient(timeout=POLL_TIMEOUT_SECONDS) as client:
                resp = await client.get(url, params=params)
        except httpx.RequestError as exc:
            logger.debug("Leg 2 poll network error: %s", exc)
            return

        if resp.status_code != 200:
            logger.debug("Leg 2 poll got %d", resp.status_code)
            return

        try:
            rows = resp.json()
        except Exception:
            return

        if not rows:
            return

        newest_cursor = self._cursor
        latest_signed_at: datetime | None = None
        any_signed = False
        for r in rows:
            try:
                created = datetime.fromisoformat(r["created_at"].replace("Z", "+00:00"))
            except Exception:
                created = None
            try:
                updated = await store.mark_signed(r["request_uuid"], r["signature"])
            except Exception:
                logger.exception("Failed to mark receipt signed uuid=%s",
                                 r.get("request_uuid"))
                updated = False
            if updated:
                any_signed = True
            if created and (newest_cursor is None or created > newest_cursor):
                newest_cursor = created
            if created and (latest_signed_at is None or created > latest_signed_at):
                latest_signed_at = created

        if newest_cursor is not None:
            # Roll back the cursor by POLL_CURSOR_BUFFER_SECONDS so a row
            # inserted with an older timestamp (clock skew, late sync-path
            # commit) gets picked up on the next tick. mark_signed's
            # WHERE signature IS NULL guard makes duplicates a no-op.
            self._cursor = newest_cursor - timedelta(seconds=POLL_CURSOR_BUFFER_SECONDS)

        # L2 + L8: persist the cursor to disk on any tick that actually
        # picked up a signed receipt. Mid-tick crashes can lose at most
        # the rows from the in-flight tick — never the cumulative
        # progress across the daemon's lifetime.
        if any_signed and latest_signed_at is not None:
            try:
                self._cursor_store.save(latest_signed_at)
            except Exception:
                logger.exception("Failed to persist poller cursor")

        # Successful sign clears the clock-skew counter — the operator
        # may have fixed NTP since the last error.
        if any_signed:
            reset_clock_skew_state()

        logger.debug("Leg 2 poller: updated %d signatures from coord API", len(rows))

    async def _tick_rejections(self, store) -> None:
        """Pull async rejections the coord API queued and persist them.

        Uses a separate cursor (``_rejection_cursor``) so signed and
        rejected streams advance independently. Authenticated the same
        way as the signed-receipts poll, with a distinct message prefix
        so the coord API can route the signature to the correct handler.
        """
        timestamp = int(time.time())
        sig = _sign_list_rejections_request(
            self._identity_key, self._node_id, timestamp,
        )
        params = {"ts": timestamp, "sig": sig, "limit": 50}
        cursor = getattr(self, "_rejection_cursor", None) or self._cursor
        if cursor is not None:
            params["since"] = cursor.isoformat()

        url = (
            self._settings.COORDINATION_API_URL.rstrip("/")
            + f"/nodes/{self._node_id}/rejected-receipts"
        )
        try:
            async with httpx.AsyncClient(timeout=POLL_TIMEOUT_SECONDS) as client:
                resp = await client.get(url, params=params)
        except httpx.RequestError as exc:
            logger.debug("Leg 2 rejection-poll network error: %s", exc)
            return

        if resp.status_code == 404:
            # Endpoint not deployed yet — gracefully degrade. Provider keeps
            # working against older coord APIs; rejections just won't
            # surface until the coord API is updated.
            return
        if resp.status_code != 200:
            logger.debug("Leg 2 rejection-poll got %d", resp.status_code)
            return

        try:
            rows = resp.json()
        except Exception:
            return
        if not rows:
            return

        newest = cursor
        for r in rows:
            try:
                when = datetime.fromisoformat(
                    r["rejected_at"].replace("Z", "+00:00"),
                )
            except Exception:
                when = None
            code = (r.get("reason") or "").strip().upper()
            if code not in reasons.SIGN_CODES:
                code = reasons.SIGN_REJECTED_UNKNOWN_REQUEST
            try:
                await store.mark_sign_failed(
                    r["request_uuid"], code, r.get("detail"),
                )
            except Exception:
                logger.exception(
                    "Failed to mark receipt rejected uuid=%s",
                    r.get("request_uuid"),
                )
            if when and (newest is None or when > newest):
                newest = when

        if newest is not None:
            from datetime import timedelta
            self._rejection_cursor = newest - timedelta(
                seconds=POLL_CURSOR_BUFFER_SECONDS,
            )

        logger.debug(
            "Leg 2 poller: recorded %d rejections from coord API", len(rows),
        )


# Module-level singleton used by the proxy_handler's post-relay hook so
# it can call into the submitter without threading the instance through
# every function signature. The poller has a dedicated slot on ctx
# (main.py) — no singleton needed because only shutdown reads it.
_submitter: ReceiptSubmitter | None = None


def set_submitter(submitter: ReceiptSubmitter | None) -> None:
    global _submitter
    _submitter = submitter


def get_submitter() -> ReceiptSubmitter | None:
    return _submitter
