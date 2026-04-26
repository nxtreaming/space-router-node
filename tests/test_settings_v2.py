"""Tests for the canonical settings.json model (Track P0)."""

from __future__ import annotations

import json

import pytest

from app.settings_v2 import (
    ClaimSection,
    CoordinationSection,
    EscrowSection,
    NodeSection,
    ReceiptsSection,
    Settings,
    WalletSection,
)


# ── Schema ───────────────────────────────────────────────────────────


class TestSchema:
    def test_defaults_are_complete(self):
        s = Settings()
        assert s.schema_version == 1
        assert s.node.port == 9090
        assert s.node.upnp_enabled is True
        assert s.node.mtls_enabled is True
        assert s.node.log_level == "INFO"
        assert s.node.registration_mode == "auto"
        assert s.wallet.staking_address is None
        assert s.wallet.identity_passphrase_set is False
        assert s.coordination.url.startswith("https://")
        assert s.escrow.contract_address is None
        assert s.claim.auto_claim_enabled is False
        assert s.claim.auto_claim_threshold_space_wei == "10000000000000000000"
        assert s.claim.auto_claim_threshold_count == 10
        assert s.claim.batch_size == 50
        assert s.receipts.max_sign_attempts == 2
        assert s.receipts.max_claim_attempts == 2
        assert s.receipts.reaper_grace_seconds == 300
        assert s.receipts.reaper_interval_seconds == 300

    def test_default_build_variant_is_production_or_seeded(self):
        # Without app/_build_variant.py present in dev tree, defaults to "production".
        s = Settings()
        assert s.build_variant in ("production", "test")

    def test_unknown_top_level_key_is_dropped(self):
        # extra="ignore" is part of the schema config — additional fields
        # in settings.json should not blow up the loader.
        s = Settings.model_validate({"unknown": 42, "node": {"port": 1234}})
        assert s.node.port == 1234

    def test_explicit_section_overrides(self):
        s = Settings(
            node=NodeSection(port=42),
            claim=ClaimSection(batch_size=7),
        )
        assert s.node.port == 42
        assert s.claim.batch_size == 7


# ── Atomic save / load round-trip ────────────────────────────────────


class TestSaveLoad:
    def test_roundtrip_defaults(self, tmp_path):
        path = tmp_path / "settings.json"
        Settings().save(path)
        loaded = Settings.load(path)
        assert loaded == Settings()

    def test_load_missing_returns_defaults(self, tmp_path):
        path = tmp_path / "does-not-exist.json"
        s = Settings.load(path)
        assert s == Settings()

    def test_load_invalid_json_raises_with_helpful_message(self, tmp_path):
        path = tmp_path / "settings.json"
        path.write_text("{not json")
        with pytest.raises(ValueError, match="not valid JSON"):
            Settings.load(path)

    def test_load_invalid_schema_names_bad_field(self, tmp_path):
        path = tmp_path / "settings.json"
        # node.port must be int; passing a dict triggers a Pydantic error.
        path.write_text(json.dumps({"node": {"port": {"nested": "garbage"}}}))
        with pytest.raises(ValueError, match="failed validation"):
            Settings.load(path)

    def test_atomic_save_replaces_tmp_file(self, tmp_path):
        path = tmp_path / "settings.json"
        Settings().save(path)
        # No leftover .tmp file should remain.
        assert not (tmp_path / "settings.json.tmp").exists()
        assert path.exists()

    def test_save_creates_parent_directory(self, tmp_path):
        nested = tmp_path / "a" / "b" / "settings.json"
        Settings().save(nested)
        assert nested.exists()

    def test_save_sets_0600_on_posix(self, tmp_path):
        import sys
        if sys.platform == "win32":
            pytest.skip("POSIX-only permission check")
        path = tmp_path / "settings.json"
        Settings().save(path)
        mode = path.stat().st_mode & 0o777
        assert mode == 0o600

    def test_save_then_save_again_overwrites(self, tmp_path):
        path = tmp_path / "settings.json"
        a = Settings(node=NodeSection(port=1111))
        a.save(path)
        b = Settings(node=NodeSection(port=2222))
        b.save(path)
        loaded = Settings.load(path)
        assert loaded.node.port == 2222


# ── Wei-as-string preservation ───────────────────────────────────────


class TestWeiStringPreservation:
    def test_huge_wei_round_trips_exactly(self, tmp_path):
        # 50 SPACE in wei = 50e18 = beyond JS Number.MAX_SAFE_INTEGER.
        big = "50000000000000000000"
        s = Settings(claim=ClaimSection(auto_claim_threshold_space_wei=big))
        path = tmp_path / "settings.json"
        s.save(path)
        # Inspect raw JSON: must be a string, not a number.
        raw = json.loads(path.read_text())
        assert raw["claim"]["auto_claim_threshold_space_wei"] == big
        assert isinstance(raw["claim"]["auto_claim_threshold_space_wei"], str)
        assert Settings.load(path).claim.auto_claim_threshold_space_wei == big

    def test_leg2_rate_per_gb_is_string(self, tmp_path):
        rate = "12345678901234567890"
        s = Settings(escrow=EscrowSection(leg2_rate_per_gb=rate))
        path = tmp_path / "settings.json"
        s.save(path)
        assert json.loads(path.read_text())["escrow"]["leg2_rate_per_gb"] == rate


# ── Migration from spacerouter.env ───────────────────────────────────


_FULL_ENV = """\
SR_BUILD_VARIANT=test
SR_NODE_PORT=9091
SR_NODE_LABEL=qa-jenna
SR_PUBLIC_IP=203.0.113.5
SR_PUBLIC_PORT=21781
SR_UPNP_ENABLED=false
SR_MTLS_ENABLED=true
SR_LOG_LEVEL=DEBUG
SR_REGISTRATION_MODE=v2
SR_STAKING_ADDRESS=0xC0A06CDdAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
SR_COLLECTION_ADDRESS=0xCd11ECbBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB
SR_IDENTITY_KEY_PATH=/custom/path/identity.key
SR_IDENTITY_PASSPHRASE=correct horse battery staple
SR_COORDINATION_API_URL=https://example.com/coord
SR_ESCROW_CONTRACT_ADDRESS=0xCCCC740e4e9175301a24FB6d22bA184b8ec07528
SR_ESCROW_CHAIN_RPC=https://rpc.cc3-testnet.creditcoin.network
SR_ESCROW_CHAIN_ID=102031
SR_GATEWAY_PAYER_ADDRESS=0xGGGG740e4e9175301a24FB6d22bA184b8ec07528
SR_NODE_RATE_PER_GB=42000000000000000000
SR_RECEIPT_REAPER_GRACE_SECONDS=400
SR_RECEIPT_REAPER_INTERVAL_SECONDS=600
SR_RECEIPT_MAX_SIGN_ATTEMPTS=3
SR_RECEIPT_MAX_CLAIM_ATTEMPTS=4
SR_CLAIM_BATCH_SIZE=25
"""


class TestMigration:
    def test_full_env_migration_maps_every_field(self, tmp_path):
        env_path = tmp_path / "spacerouter.env"
        env_path.write_text(_FULL_ENV)
        settings_path = tmp_path / "settings.json"

        s = Settings.migrate_from_env_file(env_path, settings_path)

        assert s.build_variant == "test"
        assert s.node.port == 9091
        assert s.node.label == "qa-jenna"
        assert s.node.public_ip == "203.0.113.5"
        assert s.node.public_port == 21781
        assert s.node.upnp_enabled is False
        assert s.node.mtls_enabled is True
        assert s.node.log_level == "DEBUG"
        assert s.node.registration_mode == "v2"

        assert s.wallet.staking_address.lower().startswith("0xc0a06cdd")
        assert s.wallet.collection_address.lower().startswith("0xcd11ecbb")
        assert s.wallet.settlement_key_path == "/custom/path/identity.key"
        # Passphrase NEVER stored as raw value, just the boolean.
        assert s.wallet.identity_passphrase_set is True
        # Defensive: passphrase does not leak into JSON.
        raw = json.loads(settings_path.read_text())
        assert "correct horse" not in json.dumps(raw)

        assert s.coordination.url == "https://example.com/coord"
        assert s.escrow.contract_address.lower().startswith("0xcccc")
        assert s.escrow.chain_rpc.startswith("https://rpc.cc3-testnet")
        assert s.escrow.chain_id == 102031
        assert s.escrow.gateway_payer_address.lower().startswith("0xgggg")
        assert s.escrow.leg2_rate_per_gb == "42000000000000000000"

        assert s.claim.batch_size == 25
        assert s.receipts.reaper_grace_seconds == 400
        assert s.receipts.reaper_interval_seconds == 600
        assert s.receipts.max_sign_attempts == 3
        assert s.receipts.max_claim_attempts == 4

        # Env file renamed to .migrated.bak.
        assert not env_path.exists()
        assert (tmp_path / "spacerouter.env.migrated.bak").exists()

    def test_migration_is_idempotent(self, tmp_path):
        env_path = tmp_path / "spacerouter.env"
        env_path.write_text("SR_NODE_PORT=4242\n")
        settings_path = tmp_path / "settings.json"

        s1 = Settings.migrate_from_env_file(env_path, settings_path)
        # First call moves the env file to .bak; second call should no-op.
        s2 = Settings.migrate_from_env_file(env_path, settings_path)

        assert s1 == s2
        assert s1.node.port == 4242
        # Only one .bak file; no double-rename.
        assert (tmp_path / "spacerouter.env.migrated.bak").exists()
        # No .bak.bak weirdness.
        assert not (tmp_path / "spacerouter.env.migrated.bak.migrated.bak").exists()

    def test_migration_no_env_file_returns_defaults(self, tmp_path):
        settings_path = tmp_path / "settings.json"
        env_path = tmp_path / "spacerouter.env"
        s = Settings.migrate_from_env_file(env_path, settings_path)
        assert s == Settings()
        assert not settings_path.exists()  # nothing written when no source

    def test_migration_settings_already_exists_loads_it(self, tmp_path):
        settings_path = tmp_path / "settings.json"
        env_path = tmp_path / "spacerouter.env"
        env_path.write_text("SR_NODE_PORT=1111\n")
        # Pre-existing settings.json with a different port — must win.
        Settings(node=NodeSection(port=2222)).save(settings_path)

        s = Settings.migrate_from_env_file(env_path, settings_path)
        assert s.node.port == 2222
        # Source env file untouched because we never migrated.
        assert env_path.exists()

    def test_migration_keeps_env_file_when_rename_disabled(self, tmp_path):
        # GUI path: env file must survive (GUI still writes it).
        env_path = tmp_path / "spacerouter.env"
        env_path.write_text("SR_NODE_PORT=7777\n")
        settings_path = tmp_path / "settings.json"

        s = Settings.migrate_from_env_file(env_path, settings_path, rename_after=False)
        assert s.node.port == 7777
        assert env_path.exists()  # not renamed
        assert not (tmp_path / "spacerouter.env.migrated.bak").exists()
        assert settings_path.exists()

    def test_migration_passphrase_not_persisted_to_json(self, tmp_path):
        env_path = tmp_path / "spacerouter.env"
        env_path.write_text(
            'SR_IDENTITY_PASSPHRASE="this is a top secret value"\n'
        )
        settings_path = tmp_path / "settings.json"
        s = Settings.migrate_from_env_file(env_path, settings_path)

        assert s.wallet.identity_passphrase_set is True
        body = settings_path.read_text()
        assert "top secret" not in body
        assert "this is a top secret value" not in body

    def test_migration_empty_passphrase_does_not_set_flag(self, tmp_path):
        env_path = tmp_path / "spacerouter.env"
        env_path.write_text("SR_IDENTITY_PASSPHRASE=\n")
        settings_path = tmp_path / "settings.json"
        s = Settings.migrate_from_env_file(env_path, settings_path)
        assert s.wallet.identity_passphrase_set is False

    def test_migration_logs_unknown_keys(self, tmp_path, caplog):
        env_path = tmp_path / "spacerouter.env"
        env_path.write_text(
            "SR_NODE_PORT=9090\nSR_TOTALLY_UNKNOWN_FIELD=value\nSR_LEGACY_FOO=bar\n"
        )
        settings_path = tmp_path / "settings.json"

        import logging
        with caplog.at_level(logging.INFO, logger="app.settings_v2"):
            Settings.migrate_from_env_file(env_path, settings_path)

        msg = "\n".join(r.getMessage() for r in caplog.records)
        assert "SR_TOTALLY_UNKNOWN_FIELD" in msg
        assert "SR_LEGACY_FOO" in msg

    def test_migration_bool_parsing(self, tmp_path):
        env_path = tmp_path / "spacerouter.env"
        env_path.write_text("SR_UPNP_ENABLED=FALSE\nSR_MTLS_ENABLED=1\n")
        settings_path = tmp_path / "settings.json"
        s = Settings.migrate_from_env_file(env_path, settings_path)
        assert s.node.upnp_enabled is False
        assert s.node.mtls_enabled is True


# ── Loader chain ─────────────────────────────────────────────────────


class TestLoader:
    def test_loader_uses_existing_settings_json(self, tmp_path):
        from app.settings_loader import load_provider_settings

        Settings(node=NodeSection(port=4242)).save(tmp_path / "settings.json")
        s = load_provider_settings(directory=tmp_path)
        assert s.node.port == 4242

    def test_loader_migrates_env_file_when_no_json(self, tmp_path):
        from app.settings_loader import load_provider_settings

        (tmp_path / "spacerouter.env").write_text("SR_NODE_PORT=4242\n")
        s = load_provider_settings(directory=tmp_path)
        assert s.node.port == 4242
        assert (tmp_path / "settings.json").exists()
        assert (tmp_path / "spacerouter.env.migrated.bak").exists()

    def test_loader_seeds_from_env_when_nothing_else(self, tmp_path, monkeypatch):
        from app.settings_loader import load_provider_settings

        monkeypatch.setenv("SR_NODE_PORT", "5555")
        s = load_provider_settings(directory=tmp_path)
        assert s.node.port == 5555
        # Saved as JSON for next launch.
        assert (tmp_path / "settings.json").exists()

    def test_loader_returns_defaults_when_truly_empty(self, tmp_path, monkeypatch):
        import os as _os
        from app.settings_loader import load_provider_settings

        # Defensive: scrub any inherited SR_* vars from the test env so the
        # loader's "seed from env" branch doesn't fire.
        for key in list(_os.environ):
            if key.startswith("SR_"):
                monkeypatch.delenv(key, raising=False)

        s = load_provider_settings(directory=tmp_path)
        assert s == Settings()
