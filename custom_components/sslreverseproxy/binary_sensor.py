"""Binary sensors: proxy running, a server's upstreams down, a renewal pending."""

from __future__ import annotations

from collections.abc import Iterable
from typing import override

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.components.binary_sensor.const import BinarySensorDeviceClass
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import CertificateCoordinator, ProxyCoordinator, SslrpConfigEntry
from .entity import CertificateEntity, ProxyEntity, ServerEntity, track_items

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SslrpConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    data = entry.runtime_data
    proxy, certificates = data.proxy, data.certificates
    async_add_entities([ProxyRunning(proxy)])

    def servers(server_id: str) -> Iterable[BinarySensorEntity]:
        return [UpstreamProblem(proxy, server_id)]

    def certs(certificate_id: str) -> Iterable[BinarySensorEntity]:
        return [RenewalPending(certificates, certificate_id)]

    track_items(entry, proxy, lambda d: d.servers, servers, async_add_entities)
    track_items(entry, certificates, lambda d: d, certs, async_add_entities)


class ProxyRunning(ProxyEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.RUNNING

    def __init__(self, coordinator: ProxyCoordinator) -> None:
        super().__init__(coordinator, "running")

    @property
    @override
    def is_on(self) -> bool:
        return self.data.status.state == "running"


class UpstreamProblem(ServerEntity, BinarySensorEntity):
    """On while any upstream of the server's enabled rules fails its probe."""

    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(self, coordinator: ProxyCoordinator, server_id: str) -> None:
        super().__init__(coordinator, server_id, "upstream_problem")

    @property
    @override
    def is_on(self) -> bool | None:
        server = self.server
        if server.checked_at is None:
            return None
        return server.upstreams_up < server.upstreams_total


class RenewalPending(CertificateEntity, BinarySensorEntity):
    """On from a forced renewal until Caddy serves the new certificate."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self, coordinator: CertificateCoordinator, certificate_id: str
    ) -> None:
        super().__init__(coordinator, certificate_id, "renewal_pending")

    @property
    @override
    def is_on(self) -> bool:
        return self.certificate.renew_pending
