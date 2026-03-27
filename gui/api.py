"""Python API exposed to the webview frontend via pywebview's js_api."""

import logging
import os

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

    def save_onboarding_and_start(
        self,
        passphrase: str = "",
        staking: str = "",
        collection: str = "",
        identity_key_hex: str = "",
    ) -> dict:
        """Persist onboarding choices and start the node."""
        try:
            self._config.save_onboarding(
                passphrase=passphrase,
                staking=staking,
                collection=collection,
                identity_key_hex=identity_key_hex,
            )
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}

        self._config.apply_to_env()

        try:
            self._node.start()
        except Exception as exc:
            logger.exception("Failed to start node")
            return {"ok": False, "error": f"Failed to start node: {exc}"}

        return {"ok": True}

    def unlock_and_start(self, passphrase: str) -> dict:
        """Set the identity passphrase in env and (re)start the node.

        Called from the passphrase unlock dialog when the node cannot start
        because the keystore requires a passphrase that is not configured.
        """
        os.environ["SR_IDENTITY_PASSPHRASE"] = passphrase

        if self._node.is_running:
            try:
                self._node.stop()
            except Exception as exc:
                logger.warning("Failed to stop node before unlock restart: %s", exc)

        try:
            self._node.start()
        except Exception as exc:
            logger.exception("Failed to start node after unlock")
            return {"ok": False, "error": f"Failed to start node: {exc}"}

        return {"ok": True}

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
        staking = self._config.get("SR_STAKING_ADDRESS")
        return {
            "running": self._node.is_running,
            "staking": staking,
            "error": self._node.last_error,
        }
