"""Persistent configuration storage for the SpaceRouter GUI.

Reads/writes a spacerouter.env file in a platform-appropriate location.
"""

import os
import sys
from pathlib import Path

from dotenv import dotenv_values, set_key

from app.identity import write_identity_key
from app.wallet import validate_wallet_address

# Default Coordination API for production
_DEFAULT_COORDINATION_API_URL = "https://spacerouter-coordination-api.fly.dev"

_DEFAULTS = {
    "SR_COORDINATION_API_URL": _DEFAULT_COORDINATION_API_URL,
    "SR_STAKING_ADDRESS": "",
    "SR_COLLECTION_ADDRESS": "",
    "SR_NODE_PORT": "9090",
    "SR_UPNP_ENABLED": "true",
    "SR_LOG_LEVEL": "INFO",
    "SR_REGISTRATION_MODE": "v1",
    "SR_IDENTITY_PASSPHRASE": "",
}


def _config_dir() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "SpaceRouter"
    elif sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA", "")
        if local:
            return Path(local) / "SpaceRouter"
        return Path.home() / "AppData" / "Local" / "SpaceRouter"
    else:
        # Linux / fallback
        return Path.home() / ".config" / "spacerouter"


class ConfigStore:
    """Manage spacerouter.env configuration file."""

    def __init__(self) -> None:
        self._dir = _config_dir()
        self._path = self._dir / "spacerouter.env"
        self._ensure_file()

    def _ensure_file(self) -> None:
        """Create config dir and file with defaults if they don't exist."""
        self._dir.mkdir(parents=True, exist_ok=True)
        if not self._path.exists():
            lines = [f"{k}={v}" for k, v in _DEFAULTS.items()]
            self._path.write_text("\n".join(lines) + "\n")
        else:
            self._migrate_wallet_address()

    def _migrate_wallet_address(self) -> None:
        """Migrate SR_WALLET_ADDRESS → SR_STAKING_ADDRESS for existing configs."""
        vals = dotenv_values(self._path)
        if vals.get("SR_WALLET_ADDRESS") and not vals.get("SR_STAKING_ADDRESS"):
            set_key(str(self._path), "SR_STAKING_ADDRESS", vals["SR_WALLET_ADDRESS"])

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> dict[str, str | None]:
        """Return all config values from the env file."""
        return dotenv_values(self._path)

    def get(self, key: str, default: str = "") -> str:
        vals = self.load()
        return vals.get(key) or default

    def needs_onboarding(self) -> bool:
        """True if the identity key file has not been created yet."""
        key_path = self.get("SR_IDENTITY_KEY_PATH") or str(
            self._dir / "certs" / "node-identity.key"
        )
        return not os.path.isfile(key_path)

    def save_onboarding(
        self,
        passphrase: str = "",
        staking: str = "",
        collection: str = "",
        identity_key_hex: str = "",
    ) -> None:
        """Persist onboarding choices and optionally pre-write an imported identity key.

        - *passphrase*: written as SR_IDENTITY_PASSPHRASE (may be empty).
        - *staking*: staking wallet address; empty → uses identity address at runtime.
        - *collection*: collection wallet address; empty → uses staking address.
        - *identity_key_hex*: if provided, the raw private key is written to the
          identity key file immediately (encrypted if *passphrase* is set).
        """
        if staking:
            staking = validate_wallet_address(staking)
        if collection:
            collection = validate_wallet_address(collection)

        set_key(str(self._path), "SR_IDENTITY_PASSPHRASE", passphrase)
        if staking:
            set_key(str(self._path), "SR_STAKING_ADDRESS", staking)
        if collection:
            set_key(str(self._path), "SR_COLLECTION_ADDRESS", collection)

        if identity_key_hex:
            key_path = self.get("SR_IDENTITY_KEY_PATH") or str(
                self._dir / "certs" / "node-identity.key"
            )
            write_identity_key(key_path, identity_key_hex, passphrase)

    def apply_to_env(self) -> None:
        """Load all config values into os.environ so pydantic-settings picks them up."""
        for key, value in self.load().items():
            if value and key not in os.environ:
                os.environ[key] = value

        # Point TLS cert + identity key paths to the writable config directory.
        # The default relative paths ("certs/...") resolve inside the PyInstaller
        # temp dir which is read-only.
        certs_dir = self._dir / "certs"
        for key, filename in (
            ("SR_TLS_CERT_PATH", "node.crt"),
            ("SR_TLS_KEY_PATH", "node.key"),
            ("SR_GATEWAY_CA_CERT_PATH", "gateway-ca.crt"),
            ("SR_IDENTITY_KEY_PATH", "node-identity.key"),
        ):
            if key not in os.environ:
                os.environ[key] = str(certs_dir / filename)
