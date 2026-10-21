"""Polling: proxy state every minute, certificates every fifteen."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from typing import NoReturn, override

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import (
    SslrpAuthError,
    SslrpClient,
    SslrpConnectionError,
    SslrpError,
    SslrpPermissionError,
)
from .const import (
    CERTIFICATE_SCAN_INTERVAL,
    DOMAIN,
    ISSUE_KEY_EXPIRING,
    KEY_EXPIRY_WARNING,
    REQUIRED_PERMISSIONS,
    SCAN_INTERVAL,
)
from .models import Certificate, Instance, ProxyData, WhoAmI

_LOGGER = logging.getLogger(__name__)

KIND_SERVER = "server"
KIND_CERTIFICATE = "certificate"


@dataclass(slots=True)
class SslrpRuntimeData:
    """What a loaded entry holds."""

    client: SslrpClient
    instance: Instance
    proxy: ProxyCoordinator
    certificates: CertificateCoordinator


type SslrpConfigEntry = ConfigEntry[SslrpRuntimeData]


def raise_poll_error(err: SslrpError) -> NoReturn:
    """A refused key starts reauth; anything else fails this poll only."""
    if isinstance(err, SslrpAuthError):
        raise ConfigEntryAuthFailed(
            translation_domain=DOMAIN,
            translation_key="auth_failed",
            translation_placeholders={"error": str(err)},
        ) from err
    if isinstance(err, SslrpPermissionError):
        raise ConfigEntryAuthFailed(
            translation_domain=DOMAIN,
            translation_key="missing_permissions",
            translation_placeholders={"error": str(err)},
        ) from err
    if isinstance(err, SslrpConnectionError):
        raise UpdateFailed(
            translation_domain=DOMAIN,
            translation_key="unreachable",
            translation_placeholders={"error": str(err)},
        ) from err
    raise UpdateFailed(
        translation_domain=DOMAIN,
        translation_key="bad_answer",
        translation_placeholders={"error": str(err)},
    ) from err


def key_issue_id(entry: ConfigEntry) -> str:
    return f"{ISSUE_KEY_EXPIRING}_{entry.entry_id}"


def instance_identifier(instance_id: str) -> tuple[str, str]:
    return (DOMAIN, instance_id)


def child_identifier(instance_id: str, kind: str, item_id: str) -> tuple[str, str]:
    """A server's or certificate's device identifier."""
    return (DOMAIN, f"{instance_id}_{kind}_{item_id}")


def remove_stale_devices(
    hass: HomeAssistant,
    entry: ConfigEntry,
    instance_id: str,
    kind: str,
    live: Iterable[str],
) -> None:
    """Remove this entry's devices of one kind that the API no longer lists."""
    keep = {child_identifier(instance_id, kind, item_id) for item_id in live}
    prefix = f"{instance_id}_{kind}_"
    registry = dr.async_get(hass)
    for device in dr.async_entries_for_config_entry(registry, entry.entry_id):
        ours = {i for i in device.identifiers if i[0] == DOMAIN}
        if any(i[1].startswith(prefix) for i in ours) and not ours & keep:
            registry.async_remove_device(device.id)


class ProxyCoordinator(DataUpdateCoordinator[ProxyData]):
    """Status, servers, traffic and the key's own record, every minute."""

    config_entry: SslrpConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: SslrpConfigEntry,
        client: SslrpClient,
        instance: Instance,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN} proxy",
            config_entry=entry,
            update_interval=SCAN_INTERVAL,
        )
        self.client = client
        self.instance = instance

    @override
    async def _async_update_data(self) -> ProxyData:
        try:
            status, servers, traffic, whoami = await asyncio.gather(
                self.client.get_status(),
                self.client.get_servers(),
                self.client.get_traffic(),
                self.client.get_whoami(),
            )
        except SslrpError as err:
            raise_poll_error(err)
        missing = REQUIRED_PERMISSIONS - whoami.permissions
        if missing:
            raise ConfigEntryAuthFailed(
                translation_domain=DOMAIN,
                translation_key="missing_permissions",
                translation_placeholders={"error": ", ".join(sorted(missing))},
            )
        self._update_key_issue(whoami)
        data = ProxyData(status=status, traffic=traffic, whoami=whoami, servers=servers)
        remove_stale_devices(
            self.hass,
            self.config_entry,
            self.instance.instance_id,
            KIND_SERVER,
            servers,
        )
        return data

    def _update_key_issue(self, whoami: WhoAmI) -> None:
        """Ask for a new key from KEY_EXPIRY_WARNING before this one expires."""
        issue_id = key_issue_id(self.config_entry)
        expires = whoami.key.expires_at if whoami.key else None
        if expires is None or expires - dt_util.utcnow() > KEY_EXPIRY_WARNING:
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)
            return
        assert whoami.key is not None
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            issue_id,
            is_fixable=True,
            is_persistent=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_KEY_EXPIRING,
            translation_placeholders={
                "entry": self.config_entry.title,
                "key": whoami.key.name,
                "expires": dt_util.as_local(expires).strftime("%Y-%m-%d %H:%M"),
            },
            data={"entry_id": self.config_entry.entry_id},
        )


class CertificateCoordinator(DataUpdateCoordinator[dict[str, Certificate]]):
    """The certificate rows, every fifteen minutes."""

    config_entry: SslrpConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: SslrpConfigEntry,
        client: SslrpClient,
        instance: Instance,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN} certificates",
            config_entry=entry,
            update_interval=CERTIFICATE_SCAN_INTERVAL,
        )
        self.client = client
        self.instance = instance

    @override
    async def _async_update_data(self) -> dict[str, Certificate]:
        try:
            certificates = await self.client.get_certificates()
        except SslrpError as err:
            raise_poll_error(err)
        remove_stale_devices(
            self.hass,
            self.config_entry,
            self.instance.instance_id,
            KIND_CERTIFICATE,
            certificates,
        )
        return certificates
