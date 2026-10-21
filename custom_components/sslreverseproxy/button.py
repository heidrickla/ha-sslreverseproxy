"""Buttons: reload the proxy, renew a certificate. Administrators only."""

from __future__ import annotations

from collections.abc import Iterable
from typing import override

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
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
        return [RenewButton(certificates, certificate_id)]

    # Caddy reissues only what it issued; uploaded certificates have no renewal.
    # A certificate that becomes managed gets the button; RenewButton removes
    # itself when it stops being managed.
    track_items(
        entry,
        certificates,
        lambda d: (cid for cid, cert in d.items() if cert.managed),
        certs,
        async_add_entities,
    )


class ReloadButton(ProxyEntity, ButtonEntity):
    def __init__(self, coordinator: ProxyCoordinator) -> None:
        super().__init__(coordinator, "reload")

    @override
    async def async_press(self) -> None:
        await require_admin(self.hass, context := self._context)
        try:
            await self.coordinator.client.reload(context)
        except SslrpError as err:
            raise_command_error(err)
        await self.coordinator.async_request_refresh()


class RenewButton(CertificateEntity, ButtonEntity):
    def __init__(
        self, coordinator: CertificateCoordinator, certificate_id: str
    ) -> None:
        super().__init__(coordinator, certificate_id, "renew")

    @callback
    @override
    def _handle_coordinator_update(self) -> None:
        certificate = self.coordinator.data.get(self.certificate_id)
        if certificate is not None and not certificate.managed:
            er.async_get(self.hass).async_remove(self.entity_id)
            return
        super()._handle_coordinator_update()

    @override
    async def async_press(self) -> None:
        await require_admin(self.hass, context := self._context)
        try:
            await self.coordinator.client.renew_certificate(
                self.certificate_id, context
            )
        except SslrpError as err:
            raise_command_error(err)
        await self.coordinator.async_request_refresh()
