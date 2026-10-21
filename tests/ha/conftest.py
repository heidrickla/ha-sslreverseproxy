"""Fixtures for the Home Assistant layer tests.

The real client runs against aioclient_mock, which answers with the bodies
recorded from a demo control plane. Kept in its own directory so its autouse
fixtures do not reach the pure suite.
"""

from __future__ import annotations

import copy
from collections.abc import Callable
from datetime import timedelta
from typing import Any

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.const import CONF_API_KEY, CONF_URL, CONF_VERIFY_SSL
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
)

from custom_components.sslreverseproxy.const import DOMAIN
from tests.pure import body, recorded

URL = "https://proxy.example.com"
KEY = "sslrp_ha_test_key"
INSTANCE_ID = body("instance.json")["instanceId"]

ENTRY_DATA = {CONF_URL: URL, CONF_API_KEY: KEY, CONF_VERIFY_SSL: True}

READS = {
    "/api/instance": "instance.json",
    "/api/whoami": "whoami_home_assistant.json",
    "/api/proxy/status": "status.json",
    "/api/servers": "servers.json",
    "/api/proxy/traffic": "traffic.json",
    "/api/certificates": "certificates.json",
}


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    return


class Api:
    """Scripted answers for one control plane, edited per test."""

    def __init__(self, mocker: AiohttpClientMocker) -> None:
        self.mocker = mocker
        self.answers: dict[tuple[str, str], tuple[int, Any]] = {
            ("GET", path): recorded(name) for path, name in READS.items()
        }
        self.answers[("POST", "/api/proxy/reload")] = recorded("reload.json")
        renew = recorded("renew.json")
        self.answers[("POST", f"/api/certificates/{renew[1]['id']}/renew")] = renew
        # A fresh key expiry, so no repair issue unless a test asks for one.
        self.set_key_expiry(dt_util.utcnow() + timedelta(days=30))
        self.install()

    def set(self, method: str, path: str, status: int, data: Any) -> None:
        self.answers[(method, path)] = (status, data)
        self.install()

    def edit(self, path: str, change: Callable[[Any], Any]) -> None:
        status, data = self.answers[("GET", path)]
        data = copy.deepcopy(data)
        result = change(data)
        self.answers[("GET", path)] = (status, data if result is None else result)
        self.install()

    def set_key_expiry(self, when: Any) -> None:
        status, data = self.answers[("GET", "/api/whoami")]
        data = copy.deepcopy(data)
        data["key"]["expiresAt"] = None if when is None else when.isoformat()
        self.answers[("GET", "/api/whoami")] = (status, data)

    def fail_all(self, exc: type[BaseException]) -> None:
        self.mocker.clear_requests()
        for method, path in self.answers:
            self.mocker.request(method, URL + path, exc=exc)

    def install(self) -> None:
        self.mocker.clear_requests()
        for (method, path), (status, data) in self.answers.items():
            self.mocker.request(method, URL + path, status=status, json=data)

    def calls(self, method: str, path: str) -> list[Any]:
        return [
            call
            for call in self.mocker.mock_calls
            if call[0] == method and str(call[1]) == URL + path
        ]


@pytest.fixture
def api(aioclient_mock: AiohttpClientMocker) -> Api:
    return Api(aioclient_mock)


@pytest.fixture
def entry(hass: HomeAssistant) -> MockConfigEntry:
    config = MockConfigEntry(
        domain=DOMAIN,
        title="proxy.example.com",
        data=dict(ENTRY_DATA),
        unique_id=INSTANCE_ID,
    )
    config.add_to_hass(hass)
    return config


@pytest.fixture
async def loaded(
    hass: HomeAssistant, api: Api, entry: MockConfigEntry
) -> MockConfigEntry:
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry
