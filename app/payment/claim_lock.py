"""Cross-platform exclusive lock for the on-chain claim path.

The provider runs claim_all() from two surfaces — the CLI ``--claim``
subcommand in :mod:`app.main` and the GUI's "Claim All" background
runner in :mod:`gui.api`. Both ultimately call
:func:`app.payment.settlement.claim_all` which builds raw txs and
broadcasts them. Without serialization, two concurrent claims pull the
same nonces from the receipt store and submit two ``claimBatch`` txs to
the chain — one lands, the second reverts (nonce already used). On a
flaky RPC, that revert can also burn the operator's ``claim_attempts``
budget on receipts that are actually settled.

This module provides one canonical lock acquisition path used from both
surfaces (P3/L3 in the v1.5 plan). The lock file is
``~/.spacerouter/claim.lock`` — ``fcntl.flock`` on POSIX,
``msvcrt.locking`` on Windows. Stale-lock recovery is automatic: on
Windows ``LK_NBLCK`` is released by the OS when the holder process
exits; on POSIX ``flock`` likewise releases when the owning fd is
closed (process death drops the fd).

Use as a context manager::

    from app.payment.claim_lock import acquire_claim_lock, ClaimLockHeld

    try:
        with acquire_claim_lock(settings):
            await claim_all(...)
    except ClaimLockHeld:
        ...

The settings object only needs to expose ``RECEIPT_STORE_PATH`` — the
lock file lives next to the receipts DB (same directory as
``~/.spacerouter/``) so an operator can ``rm`` it manually if they
ever need to.
"""

from __future__ import annotations

import contextlib
import logging
import os
import sys
from pathlib import Path
from typing import Iterator

logger = logging.getLogger(__name__)


class ClaimLockHeld(Exception):
    """Raised when ``claim.lock`` is already held by another process.

    Surfaced to the CLI as a non-zero exit; surfaced to the GUI as a
    silent ``noop`` so a double-click doesn't show a scary error.
    """


def claim_lock_path(settings) -> Path:
    """Resolve ``~/.spacerouter/claim.lock`` from a Settings object.

    Both the CLI and GUI build the lock path the same way — next to
    the receipts DB. Centralized here so tests don't have to hardcode
    the layout.
    """
    receipts_db = Path(settings.RECEIPT_STORE_PATH).expanduser()
    return receipts_db.parent / "claim.lock"


@contextlib.contextmanager
def acquire_claim_lock(settings) -> Iterator[Path]:
    """Acquire ``claim.lock`` exclusively or raise :class:`ClaimLockHeld`.

    Yields the resolved lock path so callers can surface it in error
    messages. Releases the lock on exit. Stale-lock recovery is
    inherited from the OS primitive (POSIX ``flock`` releases on fd
    close → process death; Windows ``msvcrt.locking`` with
    ``LK_NBLCK`` releases on holder process exit).

    Implementation note: the underlying file handle stays open for the
    duration of the ``with`` block; we never close+reopen mid-flight,
    which is what would let another process race in.
    """
    lock_path = claim_lock_path(settings)
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    is_windows = sys.platform == "win32"
    # Windows msvcrt.locking() needs read+write; POSIX flock() works on
    # any open fd. Append-mode for Windows so the file exists without
    # truncating any operator-tagged contents (some users diff this
    # file to spot a stuck claim).
    fd = open(lock_path, "a+" if is_windows else "w")
    try:
        try:
            if is_windows:
                import msvcrt
                fd.seek(0)
                msvcrt.locking(fd.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError) as e:
            fd.close()
            logger.info(
                "claim.lock contended at %s — another claim is running",
                lock_path,
            )
            raise ClaimLockHeld(str(lock_path)) from e

        # Best-effort: stamp the holder PID so a stuck-lock investigation
        # has something to look at. Failures here are non-fatal — the
        # lock itself is the source of truth.
        try:
            fd.seek(0)
            fd.truncate(0)
            fd.write(f"{os.getpid()}\n")
            fd.flush()
        except Exception:
            pass

        try:
            yield lock_path
        finally:
            try:
                if is_windows:
                    import msvcrt
                    fd.seek(0)
                    msvcrt.locking(fd.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
            except Exception:
                # Process death drops the fd anyway; logging here would
                # be noise on shutdown.
                pass
    finally:
        try:
            fd.close()
        except Exception:
            pass
