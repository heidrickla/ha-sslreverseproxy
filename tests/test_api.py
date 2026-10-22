"""The client against a local HTTP server answering with recorded bodies."""

from __future__ import annotations

import ast
import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any

import aiohttp
import pytest
from aiohttp import web

from tests.pure import COMPONENT, load, recorded

api = load("api")

KEY = "sslrp_test_key_not_a_real_one"
DOTTED_KEY = "srp.Ab3dEf9h.s3cr3t-part-of-a-test-key-0123456789abcdef"
CALLER = object()


async def _allow(caller: object) -> None:
    assert caller is CALLER


type Routes = dict[
    tuple[str, str],
    tuple[int, Any] | Callable[[web.Request], Awaitable[web.StreamResponse]],
]


def _run(
    routes: Routes,
    call: Callable[[Any], Awaitable[Any]],
    *,
    url_suffix: str = "",
    key: str = KEY,
    authorize: Callable[[object], Awaitable[None]] | None = _allow,
) -> tuple[Any, list[web.Request]]:
    """Serve routes on a free port; return call(client)'s result and the requests."""
    seen: list[web.Request] = []

    async def handler(request: web.Request) -> web.StreamResponse:
        seen.append(request)
        answer = routes.get((request.method, request.path))
        if answer is None:
            return web.Response(status=404)
        if callable(answer):
            return await answer(request)
        status, data = answer
        return web.Response(
            status=status, text=json.dumps(data), content_type="application/json"
        )

    async def main() -> Any:
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = runner.addresses[0][1]
        try:
            async with aiohttp.ClientSession() as session:
                client = api.SslrpClient(
                    session, f"http://127.0.0.1:{port}{url_suffix}", key, authorize
                )
                return await call(client)
        finally:
            await runner.cleanup()

    return asyncio.run(main()), seen


def test_reads_send_the_key_header_and_parse() -> None:
    routes: Routes = {
        ("GET", "/api/instance"): recorded("instance.json"),
        ("GET", "/api/whoami"): recorded("whoami_home_assistant.json"),
        ("GET", "/api/proxy/status"): recorded("status.json"),
        ("GET", "/api/servers"): recorded("servers.json"),
        ("GET", "/api/proxy/traffic"): recorded("traffic.json"),
        ("GET", "/api/certificates"): recorded("certificates.json"),
    }

    async def call(client: Any) -> Any:
        return (
            await client.get_instance(),
            await client.get_whoami(),
            await client.get_status(),
            await client.get_servers(),
            await client.get_traffic(),
            await client.get_certificates(),
        )

    (instance, who, status, servers, traffic, certs), seen = _run(routes, call)
    assert instance.api_version == 1
    assert who.key.name == "Home Assistant"
    assert status.state == "running"
    assert len(servers) == 3 and len(certs) >= 1
    assert traffic.requests == 18
    assert {r.headers.get("X-Api-Key") for r in seen} == {KEY}
    assert all("Authorization" not in r.headers for r in seen)


def test_reload_posts_and_returns_status() -> None:
    routes: Routes = {("POST", "/api/proxy/reload"): recorded("reload.json")}
    status, seen = _run(routes, lambda c: c.reload(CALLER))
    assert status.state == "running"
    assert seen[0].method == "POST"


def test_renew_posts_to_the_certificate() -> None:
    cert_id = recorded("renew.json")[1]["id"]
    routes: Routes = {
        ("POST", f"/api/certificates/{cert_id}/renew"): recorded("renew.json")
    }
    cert, seen = _run(routes, lambda c: c.renew_certificate(cert_id, CALLER))
    assert cert.domain == "api.example.com"
    assert seen[0].path == f"/api/certificates/{cert_id}/renew"


@pytest.mark.parametrize(
    ("fixture", "method", "error"),
    [
        ("whoami_bad_key.json", "get_whoami", "SslrpAuthError"),
        ("certificates_servers_only.json", "get_certificates", "SslrpPermissionError"),
        ("reload_read_only.json", "reload", "SslrpPermissionError"),
    ],
)
def test_refusals_map_to_their_errors(fixture: str, method: str, error: str) -> None:
    path = {
        "get_whoami": ("GET", "/api/whoami"),
        "get_certificates": ("GET", "/api/certificates"),
        "reload": ("POST", "/api/proxy/reload"),
    }[method]
    with pytest.raises(getattr(api, error)) as caught:
        args = (CALLER,) if path[0] == "POST" else ()
        _run({path: recorded(fixture)}, lambda c: getattr(c, method)(*args))
    assert KEY not in str(caught.value)


@pytest.mark.parametrize("status", [400, 401, 403, 404, 409, 500])
@pytest.mark.parametrize("echoed", [DOTTED_KEY, DOTTED_KEY.rpartition(".")[2]])
def test_server_text_never_carries_the_key(status: int, echoed: str) -> None:
    routes: Routes = {
        ("GET", "/api/instance"): (status, {"message": f"refused {echoed} here"})
    }
    with pytest.raises(api.SslrpError) as caught:
        _run(routes, lambda c: c.get_instance(), key=DOTTED_KEY)
    assert "refused [key] here" in str(caught.value)
    assert DOTTED_KEY.rpartition(".")[2] not in str(caught.value)


def test_a_certificate_id_is_one_escaped_path_segment() -> None:
    cert_id = "../proxy/reload"
    routes: Routes = {
        ("POST", "/api/certificates/../proxy/reload/renew"): recorded("renew.json")
    }
    _cert, seen = _run(routes, lambda c: c.renew_certificate(cert_id, CALLER))
    assert seen[0].raw_path == "/api/certificates/..%2Fproxy%2Freload/renew"


def test_a_renewal_refusal_carries_the_reason() -> None:
    routes: Routes = {
        ("POST", "/api/certificates/x/renew"): recorded("renew_refused.json")
    }
    with pytest.raises(api.SslrpRefusedError, match="already in progress"):
        _run(routes, lambda c: c.renew_certificate("x", CALLER))


def test_unknown_certificate_is_not_found() -> None:
    routes: Routes = {
        ("POST", "/api/certificates/x/renew"): recorded("renew_unknown.json")
    }
    with pytest.raises(api.SslrpNotFoundError):
        _run(routes, lambda c: c.renew_certificate("x", CALLER))


def test_a_server_error_is_a_response_error() -> None:
    routes: Routes = {("GET", "/api/instance"): (500, {"title": "boom"})}
    with pytest.raises(api.SslrpResponseError, match="500: boom"):
        _run(routes, lambda c: c.get_instance())


def test_a_body_that_does_not_parse_is_a_response_error() -> None:
    async def html(_: web.Request) -> web.StreamResponse:
        return web.Response(text="<html>sign in</html>", content_type="text/html")

    with pytest.raises(api.SslrpResponseError, match="unexpected answer"):
        _run({("GET", "/api/instance"): html}, lambda c: c.get_instance())


def test_a_redirect_is_not_followed() -> None:
    async def redirect(_: web.Request) -> web.StreamResponse:
        return web.Response(
            status=302, headers={"Location": "https://sso.example.com/"}
        )

    with pytest.raises(api.SslrpResponseError, match="302"):
        _run({("GET", "/api/instance"): redirect}, lambda c: c.get_instance())


def test_a_path_prefix_is_kept() -> None:
    routes: Routes = {("GET", "/sslrp/api/instance"): recorded("instance.json")}
    instance, seen = _run(routes, lambda c: c.get_instance(), url_suffix="/sslrp/api/")
    assert instance.api_version == 1
    assert seen[0].path == "/sslrp/api/instance"


def test_connection_refused_is_a_connection_error() -> None:
    async def main() -> None:
        async with aiohttp.ClientSession() as session:
            # Port 9 (discard) on loopback: nothing listens there.
            client = api.SslrpClient(session, "http://127.0.0.1:9", KEY)
            await client.get_instance()

    with pytest.raises(api.SslrpConnectionError) as caught:
        asyncio.run(main())
    assert KEY not in str(caught.value)


def test_a_timeout_is_a_connection_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(api, "REQUEST_TIMEOUT", aiohttp.ClientTimeout(total=0.2))

    async def slow(_: web.Request) -> web.StreamResponse:
        await asyncio.sleep(2)
        return web.Response(status=200)

    with pytest.raises(api.SslrpConnectionError, match="timed out"):
        _run({("GET", "/api/instance"): slow}, lambda c: c.get_instance())


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://proxy.example.com", "https://proxy.example.com"),
        (" https://proxy.example.com/ ", "https://proxy.example.com"),
        ("https://proxy.example.com/api", "https://proxy.example.com"),
        ("https://proxy.example.com/api/", "https://proxy.example.com"),
        ("http://proxy.example.com:8080/sslrp", "http://proxy.example.com:8080/sslrp"),
    ],
)
def test_normalise_url(url: str, expected: str) -> None:
    assert api.normalise_url(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "proxy.example.com",
        "ftp://proxy.example.com",
        "https://",
        "https://user:pw@proxy.example.com",
        "https://proxy.example.com/?a=1",
        "https://proxy.example.com/#x",
    ],
)
def test_normalise_url_refuses(url: str) -> None:
    with pytest.raises(ValueError):
        api.normalise_url(url)


def test_a_write_is_authorized_before_it_is_sent() -> None:
    order: list[str] = []

    async def authorize(caller: object) -> None:
        assert caller is CALLER
        order.append("authorized")

    async def reload(request: web.Request) -> web.StreamResponse:
        order.append("sent")
        return web.json_response(recorded("reload.json")[1])

    _run(
        {("POST", "/api/proxy/reload"): reload},
        lambda c: c.reload(CALLER),
        authorize=authorize,
    )
    assert order == ["authorized", "sent"]


def test_a_refused_write_sends_nothing() -> None:
    hits: list[str] = []

    async def refuse(caller: object) -> None:
        raise PermissionError("not this caller")

    async def answer(request: web.Request) -> web.StreamResponse:
        hits.append(request.path)
        return web.json_response({})

    routes: Routes = {
        ("POST", "/api/proxy/reload"): answer,
        ("POST", "/api/certificates/x/renew"): answer,
    }
    for write in (
        lambda c: c.reload(CALLER),
        lambda c: c.renew_certificate("x", CALLER),
    ):
        with pytest.raises(PermissionError):
            _run(routes, write, authorize=refuse)
    assert hits == []


def test_a_client_without_an_authorizer_only_reads() -> None:
    routes: Routes = {
        ("GET", "/api/instance"): recorded("instance.json"),
        ("POST", "/api/proxy/reload"): recorded("reload.json"),
    }
    _, seen = _run(routes, lambda c: c.get_instance(), authorize=None)
    assert [r.method for r in seen] == ["GET"]
    with pytest.raises(RuntimeError, match="only reads"):
        _run(routes, lambda c: c.reload(CALLER), authorize=None)


def _session_uses(tree: ast.AST) -> set[str]:
    """The functions of a module that touch `self._session`."""
    found: set[str] = set()
    for fn in ast.walk(tree):
        if isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef):
            for node in ast.walk(fn):
                if isinstance(node, ast.Attribute) and node.attr == "_session":
                    found.add(fn.name)
    return found


def test_every_request_leaves_through_one_method() -> None:
    """Only `_request` sends; elsewhere a session is only handed to the client."""
    client = ast.parse((COMPONENT / "api.py").read_text(encoding="utf-8"))
    assert _session_uses(client) == {"__init__", "_request"}
    for path in COMPONENT.glob("*.py"):
        if path.name == "api.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        assert not _session_uses(tree), path.name
        for node in ast.walk(tree):
            if isinstance(node, ast.Import | ast.ImportFrom):
                names = [a.name for a in node.names] + [
                    getattr(node, "module", "") or ""
                ]
                assert not any(n.split(".")[0] == "aiohttp" for n in names), path.name
        sessions = {
            t.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Call)
            and getattr(node.value.func, "id", None) == "async_get_clientsession"
            for t in node.targets
            if isinstance(t, ast.Name)
        }
        handed = {
            id(arg)
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and getattr(node.func, "id", None) == "SslrpClient"
            for arg in node.args
        }
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Name)
                and node.id in sessions
                and isinstance(node.ctx, ast.Load)
            ):
                assert id(node) in handed, f"{path.name}:{node.lineno}"
