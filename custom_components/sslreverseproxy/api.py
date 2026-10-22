"""Async client for the SSLReverseProxy control plane API. No Home Assistant imports."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

import aiohttp

from .models import (
    Certificate,
    Instance,
    ModelError,
    ProxyStatus,
    Server,
    Traffic,
    WhoAmI,
)

API_KEY_HEADER = "X-Api-Key"
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=15)


class SslrpError(Exception):
    """Base for every client error. Messages never carry the key."""


class SslrpConnectionError(SslrpError):
    """The API did not answer: refused, timed out, TLS or DNS failure."""


class SslrpAuthError(SslrpError):
    """401: the key is unknown, revoked or expired."""


class SslrpPermissionError(SslrpError):
    """403: the key is valid but lacks the permission."""


class SslrpNotFoundError(SslrpError):
    """404: the thing named is gone."""


class SslrpRefusedError(SslrpError):
    """409: the API refused the request and said why."""


class SslrpResponseError(SslrpError):
    """Any other status, or a body that does not parse."""


def normalise_url(url: str) -> str:
    """The control plane's base URL, or ValueError.

    http or https with a host; no credentials, query or fragment. A trailing
    slash and a pasted `/api` suffix are dropped.
    """
    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("not an http(s) URL with a host")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError("URL carries credentials, a query or a fragment")
    path = parts.path.rstrip("/")
    path = path.removesuffix("/api")
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def _message(body: Any) -> str | None:
    """The reason in an error body: `message`, or a problem's detail or title."""
    if isinstance(body, Mapping):
        for key in ("message", "detail", "title"):
            value = body.get(key)
            if isinstance(value, str) and value:
                return value
    return None


type WriteAuthorizer = Callable[[object], Awaitable[None]]


class SslrpClient:
    """One control plane, reached with one API key.

    Every request leaves through `_request`. A write first awaits `authorize_write`
    with its caller, which raises to refuse; without one the client only reads.
    """

    def __init__(
        self,
        session: aiohttp.ClientSession,
        url: str,
        api_key: str,
        authorize_write: WriteAuthorizer | None = None,
    ) -> None:
        self._session = session
        self.base_url = normalise_url(url)
        self._api_key = api_key
        self._authorize_write = authorize_write

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: Mapping[str, Any] | None = None,
        caller: object = None,
    ) -> Any:
        if method != "GET":
            if self._authorize_write is None:
                raise RuntimeError(f"{method} {path}: this client only reads")
            await self._authorize_write(caller)
        try:
            async with self._session.request(
                method,
                f"{self.base_url}{path}",
                headers={API_KEY_HEADER: self._api_key, "Accept": "application/json"},
                json=json,
                timeout=REQUEST_TIMEOUT,
                allow_redirects=False,
            ) as response:
                status = response.status
                try:
                    body: Any = await response.json(content_type=None)
                except ValueError:
                    body = None
        except TimeoutError as err:
            raise SslrpConnectionError(f"{method} {path} timed out") from err
        except aiohttp.ClientError as err:
            raise SslrpConnectionError(
                f"{method} {path}: {type(err).__name__}"
            ) from err

        if 200 <= status < 300:
            return body
        reason = _message(body)
        if reason:
            reason = self._redact(reason)
        detail = f"{method} {path} answered {status}" + (
            f": {reason}" if reason else ""
        )
        if status == 401:
            raise SslrpAuthError(detail)
        if status == 403:
            raise SslrpPermissionError(detail)
        if status == 404:
            raise SslrpNotFoundError(detail)
        if status == 409:
            raise SslrpRefusedError(reason or detail)
        raise SslrpResponseError(detail)

    def _redact(self, text: str) -> str:
        """Server text with the key and its secret part taken out."""
        secret = self._api_key.rpartition(".")[2]
        for value in (self._api_key, secret):
            # A short value would also match ordinary words.
            if len(value) >= 8:
                text = text.replace(value, "[key]")
        return text

    async def _get(self, path: str) -> Any:
        return await self._request("GET", path)

    async def get_instance(self) -> Instance:
        return _parse(Instance.from_json, await self._get("/api/instance"))

    async def get_whoami(self) -> WhoAmI:
        return _parse(WhoAmI.from_json, await self._get("/api/whoami"))

    async def get_status(self) -> ProxyStatus:
        return _parse(ProxyStatus.from_json, await self._get("/api/proxy/status"))

    async def get_servers(self) -> dict[str, Server]:
        return _parse(Server.list_from_json, await self._get("/api/servers"))

    async def get_traffic(self) -> Traffic:
        return _parse(Traffic.from_json, await self._get("/api/proxy/traffic"))

    async def get_certificates(self) -> dict[str, Certificate]:
        return _parse(Certificate.list_from_json, await self._get("/api/certificates"))

    async def reload(self, caller: object) -> ProxyStatus:
        """Push the stored rules to Caddy. Returns the status after the push."""
        body = await self._request("POST", "/api/proxy/reload", json={}, caller=caller)
        return _parse(ProxyStatus.from_json, body)

    async def renew_certificate(
        self, certificate_id: str, caller: object
    ) -> Certificate:
        """Ask Caddy to reissue a served certificate. 409 says why it cannot."""
        body = await self._request(
            "POST",
            f"/api/certificates/{quote(certificate_id, safe='')}/renew",
            caller=caller,
        )
        return _parse(Certificate.from_json, body)


def _parse[T](parse: Callable[[Any], T], body: Any) -> T:
    try:
        return parse(body)
    except ModelError as err:
        raise SslrpResponseError(f"unexpected answer: {err}") from err
