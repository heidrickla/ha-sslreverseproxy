"""The models against answers recorded from a demo control plane."""

from __future__ import annotations

import copy
from datetime import UTC

import pytest

from tests.pure import body, load

models = load("models")


def test_instance() -> None:
    instance = models.Instance.from_json(body("instance.json"))
    assert instance.api_version == 1
    assert "scoped-api-keys" in instance.capabilities
    assert instance.instance_id == instance.instance_id.lower()


def test_instance_id_is_the_canonical_uuid() -> None:
    data = copy.deepcopy(body("instance.json"))
    expected = data["instanceId"]
    data["instanceId"] = expected.upper()
    assert models.Instance.from_json(data).instance_id == expected


@pytest.mark.parametrize("value", ["", " ", "not-a-uuid", "f2ab8614"])
def test_an_instance_id_that_is_not_a_uuid_is_refused(value: str) -> None:
    data = copy.deepcopy(body("instance.json"))
    data["instanceId"] = value
    with pytest.raises(models.ModelError, match="instanceId"):
        models.Instance.from_json(data)


def test_whoami_home_assistant_key() -> None:
    who = models.WhoAmI.from_json(body("whoami_home_assistant.json"))
    assert who.permissions == {"ServerRead", "CertRead", "ProxyReload", "CertRenew"}
    assert who.key is not None
    assert who.key.name == "Home Assistant"
    assert who.key.expires_at is not None and who.key.expires_at.utcoffset() is not None
    assert who.key.scopes == ("ServerRead", "CertRead", "ProxyReload", "CertRenew")


def test_whoami_without_key_or_scopes() -> None:
    data = copy.deepcopy(body("whoami_read_only.json"))
    data["key"]["scopes"] = None
    data["key"]["expiresAt"] = None
    who = models.WhoAmI.from_json(data)
    assert who.key.scopes is None and who.key.expires_at is None
    data.pop("key")
    assert models.WhoAmI.from_json(data).key is None


def test_status_lowercases_the_state() -> None:
    status = models.ProxyStatus.from_json(body("status.json"))
    assert status.state == "running"
    assert status.engine == "caddy"
    assert status.active_rules == 5
    assert status.started_at is not None


def test_servers_keyed_by_id() -> None:
    servers = models.Server.list_from_json(body("servers.json"))
    assert len(servers) == 3
    by_name = {s.name: s for s in servers.values()}
    internal = by_name["Internal Services"]
    assert internal.status == "error"
    assert (internal.upstreams_up, internal.upstreams_total) == (1, 2)
    assert all(key == server.id for key, server in servers.items())


def test_traffic() -> None:
    traffic = models.Traffic.from_json(body("traffic.json"))
    assert traffic.proxy_running is True
    assert traffic.requests == 18
    assert traffic.redirect == 15 and traffic.client_error == 3
    assert isinstance(traffic.p95_ms, float)


def test_traffic_with_nothing_to_average() -> None:
    data = copy.deepcopy(body("traffic.json"))
    for key in ("inFlight", "avgTtfbMs", "avgResponseMs", "p95Ms"):
        data[key] = None
    traffic = models.Traffic.from_json(data)
    assert traffic.in_flight is None and traffic.p95_ms is None


def test_certificates() -> None:
    certs = models.Certificate.list_from_json(body("certificates.json"))
    cert = next(c for c in certs.values() if c.domain == "api.example.com")
    assert cert.status == "valid"
    assert cert.managed is True and cert.renew_pending is False
    assert cert.expires_at is not None and cert.expires_at.tzinfo is not None


def test_renew_answer_is_a_certificate() -> None:
    cert = models.Certificate.from_json(body("renew.json"))
    assert cert.domain == "api.example.com"


@pytest.mark.parametrize(
    ("factory", "fixture", "key", "value"),
    [
        ("Instance", "instance.json", "apiVersion", "1"),
        ("Instance", "instance.json", "apiVersion", True),
        ("ProxyStatus", "status.json", "state", None),
        ("ProxyStatus", "status.json", "startedAt", "yesterday"),
        ("ProxyStatus", "status.json", "startedAt", "2026-10-21T04:49:26"),
        ("Traffic", "traffic.json", "proxyRunning", "true"),
        ("Traffic", "traffic.json", "p95Ms", "5"),
        ("WhoAmI", "whoami_read_only.json", "permissions", ["ServerRead", 1]),
    ],
)
def test_wrong_types_are_refused(
    factory: str, fixture: str, key: str, value: object
) -> None:
    data = copy.deepcopy(body(fixture))
    data[key] = value
    with pytest.raises(models.ModelError):
        getattr(models, factory).from_json(data)


@pytest.mark.parametrize("data", [None, [], "x"])
def test_not_an_object(data: object) -> None:
    with pytest.raises(models.ModelError):
        models.Instance.from_json(data)
    with pytest.raises(models.ModelError):
        models.Server.list_from_json({"servers": []})


def test_times_keep_their_offset() -> None:
    status = models.ProxyStatus.from_json(body("status.json"))
    assert status.started_at.astimezone(UTC).utcoffset().total_seconds() == 0
