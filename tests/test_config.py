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


class TestConfigHTTPWarning:
    def test_http_coordination_url_warns_for_remote(self):
        """Non-localhost HTTP Coordination API URL should emit a warning."""
        os.environ["SR_COORDINATION_API_URL"] = "http://remote-server.com:8000"
        try:
            with warnings.catch_warnings(record=True) as w:
                warnings.simplefilter("always")
                from app.config import load_settings
                load_settings()
                # Check if warning was issued
                http_warnings = [x for x in w if "plain HTTP" in str(x.message)]
                assert len(http_warnings) > 0
        finally:
            os.environ.pop("SR_COORDINATION_API_URL", None)

    def test_https_coordination_url_no_warning(self):
        """HTTPS Coordination API URL should not emit a warning."""
        os.environ["SR_COORDINATION_API_URL"] = "https://api.spacerouter.net"
        try:
            with warnings.catch_warnings(record=True) as w:
                warnings.simplefilter("always")
                from app.config import load_settings
                load_settings()
                http_warnings = [x for x in w if "plain HTTP" in str(x.message)]
                assert len(http_warnings) == 0
        finally:
            os.environ.pop("SR_COORDINATION_API_URL", None)

    def test_localhost_http_no_warning(self):
        """localhost HTTP is acceptable for development — no warning."""
        os.environ["SR_COORDINATION_API_URL"] = "http://localhost:8000"
        try:
            with warnings.catch_warnings(record=True) as w:
                warnings.simplefilter("always")
                from app.config import load_settings
                load_settings()
                http_warnings = [x for x in w if "plain HTTP" in str(x.message)]
                assert len(http_warnings) == 0
        finally:
            os.environ.pop("SR_COORDINATION_API_URL", None)
