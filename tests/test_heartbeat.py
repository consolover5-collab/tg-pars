"""Daily heartbeat: seconds_until_next_hour_utc, digest formatting, count_matches_since."""

from datetime import datetime, timedelta, timezone

import pytest

from bot.control import _format_heartbeat, seconds_until_next_hour_utc
from db.database import Database


# ── seconds_until_next_hour_utc ─────────────────────────────────────

def test_before_hour_today_waits_until_today():
    now = datetime(2026, 8, 16, 5, 0, 0, tzinfo=timezone.utc)
    assert seconds_until_next_hour_utc(now, 7) == timedelta(hours=2).total_seconds()


def test_hour_passed_rolls_to_tomorrow():
    now = datetime(2026, 8, 16, 9, 30, 0, tzinfo=timezone.utc)
    assert seconds_until_next_hour_utc(now, 7) == timedelta(hours=21, minutes=30).total_seconds()


def test_exactly_at_hour_rolls_to_tomorrow_not_zero():
    now = datetime(2026, 8, 16, 7, 0, 0, 0, tzinfo=timezone.utc)
    assert seconds_until_next_hour_utc(now, 7) == timedelta(days=1).total_seconds()


# ── _format_heartbeat ────────────────────────────────────────────────

def test_format_normal():
    text = _format_heartbeat(chats_count=3, new_count=5, dup_count=2, paused=False, authorized=True)
    assert text == "💓 tg-pars: жив • юзербот ✅ • чатов 3 • за 24ч: 5 находок, 2 повторов"


def test_format_paused_appends_suffix():
    text = _format_heartbeat(chats_count=3, new_count=0, dup_count=0, paused=True, authorized=True)
    assert text == "💓 tg-pars: жив • юзербот ✅ • чатов 3 • за 24ч: 0 находок, 0 повторов • ⏸ пауза"


def test_format_not_authorized():
    text = _format_heartbeat(chats_count=3, new_count=5, dup_count=2, paused=False, authorized=False)
    assert text == "⚠️ ЮЗЕРБОТ НЕ АВТОРИЗОВАН — мониторинг стоит. Настройки → 🔐 Авторизация"


def test_format_not_authorized_overrides_paused():
    """Auth warning is standalone — pause state doesn't leak into it."""
    text = _format_heartbeat(chats_count=3, new_count=5, dup_count=2, paused=True, authorized=False)
    assert text == "⚠️ ЮЗЕРБОТ НЕ АВТОРИЗОВАН — мониторинг стоит. Настройки → 🔐 Авторизация"


# ── Database.count_matches_since ────────────────────────────────────

@pytest.fixture
async def db(tmp_path):
    d = Database(str(tmp_path / "test.db"))
    await d.connect()
    yield d
    await d.close()


async def test_count_matches_since_no_rows(db):
    assert await db.count_matches_since(24) == (0, 0)


async def test_count_matches_since_counts_new_and_duplicate(db):
    await db.add_seller(1, "chat", 10, "keyword", "колонка", 500)
    await db.add_match(1, -100500, 10, "keyword", "колонка", 500, is_duplicate=False)
    await db.add_match(1, -100500, 11, "keyword", "колонка", 500, is_duplicate=True)
    await db.add_match(1, -100500, 12, "keyword", "колонка", 500, is_duplicate=True)

    assert await db.count_matches_since(24) == (1, 2)


async def test_count_matches_since_excludes_older_than_window(db):
    await db.add_seller(1, "chat", 10, "keyword", "колонка", 500)
    await db.add_match(1, -100500, 10, "keyword", "колонка", 500)
    await db._db.execute(
        "UPDATE matches SET created_at = datetime('now', '-48 hours') WHERE message_id = 10"
    )
    await db._db.commit()

    assert await db.count_matches_since(24) == (0, 0)
