"""Entity values from the recorded answers, defaults and the key's grants."""

from __future__ import annotations

import pytest
from homeassistant.const import STATE_OFF, STATE_ON
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.sslreverseproxy.const import DOMAIN
from tests.pure import body

from .conftest import INSTANCE_ID, Api


def _state(hass: HomeAssistant, platform: str, unique_id: str) -> str:
    entity_id = er.async_get(hass).async_get_entity_id(platform, DOMAIN, unique_id)
    assert entity_id is not None, unique_id
    state = hass.states.get(entity_id)
    assert state is not None, entity_id
    return state.state


def _server(name: str) -> str:
    return next(s["id"] for s in body("servers.json") if s["name"] == name)


def _cert(domain: str) -> dict:
    return next(c for c in body("certificates.json") if c["domain"] == domain)


async def test_proxy_values(hass: HomeAssistant, loaded: MockConfigEntry) -> None:
    traffic = body("traffic.json")
    assert _state(hass, "sensor", f"{INSTANCE_ID}_proxy_state") == "running"
    assert _state(hass, "sensor", f"{INSTANCE_ID}_active_rules") == "5"
    assert _state(hass, "sensor", f"{INSTANCE_ID}_requests") == str(traffic["requests"])
    assert _state(hass, "sensor", f"{INSTANCE_ID}_in_flight") == "0"
    assert _state(hass, "sensor", f"{INSTANCE_ID}_server_errors") == "0"
    # A state string carries 15 significant digits.
    assert float(
        _state(hass, "sensor", f"{INSTANCE_ID}_response_time")
    ) == pytest.approx(traffic["avgResponseMs"])
    assert float(
        _state(hass, "sensor", f"{INSTANCE_ID}_response_time_p95")
    ) == pytest.approx(traffic["p95Ms"])
    assert _state(hass, "binary_sensor", f"{INSTANCE_ID}_running") == STATE_ON
    expiry = _state(hass, "sensor", f"{INSTANCE_ID}_api_key_expiry")
    assert expiry.endswith("+00:00")


async def test_server_values(hass: HomeAssistant, loaded: MockConfigEntry) -> None:
    internal, web, windows = (
        _server("Internal Services"),
        _server("Web Applications"),
        _server("Windows Services"),
    )
    assert (
        _state(hass, "sensor", f"{INSTANCE_ID}_server_{internal}_server_status")
        == "error"
    )
    assert (
        _state(hass, "sensor", f"{INSTANCE_ID}_server_{internal}_upstreams_up") == "1"
    )
    assert _state(hass, "sensor", f"{INSTANCE_ID}_server_{internal}_upstreams") == "2"
    assert _state(hass, "sensor", f"{INSTANCE_ID}_server_{internal}_rules") == "2"
    assert (
        _state(
            hass, "binary_sensor", f"{INSTANCE_ID}_server_{internal}_upstream_problem"
        )
        == STATE_ON
    )
    assert (
        _state(hass, "binary_sensor", f"{INSTANCE_ID}_server_{web}_upstream_problem")
        == STATE_OFF
    )
    assert (
        _state(hass, "sensor", f"{INSTANCE_ID}_server_{windows}_server_status")
        == "inactive"
    )


async def test_certificate_values(hass: HomeAssistant, loaded: MockConfigEntry) -> None:
    cert = _cert("api.example.com")
    prefix = f"{INSTANCE_ID}_certificate_{cert['id']}"
    assert _state(hass, "sensor", f"{prefix}_certificate_status") == "valid"
    assert _state(hass, "sensor", f"{prefix}_certificate_expiry").startswith(
        cert["expiresAt"][:16]
    )
    assert _state(hass, "binary_sensor", f"{prefix}_renewal_pending") == STATE_OFF


async def test_unknown_enum_values_read_as_unknown(
    hass: HomeAssistant, api: Api, entry: MockConfigEntry
) -> None:
    api.edit("/api/proxy/status", lambda d: d.update(state="Hibernating"))
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert _state(hass, "sensor", f"{INSTANCE_ID}_proxy_state") == "unknown"
    assert _state(hass, "binary_sensor", f"{INSTANCE_ID}_running") == STATE_OFF


async def test_an_unprobed_server_has_no_upstream_verdict(
    hass: HomeAssistant, api: Api, entry: MockConfigEntry
) -> None:
    api.edit("/api/servers", lambda d: [dict(s, statusCheckedAt=None) for s in d])
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert (
        _state(
            hass,
            "binary_sensor",
            f"{INSTANCE_ID}_server_{_server('Web Applications')}_upstream_problem",
        )
        == "unknown"
    )


async def test_disabled_by_default(
    hass: HomeAssistant, loaded: MockConfigEntry
) -> None:
    registry = er.async_get(hass)
    disabled = sorted(
        e.unique_id.removeprefix(f"{INSTANCE_ID}_")
        for e in er.async_entries_for_config_entry(registry, loaded.entry_id)
        if e.disabled_by is not None
    )
    assert disabled == [
        "client_errors",
        "data_received",
        "data_sent",
        "time_to_first_byte",
    ]


async def test_buttons_follow_the_keys_grants(
    hass: HomeAssistant, api: Api, entry: MockConfigEntry
) -> None:
    api.edit("/api/whoami", lambda d: d.update(permissions=["ServerRead", "CertRead"]))
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    buttons = [
        e
        for e in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
        if e.domain == "button"
    ]
    assert buttons == []


async def test_buttons_with_the_home_assistant_preset(
    hass: HomeAssistant, loaded: MockConfigEntry
) -> None:
    unique_ids = sorted(
        e.unique_id
        for e in er.async_entries_for_config_entry(er.async_get(hass), loaded.entry_id)
        if e.domain == "button"
    )
    managed = sorted(c["id"] for c in body("certificates.json") if c["managed"])
    assert unique_ids == sorted(
        [
            f"{INSTANCE_ID}_reload",
            *(f"{INSTANCE_ID}_certificate_{c}_renew" for c in managed),
        ]
    )


async def test_an_uploaded_certificate_has_no_renew_button(
    hass: HomeAssistant, api: Api, entry: MockConfigEntry
) -> None:
    api.edit("/api/certificates", lambda d: [dict(c, managed=False) for c in d])
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    buttons = [
        e.unique_id
        for e in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
        if e.domain == "button"
    ]
    assert buttons == [f"{INSTANCE_ID}_reload"]
