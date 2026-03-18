"""SpaceRouter Desktop App — pywebview entry point."""

import atexit
import logging
import os
import sys
import threading

import webview

from gui.api import Api
from gui.config_store import ConfigStore
from gui.node_manager import NodeManager
from gui.tray import SpaceRouterTray

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def _asset_path(filename: str) -> str:
    """Resolve asset path, handling PyInstaller frozen bundles."""
    if getattr(sys, "frozen", False):
        base = os.path.join(sys._MEIPASS, "gui", "assets")  # type: ignore[attr-defined]
    else:
        base = os.path.join(os.path.dirname(__file__), "assets")
    return os.path.join(base, filename)


def main() -> None:
    config = ConfigStore()
    node_manager = NodeManager()
    api = Api(config, node_manager)
    tray = SpaceRouterTray()

    # Apply saved config to environment before anything imports app.config
    config.apply_to_env()

    window = webview.create_window(
        title="SpaceRouter",
        url=_asset_path("index.html"),
        js_api=api,
        width=480,
        height=640,
        min_size=(400, 500),
        resizable=True,
    )

    # Hide window on close instead of quitting — node keeps running in background.
    def on_closing() -> bool:
        logger.info("Window closing — hiding to tray")
        window.hide()
        return False  # Cancel the close so the window stays alive

    window.events.closing += on_closing

    # Tray callbacks
    def on_show() -> None:
        window.show()

    def on_quit() -> None:
        logger.info("Quit requested — stopping node…")
        # Stop node in a background thread to avoid blocking the main/AppKit thread
        def _shutdown() -> None:
            node_manager.stop(timeout=15.0)
            tray.shutdown()
            window.destroy()

        threading.Thread(target=_shutdown, daemon=True).start()

    atexit.register(lambda: node_manager.stop(timeout=5.0))

    # Start tray icon once webview's AppKit run loop is active.
    def on_shown() -> None:
        tray.start(on_show=on_show, on_quit=on_quit, node_manager=node_manager)

    window.events.shown += on_shown

    webview.start(debug=os.environ.get("SR_GUI_DEBUG", "").lower() in ("1", "true"))


if __name__ == "__main__":
    main()
