"""Python API exposed to the webview frontend via pywebview's js_api."""

import logging

from gui.config_store import ConfigStore
from gui.node_manager import NodeManager

logger = logging.getLogger(__name__)


class Api:
    """Methods callable from JavaScript via ``window.pywebview.api.<method>()``."""

    def __init__(self, config: ConfigStore, node_manager: NodeManager) -> None:
        self._config = config
        self._node = node_manager

    def needs_onboarding(self) -> bool:
        return self._config.needs_onboarding()

    def save_wallet_and_start(self, address: str) -> dict:
        """Validate wallet, persist it, and start the node."""
        try:
            normalised = self._config.save_wallet(address)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}

        # Apply config to env so the node picks it up
        self._config.apply_to_env()

        try:
            self._node.start()
        except Exception as exc:
            logger.exception("Failed to start node")
            return {"ok": False, "error": f"Failed to start node: {exc}"}

        return {"ok": True, "wallet": normalised}

    def start_node(self) -> dict:
        """Start the node (config must already be set)."""
        if self._node.is_running:
            return {"ok": True, "message": "Already running"}

        self._config.apply_to_env()

        try:
            self._node.start()
        except Exception as exc:
            logger.exception("Failed to start node")
            return {"ok": False, "error": str(exc)}

        return {"ok": True}

    def stop_node(self) -> dict:
        """Gracefully stop the node."""
        try:
            self._node.stop()
        except Exception as exc:
            logger.exception("Failed to stop node")
            return {"ok": False, "error": str(exc)}
        return {"ok": True}

    def get_status(self) -> dict:
        """Return current node status for the dashboard."""
        wallet = self._config.get("SR_WALLET_ADDRESS")
        return {
            "running": self._node.is_running,
            "wallet": wallet,
            "error": self._node.last_error,
        }
