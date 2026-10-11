"""Shared aiohttp session: one per process, recreated after close."""

from bot import http


async def test_session_is_singleton():
    s1 = http.get_session()
    s2 = http.get_session()
    assert s1 is s2
    await http.close_session()


async def test_close_and_recreate():
    s1 = http.get_session()
    await http.close_session()
    assert s1.closed
    s2 = http.get_session()
    assert s2 is not s1
    assert not s2.closed
    await http.close_session()


# ── post_with_retry: повтор на 429 ────────────────────────────────

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer


async def _server(statuses, retry_after="0"):
    """Локальный сервер: отдаёт статусы по очереди, считает обращения."""
    calls = []

    async def handler(request):
        calls.append(1)
        status = statuses[min(len(calls) - 1, len(statuses) - 1)]
        headers = {"retry-after": retry_after} if status == 429 else {}
        return web.json_response({"n": len(calls)}, status=status, headers=headers)

    app = web.Application()
    app.router.add_post("/", handler)
    server = TestServer(app)
    await server.start_server()
    return server, calls


@pytest.mark.parametrize("statuses, expected_status, expected_calls", [
    ([200], 200, 1),
    ([429, 200], 200, 2),
    ([429, 429, 200], 200, 3),
    ([429, 429, 429, 429, 200], 429, 4),   # retries=3 → сдаёмся, отдаём 429 как есть
    ([404, 200], 404, 1),             # не 429 — без повтора
])
async def test_post_with_retry(statuses, expected_status, expected_calls):
    server, calls = await _server(statuses)
    try:
        async with http.post_with_retry(str(server.make_url("/")), max_delay=0, json={}) as resp:
            assert resp.status == expected_status
            if resp.status == 200:
                assert (await resp.json())["n"] == expected_calls
        assert len(calls) == expected_calls
    finally:
        await server.close()
        await http.close_session()


def test_retry_delay():
    assert http._retry_delay({}, 0, 30) == 5
    assert http._retry_delay({}, 2, 30) == 20
    assert http._retry_delay({}, 3, 30) == 30
    assert http._retry_delay({"retry-after": "3"}, 0, 30) == 5
    assert http._retry_delay({"retry-after": "12"}, 0, 30) == 12
    assert http._retry_delay({"retry-after": "junk"}, 1, 30) == 10
