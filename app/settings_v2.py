"""Canonical provider settings stored in ``~/.spacerouter/settings.json``.

This module is the foundation of the v1.5 stabilization plan (Track P0).
All provider configuration moves out of scattered ``SR_*`` env vars and
the GUI-managed ``spacerouter.env`` file into a single canonical JSON
document with a stable, versioned schema.

Wei amounts are stored as **strings** to avoid JavaScript
``Number.MAX_SAFE_INTEGER`` rounding issues — same convention used by
the gateway and SDK.

Migration entry point: :py:meth:`Settings.migrate_from_env_file`.

The macOS ``SR_BUILD_VARIANT`` env-var fragility (root cause of the
"Node ID rotates every restart" bug — see PR #68 / Section 13 of the
v1.5 plan) is fixed here for free: ``build_variant`` becomes a regular
persisted field, not an env-var lookup.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field, ValidationError

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1


class _Section(BaseModel):
    model_config = ConfigDict(extra="ignore")


class NodeSection(_Section):
    label: str | None = None
    port: int = 9090
    public_ip: str | None = None
    public_port: int | None = None
    upnp_enabled: bool = True
    mtls_enabled: bool = True
    log_level: str = "INFO"
    registration_mode: str = "auto"


class WalletSection(_Section):
    staking_address: str | None = None
    collection_address: str | None = None
    settlement_key_path: str = "~/.spacerouter/identity.key"
    identity_passphrase_set: bool = False


class CoordinationSection(_Section):
    url: str = "https://spacerouter-coordination-api-test.fly.dev"


class EscrowSection(_Section):
    contract_address: str | None = None
    chain_rpc: str | None = None
    chain_id: int | None = None
    gateway_payer_address: str | None = None
    leg2_rate_per_gb: str | None = None  # wei as string
    synced_from_coord_at: str | None = None  # ISO8601


class ClaimSection(_Section):
    auto_claim_enabled: bool = False
    auto_claim_threshold_space_wei: str = "10000000000000000000"  # 10 SPACE
    auto_claim_threshold_count: int = 10
    batch_size: int = 50


class ReceiptsSection(_Section):
    max_sign_attempts: int = 2
    max_claim_attempts: int = 2
    reaper_grace_seconds: int = 300
    reaper_interval_seconds: int = 300


def _seed_build_variant() -> str:
    """Read the build-variant seed from the frozen-build helper if present.

    Production binaries are stamped at CI build time via ``app/_build_variant.py``;
    that value seeds a fresh ``settings.json``. We deliberately do **not** read
    ``os.environ['SR_BUILD_VARIANT']`` here — that's the bug we're fixing.
    """
    try:
        from app._build_variant import BUILD_VARIANT  # type: ignore[import-not-found]
        return BUILD_VARIANT
    except ImportError:
        return "production"


class Settings(BaseModel):
    model_config = ConfigDict(extra="ignore")

    schema_version: int = SCHEMA_VERSION
    build_variant: str = Field(default_factory=_seed_build_variant)
    node: NodeSection = Field(default_factory=NodeSection)
    wallet: WalletSection = Field(default_factory=WalletSection)
    coordination: CoordinationSection = Field(default_factory=CoordinationSection)
    escrow: EscrowSection = Field(default_factory=EscrowSection)
    claim: ClaimSection = Field(default_factory=ClaimSection)
    receipts: ReceiptsSection = Field(default_factory=ReceiptsSection)

    # ── Load / save ──────────────────────────────────────────────────

    @classmethod
    def load(cls, path: Path) -> "Settings":
        """Load settings from *path*, or return defaults if the file is missing.

        We deliberately do NOT auto-create the file here — first-run / wizard
        flows are responsible for creating ``settings.json``. This keeps load()
        side-effect-free.

        On JSON parse or schema-validation failure, raise with a helpful
        message naming the bad field(s).
        """
        if not path.exists():
            return cls()

        try:
            raw = json.loads(path.read_text())
        except json.JSONDecodeError as e:
            raise ValueError(
                f"settings.json at {path} is not valid JSON: {e.msg} "
                f"(line {e.lineno}, column {e.colno})"
            ) from e

        try:
            return cls.model_validate(raw)
        except ValidationError as e:
            # Build a compact "field: message" summary
            issues = "; ".join(
                f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}"
                for err in e.errors()
            )
            raise ValueError(
                f"settings.json at {path} failed validation: {issues}"
            ) from e

    def save(self, path: Path) -> None:
        """Atomic write: write to ``<path>.tmp``, then ``os.replace``.

        Sets 0600 on POSIX; ``Path.chmod`` is a no-op on Windows so the
        same code is safe cross-platform.
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        # ``mode='json'`` so wei strings stay strings; pretty-print for human edits.
        tmp.write_text(
            json.dumps(self.model_dump(mode="json"), indent=2, sort_keys=False) + "\n"
        )
        os.replace(tmp, path)
        try:
            path.chmod(0o600)
        except (OSError, NotImplementedError):
            # Windows / read-only-mounted fs — best effort.
            pass

    # ── Migration from spacerouter.env ───────────────────────────────

    @classmethod
    def migrate_from_env_file(
        cls,
        env_path: Path,
        settings_path: Path,
        *,
        rename_after: bool = True,
    ) -> "Settings":
        """Migrate a legacy ``spacerouter.env`` into a fresh ``settings.json``.

        Behavior:

        * If ``settings.json`` already exists, do nothing — return the loaded
          one. Migration is idempotent.
        * If ``spacerouter.env`` exists but ``settings.json`` does not, parse
          the env file, map ``SR_*`` keys to schema fields, save the new
          ``settings.json``. When *rename_after* is true (daemon path), the
          env file is renamed to ``spacerouter.env.migrated.bak`` so we never
          re-migrate. The GUI path passes ``rename_after=False`` because the
          GUI still writes to the env file during the transition release.
        * If neither exists, return defaults.
        """
        if settings_path.exists():
            return cls.load(settings_path)

        if not env_path.exists():
            return cls()

        env_values = {k: v for k, v in dotenv_values(env_path).items() if v is not None}
        settings = cls.from_env_mapping(env_values)
        settings.save(settings_path)

        if rename_after:
            # Rename the env file to a backup so we never re-migrate. If the
            # .bak already exists (shouldn't, but be defensive), leave it;
            # ``os.replace`` overwrites atomically across platforms.
            bak = env_path.with_name(env_path.name + ".migrated.bak")
            try:
                os.replace(env_path, bak)
            except OSError as e:
                logger.warning(
                    "Could not rename %s → %s after migration: %s",
                    env_path,
                    bak,
                    e,
                )
            logger.info(
                "Migrated provider config from %s → %s (backup: %s)",
                env_path,
                settings_path,
                bak,
            )
        else:
            logger.info(
                "Seeded settings.json from %s (env file kept; GUI continues to write it)",
                env_path,
            )
        return settings

    # ── Env-mapping (used by migrate_from_env_file and config-fallback) ──

    @classmethod
    def from_env_mapping(cls, env: dict[str, str]) -> "Settings":
        """Build a Settings from a dict of ``SR_*`` keys (whatever shape).

        Unknown keys are logged at INFO and dropped — clean slate. The mapping
        table mirrors Section 9 of the v1.5 plan; non-trivial cases are
        commented inline.
        """
        node: dict[str, Any] = {}
        wallet: dict[str, Any] = {}
        coordination: dict[str, Any] = {}
        escrow: dict[str, Any] = {}
        claim: dict[str, Any] = {}
        receipts: dict[str, Any] = {}
        build_variant: str | None = None

        # Used so we can warn about unknown keys in one consolidated log line.
        consumed: set[str] = set()

        def take(key: str) -> str | None:
            v = env.get(key)
            if v is None:
                return None
            consumed.add(key)
            v = v.strip()
            return v if v != "" else None

        # ── build_variant (the macOS rotation fix) ───────────────────
        bv = take("SR_BUILD_VARIANT")
        if bv:
            build_variant = bv

        # ── node ─────────────────────────────────────────────────────
        if (v := take("SR_NODE_PORT")) is not None:
            node["port"] = int(v)
        if (v := take("SR_NODE_LABEL")) is not None:
            node["label"] = v
        if (v := take("SR_PUBLIC_IP")) is not None:
            node["public_ip"] = v
        if (v := take("SR_PUBLIC_PORT")) is not None:
            try:
                node["public_port"] = int(v)
            except ValueError:
                logger.info("ignoring non-integer SR_PUBLIC_PORT=%r", v)
        if (v := take("SR_UPNP_ENABLED")) is not None:
            node["upnp_enabled"] = _parse_bool(v)
        if (v := take("SR_MTLS_ENABLED")) is not None:
            node["mtls_enabled"] = _parse_bool(v)
        if (v := take("SR_LOG_LEVEL")) is not None:
            node["log_level"] = v
        if (v := take("SR_REGISTRATION_MODE")) is not None:
            node["registration_mode"] = v

        # ── wallet ───────────────────────────────────────────────────
        if (v := take("SR_STAKING_ADDRESS")) is not None:
            wallet["staking_address"] = v
        if (v := take("SR_COLLECTION_ADDRESS")) is not None:
            wallet["collection_address"] = v
        if (v := take("SR_IDENTITY_KEY_PATH")) is not None:
            wallet["settlement_key_path"] = v
        # Passphrase is NEVER persisted into settings.json — only the boolean.
        passphrase = take("SR_IDENTITY_PASSPHRASE")
        if passphrase:
            wallet["identity_passphrase_set"] = True

        # ── coordination ─────────────────────────────────────────────
        if (v := take("SR_COORDINATION_API_URL")) is not None:
            coordination["url"] = v

        # ── escrow ───────────────────────────────────────────────────
        if (v := take("SR_ESCROW_CONTRACT_ADDRESS")) is not None:
            escrow["contract_address"] = v
        if (v := take("SR_ESCROW_CHAIN_RPC")) is not None:
            escrow["chain_rpc"] = v
        if (v := take("SR_ESCROW_CHAIN_ID")) is not None:
            try:
                escrow["chain_id"] = int(v)
            except ValueError:
                logger.info("ignoring non-integer SR_ESCROW_CHAIN_ID=%r", v)
        if (v := take("SR_GATEWAY_PAYER_ADDRESS")) is not None:
            escrow["gateway_payer_address"] = v
        # Renamed: provider's old "local guess" → gateway-canonical rate.
        if (v := take("SR_NODE_RATE_PER_GB")) is not None:
            escrow["leg2_rate_per_gb"] = v

        # ── claim ────────────────────────────────────────────────────
        if (v := take("SR_CLAIM_BATCH_SIZE")) is not None:
            try:
                claim["batch_size"] = int(v)
            except ValueError:
                logger.info("ignoring non-integer SR_CLAIM_BATCH_SIZE=%r", v)

        # ── receipts ─────────────────────────────────────────────────
        if (v := take("SR_RECEIPT_REAPER_GRACE_SECONDS")) is not None:
            try:
                receipts["reaper_grace_seconds"] = int(v)
            except ValueError:
                logger.info("ignoring non-integer SR_RECEIPT_REAPER_GRACE_SECONDS=%r", v)
        if (v := take("SR_RECEIPT_REAPER_INTERVAL_SECONDS")) is not None:
            try:
                receipts["reaper_interval_seconds"] = int(v)
            except ValueError:
                logger.info("ignoring non-integer SR_RECEIPT_REAPER_INTERVAL_SECONDS=%r", v)
        if (v := take("SR_RECEIPT_MAX_SIGN_ATTEMPTS")) is not None:
            try:
                receipts["max_sign_attempts"] = int(v)
            except ValueError:
                logger.info("ignoring non-integer SR_RECEIPT_MAX_SIGN_ATTEMPTS=%r", v)
        if (v := take("SR_RECEIPT_MAX_CLAIM_ATTEMPTS")) is not None:
            try:
                receipts["max_claim_attempts"] = int(v)
            except ValueError:
                logger.info("ignoring non-integer SR_RECEIPT_MAX_CLAIM_ATTEMPTS=%r", v)

        # ── unknown-key sweep ────────────────────────────────────────
        unknown = [k for k in env if k.startswith("SR_") and k not in consumed]
        for k in unknown:
            logger.info("settings migration: ignoring unknown key %s", k)

        kwargs: dict[str, Any] = {}
        if build_variant is not None:
            kwargs["build_variant"] = build_variant
        if node:
            kwargs["node"] = NodeSection(**node)
        if wallet:
            kwargs["wallet"] = WalletSection(**wallet)
        if coordination:
            kwargs["coordination"] = CoordinationSection(**coordination)
        if escrow:
            kwargs["escrow"] = EscrowSection(**escrow)
        if claim:
            kwargs["claim"] = ClaimSection(**claim)
        if receipts:
            kwargs["receipts"] = ReceiptsSection(**receipts)

        return cls(**kwargs)


def _parse_bool(v: str) -> bool:
    """Lenient bool parser for env-string values (``"true"`` / ``"1"`` / etc)."""
    return str(v).strip().lower() in ("1", "true", "yes", "on")
