"""Config flow: add, errors and recovery, duplicates, reauth, reconfigure."""

from __future__ import annotations

from typing import Any

import aiohttp
import pytest
from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import CONF_API_KEY, CONF_URL, CONF_VERIFY_SSL
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.sslreverseproxy.const import DOMAIN
from tests.pure import recorded

from .conftest import INSTANCE_ID, KEY, URL, Api

USER_INPUT = {CONF_URL: f"{URL}/api/", CONF_API_KEY: KEY, CONF_VERIFY_SSL: True}


def _suggested(result: Any) -> dict[str, Any]:
    """The values a form sends back to the browser to prefill its fields."""
    return {
        str(key): key.description["suggested_value"]
        for key in result["data_schema"].schema
        if isinstance(getattr(key, "description", None), dict)
        and "suggested_value" in key.description
    }


def _no_commands(api: Api) -> None:
    """The flow reads only; it never reloads or renews."""
    assert not [c for c in api.mocker.mock_calls if c[0] == "POST"]


async def test_user_creates_the_entry(hass: HomeAssistant, api: Api) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "proxy.example.com"
    assert result["data"] == {CONF_URL: URL, CONF_API_KEY: KEY, CONF_VERIFY_SSL: True}
    assert result["result"].unique_id == INSTANCE_ID
    _no_commands(api)


@pytest.mark.parametrize(
    ("break_it", "error"),
    [
        (
            lambda api: api.set("GET", "/api/instance", 401, {"title": "Unauthorized"}),
            "invalid_auth",
        ),
        (
            lambda api: api.set("GET", "/api/whoami", 403, {"title": "Forbidden"}),
            "missing_permissions",
        ),
        (
            lambda api: api.edit(
                "/api/whoami", lambda d: d.update(permissions=["ServerRead"])
            ),
            "missing_permissions",
        ),
        (
            lambda api: api.edit("/api/instance", lambda d: d.update(apiVersion=2)),
            "unsupported_version",
        ),
        (
            lambda api: api.set("GET", "/api/instance", 500, {"title": "boom"}),
            "unknown",
        ),
        (lambda api: api.fail_all(aiohttp.ClientConnectionError), "cannot_connect"),
    ],
)
async def test_errors_then_recovery(
    hass: HomeAssistant, api: Api, break_it, error: str
) -> None:
    good = dict(api.answers)
    break_it(api)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": error}
    # The key never goes back to the browser; the address does.
    assert _suggested(result)[CONF_URL] == USER_INPUT[CONF_URL]
    assert CONF_API_KEY not in _suggested(result)
    assert KEY not in repr(result)
    api.answers = good
    api.install()
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_invalid_url(hass: HomeAssistant, api: Api) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**USER_INPUT, CONF_URL: "proxy.example.com"}
    )
    assert result["errors"] == {"base": "invalid_url"}
    assert not api.mocker.mock_calls


async def test_a_known_instance_is_updated_not_added(
    hass: HomeAssistant, api: Api, entry: MockConfigEntry
) -> None:
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, CONF_API_KEY: "old"}
    )
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert entry.data[CONF_API_KEY] == KEY


async def test_reauth_takes_a_new_key(
    hass: HomeAssistant, api: Api, entry: MockConfigEntry
) -> None:
    result = await entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"
    api.set("GET", "/api/instance", 401, {"title": "Unauthorized"})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_API_KEY: "bad"}
    )
    assert result["errors"] == {"base": "invalid_auth"}
    api.answers[("GET", "/api/instance")] = recorded("instance.json")
    api.install()
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_API_KEY: "new"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_API_KEY] == "new"
    _no_commands(api)


async def test_reauth_refuses_another_instance(
    hass: HomeAssistant, api: Api, entry: MockConfigEntry
) -> None:
    api.edit(
        "/api/instance",
        lambda d: d.update(instanceId="00000000-0000-0000-0000-000000000001"),
    )
    result = await entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_API_KEY: "new"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "wrong_instance"
    assert entry.data[CONF_API_KEY] == KEY


async def test_reconfigure_keeps_a_blank_key(
    hass: HomeAssistant, api: Api, entry: MockConfigEntry
) -> None:
    result = await entry.start_reconfigure_flow(hass)
    assert result["step_id"] == "reconfigure"
    assert _suggested(result)[CONF_URL] == URL
    assert CONF_API_KEY not in _suggested(result)
    assert KEY not in repr(result)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: f"{URL}/", CONF_VERIFY_SSL: False}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data == {CONF_URL: URL, CONF_API_KEY: KEY, CONF_VERIFY_SSL: False}


async def test_reconfigure_with_a_new_key_and_an_error_first(
    hass: HomeAssistant, api: Api, entry: MockConfigEntry
) -> None:
    result = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_URL: "ftp://proxy.example.com",
            CONF_API_KEY: "new",
            CONF_VERIFY_SSL: True,
        },
    )
    assert result["errors"] == {"base": "invalid_url"}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: URL, CONF_API_KEY: "new", CONF_VERIFY_SSL: True}
    )
    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_API_KEY] == "new"


async def test_reconfigure_refuses_another_instance(
    hass: HomeAssistant, api: Api, entry: MockConfigEntry
) -> None:
    api.edit(
        "/api/instance",
        lambda d: d.update(instanceId="00000000-0000-0000-0000-000000000001"),
    )
    result = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: URL, CONF_VERIFY_SSL: True}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "wrong_instance"
