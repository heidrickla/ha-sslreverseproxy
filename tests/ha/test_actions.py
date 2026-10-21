"""Actions and buttons: administrators and automations only, translated refusals."""

from __future__ import annotations

import importlib
from typing import Any

import pytest
from homeassistant.auth.models import User
from homeassistant.core import Context, HomeAssistant
from homeassistant.exceptions import (
    HomeAssistantError,
    ServiceValidationError,
    Unauthorized,
)
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import async_get_platforms
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.sslreverseproxy import PLATFORMS
from custom_components.sslreverseproxy.const import DOMAIN
from tests.pure import body, recorded

from .conftest import INSTANCE_ID, Api

CERT = next(c for c in body("certificates.json") if c["id"] == body("renew.json")["id"])
RENEW_PATH = f"/api/certificates/{CERT['id']}/renew"


@pytest.fixture
async def users(hass: HomeAssistant) -> tuple[User, User]:
    """An owner (the first user is an administrator), then an ordinary user."""
    owner = await hass.auth.async_create_user("Owner")
    user = await hass.auth.async_create_user("User", group_ids=["system-users"])
    assert owner.is_admin and not user.is_admin
    return owner, user


def _button(hass: HomeAssistant, unique_id: str) -> str:
    entity_id = er.async_get(hass).async_get_entity_id("button", DOMAIN, unique_id)
    assert entity_id is not None
    return entity_id


async def _reload(
    hass: HomeAssistant, entry_id: str, context: Context | None = None
) -> None:
    await hass.services.async_call(
        DOMAIN, "reload", {"config_entry_id": entry_id}, blocking=True, context=context
    )


async def _renew(
    hass: HomeAssistant, entry_id: str, name: str, context: Context | None = None
) -> None:
    await hass.services.async_call(
        DOMAIN,
        "renew_certificate",
        {"config_entry_id": entry_id, "certificate": name},
        blocking=True,
        context=context,
    )


async def _press(
    hass: HomeAssistant, entity_id: str, context: Context | None = None
) -> None:
    await hass.services.async_call(
        "button", "press", {"entity_id": entity_id}, blocking=True, context=context
    )


async def test_reload_action_for_an_admin_and_an_automation(
    hass: HomeAssistant, api: Api, loaded: MockConfigEntry, users: tuple[User, User]
) -> None:
    owner, _ = users
    await _reload(hass, loaded.entry_id, Context(user_id=owner.id))
    await _reload(hass, loaded.entry_id)
    assert len(api.calls("POST", "/api/proxy/reload")) == 2


async def test_actions_refuse_an_ordinary_user(
    hass: HomeAssistant, api: Api, loaded: MockConfigEntry, users: tuple[User, User]
) -> None:
    _, user = users
    with pytest.raises(Unauthorized):
        await _reload(hass, loaded.entry_id, Context(user_id=user.id))
    with pytest.raises(Unauthorized):
        await _renew(hass, loaded.entry_id, CERT["domain"], Context(user_id=user.id))
    assert not [c for c in api.mocker.mock_calls if c[0] == "POST"]


async def test_renew_action_by_domain(
    hass: HomeAssistant, api: Api, loaded: MockConfigEntry
) -> None:
    await _renew(hass, loaded.entry_id, f" {CERT['domain'].upper()} ")
    assert len(api.calls("POST", RENEW_PATH)) == 1


async def test_renew_action_refusals(
    hass: HomeAssistant, api: Api, loaded: MockConfigEntry
) -> None:
    with pytest.raises(ServiceValidationError) as caught:
        await _renew(hass, loaded.entry_id, "nothing.example.com")
    assert caught.value.translation_key == "certificate_not_found"
    api.set("POST", RENEW_PATH, *recorded("renew_refused.json"))
    with pytest.raises(HomeAssistantError) as caught:
        await _renew(hass, loaded.entry_id, CERT["domain"])
    assert caught.value.translation_key == "refused"
    assert "already in progress" in str(caught.value.translation_placeholders)


@pytest.mark.parametrize(
    ("status", "key"),
    [(401, "auth_failed"), (403, "missing_permissions"), (500, "command_failed")],
)
async def test_reload_failures_are_translated(
    hass: HomeAssistant, api: Api, loaded: MockConfigEntry, status: int, key: str
) -> None:
    api.set("POST", "/api/proxy/reload", status, {"title": "x"})
    with pytest.raises(HomeAssistantError) as caught:
        await _reload(hass, loaded.entry_id)
    assert caught.value.translation_key == key


async def test_actions_name_a_loaded_entry(
    hass: HomeAssistant, api: Api, loaded: MockConfigEntry
) -> None:
    with pytest.raises(ServiceValidationError) as caught:
        await _reload(hass, "nope")
    assert caught.value.translation_key == "entry_not_found"
    other = MockConfigEntry(domain="other")
    other.add_to_hass(hass)
    with pytest.raises(ServiceValidationError) as caught:
        await _reload(hass, other.entry_id)
    assert caught.value.translation_key == "entry_not_found"
    assert await hass.config_entries.async_unload(loaded.entry_id)
    with pytest.raises(ServiceValidationError) as caught:
        await _reload(hass, loaded.entry_id)
    assert caught.value.translation_key == "not_loaded"


async def test_buttons_for_an_admin_and_an_automation(
    hass: HomeAssistant, api: Api, loaded: MockConfigEntry, users: tuple[User, User]
) -> None:
    owner, _ = users
    reload_button = _button(hass, f"{INSTANCE_ID}_reload")
    renew_button = _button(hass, f"{INSTANCE_ID}_certificate_{CERT['id']}_renew")
    await _press(hass, reload_button, Context(user_id=owner.id))
    await _press(hass, reload_button)
    await _press(hass, renew_button, Context(user_id=owner.id))
    assert len(api.calls("POST", "/api/proxy/reload")) == 2
    assert len(api.calls("POST", RENEW_PATH)) == 1


async def test_buttons_refuse_an_ordinary_user(
    hass: HomeAssistant, api: Api, loaded: MockConfigEntry, users: tuple[User, User]
) -> None:
    _, user = users
    for unique_id in (
        f"{INSTANCE_ID}_reload",
        f"{INSTANCE_ID}_certificate_{CERT['id']}_renew",
    ):
        with pytest.raises(Unauthorized):
            await _press(hass, _button(hass, unique_id), Context(user_id=user.id))
    assert not [c for c in api.mocker.mock_calls if c[0] == "POST"]


async def test_a_button_failure_is_translated(
    hass: HomeAssistant, api: Api, loaded: MockConfigEntry
) -> None:
    api.set(
        "POST", RENEW_PATH, 409, {"message": "The proxy is stopped; start it first."}
    )
    with pytest.raises(HomeAssistantError) as caught:
        await _press(
            hass, _button(hass, f"{INSTANCE_ID}_certificate_{CERT['id']}_renew")
        )
    assert caught.value.translation_key == "refused"
    api.set("POST", "/api/proxy/reload", 401, {})
    with pytest.raises(HomeAssistantError) as caught:
        await _press(hass, _button(hass, f"{INSTANCE_ID}_reload"))
    assert caught.value.translation_key == "auth_failed"


def _concrete_entity_classes() -> set[type[Entity]]:
    """Every entity class the component's platforms define that no other one extends."""
    found: set[type[Entity]] = set()
    for platform in PLATFORMS:
        module = importlib.import_module(f"custom_components.{DOMAIN}.{platform.value}")
        found |= {
            obj
            for obj in vars(module).values()
            if isinstance(obj, type)
            and issubclass(obj, Entity)
            and obj.__module__.startswith(f"custom_components.{DOMAIN}.")
        }
    return {c for c in found if not any(o is not c and issubclass(o, c) for o in found)}


async def test_every_action_refuses_an_ordinary_user(
    hass: HomeAssistant, api: Api, loaded: MockConfigEntry, users: tuple[User, User]
) -> None:
    """Every entity's domain actions and every integration action refuse a non-admin.

    Nothing but a read reaches the control plane, and every concrete entity class
    the platforms define is among the entities created.
    """
    _, user = users
    context = Context(user_id=user.id)
    entities = [
        e for p in async_get_platforms(hass, DOMAIN) for e in p.entities.values()
    ]
    assert {type(e) for e in entities} >= _concrete_entity_classes()
    calls: list[tuple[str, str, dict[str, Any]]] = [
        (e.entity_id.split(".")[0], action, {"entity_id": e.entity_id})
        for e in entities
        for action in hass.services.async_services_for_domain(e.entity_id.split(".")[0])
    ]
    assert any(domain == "button" for domain, _, _ in calls)
    action_data: dict[str, dict[str, Any]] = {
        "reload": {"config_entry_id": loaded.entry_id},
        "renew_certificate": {
            "config_entry_id": loaded.entry_id,
            "certificate": CERT["domain"],
        },
    }
    actions = hass.services.async_services_for_domain(DOMAIN)
    assert set(actions) == set(action_data)
    calls += [(DOMAIN, action, action_data[action]) for action in actions]
    for domain, action, data in calls:
        with pytest.raises(Unauthorized):
            await hass.services.async_call(
                domain, action, data, blocking=True, context=context
            )
    assert not [c for c in api.mocker.mock_calls if c[0] != "GET"]
