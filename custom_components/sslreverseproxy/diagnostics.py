"""Downloadable diagnostics. The key, addresses, names and domains are redacted."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_API_KEY, CONF_URL
from homeassistant.core import HomeAssistant

from .coordinator import SslrpConfigEntry
from .models import ProxyData

# Diagnostics files end up in public issue trackers.
REDACT = {
    CONF_API_KEY,
    CONF_URL,
    "instance_id",
    "id",
    "name",
    "host",
    "domain",
    "message",
    "last_error",
}


def _plain(value: Any) -> Any:
    """Tuples, sets and times to the lists and strings the redactor walks."""
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, set | frozenset):
        return sorted(_plain(v) for v in value)
    if isinstance(value, list | tuple):
        return [_plain(v) for v in value]
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _proxy(data: ProxyData) -> dict[str, Any]:
    # Servers are keyed by id; as a list the ids sit under "id" and are redacted.
    plain: dict[str, Any] = _plain(asdict(data))
    plain["servers"] = list(plain["servers"].values())
    return plain


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: SslrpConfigEntry
) -> dict[str, Any]:
    runtime = entry.runtime_data
    proxy = runtime.proxy.data
    certificates = runtime.certificates.data or {}
    return {
        "config": async_redact_data(dict(entry.data), REDACT),
        "instance": async_redact_data(_plain(asdict(runtime.instance)), REDACT),
        "last_update_success": {
            "proxy": runtime.proxy.last_update_success,
            "certificates": runtime.certificates.last_update_success,
        },
        "proxy": async_redact_data(_proxy(proxy), REDACT) if proxy else {},
        "certificates": [
            async_redact_data(_plain(asdict(c)), REDACT) for c in certificates.values()
        ],
    }
