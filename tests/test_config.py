"""Tests for configuration, defaults, and validation."""

import os
import warnings

import pytest


class TestWalletAddressBackwardCompat:
    """SR_WALLET_ADDRESS (v0.1.2) must be accepted as an alias for SR_STAKING_ADDRESS."""

    def test_sr_wallet_address_env_var_maps_to_staking_address(self):
        """Existing deployments that set SR_WALLET_ADDRESS must keep working."""
        from app.config import Settings

        os.environ["SR_WALLET_ADDRESS"] = "0xdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
        try:
            s = Settings()
            assert s.STAKING_ADDRESS == "0xdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
        finally:
            del os.environ["SR_WALLET_ADDRESS"]

    def test_sr_staking_address_takes_precedence_over_wallet_address(self):
        """If both are set, SR_STAKING_ADDRESS wins."""
        from app.config import Settings

        os.environ["SR_WALLET_ADDRESS"] = "0x" + "aa" * 20
        os.environ["SR_STAKING_ADDRESS"] = "0x" + "bb" * 20
        try:
            s = Settings()
            assert s.STAKING_ADDRESS == "0x" + "bb" * 20
        finally:
            del os.environ["SR_WALLET_ADDRESS"]
            del os.environ["SR_STAKING_ADDRESS"]


class TestConfigDefaults:
    def test_default_port(self):
        from app.config import Settings
        s = Settings()
        assert s.NODE_PORT == 9090

    def test_default_buffer_size(self):
        from app.config import Settings
        s = Settings()
        assert s.BUFFER_SIZE == 65536

    def test_default_max_connections(self):
        from app.config import Settings
        s = Settings()
        assert s.MAX_CONNECTIONS == 256

    def test_default_bind_address(self):
        from app.config import Settings
        s = Settings()
        assert s.BIND_ADDRESS == "0.0.0.0"

    def test_default_upnp_enabled(self):
        from app.config import Settings
        s = Settings()
        assert s.UPNP_ENABLED is True

    def test_default_tls_paths(self):
        from app.config import Settings
        s = Settings()
        assert s.TLS_CERT_PATH == "certs/node.crt"
        assert s.TLS_KEY_PATH == "certs/node.key"


class TestConfigOverrides:
    def test_env_prefix(self):
        """Settings should read SR_ prefixed environment variables."""
        from app.config import Settings
        os.environ["SR_NODE_PORT"] = "8888"
        os.environ["SR_LOG_LEVEL"] = "DEBUG"
        try:
            s = Settings()
            assert s.NODE_PORT == 8888
            assert s.LOG_LEVEL == "DEBUG"
        finally:
            del os.environ["SR_NODE_PORT"]
            del os.environ["SR_LOG_LEVEL"]


# NOTE — the previous TestConfigHTTPWarning class was removed during the
# v1.5 settings.json migration. The plain-HTTP soft-warning it tested
# has been superseded by a hard-reject Pydantic validator in
# app/settings_v2.py: production / staging variants now refuse to load
# any settings.json or env-seeded config with an http:// URL. Coverage
# for the new behaviour lives in tests/test_settings_v2.py
# (test_http_url_rejected_on_production / _on_staging,
# test_http_url_ok_on_test_variant).
