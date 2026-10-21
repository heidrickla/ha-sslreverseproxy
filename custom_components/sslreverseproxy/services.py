"""Actions: reload the proxy, renew a certificate by name. Administrators only."""

from __future__ import annotations

import probatio
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.service import async_register_admin_service

from .api import SslrpError
from .const import (
    ATTR_CERTIFICATE,
    ATTR_CONFIG_ENTRY_ID,
    DOMAIN,
    SERVICE_RELOAD,
    SERVICE_RENEW_CERTIFICATE,
)
from .coordinator import SslrpRuntimeData
from .entity import raise_command_error

RELOAD_SCHEMA = probatio.Schema({probatio.Required(ATTR_CONFIG_ENTRY_ID): cv.string})
RENEW_SCHEMA = probatio.Schema(
    {
        probatio.Required(ATTR_CONFIG_ENTRY_ID): cv.string,
        probatio.Required(ATTR_CERTIFICATE): cv.string,
    }
)


def _runtime(hass: HomeAssistant, call: ServiceCall) -> SslrpRuntimeData:
    """The loaded entry the call names, or a translated refusal."""
    entry_id: str = call.data[ATTR_CONFIG_ENTRY_ID]
    entry = hass.config_entries.async_get_entry(entry_id)
    if entry is None or entry.domain != DOMAIN:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="entry_not_found",
            translation_placeholders={"entry_id": entry_id},
        )
    if entry.state is not ConfigEntryState.LOADED:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="not_loaded",
            translation_placeholders={"name": entry.title},
        )
    runtime: SslrpRuntimeData = entry.runtime_data
    return runtime


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Register both actions once, independent of any entry."""

    async def _reload(call: ServiceCall) -> None:
        runtime = _runtime(hass, call)
        try:
            await runtime.client.reload(call.context)
        except SslrpError as err:
            raise_command_error(err)
        await runtime.proxy.async_request_refresh()

    async def _renew(call: ServiceCall) -> None:
        runtime = _runtime(hass, call)
        name = call.data[ATTR_CERTIFICATE].strip().lower()
        match = [
            c for c in runtime.certificates.data.values() if c.domain.lower() == name
        ]
        if not match:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="certificate_not_found",
                translation_placeholders={"certificate": name},
            )
        try:
            await runtime.client.renew_certificate(match[0].id, call.context)
        except SslrpError as err:
            raise_command_error(err)
        await runtime.certificates.async_request_refresh()

    async_register_admin_service(
        hass, DOMAIN, SERVICE_RELOAD, _reload, schema=RELOAD_SCHEMA
    )
    async_register_admin_service(
        hass, DOMAIN, SERVICE_RENEW_CERTIFICATE, _renew, schema=RENEW_SCHEMA
    )
