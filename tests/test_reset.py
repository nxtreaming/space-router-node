"""Tests for the reset functionality across GUI and CLI paths."""

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from dotenv import dotenv_values


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def store(tmp_path):
    """Return a ConfigStore whose config directory is isolated to tmp_path."""
    with patch("gui.config_store._config_dir", return_value=tmp_path):
        from gui.config_store import ConfigStore
        yield ConfigStore()


@pytest.fixture()
def store_with_state(store, tmp_path):
    """ConfigStore with identity key, certs, and custom addresses written.

    Uses the v1.5 settings.json store (not the legacy spacerouter.env).
    """
    # Create identity key + cert files
    certs_dir = tmp_path / "certs"
    certs_dir.mkdir(parents=True, exist_ok=True)
    (certs_dir / "node-identity.key").write_text("fake-identity-key\n")
    (certs_dir / "node.crt").write_text("fake-cert\n")
    (certs_dir / "node.key").write_text("fake-key\n")
    (certs_dir / "gateway-ca.crt").write_text("fake-ca\n")

    # Persist custom addresses via the canonical save_wallets() path —
    # writes settings.json, not spacerouter.env.
    store.save_wallets("0x" + "aa" * 20, "0x" + "bb" * 20)
    return store


# ---------------------------------------------------------------------------
# ConfigStore.reset() — full clear
# ---------------------------------------------------------------------------

class TestConfigStoreReset:
    def test_reset_deletes_identity_key(self, store_with_state, tmp_path):
        """reset() must delete the identity key file."""
        identity_path = tmp_path / "certs" / "node-identity.key"
        assert identity_path.exists()

        store_with_state.reset()

        assert not identity_path.exists()

    def test_reset_deletes_all_certificates(self, store_with_state, tmp_path):
        """reset() must remove the entire certs directory."""
        certs_dir = tmp_path / "certs"
        assert certs_dir.is_dir()

        store_with_state.reset()

        assert not certs_dir.exists()

    def test_reset_clears_addresses(self, store_with_state):
        """reset() must clear staking and collection addresses."""
        # Pre-condition: addresses persisted via store_with_state fixture.
        assert store_with_state.get("SR_STAKING_ADDRESS") == "0x" + "aa" * 20

        store_with_state.reset()

        assert store_with_state.get("SR_STAKING_ADDRESS") == ""
        assert store_with_state.get("SR_COLLECTION_ADDRESS") == ""

    def test_reset_restores_default_config(self, store_with_state):
        """reset() must rewrite settings.json with a fresh defaults instance."""
        import json

        store_with_state.reset()

        settings_path = store_with_state._settings_json_path
        assert settings_path.exists()
        data = json.loads(settings_path.read_text())
        # Schema-fresh defaults: wallet wiped, schema version present,
        # build_variant matches the active value.
        assert data["wallet"]["staking_address"] in (None, "")
        assert data["wallet"]["collection_address"] in (None, "")
        assert data.get("schema_version") == 1

    def test_reset_makes_needs_onboarding_true(self, store_with_state):
        """After reset(), needs_onboarding() must return True (identity key gone)."""
        assert store_with_state.needs_onboarding() is False  # pre-condition

        store_with_state.reset()

        assert store_with_state.needs_onboarding() is True

    def test_reset_no_args(self, store):
        """reset() takes no arguments (keep_addresses/keep_identity removed)."""
        import inspect
        sig = inspect.signature(store.reset)
        # Only 'self' parameter (implicit, not in sig.parameters for bound methods)
        assert len(sig.parameters) == 0

    def test_reset_idempotent_no_certs_dir(self, store, tmp_path):
        """reset() must not fail if certs directory does not exist."""
        certs_dir = tmp_path / "certs"
        assert not certs_dir.exists()

        store.reset()  # should not raise

        assert store.get("SR_STAKING_ADDRESS") == ""


# ---------------------------------------------------------------------------
# GUI Api.fresh_restart() — full clear through API layer
# ---------------------------------------------------------------------------

class TestApiFreshRestart:
    def test_fresh_restart_no_args(self):
        """fresh_restart() takes no arguments (keep_addresses removed)."""
        from gui.api import Api
        import inspect
        sig = inspect.signature(Api.fresh_restart)
        # Only 'self' parameter
        params = [p for p in sig.parameters if p != "self"]
        assert len(params) == 0

    def test_fresh_restart_calls_full_reset(self, store_with_state, tmp_path):
        """fresh_restart() must call config.reset() with no arguments."""
        from gui.api import Api
        from gui.node_manager import NodeManager

        node = MagicMock(spec=NodeManager)
        api = Api(config=store_with_state, node_manager=node)

        result = api.fresh_restart()

        assert result["ok"] is True
        # Verify identity key was deleted
        assert not (tmp_path / "certs" / "node-identity.key").exists()
        # Verify certs dir was removed
        assert not (tmp_path / "certs").exists()

    def test_fresh_restart_clears_env_vars(self, store_with_state):
        """fresh_restart() must remove all SR_ env vars."""
        from gui.api import Api
        from gui.node_manager import NodeManager

        os.environ["SR_STAKING_ADDRESS"] = "0x" + "aa" * 20
        os.environ["SR_TEST_VAR"] = "test"

        node = MagicMock(spec=NodeManager)
        api = Api(config=store_with_state, node_manager=node)

        try:
            result = api.fresh_restart()
            assert result["ok"] is True
            assert "SR_STAKING_ADDRESS" not in os.environ
            assert "SR_TEST_VAR" not in os.environ
        finally:
            # Cleanup in case test fails
            os.environ.pop("SR_STAKING_ADDRESS", None)
            os.environ.pop("SR_TEST_VAR", None)

    def test_fresh_restart_stops_node(self, store_with_state):
        """fresh_restart() must stop the node before resetting."""
        from gui.api import Api
        from gui.node_manager import NodeManager

        node = MagicMock(spec=NodeManager)
        api = Api(config=store_with_state, node_manager=node)

        api.fresh_restart()

        node.stop.assert_called_once_with(timeout=5.0)


# ---------------------------------------------------------------------------
# CLI _do_reset() — no --keep-identity option
# ---------------------------------------------------------------------------

class TestCliReset:
    def test_do_reset_removes_env_file(self, tmp_path):
        """_do_reset() must remove the .env config file."""
        env_file = tmp_path / "spacerouter.env"
        env_file.write_text("SR_STAKING_ADDRESS=0x" + "aa" * 20 + "\n")
        certs_dir = tmp_path / "certs"
        certs_dir.mkdir()
        (certs_dir / "node-identity.key").write_text("fake\n")

        with patch("app.main.load_settings") as mock_settings, \
             patch("app.paths.config_dir", return_value=tmp_path), \
             patch("app.main.sys") as mock_sys:
            mock_settings.return_value.IDENTITY_KEY_PATH = str(certs_dir / "node-identity.key")
            mock_sys.argv = ["prog", "--reset"]
            mock_sys.stdin.isatty.return_value = False
            mock_sys.exit = MagicMock(side_effect=SystemExit)

            from app.main import _do_reset
            _do_reset()

        assert not env_file.exists()
        assert not certs_dir.exists()

    def test_do_reset_no_keep_identity_arg(self):
        """_do_reset() source must not reference --keep-identity."""
        import inspect
        from app.main import _do_reset
        source = inspect.getsource(_do_reset)
        assert "--keep-identity" not in source
        assert "keep_identity" not in source
