"""The key-replacement repair flow, and what diagnostics leave out."""

from __future__ import annotations

import json
from datetime import timedelta

from homeassistant.const import CONF_API_KEY
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.sslreverseproxy.const import DOMAIN
from custom_components.sslreverseproxy.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.sslreverseproxy.repairs import async_create_fix_flow
from tests.pure import body, recorded

from .conftest import INSTANCE_ID, KEY, Api


async def _expiring(hass: HomeAssistant, api: Api, entry: MockConfigEntry) -> str:
    api.set_key_expiry(dt_util.utcnow() + timedelta(days=2))
    api.install()
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    issue_id = f"api_key_expiring_{entry.entry_id}"
    issue = ir.async_get(hass).async_get_issue(DOMAIN, issue_id)
    assert issue is not None
    return issue_id


async def _flow(hass: HomeAssistant, issue_id: str):
    issue = ir.async_get(hass).async_get_issue(DOMAIN, issue_id)
    flow = await async_create_fix_flow(hass, issue_id, issue.data)
    flow.hass = hass
    flow.handler = DOMAIN
    flow.flow_id = "test"
    return flow


async def test_the_repair_takes_a_new_key_and_reloads(
    hass: HomeAssistant, api: Api, entry: MockConfigEntry
) -> None:
    issue_id = await _expiring(hass, api, entry)
    flow = await _flow(hass, issue_id)
    result = await flow.async_step_init()
    assert result["type"] is FlowResultType.FORM
    assert KEY not in repr(result)

    api.set("GET", "/api/instance", 401, {})
    result = await flow.async_step_confirm({CONF_API_KEY: "bad"})
    assert result["errors"] == {"base": "invalid_auth"}

    api.answers[("GET", "/api/instance")] = recorded("instance.json")
    api.set_key_expiry(dt_util.utcnow() + timedelta(days=90))
    api.install()
    result = await flow.async_step_confirm({CONF_API_KEY: "fresh"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.data[CONF_API_KEY] == "fresh"
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None


async def test_the_repair_refuses_another_instance(
    hass: HomeAssistant, api: Api, entry: MockConfigEntry
) -> None:
    issue_id = await _expiring(hass, api, entry)
    flow = await _flow(hass, issue_id)
    api.edit(
        "/api/instance",
        lambda d: d.update(instanceId="00000000-0000-0000-0000-000000000001"),
    )
    result = await flow.async_step_confirm({CONF_API_KEY: "other"})
    assert result["errors"] == {"base": "wrong_instance"}
    assert entry.data[CONF_API_KEY] == KEY


async def test_the_repair_aborts_for_a_removed_entry(
    hass: HomeAssistant, api: Api, entry: MockConfigEntry
) -> None:
    issue_id = await _expiring(hass, api, entry)
    flow = await _flow(hass, issue_id)
    await hass.config_entries.async_remove(entry.entry_id)
    result = await flow.async_step_init()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "entry_removed"


async def test_diagnostics_leave_out_keys_names_and_addresses(
    hass: HomeAssistant, loaded: MockConfigEntry
) -> None:
    diagnostics = await async_get_config_entry_diagnostics(hass, loaded)
    text = json.dumps(diagnostics)
    assert KEY not in text
    assert "proxy.example.com" not in text
    assert INSTANCE_ID not in text
    for server in body("servers.json"):
        assert (
            server["name"] not in text
            and server["host"] not in text
            and server["id"] not in text
        )
    for cert in body("certificates.json"):
        assert cert["domain"] not in text and cert["id"] not in text
    assert diagnostics["proxy"]["whoami"]["permissions"] == [
        "CertRead",
        "CertRenew",
        "ProxyReload",
        "ServerRead",
    ]
    assert diagnostics["proxy"]["status"]["state"] == "running"
    assert len(diagnostics["certificates"]) == len(body("certificates.json"))
    assert diagnostics["last_update_success"] == {"proxy": True, "certificates": True}
