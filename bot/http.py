"""Shared aiohttp session — один TCP/TLS-пул на процесс вместо handshake на каждый вызов."""

import asyncio
import logging
from contextlib import asynccontextmanager

import aiohttp

logger = logging.getLogger(__name__)

_session: aiohttp.ClientSession | None = None


def get_session() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession()
    return _session


async def close_session():
    global _session
    if _session is not None and not _session.closed:
        await _session.close()
    _session = None


_RETRY_STATUSES = (429,)


def _retry_delay(headers, attempt: int, cap: float) -> float:
    """Секунды до повтора: 5, 10, 20… или retry-after, если он больше; не больше cap.

    Groq на лимите токенов в минуту шлёт retry-after ~3 с, а окно освобождается за ~20 с,
    поэтому заголовку одному не верим.
    """
    backoff = 5.0 * 2 ** attempt
    try:
        delay = max(backoff, float(headers.get("retry-after")))
    except (TypeError, ValueError):
        delay = backoff
    return max(0.0, min(delay, cap))


@asynccontextmanager
async def post_with_retry(url: str, *, retries: int = 3, max_delay: float = 30.0, **kwargs):
    """session.post с повтором на 429 (rate limit Groq). Отдаёт последний ответ как есть."""
    session = get_session()
    attempt = 0
    while True:
        resp = await session.post(url, **kwargs)
        if resp.status in _RETRY_STATUSES and attempt < retries:
            delay = _retry_delay(resp.headers, attempt, max_delay)
            resp.release()
            logger.info("HTTP %s от %s, повтор через %.1f с", resp.status, url, delay)
            await asyncio.sleep(delay)
            attempt += 1
            continue
        try:
            yield resp
        finally:
            resp.release()
        return
