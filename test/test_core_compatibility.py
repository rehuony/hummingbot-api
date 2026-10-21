"""The core compatibility guard checks the external surface used by managed groups."""

from types import SimpleNamespace

import pytest

pytest.importorskip("hummingbot")

from utils.core_compatibility import REQUIRED_CORE_SURFACE, require_core_surface  # noqa: E402


def test_the_installed_core_carries_everything_this_api_reads():
    """The installed package satisfies the current explicit requirements."""
    require_core_surface()


def test_the_list_covers_managed_order_transport_and_read_views():
    assert {attribute for _, attribute, _ in REQUIRED_CORE_SURFACE} >= {"_api_get", "_api_post", "_api_delete", "config"}


def test_a_missing_field_is_a_startup_error_naming_it(monkeypatch):
    """The whole point: loud, and specific enough to act on."""
    monkeypatch.setattr(
        "utils.core_compatibility.REQUIRED_CORE_SURFACE",
        [("hummingbot.strategy_v2.models.executors_info:ExecutorInfo", "not_a_field", "a field from the future")],
    )

    with pytest.raises(RuntimeError) as excinfo:
        require_core_surface()

    message = str(excinfo.value)
    assert "not_a_field" in message
    assert "older than this API requires" in message


def test_an_unimportable_target_is_reported_rather_than_raised_raw(monkeypatch):
    monkeypatch.setattr(
        "utils.core_compatibility.REQUIRED_CORE_SURFACE",
        [("hummingbot.strategy_v2.models.nope:Gone", "anything", "a module that is not there")],
    )

    with pytest.raises(RuntimeError) as excinfo:
        require_core_surface()

    assert "could not be imported" in str(excinfo.value)


def test_a_plain_class_is_checked_by_attribute_not_model_fields(monkeypatch):
    """ExecutorBase is not a pydantic model, so `model_fields` must not be assumed."""
    monkeypatch.setattr(
        "utils.core_compatibility._resolve",
        lambda path: SimpleNamespace(volume_traded_quote=property(lambda self: None)),
    )
    monkeypatch.setattr(
        "utils.core_compatibility.REQUIRED_CORE_SURFACE",
        [("anything:AtAll", "volume_traded_quote", "present as an attribute")],
    )

    require_core_surface()
