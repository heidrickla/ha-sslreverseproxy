"""Sensors: proxy state and traffic, server health, certificate expiry."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import override

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import EntityCategory, UnitOfInformation, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import CertificateCoordinator, ProxyCoordinator, SslrpConfigEntry
from .entity import CertificateEntity, ProxyEntity, ServerEntity, track_items
from .models import Certificate, ProxyData, Server

# Reads come from the coordinators, which poll once for every entity.
PARALLEL_UPDATES = 0

type Value = str | int | float | datetime | None

PROXY_STATES = ["unknown", "stopped", "running", "faulted", "unavailable"]
SERVER_STATES = ["unknown", "active", "inactive", "error"]
CERTIFICATE_STATES = ["unknown", "valid", "expiring", "expired", "issuing", "failed"]


def _option(value: str, options: list[str]) -> str | None:
    """An enum state, or None for one this version does not know."""
    return value if value in options else None


@dataclass(frozen=True, kw_only=True)
class ProxySensorDescription(SensorEntityDescription):
    value_fn: Callable[[ProxyData], Value]


@dataclass(frozen=True, kw_only=True)
class ServerSensorDescription(SensorEntityDescription):
    value_fn: Callable[[Server], Value]


@dataclass(frozen=True, kw_only=True)
class CertificateSensorDescription(SensorEntityDescription):
    value_fn: Callable[[Certificate], Value]


def _ms(
    key: str, value_fn: Callable[[ProxyData], Value], *, enabled: bool = True
) -> ProxySensorDescription:
    return ProxySensorDescription(
        key=key,
        translation_key=key,
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.MILLISECONDS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        entity_registry_enabled_default=enabled,
        value_fn=value_fn,
    )


PROXY_SENSORS: tuple[ProxySensorDescription, ...] = (
    ProxySensorDescription(
        key="proxy_state",
        translation_key="proxy_state",
        device_class=SensorDeviceClass.ENUM,
        options=PROXY_STATES,
        value_fn=lambda d: _option(d.status.state, PROXY_STATES),
    ),
    ProxySensorDescription(
        key="active_rules",
        translation_key="active_rules",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: d.status.active_rules,
    ),
    ProxySensorDescription(
        key="requests",
        translation_key="requests",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: d.traffic.requests,
    ),
    ProxySensorDescription(
        key="in_flight",
        translation_key="in_flight",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: d.traffic.in_flight,
    ),
    ProxySensorDescription(
        key="server_errors",
        translation_key="server_errors",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: d.traffic.server_error,
    ),
    ProxySensorDescription(
        key="client_errors",
        translation_key="client_errors",
        state_class=SensorStateClass.MEASUREMENT,
        entity_registry_enabled_default=False,
        value_fn=lambda d: d.traffic.client_error,
    ),
    _ms("response_time", lambda d: d.traffic.avg_response_ms),
    _ms("response_time_p95", lambda d: d.traffic.p95_ms),
    _ms(
        "time_to_first_byte",
        lambda d: d.traffic.avg_ttfb_ms,
        enabled=False,
    ),
    ProxySensorDescription(
        key="data_received",
        translation_key="data_received",
        device_class=SensorDeviceClass.DATA_SIZE,
        native_unit_of_measurement=UnitOfInformation.BYTES,
        suggested_unit_of_measurement=UnitOfInformation.MEGABYTES,
        state_class=SensorStateClass.MEASUREMENT,
        entity_registry_enabled_default=False,
        value_fn=lambda d: d.traffic.bytes_in,
    ),
    ProxySensorDescription(
        key="data_sent",
        translation_key="data_sent",
        device_class=SensorDeviceClass.DATA_SIZE,
        native_unit_of_measurement=UnitOfInformation.BYTES,
        suggested_unit_of_measurement=UnitOfInformation.MEGABYTES,
        state_class=SensorStateClass.MEASUREMENT,
        entity_registry_enabled_default=False,
        value_fn=lambda d: d.traffic.bytes_out,
    ),
    ProxySensorDescription(
        key="api_key_expiry",
        translation_key="api_key_expiry",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.whoami.key.expires_at if d.whoami.key else None,
    ),
)

SERVER_SENSORS: tuple[ServerSensorDescription, ...] = (
    ServerSensorDescription(
        key="server_status",
        translation_key="server_status",
        device_class=SensorDeviceClass.ENUM,
        options=SERVER_STATES,
        value_fn=lambda s: _option(s.status, SERVER_STATES),
    ),
    ServerSensorDescription(
        key="upstreams_up",
        translation_key="upstreams_up",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda s: s.upstreams_up,
    ),
    ServerSensorDescription(
        key="upstreams",
        translation_key="upstreams",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda s: s.upstreams_total,
    ),
    ServerSensorDescription(
        key="rules",
        translation_key="rules",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda s: s.rule_count,
    ),
)

CERTIFICATE_SENSORS: tuple[CertificateSensorDescription, ...] = (
    CertificateSensorDescription(
        key="certificate_expiry",
        translation_key="certificate_expiry",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda c: c.expires_at,
    ),
    CertificateSensorDescription(
        key="certificate_status",
        translation_key="certificate_status",
        device_class=SensorDeviceClass.ENUM,
        options=CERTIFICATE_STATES,
        value_fn=lambda c: _option(c.status, CERTIFICATE_STATES),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SslrpConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    data = entry.runtime_data
    proxy, certificates = data.proxy, data.certificates
    async_add_entities(ProxySensor(proxy, d) for d in PROXY_SENSORS)

    def servers(server_id: str) -> Iterable[SensorEntity]:
        return (ServerSensor(proxy, server_id, d) for d in SERVER_SENSORS)

    def certs(certificate_id: str) -> Iterable[SensorEntity]:
        return (
            CertificateSensor(certificates, certificate_id, d)
            for d in CERTIFICATE_SENSORS
        )

    track_items(entry, proxy, lambda d: d.servers, servers, async_add_entities)
    track_items(entry, certificates, lambda d: d, certs, async_add_entities)


class ProxySensor(ProxyEntity, SensorEntity):
    entity_description: ProxySensorDescription

    def __init__(
        self, coordinator: ProxyCoordinator, description: ProxySensorDescription
    ) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    @override
    def native_value(self) -> Value:
        return self.entity_description.value_fn(self.data)


class ServerSensor(ServerEntity, SensorEntity):
    entity_description: ServerSensorDescription

    def __init__(
        self,
        coordinator: ProxyCoordinator,
        server_id: str,
        description: ServerSensorDescription,
    ) -> None:
        super().__init__(coordinator, server_id, description.key)
        self.entity_description = description

    @property
    @override
    def native_value(self) -> Value:
        return self.entity_description.value_fn(self.server)


class CertificateSensor(CertificateEntity, SensorEntity):
    entity_description: CertificateSensorDescription

    def __init__(
        self,
        coordinator: CertificateCoordinator,
        certificate_id: str,
        description: CertificateSensorDescription,
    ) -> None:
        super().__init__(coordinator, certificate_id, description.key)
        self.entity_description = description

    @property
    @override
    def native_value(self) -> Value:
        return self.entity_description.value_fn(self.certificate)
