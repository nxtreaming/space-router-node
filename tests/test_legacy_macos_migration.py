"""Tests for the macOS legacy ``Application Support`` -> ``~/.spacerouter`` copy.

The unification of config dirs (v1.5 plan, D1-b) abandoned macOS's
``~/Library/Application Support/SpaceRouter[-Test]/`` location in favour
of ``~/.spacerouter`` everywhere. Pre-existing v1.4 macOS users must
have their identity key, certs, receipts.db etc. carried over on first
launch — that's what :py:mod:`app.legacy_migration` does.

These tests fake ``Path.home()`` so the real user dir is never touched.
``sys.platform`` is monkeypatched to ``"darwin"`` so the same tests run
green on the Linux CI box.
"""

from __future__ import annotations

import json

import pytest

from app import legacy_migration


@pytest.fixture
def fake_macos(tmp_path, monkeypatch):
    """Yield ``(home, legacy_prod, legacy_test, target)`` rooted in tmp_path.

    Pre-creates the legacy parent directory but NOT the SpaceRouter /
    SpaceRouter-Test subfolders — individual tests opt in to whichever
    layout they want.
    """
    home = tmp_path / "home"
    home.mkdir()
    appsupport = home / "Library" / "Application Support"
    appsupport.mkdir(parents=True)

    monkeypatch.setattr("pathlib.Path.home", lambda: home)
    monkeypatch.setattr("sys.platform", "darwin")

    return {
        "home": home,
        "prod": appsupport / "SpaceRouter",
        "test": appsupport / "SpaceRouter-Test",
        "target": home / ".spacerouter",
    }


def _seed_legacy(dir_: "Path", *, files: dict[str, str]) -> None:  # noqa: F821
    dir_.mkdir(parents=True, exist_ok=True)
    for rel, content in files.items():
        p = dir_ / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)


def test_no_op_on_linux(tmp_path, monkeypatch):
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path / "home")
    target = tmp_path / "home" / ".spacerouter"
    assert legacy_migration.maybe_migrate_legacy_macos(target) is False
    assert not target.exists()


def test_no_op_on_windows(tmp_path, monkeypatch):
    monkeypatch.setattr("sys.platform", "win32")
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path / "home")
    target = tmp_path / "home" / ".spacerouter"
    assert legacy_migration.maybe_migrate_legacy_macos(target) is False


def test_no_op_when_no_legacy_dir(fake_macos):
    target = fake_macos["target"]
    assert legacy_migration.maybe_migrate_legacy_macos(target) is False
    assert not target.exists()


def test_migrates_files_from_legacy_prod(fake_macos):
    legacy = fake_macos["prod"]
    _seed_legacy(
        legacy,
        files={
            "spacerouter.env": "SR_NODE_PORT=9090\n",
            "certs/node.crt": "-----BEGIN CERTIFICATE-----\nfake\n",
            "certs/node-identity.key": "deadbeef",
            "receipts.db": "sqlite-bytes",
        },
    )
    target = fake_macos["target"]

    moved = legacy_migration.maybe_migrate_legacy_macos(target)

    assert moved is True
    assert (target / "spacerouter.env").read_text() == "SR_NODE_PORT=9090\n"
    assert (target / "certs" / "node.crt").exists()
    assert (target / "certs" / "node-identity.key").read_text() == "deadbeef"
    assert (target / "receipts.db").read_text() == "sqlite-bytes"

    # Sentinel written and points at the source.
    sentinel = target / ".migrated_from_appsupport"
    assert sentinel.exists()
    assert str(legacy) in sentinel.read_text()

    # Source is preserved — operator cleans up manually.
    assert legacy.exists()
    assert (legacy / "spacerouter.env").exists()


def test_idempotent_via_sentinel(fake_macos):
    legacy = fake_macos["prod"]
    _seed_legacy(legacy, files={"a.txt": "v1"})
    target = fake_macos["target"]

    assert legacy_migration.maybe_migrate_legacy_macos(target) is True

    # Mutate target post-migration; re-running must leave that mutation
    # alone (the sentinel guards against re-copy).
    (target / "a.txt").write_text("v2-edited")

    assert legacy_migration.maybe_migrate_legacy_macos(target) is False
    assert (target / "a.txt").read_text() == "v2-edited"


def test_aborts_when_target_already_populated(fake_macos, caplog):
    legacy = fake_macos["prod"]
    _seed_legacy(legacy, files={"file.txt": "from-legacy"})

    target = fake_macos["target"]
    target.mkdir()
    (target / "preexisting.txt").write_text("user-data-here")

    with caplog.at_level("WARNING", logger="app.legacy_migration"):
        moved = legacy_migration.maybe_migrate_legacy_macos(target)

    assert moved is False
    # Preexisting data untouched.
    assert (target / "preexisting.txt").read_text() == "user-data-here"
    # And no copy happened.
    assert not (target / "file.txt").exists()
    # Operator-friendly warning so they can resolve it manually.
    assert "Skipping auto-migration" in caplog.text


def test_picks_prod_over_test_when_both_exist(fake_macos, caplog):
    _seed_legacy(fake_macos["prod"], files={"who.txt": "prod"})
    _seed_legacy(fake_macos["test"], files={"who.txt": "test"})

    target = fake_macos["target"]

    with caplog.at_level("WARNING", logger="app.legacy_migration"):
        moved = legacy_migration.maybe_migrate_legacy_macos(target)

    assert moved is True
    assert (target / "who.txt").read_text() == "prod"
    assert "ignoring" in caplog.text
    assert "SpaceRouter-Test" in caplog.text


def test_uses_only_test_dir_when_prod_missing(fake_macos):
    _seed_legacy(fake_macos["test"], files={"who.txt": "test"})
    target = fake_macos["target"]

    assert legacy_migration.maybe_migrate_legacy_macos(target) is True
    assert (target / "who.txt").read_text() == "test"


def test_settings_json_is_carried_over(fake_macos):
    """A v1.4-shipping settings.json that lived in App Support must move."""
    payload = {
        "schema_version": 1,
        "build_variant": "test",
        "node": {"port": 9091},
    }
    _seed_legacy(
        fake_macos["prod"], files={"settings.json": json.dumps(payload)},
    )
    target = fake_macos["target"]

    legacy_migration.maybe_migrate_legacy_macos(target)

    moved = json.loads((target / "settings.json").read_text())
    assert moved["build_variant"] == "test"
    assert moved["node"]["port"] == 9091


def test_migrator_runs_inside_load_provider_settings(fake_macos):
    """End-to-end: settings_loader invokes the migrator before reading."""
    _seed_legacy(
        fake_macos["prod"],
        files={
            "settings.json": json.dumps(
                {"schema_version": 1, "node": {"port": 4242}}
            )
        },
    )

    from app.settings_loader import load_provider_settings

    s = load_provider_settings(directory=fake_macos["target"])
    assert s.node.port == 4242
    assert (fake_macos["target"] / ".migrated_from_appsupport").exists()
