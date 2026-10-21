"""The SSL Reverse Proxy integration."""

from __future__ import annotations

from homeassistant.const import CONF_API_KEY, CONF_URL, CONF_VERIFY_SSL, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    ConfigEntryError,
    ConfigEntryNotReady,
)
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import ConfigType

from .api import (
    SslrpAuthError,
    SslrpClient,
    SslrpConnectionError,
    SslrpError,
    SslrpPermissionError,
)
from .const import API_VERSION, DEFAULT_VERIFY_SSL, DOMAIN, MANUFACTURER
from .coordinator import (
    KIND_CERTIFICATE,
    KIND_SERVER,
    CertificateCoordinator,
    ProxyCoordinator,
    SslrpConfigEntry,
    SslrpRuntimeData,
    child_identifier,
    instance_identifier,
    key_issue_id,
)
from .models import Instance
from .services import async_setup_services

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

PLATFORMS: list[Platform] = [Platform.BINARY_SENSOR, Platform.BUTTON, Platform.SENSOR]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the actions once, so a call while unloaded gets a translated refusal."""
    async_setup_services(hass)
    return True


async def _async_instance(client: SslrpClient) -> Instance:
    try:
        instance = await client.get_instance()
    except SslrpAuthError as err:
        raise ConfigEntryAuthFailed(
            translation_domain=DOMAIN,
            translation_key="auth_failed",
            translation_placeholders={"error": str(err)},
        ) from err
    except SslrpPermissionError as err:
        raise ConfigEntryAuthFailed(
            translation_domain=DOMAIN,
            translation_key="missing_permissions",
            translation_placeholders={"error": str(err)},
        ) from err
    except SslrpConnectionError as err:
        raise ConfigEntryNotReady(
            translation_domain=DOMAIN,
            translation_key="unreachable",
            translation_placeholders={"error": str(err)},
        ) from err
    except SslrpError as err:
        raise ConfigEntryNotReady(
            translation_domain=DOMAIN,
            translation_key="bad_answer",
            translation_placeholders={"error": str(err)},
        ) from err
    if instance.api_version != API_VERSION:
        raise ConfigEntryError(
            translation_domain=DOMAIN,
            translation_key="unsupported_version",
            translation_placeholders={"version": str(instance.api_version)},
        )
    return instance


async def async_setup_entry(hass: HomeAssistant, entry: SslrpConfigEntry) -> bool:
    """Set up one control plane from its config entry."""
    session = async_get_clientsession(
        hass, verify_ssl=entry.data.get(CONF_VERIFY_SSL, DEFAULT_VERIFY_SSL)
    )
    client = SslrpClient(session, entry.data[CONF_URL], entry.data[CONF_API_KEY])
    instance = await _async_instance(client)
    if instance.instance_id != entry.unique_id:
        # Another installation answers at this address; its rows are not this entry's.
        raise ConfigEntryError(
            translation_domain=DOMAIN, translation_key="wrong_instance"
        )

    proxy = ProxyCoordinator(hass, entry, client, instance)
    certificates = CertificateCoordinator(hass, entry, client, instance)
    await proxy.async_config_entry_first_refresh()
    await certificates.async_config_entry_first_refresh()

    # Registered first so server and certificate devices can name it as via_device_id.
    dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={instance_identifier(instance.instance_id)},
        manufacturer=MANUFACTURER,
        name=entry.title,
        model=proxy.data.status.engine,
        entry_type=dr.DeviceEntryType.SERVICE,
        configuration_url=client.base_url,
    )
    entry.runtime_data = SslrpRuntimeData(client, instance, proxy, certificates)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: SslrpConfigEntry) -> bool:
    """Unload a config entry. The actions stay registered."""
    unloaded: bool = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        ir.async_delete_issue(hass, DOMAIN, key_issue_id(entry))
    return unloaded


async def async_remove_config_entry_device(
    hass: HomeAssistant, entry: SslrpConfigEntry, device: dr.DeviceEntry
) -> bool:
    """A server or certificate device may go once the API no longer lists it."""
    runtime = entry.runtime_data
    instance_id = runtime.instance.instance_id
    if instance_identifier(instance_id) in device.identifiers:
        return False
    live = {
        child_identifier(instance_id, KIND_SERVER, server_id)
        for server_id in runtime.proxy.data.servers
    } | {
        child_identifier(instance_id, KIND_CERTIFICATE, certificate_id)
        for certificate_id in runtime.certificates.data
    }
    return not device.identifiers & live
