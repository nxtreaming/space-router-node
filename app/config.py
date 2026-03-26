import logging
import warnings

from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SR_", env_file=".env")

    NODE_PORT: int = 9090
    COORDINATION_API_URL: str = "http://localhost:8000"

    # Max concurrent proxy connections (DoS protection)
    MAX_CONNECTIONS: int = 256

    # Bind address — restrict to specific interface if needed
    BIND_ADDRESS: str = "0.0.0.0"

    NODE_LABEL: str = ""

    PUBLIC_IP: str = ""  # Auto-detected if empty
    PUBLIC_PORT: int = 0  # Override advertised port (0 = use NODE_PORT)
    WALLET_ADDRESS: str = ""  # Required — user-provided EVM address

    # v0.2.0 multi-wallet: separate staking and collection addresses.
    # When set, the node uses the v0.2.0 registration protocol.
    # When empty, falls back to WALLET_ADDRESS (v0.1.2 compat).
    STAKING_ADDRESS: str = ""
    COLLECTION_ADDRESS: str = ""  # Defaults to STAKING_ADDRESS if empty

    # UPnP / NAT-PMP automatic port forwarding
    UPNP_ENABLED: bool = True
    UPNP_LEASE_DURATION: int = 3600  # seconds; 0 = permanent

    BUFFER_SIZE: int = 65536
    REQUEST_TIMEOUT: float = 30.0
    RELAY_TIMEOUT: float = 300.0

    LOG_LEVEL: str = "INFO"

    # Registration retry limits
    REGISTER_MAX_RETRIES: int = 5

    # Node identity keypair (auto-generated secp256k1 for signing API requests)
    IDENTITY_KEY_PATH: str = "certs/node-identity.key"

    # TLS — auto-generates a self-signed cert if files don't exist
    TLS_CERT_PATH: str = "certs/node.crt"
    TLS_KEY_PATH: str = "certs/node.key"

    # mTLS — Gateway authentication (requires gateway_ca_cert from registration)
    MTLS_ENABLED: bool = True
    GATEWAY_CA_CERT_PATH: str = "certs/gateway-ca.crt"


def load_settings() -> Settings:
    """Create a fresh Settings instance from current environment variables.

    Call this instead of importing the module-level ``settings`` when you need
    to pick up env-var changes (e.g. after a config reload or fresh restart).
    """
    s = Settings()
    if not s.COORDINATION_API_URL.startswith("https://"):
        if "localhost" not in s.COORDINATION_API_URL and "127.0.0.1" not in s.COORDINATION_API_URL:
            warnings.warn(
                f"COORDINATION_API_URL uses plain HTTP ({s.COORDINATION_API_URL}). "
                "This exposes registration data to MITM attacks. Use HTTPS in production.",
                stacklevel=2,
            )
    return s


# Lazy module-level singleton for backward compatibility.
# Code that imports ``from app.config import settings`` still works, but new
# code should call ``load_settings()`` for a guaranteed-fresh instance.
settings = load_settings()
