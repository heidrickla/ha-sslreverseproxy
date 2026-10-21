"""Entity bases for the proxy, its servers and its certificates."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from typing import NoReturn, override

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Context, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError, Unauthorized
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
)

from .api import SslrpAuthError, SslrpError, SslrpPermissionError, SslrpRefusedError
from .const import DOMAIN, MANUFACTURER
from .coordinator import (
    KIND_CERTIFICATE,
    KIND_SERVER,
    CertificateCoordinator,
    ProxyCoordinator,
    child_identifier,
    instance_identifier,
)
from .models import Certificate, ProxyData, Server


def raise_command_error(err: SslrpError) -> NoReturn:
    """A failed Reload or Renew as a translated error."""
    placeholders = {"error": str(err)}
    if isinstance(err, SslrpAuthError):
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="auth_failed",
            translation_placeholders=placeholders,
        ) from err
    if isinstance(err, SslrpPermissionError):
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="missing_permissions",
            translation_placeholders=placeholders,
        ) from err
    if isinstance(err, SslrpRefusedError):
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="refused",
            translation_placeholders=placeholders,
        ) from err
    raise HomeAssistantError(
        translation_domain=DOMAIN,
        translation_key="command_failed",
        translation_placeholders=placeholders,
    ) from err


async def require_admin(hass: HomeAssistant, context: Context | None) -> None:
    """Refuse a signed-in user who is not an administrator.

    A call with no user (an automation or script run by the system) passes.
    """
    if context is None or context.user_id is None:
        return
    user = await hass.auth.async_get_user(context.user_id)
    if user is None or not user.is_admin:
        raise Unauthorized(context=context)


def write_authorizer(hass: HomeAssistant) -> Callable[[object], Awaitable[None]]:
    """The client's check before every control plane write.

    The caller must be the call's own Context: an administrator, or no user at
    all (an automation or script). Anything else is refused.
    """

    async def authorize(caller: object) -> None:
        if not isinstance(caller, Context):
            raise Unauthorized()
        await require_admin(hass, caller)

    return authorize


@callback
def track_items[T](
    entry: ConfigEntry,
    coordinator: DataUpdateCoordinator[T],
    ids: Callable[[T], Iterable[str]],
    make: Callable[[str], Iterable[Entity]],
    add: AddConfigEntryEntitiesCallback,
) -> None:
    """Add entities for each server or certificate as the API starts listing it.

    An id that disappears is forgotten, so one that comes back is added again
    after its device was removed.
    """
    known: set[str] = set()

    @callback
    def _update() -> None:
        current = set(ids(coordinator.data))
        known.intersection_update(current)
        new = sorted(current - known)
        known.update(new)
        entities = [entity for item_id in new for entity in make(item_id)]
        if entities:
            add(entities)

    _update()
    entry.async_on_unload(coordinator.async_add_listener(_update))


def proxy_device_info(coordinator: ProxyCoordinator) -> DeviceInfo:
    status = coordinator.data.status if coordinator.data else None
    return DeviceInfo(
        identifiers={instance_identifier(coordinator.instance.instance_id)},
        manufacturer=MANUFACTURER,
        name=coordinator.config_entry.title,
        model=status.engine if status else None,
        entry_type=DeviceEntryType.SERVICE,
        configuration_url=coordinator.client.base_url,
    )


class ProxyEntity(CoordinatorEntity[ProxyCoordinator]):
    """An entity of the proxy itself."""

    _attr_has_entity_name = True
    coordinator: ProxyCoordinator

    def __init__(self, coordinator: ProxyCoordinator, key: str) -> None:
        super().__init__(coordinator)
        self._attr_translation_key = key
        self._attr_unique_id = f"{coordinator.instance.instance_id}_{key}"
        self._attr_device_info = proxy_device_info(coordinator)

    @property
    def data(self) -> ProxyData:
        data: ProxyData = self.coordinator.data
        return data


def _instance_device_id(
    coordinator: ProxyCoordinator | CertificateCoordinator,
) -> str:
    """The instance device, which setup registers before the platforms."""
    return dr.async_get_device_id_by_identifier(
        coordinator.hass,
        instance_identifier(coordinator.instance.instance_id),
        config_entry_id=coordinator.config_entry.entry_id,
    )


class ServerEntity(CoordinatorEntity[ProxyCoordinator]):
    """An entity of one proxied server, unavailable once the API stops listing it."""

    _attr_has_entity_name = True
    coordinator: ProxyCoordinator

    def __init__(self, coordinator: ProxyCoordinator, server_id: str, key: str) -> None:
        super().__init__(coordinator)
        instance_id = coordinator.instance.instance_id
        self.server_id = server_id
        server = coordinator.data.servers[server_id]
        self._attr_translation_key = key
        self._attr_unique_id = f"{instance_id}_{KIND_SERVER}_{server_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={child_identifier(instance_id, KIND_SERVER, server_id)},
            manufacturer=MANUFACTURER,
            name=server.name,
            model=server.os,
            via_device_id=_instance_device_id(coordinator),
        )

    @property
    def server(self) -> Server:
        return self.coordinator.data.servers[self.server_id]

    @property
    @override
    def available(self) -> bool:
        return super().available and self.server_id in self.coordinator.data.servers


class CertificateEntity(CoordinatorEntity[CertificateCoordinator]):
    """An entity of one certificate, unavailable once the API stops listing it."""

    _attr_has_entity_name = True
    coordinator: CertificateCoordinator

    def __init__(
        self, coordinator: CertificateCoordinator, certificate_id: str, key: str
    ) -> None:
        super().__init__(coordinator)
        instance_id = coordinator.instance.instance_id
        self.certificate_id = certificate_id
        certificate = coordinator.data[certificate_id]
        self._attr_translation_key = key
        self._attr_unique_id = (
            f"{instance_id}_{KIND_CERTIFICATE}_{certificate_id}_{key}"
        )
        self._attr_device_info = DeviceInfo(
            identifiers={
                child_identifier(instance_id, KIND_CERTIFICATE, certificate_id)
            },
            manufacturer=MANUFACTURER,
            name=certificate.domain,
            model=certificate.issuer,
            entry_type=DeviceEntryType.SERVICE,
            via_device_id=_instance_device_id(coordinator),
        )

    @property
    def certificate(self) -> Certificate:
        return self.coordinator.data[self.certificate_id]

    @property
    @override
    def available(self) -> bool:
        return super().available and self.certificate_id in self.coordinator.data
