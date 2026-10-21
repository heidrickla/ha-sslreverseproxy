"""Config flow: add, reauthenticate and reconfigure one control plane."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, override
from urllib.parse import urlsplit

import probatio
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_API_KEY, CONF_URL, CONF_VERIFY_SSL
from homeassistant.core import HomeAssistant
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import (
    SslrpAuthError,
    SslrpClient,
    SslrpConnectionError,
    SslrpError,
    SslrpPermissionError,
    normalise_url,
)
from .const import API_VERSION, DEFAULT_VERIFY_SSL, DOMAIN, NAME, REQUIRED_PERMISSIONS
from .models import Instance

_KEY = selector.TextSelector(
    selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
)
_URL = selector.TextSelector(
    selector.TextSelectorConfig(type=selector.TextSelectorType.URL)
)

# The key has no default: a default goes to the browser and is resubmitted
# when the field is cleared.
USER_SCHEMA = probatio.Schema(
    {
        probatio.Required(CONF_URL): _URL,
        probatio.Required(CONF_API_KEY): _KEY,
        probatio.Required(CONF_VERIFY_SSL, default=DEFAULT_VERIFY_SSL): bool,
    }
)
RECONFIGURE_SCHEMA = probatio.Schema(
    {
        probatio.Required(CONF_URL): _URL,
        probatio.Optional(CONF_API_KEY): _KEY,
        probatio.Required(CONF_VERIFY_SSL, default=DEFAULT_VERIFY_SSL): bool,
    }
)
KEY_SCHEMA = probatio.Schema({probatio.Required(CONF_API_KEY): _KEY})


class FlowError(Exception):
    """A form error key."""

    def __init__(self, key: str) -> None:
        super().__init__(key)
        self.key = key


async def async_validate(hass: HomeAssistant, data: Mapping[str, Any]) -> Instance:
    """Check address, key, grants and contract against the real control plane.

    Never calls Reload or Renew. Raises FlowError with the form's error key.
    """
    try:
        url = normalise_url(str(data[CONF_URL]))
    except ValueError as err:
        raise FlowError("invalid_url") from err
    session = async_get_clientsession(
        hass, verify_ssl=bool(data.get(CONF_VERIFY_SSL, DEFAULT_VERIFY_SSL))
    )
    client = SslrpClient(session, url, str(data[CONF_API_KEY]))
    try:
        instance = await client.get_instance()
        whoami = await client.get_whoami()
    except SslrpAuthError as err:
        raise FlowError("invalid_auth") from err
    except SslrpPermissionError as err:
        raise FlowError("missing_permissions") from err
    except SslrpConnectionError as err:
        raise FlowError("cannot_connect") from err
    except SslrpError as err:
        raise FlowError("unknown") from err
    if instance.api_version != API_VERSION:
        raise FlowError("unsupported_version")
    if not REQUIRED_PERMISSIONS.issubset(whoami.permissions):
        raise FlowError("missing_permissions")
    return instance


def _stored(data: Mapping[str, Any]) -> dict[str, Any]:
    return {
        CONF_URL: normalise_url(str(data[CONF_URL])),
        CONF_API_KEY: str(data[CONF_API_KEY]),
        CONF_VERIFY_SSL: bool(data.get(CONF_VERIFY_SSL, DEFAULT_VERIFY_SSL)),
    }


def _without_key(data: Mapping[str, Any]) -> dict[str, Any]:
    """What may go back to the browser as suggested values."""
    return {k: v for k, v in data.items() if k != CONF_API_KEY}


class SslrpConfigFlow(ConfigFlow, domain=DOMAIN):
    """One entry per control plane, keyed on its instance id."""

    VERSION = 1

    async def _try(
        self, data: Mapping[str, Any]
    ) -> tuple[Instance | None, dict[str, str]]:
        try:
            return await async_validate(self.hass, data), {}
        except FlowError as err:
            return None, {"base": err.key}

    @override
    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            instance, errors = await self._try(user_input)
            if instance is not None:
                await self.async_set_unique_id(instance.instance_id)
                data = _stored(user_input)
                self._abort_if_unique_id_configured(updates=data)
                title = urlsplit(data[CONF_URL]).hostname or NAME
                return self.async_create_entry(title=title, data=data)
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(
                USER_SCHEMA, _without_key(user_input or {})
            ),
            errors=errors,
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """A new key for the same control plane."""
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            data = {**entry.data, CONF_API_KEY: user_input[CONF_API_KEY]}
            instance, errors = await self._try(data)
            if instance is not None:
                await self.async_set_unique_id(instance.instance_id)
                self._abort_if_unique_id_mismatch(reason="wrong_instance")
                return self.async_update_reload_and_abort(entry, data=_stored(data))
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=KEY_SCHEMA,
            description_placeholders={"url": entry.data[CONF_URL]},
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """A new address or certificate check; a blank key keeps the stored one."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            data = dict(user_input)
            if not data.get(CONF_API_KEY):
                data[CONF_API_KEY] = entry.data[CONF_API_KEY]
            instance, errors = await self._try(data)
            if instance is not None:
                await self.async_set_unique_id(instance.instance_id)
                self._abort_if_unique_id_mismatch(reason="wrong_instance")
                return self.async_update_reload_and_abort(entry, data=_stored(data))
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(
                RECONFIGURE_SCHEMA, _without_key(user_input or entry.data)
            ),
            errors=errors,
        )
