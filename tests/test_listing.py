"""Скам-шаблон, отпечаток текста, правило повтора."""

from datetime import datetime, timedelta, timezone

import pytest

from bot.listing import (
    is_scam_list, judge_repeat, normalize_listing_text, text_fingerprint,
)
from db.database import Database, format_ts


# ── is_scam_list ────────────────────────────────────────────────────

_ITEMS = "\n".join([
    "Кровать", "Диван", "Шкаф", "Холодильник", "Стиральная машина", "Телевизор",
    "PlayStation 5", "Робот-пылесос", "Dyson Стайлер", "Пианино",
])

SCAM_TEXTS = [
    f"Приветствую 👋\n\nПродаю свои личные вещи :\n{_ITEMS}\n\nКого что интересует, пиши в личку , пришлю больше фото и цены",
    f"Здравствуйте 👋\nОтдам по хорошей цене в связи с переездом:\n\n{_ITEMS}\n\nПишите в личку по поводу цены и доп фото",
    f"🔥 **СРОЧНАЯ РАСПРОДАЖА🔥**\n\n{_ITEMS}\n\nПо поводу цены, пишите в лички , обсудим \nБольше фото отправлю в личку",
    f"Очень срочная распродажа , по очень классной цене :\n\n{_ITEMS}\n\nФото, цену и все остальное, отправлю в личное сообщение.",
    f"Всем привет, в связи со срочным переездом :\n\n{_ITEMS}\n\nФото, цены отправлю в личку , цены хорошие. Так же доставка либо самовывоз.",
    f"Срочная распродажа вещей\n{_ITEMS}\n\nПишите пожалуйста в личку, по поводу цен и доп фото.\nЦена приятная , не дорого",
    f"В связи с переездом :\n\n{_ITEMS}\n\nПо поводу фото и цен, пишите пожалуйста в личное сообщение",
    f"Отдам вещи\n{_ITEMS}\n\nЦены и фото в ЛС",
]


@pytest.mark.parametrize("text", SCAM_TEXTS)
def test_scam_list_detected(text):
    assert is_scam_list(text) is True


def test_children_books_with_prices_not_scam():
    """Честный список с ценами в € и той же фразой про личку не попадает."""
    lines = "\n".join(f"{i}. Книга номер {i} — {5 + i} €" for i in range(1, 12))
    text = f"Продаю детские книги комплектами 📚\n\n{lines}\n\nЕщё фото отправлю в личку"
    assert is_scam_list(text) is False


def test_short_list_not_scam():
    text = "Продаю:\nДиван\nШкаф\nКровать\n\nЦену и фото пришлю в личку"
    assert is_scam_list(text) is False


def test_long_list_without_pm_phrase_not_scam():
    assert is_scam_list(f"Продаю дёшево\n{_ITEMS}\n\nЗвоните по телефону") is False


def test_long_lines_do_not_count_as_list():
    long_lines = "\n".join("Очень подробное описание вещи с множеством деталей и нюансов" for _ in range(10))
    assert is_scam_list(f"{long_lines}\nФото и цену пришлю в личку") is False


def test_scam_empty_text():
    assert is_scam_list("") is False
    assert is_scam_list(None) is False


# ── отпечаток текста ────────────────────────────────────────────────

def test_fingerprint_ignores_prices_case_and_punctuation():
    a = "Продам iPhone 13 128 GB!! Цена: 200€"
    b = "продам iphone 13 128 gb, цена 250 евро"
    assert text_fingerprint(a) == text_fingerprint(b)


def test_fingerprint_keeps_model_numbers():
    assert text_fingerprint("iPhone 13 128 gb 200€") != text_fingerprint("iPhone 14 256 gb 200€")


def test_fingerprint_price_markers():
    base = text_fingerprint("Колонка JBL")
    for variant in ("Колонка JBL 50€", "Колонка JBL €50", "Колонка JBL 50 eur",
                    "Колонка JBL 3000 руб", "Колонка JBL 15 тыс", "Колонка JBL 30₽"):
        assert text_fingerprint(variant) == base, variant


@pytest.mark.parametrize("a, b", [
    ("Samsung 4K 200€", "Samsung 8K 300€"),
    ("Кроссовки Nike 42р 50€", "Кроссовки Nike 44р 50€"),
    ("Телевизор 55 дюймов 4k", "Телевизор 55 дюймов 8k"),
    ("Монитор 2к 150€", "Монитор 4к 150€"),
])
def test_fingerprint_keeps_sizes_and_resolutions(a, b):
    """«р», «к», «k» после числа — размер/разрешение, не цена: разные объявления не склеиваются."""
    assert text_fingerprint(a) != text_fingerprint(b)


def test_fingerprint_emoji_and_markup_are_noise():
    assert text_fingerprint("**Колонка** 🔊 JBL!!!") == text_fingerprint("колонка jbl")


def test_fingerprint_empty_when_no_text_or_only_price():
    assert text_fingerprint("") == ""
    assert text_fingerprint(None) == ""
    assert text_fingerprint("  100€  ") == ""
    assert normalize_listing_text("🔥🔥") == ""


# ── judge_repeat ────────────────────────────────────────────────────

@pytest.fixture
async def db(tmp_path):
    d = Database(str(tmp_path / "test.db"))
    await d.connect()
    yield d
    await d.close()


NOW = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)
TEXT = "Продам колонку JBL Flip 5, отличное состояние"


async def _delivered(db, seller, text, price, days_ago, **kw):
    await db.add_notified(
        seller, "колонка", text_fingerprint(text), price, -100, 1,
        delivered=kw.get("delivered", True), suppressed=kw.get("suppressed", False),
        created_at=format_ts(NOW - timedelta(days=days_ago)),
    )


async def test_new_when_nothing_delivered(db):
    v = await judge_repeat(db, 1, TEXT, 50, 7, NOW)
    assert not v.repeat and v.price_drop_from is None and v.text_fp


async def test_same_text_within_window_is_repeat(db):
    await _delivered(db, 1, TEXT, 50, days_ago=2)
    assert (await judge_repeat(db, 1, TEXT + "!", 50, 7, NOW)).repeat is True


async def test_other_seller_not_repeat(db):
    await _delivered(db, 1, TEXT, 50, days_ago=1)
    assert (await judge_repeat(db, 2, TEXT, 50, 7, NOW)).repeat is False


async def test_different_text_same_item_is_new(db):
    await _delivered(db, 1, TEXT, 50, days_ago=1)
    v = await judge_repeat(db, 1, "Продаю колонку JBL Charge 4, новая", 50, 7, NOW)
    assert v.repeat is False


async def test_old_delivery_outside_window_is_new(db):
    await _delivered(db, 1, TEXT, 50, days_ago=8)
    assert (await judge_repeat(db, 1, TEXT, 50, 7, NOW)).repeat is False


async def test_price_drop_is_not_repeat_and_reports_old_price(db):
    await _delivered(db, 1, TEXT, 50, days_ago=1)
    v = await judge_repeat(db, 1, TEXT, 40, 7, NOW)
    assert v.repeat is False and v.price_drop_from == 50


@pytest.mark.parametrize("old, new", [(50, 50), (50, 60), (None, 40), (50, None), (None, None)])
async def test_price_up_same_appeared_vanished_is_repeat(db, old, new):
    await _delivered(db, 1, TEXT, old, days_ago=1)
    assert (await judge_repeat(db, 1, TEXT, new, 7, NOW)).repeat is True


async def test_no_text_never_suppressed(db):
    await _delivered(db, 1, "", None, days_ago=0)
    v = await judge_repeat(db, 1, "", None, 7, NOW)
    assert v.repeat is False and v.text_fp == ""


async def test_undelivered_does_not_count(db):
    await _delivered(db, 1, TEXT, 50, days_ago=1, delivered=False)
    assert (await judge_repeat(db, 1, TEXT, 50, 7, NOW)).repeat is False


async def test_suppressed_repeats_do_not_extend_window(db):
    """Доставлено 8 дней назад, подавленный повтор вчера: окно от доставленного — значит, новое."""
    await _delivered(db, 1, TEXT, 50, days_ago=8)
    await _delivered(db, 1, TEXT, 50, days_ago=1, delivered=False, suppressed=True)
    assert (await judge_repeat(db, 1, TEXT, 50, 7, NOW)).repeat is False


async def test_window_follows_config_days(db):
    await _delivered(db, 1, TEXT, 50, days_ago=3)
    assert (await judge_repeat(db, 1, TEXT, 50, 2, NOW)).repeat is False
    assert (await judge_repeat(db, 1, TEXT, 50, 4, NOW)).repeat is True
