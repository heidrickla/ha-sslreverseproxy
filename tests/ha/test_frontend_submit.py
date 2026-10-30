"""The forms with an Advanced section, and the key step, submitted as the dialog does.

The data comes from the schema the frontend receives, through a port of its
initial-data code (frontend_form.py); Advanced is never opened.
"""

from __future__ import annotations

import aiohttp
from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import CONF_API_KEY, CONF_URL, CONF_VERIFY_SSL
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.sslreverseproxy.const import CONF_ADVANCED, DOMAIN

from .conftest import ENTRY_DATA, INSTANCE_ID, KEY, URL, Api
from .frontend_form import frontend_submission

TYPED = {CONF_URL: URL, CONF_API_KEY: KEY}


async def _first_screen(hass: HomeAssistant) -> dict:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["step_id"] == "user"
    return result


async def test_the_first_screen_submits_with_advanced_collapsed(
    hass: HomeAssistant, api: Api
) -> None:
    result = await _first_screen(hass)
    done = await hass.config_entries.flow.async_configure(
        result["flow_id"], frontend_submission(result, TYPED)
    )
    assert done["type"] is FlowResultType.CREATE_ENTRY
    # Advanced untouched: the certificate is checked.
    assert done["data"] == ENTRY_DATA
    assert done["result"].unique_id == INSTANCE_ID


async def test_the_first_screen_shown_again_after_an_error_submits(
    hass: HomeAssistant, api: Api
) -> None:
    good = dict(api.answers)
    api.fail_all(aiohttp.ClientConnectionError)
    result = await _first_screen(hass)
    shown = await hass.config_entries.flow.async_configure(
        result["flow_id"], frontend_submission(result, TYPED)
    )
    assert shown["step_id"] == "user"
    assert shown["errors"] == {"base": "cannot_connect"}

    # The address comes back filled in; the key, never echoed, is typed again.
    api.answers = good
    api.install()
    done = await hass.config_entries.flow.async_configure(
        shown["flow_id"], frontend_submission(shown, {CONF_API_KEY: KEY})
    )
    assert done["type"] is FlowResultType.CREATE_ENTRY
    assert done["data"] == ENTRY_DATA


async def test_the_key_step_submits_with_a_new_key(
    hass: HomeAssistant, api: Api
) -> None:
    good = dict(api.answers)
    api.set("GET", "/api/instance", 401, {"title": "Unauthorized"})
    result = await _first_screen(hass)
    shown = await hass.config_entries.flow.async_configure(
        result["flow_id"], frontend_submission(result, TYPED)
    )
    assert shown["step_id"] == "api_key"

    api.answers = good
    api.install()
    done = await hass.config_entries.flow.async_configure(
        shown["flow_id"], frontend_submission(shown, {CONF_API_KEY: "issued"})
    )
    assert done["type"] is FlowResultType.CREATE_ENTRY
    assert done["data"] == {**ENTRY_DATA, CONF_API_KEY: "issued"}


async def test_reconfigure_submits_as_it_opened(
    hass: HomeAssistant, api: Api, entry: MockConfigEntry
) -> None:
    result = await entry.start_reconfigure_flow(hass)
    assert result["step_id"] == "reconfigure"
    done = await hass.config_entries.flow.async_configure(
        result["flow_id"], frontend_submission(result)
    )
    assert done["type"] is FlowResultType.ABORT
    assert done["reason"] == "reconfigure_successful"
    # A blank key keeps the stored one, and the check stays as stored.
    assert entry.data == {CONF_URL: URL, CONF_API_KEY: KEY, CONF_VERIFY_SSL: True}


async def test_what_was_typed_in_advanced_comes_back_after_an_error(
    hass: HomeAssistant, api: Api
) -> None:
    api.fail_all(aiohttp.ClientConnectionError)
    result = await _first_screen(hass)
    sent = frontend_submission(result, TYPED)
    sent[CONF_ADVANCED][CONF_VERIFY_SSL] = False
    shown = await hass.config_entries.flow.async_configure(result["flow_id"], sent)
    assert shown["errors"] == {"base": "cannot_connect"}

    # The section reopens holding the typed choice, and Submit sends it back.
    again = frontend_submission(shown, {CONF_API_KEY: KEY})
    assert again[CONF_ADVANCED][CONF_VERIFY_SSL] is False
    hass.config_entries.flow.async_abort(shown["flow_id"])
