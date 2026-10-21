"""Setup, polling failures, key expiry, and devices that come and go."""

from __future__ import annotations

from datetime import timedelta

import aiohttp
import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.sslreverseproxy import async_remove_config_entry_device
from custom_components.sslreverseproxy.const import (
    CERTIFICATE_SCAN_INTERVAL,
    DOMAIN,
    SCAN_INTERVAL,
)
from tests.pure import body

from .conftest import INSTANCE_ID, Api


def _entity(hass: HomeAssistant, platform: str, unique_id: str) -> str:
    entity_id = er.async_get(hass).async_get_entity_id(platform, DOMAIN, unique_id)
    assert entity_id is not None, unique_id
    return entity_id


def _server_id(name: str) -> str:
    return next(s["id"] for s in body("servers.json") if s["name"] == name)


async def _tick(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, delta: timedelta
) -> None:
    freezer.tick(delta)
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_setup_creates_devices(
    hass: HomeAssistant, loaded: MockConfigEntry
) -> None:
    assert loaded.state is ConfigEntryState.LOADED
    devices = dr.async_entries_for_config_entry(dr.async_get(hass), loaded.entry_id)
    names = sorted(d.name or "" for d in devices)
    certs = [c["domain"] for c in body("certificates.json")]
    assert names == sorted(
        [
            "proxy.example.com",
            "Internal Services",
            "Web Applications",
            "Windows Services",
            *certs,
        ]
    )
    proxy = next(d for d in devices if (DOMAIN, INSTANCE_ID) in d.identifiers)
    assert proxy.model == "caddy"
    assert all(d.via_device_id == proxy.id for d in devices if d.id != proxy.id)


@pytest.mark.parametrize(
    ("break_it", "state"),
    [
        (
            lambda api: api.fail_all(aiohttp.ClientConnectionError),
            ConfigEntryState.SETUP_RETRY,
        ),
        (
            lambda api: api.set("GET", "/api/instance", 500, {}),
            ConfigEntryState.SETUP_RETRY,
        ),
        (
            lambda api: api.set("GET", "/api/servers", 500, {}),
            ConfigEntryState.SETUP_RETRY,
        ),
        (
            lambda api: api.edit("/api/instance", lambda d: d.update(apiVersion=2)),
            ConfigEntryState.SETUP_ERROR,
        ),
        (
            lambda api: api.edit(
                "/api/instance",
                lambda d: d.update(instanceId="00000000-0000-0000-0000-000000000001"),
            ),
            ConfigEntryState.SETUP_ERROR,
        ),
    ],
)
async def test_setup_failures(
    hass: HomeAssistant,
    api: Api,
    entry: MockConfigEntry,
    break_it,
    state: ConfigEntryState,
) -> None:
    break_it(api)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is state


@pytest.mark.parametrize(
    "break_it",
    [
        lambda api: api.set("GET", "/api/instance", 401, {}),
        lambda api: api.set("GET", "/api/instance", 403, {}),
        lambda api: api.set("GET", "/api/whoami", 401, {}),
        lambda api: api.set("GET", "/api/certificates", 403, {}),
        lambda api: api.edit(
            "/api/whoami", lambda d: d.update(permissions=["ServerRead"])
        ),
    ],
)
async def test_a_refused_key_starts_reauth(
    hass: HomeAssistant, api: Api, entry: MockConfigEntry, break_it
) -> None:
    break_it(api)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [f["context"]["source"] for f in flows] == [SOURCE_REAUTH]


async def test_a_failed_poll_marks_entities_unavailable_then_recovers(
    hass: HomeAssistant,
    api: Api,
    loaded: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    state = _entity(hass, "sensor", f"{INSTANCE_ID}_proxy_state")
    expiry = _entity(
        hass,
        "sensor",
        f"{INSTANCE_ID}_certificate_{body('certificates.json')[0]['id']}_certificate_expiry",
    )
    assert hass.states.get(state).state == "running"
    api.fail_all(aiohttp.ClientConnectionError)
    await _tick(hass, freezer, SCAN_INTERVAL)
    assert hass.states.get(state).state == STATE_UNAVAILABLE
    await _tick(hass, freezer, CERTIFICATE_SCAN_INTERVAL)
    assert hass.states.get(expiry).state == STATE_UNAVAILABLE
    api.install()
    await _tick(hass, freezer, CERTIFICATE_SCAN_INTERVAL)
    assert hass.states.get(state).state == "running"
    assert hass.states.get(expiry).state != STATE_UNAVAILABLE


async def test_a_key_that_starts_failing_starts_reauth(
    hass: HomeAssistant,
    api: Api,
    loaded: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    api.set("GET", "/api/whoami", 401, {})
    await _tick(hass, freezer, SCAN_INTERVAL)
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [f["context"]["source"] for f in flows] == [SOURCE_REAUTH]


async def test_key_expiry_raises_and_clears_a_repair_issue(
    hass: HomeAssistant,
    api: Api,
    entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    issues = ir.async_get(hass)
    issue_id = f"api_key_expiring_{entry.entry_id}"
    api.set_key_expiry(dt_util.utcnow() + timedelta(days=6))
    api.install()
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    issue = issues.async_get_issue(DOMAIN, issue_id)
    assert issue is not None and issue.is_fixable
    assert issue.translation_placeholders["key"] == "Home Assistant"

    api.set_key_expiry(dt_util.utcnow() + timedelta(days=90))
    api.install()
    await _tick(hass, freezer, SCAN_INTERVAL)
    assert issues.async_get_issue(DOMAIN, issue_id) is None

    api.set_key_expiry(None)
    api.install()
    await _tick(hass, freezer, SCAN_INTERVAL)
    assert issues.async_get_issue(DOMAIN, issue_id) is None


async def test_unload_clears_the_issue(
    hass: HomeAssistant, api: Api, entry: MockConfigEntry
) -> None:
    api.set_key_expiry(dt_util.utcnow() + timedelta(days=1))
    api.install()
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    issue_id = f"api_key_expiring_{entry.entry_id}"
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert entry.state is ConfigEntryState.NOT_LOADED
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None


async def test_servers_and_certificates_come_and_go(
    hass: HomeAssistant,
    api: Api,
    loaded: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    devices = dr.async_get(hass)
    windows = _server_id("Windows Services")
    windows_device = (DOMAIN, f"{INSTANCE_ID}_server_{windows}")
    status = _entity(hass, "sensor", f"{INSTANCE_ID}_server_{windows}_server_status")
    assert (
        devices.async_get_device_by_identifier(windows_device, loaded.entry_id)
        is not None
    )

    api.edit("/api/servers", lambda d: [s for s in d if s["id"] != windows])
    await _tick(hass, freezer, SCAN_INTERVAL)
    assert (
        devices.async_get_device_by_identifier(windows_device, loaded.entry_id) is None
    )
    assert er.async_get(hass).async_get(status) is None

    api.edit(
        "/api/servers",
        lambda d: [*d, next(s for s in body("servers.json") if s["id"] == windows)],
    )
    await _tick(hass, freezer, SCAN_INTERVAL)
    assert (
        devices.async_get_device_by_identifier(windows_device, loaded.entry_id)
        is not None
    )
    assert (
        hass.states.get(
            _entity(hass, "sensor", f"{INSTANCE_ID}_server_{windows}_server_status")
        ).state
        == "inactive"
    )

    new_cert = dict(
        body("certificates.json")[0],
        id="00000000-0000-0000-0000-0000000000c1",
        domain="new.example.com",
    )
    cert_device = (DOMAIN, f"{INSTANCE_ID}_certificate_{new_cert['id']}")
    renew_id = f"{INSTANCE_ID}_certificate_{new_cert['id']}_renew"
    status_id = f"{INSTANCE_ID}_certificate_{new_cert['id']}_certificate_status"
    api.edit("/api/certificates", lambda d: [*d, new_cert])
    await _tick(hass, freezer, CERTIFICATE_SCAN_INTERVAL)
    assert (
        devices.async_get_device_by_identifier(cert_device, loaded.entry_id) is not None
    )
    renew = _entity(hass, "button", renew_id)

    def managed(value: bool) -> None:
        api.edit(
            "/api/certificates",
            lambda d: [
                dict(c, managed=value) if c["id"] == new_cert["id"] else c for c in d
            ],
        )

    # Uploaded in Caddy's place: no renewal, so only the button goes.
    managed(False)
    await _tick(hass, freezer, CERTIFICATE_SCAN_INTERVAL)
    assert er.async_get(hass).async_get(renew) is None
    assert hass.states.get(renew) is None
    status = _entity(hass, "sensor", status_id)
    assert hass.states.get(status).state != STATE_UNAVAILABLE

    managed(True)
    await _tick(hass, freezer, CERTIFICATE_SCAN_INTERVAL)
    renew = _entity(hass, "button", renew_id)
    assert hass.states.get(renew) is not None

    # Gone from the control plane: its device and every entity of it go.
    api.edit("/api/certificates", lambda d: [c for c in d if c["id"] != new_cert["id"]])
    await _tick(hass, freezer, CERTIFICATE_SCAN_INTERVAL)
    assert devices.async_get_device_by_identifier(cert_device, loaded.entry_id) is None
    assert er.async_get(hass).async_get(renew) is None
    assert er.async_get(hass).async_get(status) is None


async def test_remove_device_by_hand(
    hass: HomeAssistant, api: Api, loaded: MockConfigEntry
) -> None:
    devices = dr.async_get(hass)
    proxy = devices.async_get_device_by_identifier(
        (DOMAIN, INSTANCE_ID), loaded.entry_id
    )
    server = devices.async_get_device_by_identifier(
        (DOMAIN, f"{INSTANCE_ID}_server_{_server_id('Web Applications')}"),
        loaded.entry_id,
    )
    assert not await async_remove_config_entry_device(hass, loaded, proxy)
    assert not await async_remove_config_entry_device(hass, loaded, server)
    stale = devices.async_get_or_create(
        config_entry_id=loaded.entry_id,
        identifiers={(DOMAIN, f"{INSTANCE_ID}_server_gone")},
    )
    assert await async_remove_config_entry_device(hass, loaded, stale)


async def test_component_setup_without_entries(hass: HomeAssistant) -> None:
    assert await async_setup_component(hass, DOMAIN, {})
    assert hass.services.has_service(DOMAIN, "reload")
    assert hass.services.has_service(DOMAIN, "renew_certificate")
