"""Config flow: add, reauthenticate and reconfigure one control plane.

The first screen asks for the address and the key; the certificate check waits
in a collapsed Advanced section. A key the control plane refuses or that lacks
the read grants leads to a step that says how to issue one and checks again.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, override
from urllib.parse import urlsplit

import probatio
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_API_KEY, CONF_URL, CONF_VERIFY_SSL
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import section
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
from .const import (
    API_VERSION,
    CONF_ADVANCED,
    DEFAULT_VERIFY_SSL,
    DOMAIN,
    NAME,
    REQUIRED_PERMISSIONS,
)
from .models import Instance

_KEY = selector.TextSelector(
    selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
)
# hassfest refuses a URL inside a translation string.
EXAMPLE_URL = {"example_url": "https://proxy.example.com"}

_URL = selector.TextSelector(
    selector.TextSelectorConfig(type=selector.TextSelectorType.URL)
)

ADVANCED_SCHEMA = probatio.Schema(
    {probatio.Required(CONF_VERIFY_SSL, default=DEFAULT_VERIFY_SSL): bool}
)
KEY_SCHEMA = probatio.Schema({probatio.Required(CONF_API_KEY): _KEY})

# The control plane answered and the key is what is wrong: a new key fixes it.
KEY_ERRORS = frozenset({"invalid_auth", "missing_permissions"})


def _form(key: type[probatio.Marker], collapsed: bool) -> probatio.Schema:
    """Address and key on the screen, the certificate check in Advanced.

    The key has no default: a default goes to the browser and is resubmitted
    when the field is cleared.
    """
    return probatio.Schema(
        {
            probatio.Required(CONF_URL): _URL,
            key(CONF_API_KEY): _KEY,
            probatio.Required(CONF_ADVANCED, default={}): section(
                ADVANCED_SCHEMA, {"collapsed": collapsed}
            ),
        }
    )


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


def _flat(user_input: Mapping[str, Any]) -> dict[str, Any]:
    """The form's input with the Advanced section's fields beside the rest."""
    data = {k: v for k, v in user_input.items() if k != CONF_ADVANCED}
    data.update(user_input.get(CONF_ADVANCED) or {})
    return data


def _stored(data: Mapping[str, Any]) -> dict[str, Any]:
    return {
        CONF_URL: normalise_url(str(data[CONF_URL])),
        CONF_API_KEY: str(data[CONF_API_KEY]),
        CONF_VERIFY_SSL: bool(data.get(CONF_VERIFY_SSL, DEFAULT_VERIFY_SSL)),
    }


def _suggested(data: Mapping[str, Any]) -> dict[str, Any]:
    """What may go back to the browser to prefill the form: never the key."""
    shown: dict[str, Any] = {
        CONF_ADVANCED: {
            CONF_VERIFY_SSL: bool(data.get(CONF_VERIFY_SSL, DEFAULT_VERIFY_SSL))
        }
    }
    if CONF_URL in data:
        shown[CONF_URL] = data[CONF_URL]
    return shown


class SslrpConfigFlow(ConfigFlow, domain=DOMAIN):
    """One entry per control plane, keyed on its instance id."""

    VERSION = 1

    def __init__(self) -> None:
        # The first screen's address and certificate check, kept while the
        # api_key step asks for a key that works.
        self._data: dict[str, Any] = {}
        self._key_error = ""

    async def _try(
        self, data: Mapping[str, Any]
    ) -> tuple[Instance | None, dict[str, str]]:
        try:
            return await async_validate(self.hass, data), {}
        except FlowError as err:
            return None, {"base": err.key}

    async def _create(
        self, instance: Instance, data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        await self.async_set_unique_id(instance.instance_id)
        stored = _stored(data)
        self._abort_if_unique_id_configured(updates=stored)
        title = urlsplit(stored[CONF_URL]).hostname or NAME
        return self.async_create_entry(title=title, data=stored)

    @override
    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        data = _flat(user_input or {})
        if user_input is not None:
            instance, errors = await self._try(data)
            if instance is not None:
                return await self._create(instance, data)
            if errors["base"] in KEY_ERRORS:
                self._data = data
                self._key_error = errors["base"]
                return await self.async_step_api_key()
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(
                _form(probatio.Required, collapsed=not errors), _suggested(data)
            ),
            description_placeholders=EXAMPLE_URL,
            errors=errors,
        )

    async def async_step_api_key(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """The address answered but not to this key: how to issue one; check again."""
        if user_input is not None:
            data = {**self._data, CONF_API_KEY: user_input[CONF_API_KEY]}
            instance, errors = await self._try(data)
            if instance is not None:
                return await self._create(instance, data)
            self._key_error = errors["base"]
        return self.async_show_form(
            step_id="api_key",
            data_schema=KEY_SCHEMA,
            description_placeholders={"url": str(self._data[CONF_URL])},
            errors={"base": self._key_error},
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
        data = dict(entry.data) if user_input is None else _flat(user_input)
        if user_input is not None:
            if not data.get(CONF_API_KEY):
                data[CONF_API_KEY] = entry.data[CONF_API_KEY]
            instance, errors = await self._try(data)
            if instance is not None:
                await self.async_set_unique_id(instance.instance_id)
                self._abort_if_unique_id_mismatch(reason="wrong_instance")
                return self.async_update_reload_and_abort(entry, data=_stored(data))
        # Open while the check is off, so a stored exception is not hidden.
        collapsed = not errors and bool(data.get(CONF_VERIFY_SSL, DEFAULT_VERIFY_SSL))
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(
                _form(probatio.Optional, collapsed), _suggested(data)
            ),
            description_placeholders=EXAMPLE_URL,
            errors=errors,
        )
