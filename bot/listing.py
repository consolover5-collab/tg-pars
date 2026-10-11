"""Правила над текстом объявления: скам-рассылки, отпечаток текста, повтор от продавца.

Чистые функции + одна async-проверка по таблице `notified`; без Telethon,
поэтому их же проигрывает scripts/replay_stage1.py на копии истории.
"""

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from db.database import Database, format_ts

# ── Скам «распродажа списком, фото и цены в личку» ─────────────────

SCAM_MIN_SHORT_LINES = 8
SCAM_SHORT_LINE_LEN = 40

_EURO_PRICE_RE = re.compile(r"\d\s*(?:€|евро|euro|eur\b)|€\s*\d", re.IGNORECASE)

# «фото/цену … в личку/личное/ЛС» — в любом порядке, рядом (в пределах ~80 знаков)
_PM = r"(?:личк\w*|лички|личн\w*|\bлс\b|л/с)"
_PHOTO_PRICE = r"(?:фото\w*|цен\w*)"
_SCAM_PHRASE_RE = re.compile(
    rf"{_PHOTO_PRICE}[^\n]{{0,80}}?{_PM}|{_PM}[^\n]{{0,80}}?{_PHOTO_PRICE}",
    re.IGNORECASE,
)


def is_scam_list(text: str) -> bool:
    """Скам-шаблон: длинный список коротких строк + нет цены в € + «фото/цену в личку».

    Все три условия сразу: честные «книги комплектами» с ценами в € не попадают.
    """
    if not text:
        return False
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    short = [ln for ln in lines if len(ln) <= SCAM_SHORT_LINE_LEN]
    if len(short) < SCAM_MIN_SHORT_LINES:
        return False
    if _EURO_PRICE_RE.search(text):
        return False
    return bool(_SCAM_PHRASE_RE.search(text))


# ── Отпечаток текста ───────────────────────────────────────────────

_NUM = r"\d+(?:[.,]\d{1,2})?"
# «р», «к», «k» не берём: после числа это размеры и разрешения («42р», «4K»), а не цена —
# лучше лишнее уведомление, чем потерянное
_CURRENCY = r"(?:€|₽|евро|euro?|eur\b|руб\w*|тыс\w*)"
# число вплотную к валюте: «200€», «200 евро», «€200», «5 тыс»; остальные цифры
# (модель, объём памяти) остаются — «iPhone 13 128» ≠ «iPhone 14 256»
_PRICE_RE = re.compile(rf"(?:[€₽]\s*{_NUM}|{_NUM}\s*{_CURRENCY})", re.IGNORECASE)


def normalize_listing_text(text: str) -> str:
    """Нижний регистр, цены убраны, пунктуация/эмодзи → пробел."""
    t = _PRICE_RE.sub(" ", (text or "").lower())
    return re.sub(r"[\W_]+", " ", t).strip()


def text_fingerprint(text: str) -> str:
    """Хеш нормализованного текста; пустая строка, если текста нет — тогда не подавляем."""
    norm = normalize_listing_text(text)
    if not norm:
        return ""
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()[:16]


# ── Правило повтора ────────────────────────────────────────────────

@dataclass
class RepeatVerdict:
    repeat: bool = False                 # подавить уведомление
    price_drop_from: int | None = None   # цена снижена: прежняя цена
    text_fp: str = ""


async def judge_repeat(
    db: Database,
    seller_id: int,
    text: str,
    price: int | None,
    window_days: int,
    now: datetime | None = None,
) -> RepeatVerdict:
    """Повтор = у продавца уже есть доставленное уведомление с тем же текстом за window_days.

    Обе цены известны и новая ниже — не повтор, а «цена снижена».
    Цена появилась/пропала/выросла — повтор. Без текста не подавляем никогда.
    """
    fp = text_fingerprint(text)
    if not fp:
        return RepeatVerdict(text_fp="")
    moment = now or datetime.now(timezone.utc)
    since = format_ts(moment - timedelta(days=window_days))
    prev = await db.find_delivered_notification(seller_id, fp, since)
    if prev is None:
        return RepeatVerdict(text_fp=fp)
    old_price = prev["price"]
    if price is not None and old_price is not None and price < old_price:
        return RepeatVerdict(price_drop_from=old_price, text_fp=fp)
    return RepeatVerdict(repeat=True, text_fp=fp)
