"""Единый клиент Groq (OpenAI-совместимый) с бюджетом.

Все вызовы — vision, NLP, генерация DM, меню — идут через LLMClient.chat():
одна сборка запроса и заголовков, один разбор x-ratelimit-*, один минутный
лимитер, одна пауза после исчерпания дневного лимита (TPD).

Groq на дневном лимите отвечает 429 с телом «… tokens per day (TPD): Limit 200000,
Used …, Requested …. Please try again in 7m12.3s …». Повторять такой запрос
бессмысленно: клиент запоминает срок из ответа и до его конца отвечает «нет ответа»
без сети. После паузы уходит один пробный запрос; остальные ждут его исхода.
"""

import asyncio
import logging
import re
import time
from datetime import datetime, timezone

import aiohttp

from bot.http import _retry_delay, get_session
from bot.ratelimit import RateLimiter

logger = logging.getLogger(__name__)

# Последнее известное состояние минутных лимитов Groq из заголовков ответа.
# Дополнительных запросов нет — обновляется на каждом успешном ответе.
rate_info: dict = {}

DEFAULT_BLOCK_SECONDS = 600.0      # «try again in» не разобралось
MAX_BLOCK_SECONDS = 24 * 3600.0    # окно Groq скользящее: больше суток не ждём
MINUTE_RETRIES = 3                 # повторы 429 по минутному лимиту
MINUTE_RETRY_CAP = 30.0

_DAILY_LIMIT_RE = re.compile(r"per day|\((?:TPD|RPD)\)", re.IGNORECASE)
_TRY_AGAIN_RE = re.compile(r"try again in\s+([0-9a-z.]+)", re.IGNORECASE)
_DURATION_PART_RE = re.compile(r"(\d+(?:\.\d+)?)(ms|d|h|m|s)")
_UNIT_SECONDS = {"d": 86400.0, "h": 3600.0, "m": 60.0, "s": 1.0, "ms": 0.001}


def is_daily_limit(body: str) -> bool:
    """429 про суточный лимит (tokens/requests per day), а не минутный."""
    return bool(_DAILY_LIMIT_RE.search(body or ""))


def parse_retry_in(body: str) -> float | None:
    """Секунды из «Please try again in 1h2m3.4s» / «7m12.3s» / «45.6s» / «340ms»; None — не разобралось."""
    m = _TRY_AGAIN_RE.search(body or "")
    if not m:
        return None
    chunk = m.group(1).rstrip(".")
    parts = _DURATION_PART_RE.findall(chunk)
    if not parts or "".join(n + u for n, u in parts) != chunk:
        return None
    return sum(float(n) * _UNIT_SECONDS[u] for n, u in parts)


def _clock_utc(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%H:%M")


def describe_downtime(stats: dict) -> str:
    """Строка для heartbeat по LLMClient.daily_stats(); пустая, если пауз не было."""
    blocked = stats.get("blocked_seconds") or 0
    if blocked <= 0:
        return ""
    if blocked < 3600:
        stood = f"{max(1, round(blocked / 60))} мин"
    else:
        stood = f"{blocked / 3600:.0f} ч"
    text = f"Groq: лимит кончился в {_clock_utc(stats['first_block_at'])} UTC, распознавание стояло {stood}"
    if stats.get("skipped"):
        text += f", пропущено вызовов: {stats['skipped']}"
    if stats.get("blocked_now_until"):
        text += f", пауза до {_clock_utc(stats['blocked_now_until'])} UTC"
    return text


class LLMClient:
    def __init__(self, vision_cfg, minute_limit: int = 6, *, now=time.time, sleep=asyncio.sleep):
        self.cfg = vision_cfg            # VisionConfig: ключ/модель/URL читаются на каждом вызове
        self.limiter = RateLimiter(minute_limit, 60.0)  # общий на vision+NLP+DM+меню
        self._now = now
        self._sleep = sleep
        self._blocked_until = 0.0        # 0 — не блокировано
        self._probe_done: asyncio.Event | None = None   # не None — пробный запрос в полёте
        self._intervals: list[tuple[float, float]] = []  # (начало, конец) блокировок за сутки
        self._skips: list[float] = []    # моменты пропущенных вызовов за сутки

    # ── Состояние блокировки ───────────────────────────────────────

    def is_blocked(self) -> bool:
        return self._blocked_until > self._now()

    def blocked_until(self) -> float:
        """Конец текущей паузы (unix-секунды) или 0, если не блокировано."""
        return self._blocked_until if self.is_blocked() else 0.0

    def note_skip(self):
        """Вызов пропущен из-за паузы (для вызывающих, которые не дошли до chat())."""
        now = self._now()
        self._skips.append(now)
        self._trim(now)

    def _trim(self, now: float):
        cutoff = now - 86400.0
        self._skips = [t for t in self._skips if t > cutoff]
        self._intervals = [(a, b) for a, b in self._intervals if b > cutoff]

    def _block(self, seconds: float, kind: str):
        now = self._now()
        seconds = min(max(seconds, 1.0), MAX_BLOCK_SECONDS)
        self._blocked_until = now + seconds
        self._intervals.append((now, self._blocked_until))
        self._trim(now)
        logger.warning(
            "Groq: дневной лимит исчерпан (%s), пауза %.0f мин до %s UTC; вызовы пропускаются без сети",
            kind, seconds / 60, _clock_utc(self._blocked_until),
        )

    def daily_stats(self) -> dict:
        """За последние 24 ч: сколько секунд Groq был на паузе, когда она началась, сколько вызовов пропущено."""
        now = self._now()
        self._trim(now)
        cutoff = now - 86400.0
        blocked = 0.0
        first = None
        for a, b in self._intervals:
            start, end = max(a, cutoff), min(b, now)
            if end > start:
                blocked += end - start
                first = start if first is None else min(first, start)
        return {
            "blocked_seconds": blocked,
            "first_block_at": first,
            "skipped": len(self._skips),
            "blocked_now_until": self.blocked_until() or None,
        }

    # ── Запрос ─────────────────────────────────────────────────────

    async def chat(
        self,
        messages: list[dict],
        *,
        max_tokens: int,
        temperature: float,
        response_format: dict | None = None,
        kind: str = "llm",
        timeout: float = 20.0,
    ) -> str | None:
        """Один вызов chat/completions. Текст ответа или None (ошибка, лимит, пауза)."""
        probe = False
        while True:
            now = self._now()
            if self._blocked_until > now:
                self.note_skip()
                return None
            if self._blocked_until and self._probe_done is not None:
                await self._probe_done.wait()   # пауза кончилась, пробный уже в полёте
                continue
            if self._blocked_until:             # первый после паузы — пробный
                probe = True
                self._probe_done = asyncio.Event()
            break

        try:
            if not self.limiter.consume():
                logger.debug("Groq: общий минутный лимит (%s), вызов пропущен", kind)
                return None
            return await self._request(messages, max_tokens, temperature, response_format, kind, timeout, probe)
        finally:
            if probe:
                if self._probe_done is not None:
                    self._probe_done.set()
                self._probe_done = None

    async def _request(self, messages, max_tokens, temperature, response_format, kind, timeout, probe) -> str | None:
        payload = {
            "model": self.cfg.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if response_format:
            payload["response_format"] = response_format
        headers = {
            "Authorization": f"Bearer {self.cfg.api_key}",
            "Content-Type": "application/json",
        }
        session = get_session()
        attempt = 0
        try:
            while True:
                resp = await session.post(
                    self.cfg.base_url, json=payload, headers=headers,
                    timeout=aiohttp.ClientTimeout(total=timeout),
                )
                try:
                    if resp.status == 200:
                        self._on_success(probe, resp.headers)
                        data = await resp.json()
                        return data["choices"][0]["message"]["content"]
                    body = await resp.text()
                    retry_headers = resp.headers
                    status = resp.status
                finally:
                    resp.release()

                if status != 429:
                    logger.error("Groq %s: HTTP %s: %s", kind, status, body[:300])
                    self._on_other_answer(probe)
                    return None
                if is_daily_limit(body):
                    self._block(parse_retry_in(body) or DEFAULT_BLOCK_SECONDS, kind)
                    return None   # повтор бессмысленен — квота кончилась
                # минутный лимит: повтор как раньше (5, 10, 20 с), но пауза могла включиться
                if attempt >= MINUTE_RETRIES:
                    logger.warning("Groq %s: минутный лимит, повторы кончились", kind)
                    self._on_other_answer(probe)
                    return None
                delay = _retry_delay(retry_headers, attempt, MINUTE_RETRY_CAP)
                logger.info("Groq %s: 429 (минутный лимит), повтор через %.1f с", kind, delay)
                await self._sleep(delay)
                if self.is_blocked():
                    self.note_skip()
                    return None
                attempt += 1
        except Exception as e:
            logger.error("Groq %s: запрос не удался: %s", kind, e)
            self._on_other_answer(probe)
            return None

    def _on_success(self, probe: bool, headers):
        rate_info.update({
            "limit_requests":     headers.get("x-ratelimit-limit-requests"),
            "remaining_requests": headers.get("x-ratelimit-remaining-requests"),
            "limit_tokens":       headers.get("x-ratelimit-limit-tokens"),
            "remaining_tokens":   headers.get("x-ratelimit-remaining-tokens"),
            "reset_requests":     headers.get("x-ratelimit-reset-requests"),
            "updated_at":         time.monotonic(),
        })
        if probe:
            self._recover()

    def _on_other_answer(self, probe: bool):
        # Пробный запрос вернул не суточный лимит (ошибка/таймаут) — пауза закончена,
        # дальше работаем как обычно: следующий 429 TPD включит новую.
        if probe:
            self._recover()

    def _recover(self):
        self._blocked_until = 0.0
        logger.info("Groq: пауза окончена, распознавание снова работает")


_client: LLMClient | None = None


def get_client(vision_cfg) -> LLMClient:
    """Один клиент на процесс (общие пауза и минутный лимитер для vision, NLP и меню)."""
    global _client
    if _client is None or _client.cfg is not vision_cfg:
        _client = LLMClient(vision_cfg)
    return _client
