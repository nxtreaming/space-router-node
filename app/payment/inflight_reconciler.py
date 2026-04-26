"""On-startup reconciliation of claim txs that crashed mid-flight.

Track P3 / loophole L5: ``app.payment.settlement`` persists a
deterministic tx hash on every UUID in a batch BEFORE
``send_raw_transaction`` so a crash between broadcast and
``mark_claimed`` no longer creates a re-claim revert loop. On the next
daemon startup, this module walks every row where
``claim_tx_pending IS NOT NULL AND claimed_at IS NULL`` and asks the
escrow contract whether each nonce was actually used:

- ``isNonceUsed=true``  → the tx landed; mark the row claimed using
  the breadcrumbed hash.
- ``isNonceUsed=false`` → the tx never broadcast (or was dropped from
  the mempool before any node mined it); clear the breadcrumb and
  reset ``claim_attempts`` so the row re-enters the queue without a
  burnt retry.

This is a ONE-SHOT pass at daemon startup, not a recurring loop. The
reaper handles the recurring case (timeouts and reorgs) and the
crash window we're closing here is small — startup runs once per
daemon lifetime.

The reconciler shares the chain-RPC pattern with the reaper to keep
test-shape parity: synchronous web3 calls inside ``asyncio.to_thread``,
ABI loaded via :func:`app.payment.reaper._load_abi_once`.
"""

from __future__ import annotations

import asyncio
import logging

from app.config import Settings
from app.payment.receipt_store import get_store

logger = logging.getLogger(__name__)


async def reconcile_inflight(settings: Settings) -> dict:
    """Run one reconciliation pass and return a summary dict.

    Returns ``{"checked": N, "marked_claimed": M, "cleared": K}`` so
    callers (daemon startup, tests) have something concrete to log.
    """
    if not settings.ESCROW_CHAIN_RPC or not settings.ESCROW_CONTRACT_ADDRESS:
        # No chain configured — nothing to reconcile against. Common in
        # local dev / tests; not an error.
        return {"checked": 0, "marked_claimed": 0, "cleared": 0}

    store = get_store(settings.RECEIPT_STORE_PATH)
    await store.initialize()

    rows = await store.list_inflight(limit=500)
    if not rows:
        return {"checked": 0, "marked_claimed": 0, "cleared": 0}

    def _check(rs) -> tuple[list[tuple[str, str]], list[str]]:
        from web3 import Web3
        from eth_utils import to_checksum_address

        # Local import to avoid an import cycle — reaper imports
        # settlement which imports this module.
        from app.payment.reaper import _load_abi_once

        w3 = Web3(Web3.HTTPProvider(
            settings.ESCROW_CHAIN_RPC,
            request_kwargs={"timeout": 10},
        ))
        if not w3.is_connected():
            return [], []
        contract = w3.eth.contract(
            address=Web3.to_checksum_address(settings.ESCROW_CONTRACT_ADDRESS),
            abi=_load_abi_once(),
        )
        landed: list[tuple[str, str]] = []
        unused: list[str] = []
        for sr in rs:
            try:
                used = contract.functions.isNonceUsed(
                    to_checksum_address(sr.receipt.client_address),
                    sr.receipt.request_uuid,
                ).call()
            except Exception as e:
                logger.debug(
                    "Reconciler: isNonceUsed failed uuid=%s: %s",
                    sr.receipt.request_uuid, e,
                )
                continue
            if used:
                landed.append(
                    (sr.receipt.request_uuid, sr.claim_tx_pending or "external"),
                )
            else:
                unused.append(sr.receipt.request_uuid)
        return landed, unused

    landed, unused = await asyncio.to_thread(_check, rows)

    marked_claimed = 0
    # Group landed rows by tx hash (rows in the same batch share one).
    by_hash: dict[str, list[str]] = {}
    for uuid_str, tx_hash in landed:
        by_hash.setdefault(tx_hash, []).append(uuid_str)
    for tx_hash, uuids in by_hash.items():
        n = await store.mark_claimed(uuids, tx_hash)
        marked_claimed += n
        # Clear the breadcrumb once the row is settled.
        for u in uuids:
            await store.clear_claim_tx_pending(u)

    cleared = 0
    for uuid_str in unused:
        # Tx never landed — clear the in-flight marker and reset the
        # claim-attempts counter so the row gets a fresh retry budget
        # on the next manual claim.
        await store.clear_claim_tx_pending(uuid_str)
        await store.reset_claim_attempts(uuid_str)
        cleared += 1

    if marked_claimed or cleared:
        logger.info(
            "In-flight reconciler: checked=%d marked_claimed=%d cleared=%d",
            len(rows), marked_claimed, cleared,
        )

    return {
        "checked": len(rows),
        "marked_claimed": marked_claimed,
        "cleared": cleared,
    }
