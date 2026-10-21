"""The API's answers as typed values. No Home Assistant imports."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


class ModelError(ValueError):
    """An answer is missing a field or holds the wrong type."""


def _obj(data: Any, what: str) -> Mapping[str, Any]:
    if not isinstance(data, Mapping):
        raise ModelError(f"{what} is not an object")
    return data


def _list(data: Any, what: str) -> list[Any]:
    if not isinstance(data, list):
        raise ModelError(f"{what} is not a list")
    return data


def _str(data: Mapping[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str):
        raise ModelError(f"{key} is not a string")
    return value


def _opt_str(data: Mapping[str, Any], key: str) -> str | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ModelError(f"{key} is not a string")
    return value


def _int(data: Mapping[str, Any], key: str) -> int:
    value = data.get(key)
    # bool is an int subclass and never a count.
    if not isinstance(value, int) or isinstance(value, bool):
        raise ModelError(f"{key} is not an integer")
    return value


def _opt_int(data: Mapping[str, Any], key: str) -> int | None:
    if data.get(key) is None:
        return None
    return _int(data, key)


def _opt_float(data: Mapping[str, Any], key: str) -> float | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, int | float) or isinstance(value, bool):
        raise ModelError(f"{key} is not a number")
    return float(value)


def _bool(data: Mapping[str, Any], key: str) -> bool:
    value = data.get(key)
    if not isinstance(value, bool):
        raise ModelError(f"{key} is not a boolean")
    return value


def _opt_time(data: Mapping[str, Any], key: str) -> datetime | None:
    value = _opt_str(data, key)
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as err:
        raise ModelError(f"{key} is not an ISO 8601 time") from err
    if parsed.tzinfo is None:
        raise ModelError(f"{key} has no UTC offset")
    return parsed


def _names(data: Mapping[str, Any], key: str) -> tuple[str, ...]:
    items = _list(data.get(key), key)
    if not all(isinstance(item, str) for item in items):
        raise ModelError(f"{key} holds a non-string")
    return tuple(items)


@dataclass(frozen=True, slots=True)
class Instance:
    """GET /api/instance: the installation's stable id and contract."""

    instance_id: str
    api_version: int
    capabilities: tuple[str, ...]

    @classmethod
    def from_json(cls, data: Any) -> Instance:
        d = _obj(data, "instance")
        return cls(
            instance_id=_str(d, "instanceId").lower(),
            api_version=_int(d, "apiVersion"),
            capabilities=_names(d, "capabilities"),
        )


@dataclass(frozen=True, slots=True)
class ApiKeyInfo:
    """The key a request signed in with. Null scopes follow the owner's role."""

    id: str
    name: str
    expires_at: datetime | None
    scopes: tuple[str, ...] | None

    @classmethod
    def from_json(cls, data: Any) -> ApiKeyInfo:
        d = _obj(data, "key")
        return cls(
            id=_str(d, "id").lower(),
            name=_str(d, "name"),
            expires_at=_opt_time(d, "expiresAt"),
            scopes=None if d.get("scopes") is None else _names(d, "scopes"),
        )


@dataclass(frozen=True, slots=True)
class WhoAmI:
    """GET /api/whoami. Permissions are the effective ones: role and scopes."""

    name: str
    role: str
    permissions: frozenset[str]
    key: ApiKeyInfo | None

    @classmethod
    def from_json(cls, data: Any) -> WhoAmI:
        d = _obj(data, "whoami")
        key = d.get("key")
        return cls(
            name=_str(d, "name"),
            role=_str(d, "role"),
            permissions=frozenset(_names(d, "permissions")),
            key=None if key is None else ApiKeyInfo.from_json(key),
        )


@dataclass(frozen=True, slots=True)
class ProxyStatus:
    """GET /api/proxy/status. State is lowercased: running, stopped, ..."""

    state: str
    engine: str
    active_rules: int
    started_at: datetime | None
    message: str | None

    @classmethod
    def from_json(cls, data: Any) -> ProxyStatus:
        d = _obj(data, "status")
        return cls(
            state=_str(d, "state").lower(),
            engine=_str(d, "engine"),
            active_rules=_int(d, "activeRuleCount"),
            started_at=_opt_time(d, "startedAt"),
            message=_opt_str(d, "message"),
        )


@dataclass(frozen=True, slots=True)
class Server:
    """One entry of GET /api/servers. Status is lowercased."""

    id: str
    name: str
    host: str
    os: str
    rule_count: int
    status: str
    upstreams_up: int
    upstreams_total: int
    checked_at: datetime | None

    @classmethod
    def from_json(cls, data: Any) -> Server:
        d = _obj(data, "server")
        return cls(
            id=_str(d, "id").lower(),
            name=_str(d, "name"),
            host=_str(d, "host"),
            os=_str(d, "os"),
            rule_count=_int(d, "ruleCount"),
            status=_str(d, "status").lower(),
            upstreams_up=_int(d, "upstreamsUp"),
            upstreams_total=_int(d, "upstreamsTotal"),
            checked_at=_opt_time(d, "statusCheckedAt"),
        )

    @classmethod
    def list_from_json(cls, data: Any) -> dict[str, Server]:
        servers = [cls.from_json(item) for item in _list(data, "servers")]
        return {server.id: server for server in servers}


@dataclass(frozen=True, slots=True)
class Traffic:
    """GET /api/proxy/traffic: the last 24 hours of stored samples."""

    sample_seconds: int
    proxy_running: bool
    samples: int
    requests: int
    bytes_in: int
    bytes_out: int
    in_flight: int | None
    avg_ttfb_ms: float | None
    avg_response_ms: float | None
    p95_ms: float | None
    success: int
    redirect: int
    client_error: int
    server_error: int

    @classmethod
    def from_json(cls, data: Any) -> Traffic:
        d = _obj(data, "traffic")
        status = _obj(d.get("status"), "status")
        return cls(
            sample_seconds=_int(d, "sampleSeconds"),
            proxy_running=_bool(d, "proxyRunning"),
            samples=_int(d, "samples"),
            requests=_int(d, "requests"),
            bytes_in=_int(d, "bytesIn"),
            bytes_out=_int(d, "bytesOut"),
            in_flight=_opt_int(d, "inFlight"),
            avg_ttfb_ms=_opt_float(d, "avgTtfbMs"),
            avg_response_ms=_opt_float(d, "avgResponseMs"),
            p95_ms=_opt_float(d, "p95Ms"),
            success=_int(status, "success"),
            redirect=_int(status, "redirect"),
            client_error=_int(status, "clientError"),
            server_error=_int(status, "serverError"),
        )


@dataclass(frozen=True, slots=True)
class Certificate:
    """One entry of GET /api/certificates. Status is lowercased."""

    id: str
    domain: str
    issuer: str
    status: str
    days_remaining: int | None
    issued_at: datetime | None
    expires_at: datetime | None
    managed: bool
    renew_pending: bool
    last_error: str | None

    @classmethod
    def from_json(cls, data: Any) -> Certificate:
        d = _obj(data, "certificate")
        return cls(
            id=_str(d, "id").lower(),
            domain=_str(d, "domain"),
            issuer=_str(d, "issuer"),
            status=_str(d, "status").lower(),
            days_remaining=_opt_int(d, "daysRemaining"),
            issued_at=_opt_time(d, "issuedAt"),
            expires_at=_opt_time(d, "expiresAt"),
            managed=_bool(d, "managed"),
            renew_pending=_bool(d, "renewPending"),
            last_error=_opt_str(d, "lastError"),
        )

    @classmethod
    def list_from_json(cls, data: Any) -> dict[str, Certificate]:
        certificates = [cls.from_json(item) for item in _list(data, "certificates")]
        return {certificate.id: certificate for certificate in certificates}


@dataclass(frozen=True, slots=True)
class ProxyData:
    """One poll of the fast coordinator."""

    status: ProxyStatus
    traffic: Traffic
    whoami: WhoAmI
    servers: dict[str, Server] = field(default_factory=dict)
