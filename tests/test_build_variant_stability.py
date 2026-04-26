"""Regression test for the macOS Node ID rotation bug.

Root cause (Section 13 of v1.5 plan): ``app/variant.py`` used to read
``os.environ.get("SR_BUILD_VARIANT", "production")`` at module import.
The env var was unstable across launchers (Finder vs shell), producing
two different config dirs and two different identity keys.

The Track P0 fix moves ``build_variant`` into ``settings.json`` and
makes ``app.variant.BUILD_VARIANT`` resolve from the persisted file
(or the frozen-build seed). This test pins the new contract: once
settings.json says "test", mutating ``SR_BUILD_VARIANT`` cannot change
the resolved variant.
"""

from __future__ import annotations

import importlib
import os

import pytest

from app.settings_v2 import Settings


@pytest.fixture
def isolated_settings_dir(tmp_path, monkeypatch):
    """Point settings_loader at a per-test temp dir.

    Also resets ``app.variant``'s cached value so the test sees a fresh
    resolution. ``app/_build_variant.py`` does not exist in the dev tree,
    so the resolution falls through to the settings.json branch — which
    is exactly what we want to exercise.
    """
    # Make Path.home() return a unique per-test directory so the loader's
    # default ``Path.home() / ".spacerouter"`` lands inside this test's
    # tmp_path. Each test gets a fresh tmp_path, so there's no cross-test
    # pollution.
    fake_home = tmp_path / "home"
    fake_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("pathlib.Path.home", lambda: fake_home)
    spacerouter_dir = fake_home / ".spacerouter"
    spacerouter_dir.mkdir(parents=True, exist_ok=True)

    # Reset variant cache.
    import app.variant as variant_mod
    variant_mod.reset_cached_build_variant()
    yield spacerouter_dir
    variant_mod.reset_cached_build_variant()


def test_settings_json_pins_build_variant_against_env_drift(
    isolated_settings_dir, monkeypatch
):
    # 1. Persist build_variant=test in settings.json.
    settings_path = isolated_settings_dir / "settings.json"
    Settings(build_variant="test").save(settings_path)

    # 2. Mutate the env var the way Finder/shell launches used to differ.
    monkeypatch.setenv("SR_BUILD_VARIANT", "production")

    # 3. Resolution must come from settings.json, NOT the env var.
    import app.variant as variant_mod
    variant_mod.reset_cached_build_variant()
    assert variant_mod.get_build_variant() == "test"

    # And mutating the env var again does not change the cached value.
    monkeypatch.setenv("SR_BUILD_VARIANT", "staging")
    assert variant_mod.get_build_variant() == "test"


def test_module_attribute_access_resolves_consistently(isolated_settings_dir):
    Settings(build_variant="test").save(isolated_settings_dir / "settings.json")

    # PEP 562 __getattr__ should hand back the same value as get_build_variant().
    import app.variant as variant_mod
    variant_mod.reset_cached_build_variant()

    direct = variant_mod.get_build_variant()
    via_attr = variant_mod.BUILD_VARIANT  # type: ignore[attr-defined]
    assert direct == via_attr == "test"


def test_no_settings_json_falls_back_to_default(isolated_settings_dir, monkeypatch):
    # No settings.json, no _build_variant.py, no SR_BUILD_VARIANT.
    monkeypatch.delenv("SR_BUILD_VARIANT", raising=False)

    import app.variant as variant_mod
    variant_mod.reset_cached_build_variant()
    assert variant_mod.get_build_variant() == "production"


def test_env_var_does_not_seed_resolved_variant(isolated_settings_dir, monkeypatch):
    """Env var must NEVER override settings.json (the original bug)."""
    Settings(build_variant="production").save(isolated_settings_dir / "settings.json")
    monkeypatch.setenv("SR_BUILD_VARIANT", "test")

    import app.variant as variant_mod
    variant_mod.reset_cached_build_variant()
    assert variant_mod.get_build_variant() == "production"
