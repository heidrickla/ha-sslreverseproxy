"""Buttons: reload the proxy, renew a certificate. Administrators only."""

from __future__ import annotations

from collections.abc import Iterable
from typing import override

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api import SslrpError
from .const import PERMISSION_RELOAD, PERMISSION_RENEW
from .coordinator import CertificateCoordinator, ProxyCoordinator, SslrpConfigEntry
from .entity import (
    CertificateEntity,
    ProxyEntity,
    raise_command_error,
    require_admin,
    track_items,
)

# Each press is one request to the control plane; one at a time.
PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SslrpConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Buttons only for what the key may do; the key's grants are read at setup."""
    data = entry.runtime_data
    proxy, certificates = data.proxy, data.certificates
    granted = proxy.data.whoami.permissions
    if PERMISSION_RELOAD in granted:
        async_add_entities([ReloadButton(proxy)])
    if PERMISSION_RENEW not in granted:
        return

    def certs(certificate_id: str) -> Iterable[ButtonEntity]:
        # Caddy reissues only what it issued; uploaded certificates have no renewal.
        if certificates.data[certificate_id].managed:
            return [RenewButton(certificates, certificate_id)]
        return []

    track_items(entry, certificates, lambda d: d, certs, async_add_entities)


class ReloadButton(ProxyEntity, ButtonEntity):
    def __init__(self, coordinator: ProxyCoordinator) -> None:
        super().__init__(coordinator, "reload")

    @override
    async def async_press(self) -> None:
        await require_admin(self.hass, self._context)
        try:
            await self.coordinator.client.reload()
        except SslrpError as err:
            raise_command_error(err)
        await self.coordinator.async_request_refresh()


class RenewButton(CertificateEntity, ButtonEntity):
    def __init__(
        self, coordinator: CertificateCoordinator, certificate_id: str
    ) -> None:
        super().__init__(coordinator, certificate_id, "renew")

    @override
    async def async_press(self) -> None:
        await require_admin(self.hass, self._context)
        try:
            await self.coordinator.client.renew_certificate(self.certificate_id)
        except SslrpError as err:
            raise_command_error(err)
        await self.coordinator.async_request_refresh()
