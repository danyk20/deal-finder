"""Registry behavior for internal/dev-only adapters (e.g. Demo)."""

from __future__ import annotations

from deal_finder import registry


def test_demo_is_internal_only():
    demo = registry.get_adapter("demo")
    assert demo is not None and demo.internal_only is True


def test_list_adapters_hides_internal_by_default():
    keys = {a.key for a in registry.list_adapters()}
    assert "demo" not in keys
    assert {"tutti", "ricardo", "autoscout24", "autolina", "autouncle", "facebook"} <= keys


def test_list_adapters_include_internal_shows_demo():
    keys = {a.key for a in registry.list_adapters(include_internal=True)}
    assert "demo" in keys


def test_adapters_for_category_hides_internal_by_default():
    keys = {a.key for a in registry.adapters_for_category("car")}
    assert "demo" not in keys
    assert "demo" in {a.key for a in registry.adapters_for_category("car", include_internal=True)}


def test_get_adapter_still_resolves_demo():
    """Hidden from listings, but a watch that already references it must keep working."""
    assert registry.get_adapter("demo") is not None
