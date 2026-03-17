"""SpaceRouter Desktop App — pywebview entry point."""

import atexit
import logging
import os
import sys

import webview

from gui.api import Api
from gui.config_store import ConfigStore
from gui.node_manager import NodeManager

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

    # Apply saved config to environment before anything imports app.config
    config.apply_to_env()

    def on_closing() -> None:
        logger.info("Window closing — stopping node…")
        node_manager.stop(timeout=15.0)

    atexit.register(lambda: node_manager.stop(timeout=5.0))

    window = webview.create_window(
        title="SpaceRouter",
        url=_asset_path("index.html"),
        js_api=api,
        width=480,
        height=640,
        min_size=(400, 500),
        resizable=True,
    )
    window.events.closing += on_closing

    webview.start(debug=os.environ.get("SR_GUI_DEBUG", "").lower() in ("1", "true"))


if __name__ == "__main__":
    main()
