"""LLMClient: разбор «try again in», пауза после дневного лимита, пробный запрос, общий лимитер."""

import asyncio
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from bot import http, llm
from bot.llm import LLMClient, describe_downtime, is_daily_limit, parse_retry_in
from bot.models import VisionConfig

TPD_BODY = (
    '{"error":{"message":"Rate limit reached for model `qwen/qwen3.8-27b` in organization `org_x` '
    "service tier `on_demand` on tokens per day (TPD): Limit 200000, Used 199100, Requested 2300. "
    'Please try again in 7m12.3s. Need more tokens?","type":"tokens","code":"rate_limit_exceeded"}}'
)
TPM_BODY = (
    '{"error":{"message":"Rate limit reached for model `x` on tokens per minute (TPM): '
    'Limit 7000, Used 6500, Requested 2100. Please try again in 3.2s.","type":"tokens"}}'
)
OK_BODY = {"choices": [{"message": {"content": "ответ"}}]}


# ── разбор текста ошибки ────────────────────────────────────────────

@pytest.mark.parametrize("body, expected", [
    ("Please try again in 1h2m3.4s. Need", 3600 + 120 + 3.4),
    ("Please try again in 7m12.3s.", 432.3),
    ("Please try again in 45.6s.", 45.6),
    ("try again in 340ms", 0.34),
    ("Please try again in 2d1h", 2 * 86400 + 3600),
    ("try again in 12m", 720),
    ("ничего похожего", None),
    ("try again in soon", None),
    ("try again in 5q", None),
    ("", None),
])
def test_parse_retry_in(body, expected):
    got = parse_retry_in(body)
    assert got == pytest.approx(expected) if expected is not None else got is None


def test_is_daily_limit():
    assert is_daily_limit(TPD_BODY)
    assert is_daily_limit("requests per day (RPD): Limit 1000")
    assert not is_daily_limit(TPM_BODY)
    assert not is_daily_limit("")


# ── сервер-заглушка ─────────────────────────────────────────────────

class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


async def _server(script):
    """script: список (status, body|dict) по порядку обращений; последний повторяется."""
    calls = []

    async def handler(request):
        calls.append(await request.json())
        status, body = script[min(len(calls) - 1, len(script) - 1)]
        if isinstance(body, dict):
            return web.json_response(body, status=status, headers={"x-ratelimit-limit-tokens": "7000"})
        return web.Response(text=body, status=status, content_type="application/json")

    app = web.Application()
    app.router.add_post("/", handler)
    server = TestServer(app)
    await server.start_server()
    return server, calls


def _client(server, clock, minute_limit=100, sleeps=None):
    cfg = VisionConfig(api_key="k", base_url=str(server.make_url("/")), model="m")

    async def fake_sleep(seconds):
        if sleeps is not None:
            sleeps.append(seconds)

    return LLMClient(cfg, minute_limit=minute_limit, now=clock, sleep=fake_sleep)


async def _ask(client, kind="nlp"):
    return await client.chat([{"role": "user", "content": "hi"}], max_tokens=10, temperature=0.1, kind=kind)


@pytest.fixture
async def cleanup():
    yield
    await http.close_session()


async def test_success_returns_content_and_builds_request(cleanup):
    server, calls = await _server([(200, OK_BODY)])
    try:
        c = _client(server, Clock())
        got = await c.chat(
            [{"role": "user", "content": "hi"}], max_tokens=77, temperature=0.4,
            response_format={"type": "json_object"}, kind="menu",
        )
        assert got == "ответ"
        assert calls[0]["model"] == "m" and calls[0]["max_tokens"] == 77
        assert calls[0]["temperature"] == 0.4 and calls[0]["response_format"] == {"type": "json_object"}
        assert llm.rate_info["limit_tokens"] == "7000"
    finally:
        await server.close()


async def test_daily_429_blocks_without_retry_and_skips_network(cleanup):
    server, calls = await _server([(429, TPD_BODY)])
    clock = Clock()
    try:
        c = _client(server, clock)
        assert await _ask(c) is None
        assert len(calls) == 1                       # повторов нет
        assert c.is_blocked()
        assert c.blocked_until() == pytest.approx(clock.t + 432.3)
        for _ in range(5):                            # пока пауза — сети нет
            assert await _ask(c) is None
        assert len(calls) == 1
        assert c.daily_stats()["skipped"] == 5
    finally:
        await server.close()


async def test_daily_429_without_duration_blocks_ten_minutes(cleanup):
    server, calls = await _server([(429, "Rate limit on tokens per day (TPD) reached")])
    clock = Clock()
    try:
        c = _client(server, clock)
        await _ask(c)
        assert c.blocked_until() == pytest.approx(clock.t + 600)
    finally:
        await server.close()


async def test_probe_after_pause_success_clears_block(cleanup):
    server, calls = await _server([(429, TPD_BODY), (200, OK_BODY)])
    clock = Clock()
    try:
        c = _client(server, clock)
        await _ask(c)
        clock.t += 433
        assert not c.is_blocked()
        assert await _ask(c) == "ответ"              # пробный
        assert len(calls) == 2
        assert await _ask(c) == "ответ"              # дальше как обычно
        assert len(calls) == 3
    finally:
        await server.close()


async def test_probe_hits_daily_limit_again_new_pause(cleanup):
    server, calls = await _server([(429, TPD_BODY)])
    clock = Clock()
    try:
        c = _client(server, clock)
        await _ask(c)
        clock.t += 433
        assert await _ask(c) is None                 # пробный снова 429 TPD
        assert len(calls) == 2
        assert c.blocked_until() == pytest.approx(clock.t + 432.3)
        assert await _ask(c) is None
        assert len(calls) == 2                       # опять пауза, без сети
    finally:
        await server.close()


async def test_concurrent_calls_wait_for_single_probe(cleanup):
    gate = asyncio.Event()
    calls = []
    script = [(429, TPD_BODY), (200, OK_BODY)]

    async def handler(request):
        calls.append(1)
        status, body = script[min(len(calls) - 1, len(script) - 1)]
        if len(calls) == 2:
            await gate.wait()                         # пробный висит, пока не отпустим
        if isinstance(body, dict):
            return web.json_response(body, status=status)
        return web.Response(text=body, status=status)

    app = web.Application()
    app.router.add_post("/", handler)
    server = TestServer(app)
    await server.start_server()
    clock = Clock()
    try:
        c = _client(server, clock)
        await _ask(c)
        clock.t += 433
        tasks = [asyncio.create_task(_ask(c)) for _ in range(4)]
        await asyncio.sleep(0.2)
        assert len(calls) == 2                        # на сервере только пробный
        gate.set()
        results = await asyncio.gather(*tasks)
        assert results == ["ответ"] * 4               # остальные дождались и прошли
        assert len(calls) == 5
    finally:
        await server.close()


async def test_waiters_get_nothing_if_probe_renews_pause(cleanup):
    gate = asyncio.Event()
    calls = []

    async def handler(request):
        calls.append(1)
        if len(calls) >= 2:
            await gate.wait()
        return web.Response(text=TPD_BODY, status=429)

    app = web.Application()
    app.router.add_post("/", handler)
    server = TestServer(app)
    await server.start_server()
    clock = Clock()
    try:
        c = _client(server, clock)
        await _ask(c)
        clock.t += 433
        tasks = [asyncio.create_task(_ask(c)) for _ in range(3)]
        await asyncio.sleep(0.2)
        gate.set()
        assert await asyncio.gather(*tasks) == [None, None, None]
        assert len(calls) == 2
    finally:
        await server.close()


async def test_minute_429_retries_like_before(cleanup):
    server, calls = await _server([(429, TPM_BODY), (429, TPM_BODY), (200, OK_BODY)])
    sleeps = []
    try:
        c = _client(server, Clock(), sleeps=sleeps)
        assert await _ask(c) == "ответ"
        assert len(calls) == 3
        assert sleeps == [5.0, 10.0]
        assert not c.is_blocked()
    finally:
        await server.close()


async def test_minute_429_gives_up_after_retries(cleanup):
    server, calls = await _server([(429, TPM_BODY)])
    try:
        c = _client(server, Clock(), sleeps=[])
        assert await _ask(c) is None
        assert len(calls) == 4                        # 1 + 3 повтора
        assert not c.is_blocked()
    finally:
        await server.close()


async def test_pause_set_during_retry_sleep_stops_retries(cleanup):
    """Блокировка проверяется перед каждым повтором: другой вызов включил паузу — не повторяем."""
    server, calls = await _server([(429, TPM_BODY), (200, OK_BODY)])
    clock = Clock()
    sleeps = []
    try:
        c = _client(server, clock)

        async def sleep_and_block(seconds):
            c._block(600, "other")

        c._sleep = sleep_and_block
        assert await _ask(c) is None
        assert len(calls) == 1
    finally:
        await server.close()


async def test_other_errors_do_not_block(cleanup):
    server, calls = await _server([(500, "boom"), (200, OK_BODY)])
    try:
        c = _client(server, Clock())
        assert await _ask(c) is None
        assert not c.is_blocked()
        assert await _ask(c) == "ответ"
    finally:
        await server.close()


async def test_shared_minute_limiter_across_kinds(cleanup):
    server, calls = await _server([(200, OK_BODY)])
    try:
        c = _client(server, Clock(), minute_limit=3)
        results = [await _ask(c, kind=k) for k in ("vision", "nlp", "menu", "menu", "vision")]
        assert results == ["ответ"] * 3 + [None, None]
        assert len(calls) == 3
        c.limiter.max_tokens = 5                      # лимит из меню действует сразу
        assert await _ask(c) == "ответ"
    finally:
        await server.close()


# ── статистика для heartbeat ────────────────────────────────────────

async def test_daily_stats_and_downtime_line(cleanup):
    server, calls = await _server([(429, TPD_BODY)])
    clock = Clock(t=1_800_000_000.0)   # 2027-01-15 08:00:00 UTC
    try:
        c = _client(server, clock)
        assert describe_downtime(c.daily_stats()) == ""
        await _ask(c)
        clock.t += 600
        await _ask(c)                                 # пробный снова 429 → вторая пауза
        stats = c.daily_stats()
        assert stats["skipped"] == 0 and stats["blocked_seconds"] > 0
        line = describe_downtime(stats)
        assert line.startswith("Groq: лимит кончился в 08:00 UTC, распознавание стояло ")
        assert "пауза до" in line
    finally:
        await server.close()


def test_downtime_line_hours_and_skips():
    stats = {"blocked_seconds": 9 * 3600 + 120, "first_block_at": 1_800_000_000.0 - 8 * 3600 + 48 * 60,
             "skipped": 31, "blocked_now_until": None}
    line = describe_downtime(stats)
    assert "стояло 9 ч" in line and "пропущено вызовов: 31" in line and "пауза до" not in line


def test_downtime_line_minutes():
    line = describe_downtime({"blocked_seconds": 432.0, "first_block_at": 1_800_000_000.0,
                              "skipped": 0, "blocked_now_until": None})
    assert "стояло 7 мин" in line


def test_get_client_is_shared_per_config():
    cfg = VisionConfig(api_key="x")
    assert llm.get_client(cfg) is llm.get_client(cfg)
    assert llm.get_client(VisionConfig(api_key="x")) is not llm.get_client(cfg)
