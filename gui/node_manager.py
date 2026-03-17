"""Manages the SpaceRouter node lifecycle in a background thread."""

import asyncio
import logging
import threading

logger = logging.getLogger(__name__)


class NodeManager:
    """Start/stop the Home Node daemon in a background thread with its own event loop."""

    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop_event: asyncio.Event | None = None
        self._running = False
        self._error: str | None = None

    @property
    def is_running(self) -> bool:
        return self._running and self._thread is not None and self._thread.is_alive()

    @property
    def last_error(self) -> str | None:
        return self._error

    def start(self) -> None:
        """Start the node in a background thread."""
        if self.is_running:
            logger.warning("Node is already running")
            return

        self._error = None
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="spacerouter-node")
        self._thread.start()

    def _run_loop(self) -> None:
        """Thread target: create an event loop and run _run()."""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._stop_event = asyncio.Event()
        self._running = True

        try:
            # Import here to avoid circular imports and to pick up env vars
            # set by config_store.apply_to_env() before this thread starts.
            from app.main import _run

            self._loop.run_until_complete(_run(stop_event=self._stop_event))
        except SystemExit:
            # _run() calls sys.exit() on fatal errors; catch it so the GUI stays alive
            logger.warning("Node exited with SystemExit")
            self._error = "Node failed to start. Check your configuration."
        except Exception as exc:
            logger.exception("Node crashed: %s", exc)
            self._error = str(exc)
        finally:
            self._running = False
            self._loop.close()
            self._loop = None
            self._stop_event = None

    def stop(self, timeout: float = 20.0) -> None:
        """Signal the node to stop and wait for the thread to finish."""
        if not self.is_running:
            return

        if self._loop and self._stop_event:
            self._loop.call_soon_threadsafe(self._stop_event.set)

        if self._thread:
            self._thread.join(timeout=timeout)
            if self._thread.is_alive():
                logger.warning("Node thread did not stop within %.1fs", timeout)
            self._thread = None
