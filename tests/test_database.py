"""Database: PRAGMA setup and FK enforcement."""

import pytest

from db.database import Database


@pytest.fixture
async def db(tmp_path):
    d = Database(str(tmp_path / "test.db"))
    await d.connect()
    yield d
    await d.close()


async def test_pragmas_applied(db):
    async with db._db.execute("PRAGMA journal_mode") as cur:
        assert (await cur.fetchone())[0] == "wal"
    async with db._db.execute("PRAGMA synchronous") as cur:
        assert (await cur.fetchone())[0] == 1  # NORMAL
    async with db._db.execute("PRAGMA foreign_keys") as cur:
        assert (await cur.fetchone())[0] == 1


async def test_match_without_seller_rejected(db):
    import aiosqlite
    with pytest.raises(aiosqlite.IntegrityError):
        await db.add_match(999, -100500, 1, "keyword", "колонка", None)


async def test_match_with_seller_ok(db):
    await db.add_seller(1, "chat", 10, "keyword", "колонка", 500)
    await db.add_match(1, -100500, 10, "keyword", "колонка", 500)
    assert len(await db.get_recent_matches()) == 1


async def _insert_old_message(db, msg_id: str, days_ago: int):
    chat_id = await db.get_or_create_chat("-100500", "чат")
    await db._db.execute(
        "INSERT INTO messages (id, chat_id, source_chat, author, text, ts) "
        "VALUES (?, ?, ?, ?, ?, datetime('now', ?))",
        (msg_id, chat_id, "-100500", "1", "txt", f"-{days_ago} days"),
    )
    await db._db.commit()


async def test_prune_deletes_old_keeps_fresh(db):
    await _insert_old_message(db, "old", 45)
    await _insert_old_message(db, "fresh", 1)
    await db._db.execute(
        "INSERT INTO actions_log (message_id, action_type, result, timestamp) "
        "VALUES ('old', 'dm', 'success', datetime('now', '-45 days'))")
    await db.add_seller(1, "chat", 10, "keyword", "колонка", 500)
    await db._db.execute(
        "INSERT INTO matches (seller_id, chat_id, message_id, match_type, created_at) "
        "VALUES (1, -100500, 10, 'keyword', datetime('now', '-100 days'))")
    await db._db.commit()

    counts = await db.prune_old_rows()

    assert counts == {"actions_log": 1, "messages": 1, "matches": 1, "notified": 0}
    async with db._db.execute("SELECT id FROM messages") as cur:
        rows = [r[0] for r in await cur.fetchall()]
    assert rows == ["fresh"]


async def test_dead_tables_dropped(db):
    async with db._db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('pools','pool_chats','stats')"
    ) as cur:
        assert await cur.fetchall() == []


async def test_prune_keeps_message_with_fresh_action(db):
    """Сообщение старше 30д, но с недавним action_log — не удаляем (FK)."""
    await _insert_old_message(db, "old-with-action", 45)
    await db.log_action("old-with-action", "dm", "success")

    counts = await db.prune_old_rows()

    assert counts["messages"] == 0
    assert counts["actions_log"] == 0


# ── notified (этап 1) ───────────────────────────────────────────────

async def test_notified_created_on_existing_db(tmp_path):
    """Миграция только добавляет: БД без таблицы notified открывается и получает её."""
    import sqlite3
    path = str(tmp_path / "old.db")
    raw = sqlite3.connect(path)
    raw.executescript(
        "CREATE TABLE seen_sellers (id INTEGER PRIMARY KEY AUTOINCREMENT, seller_id INTEGER NOT NULL UNIQUE,"
        " first_chat TEXT NOT NULL, first_msg_id INTEGER NOT NULL, match_type TEXT NOT NULL, matched_value TEXT,"
        " price INTEGER, dm_sent INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL DEFAULT (datetime('now')));"
        "INSERT INTO seen_sellers (seller_id, first_chat, first_msg_id, match_type) VALUES (5, 'c', 1, 'keyword');"
    )
    raw.commit()
    raw.close()
    d = Database(path)
    await d.connect()
    try:
        assert await d.is_seller_seen(5, 0) is False          # данные на месте
        await d.add_notified(5, "колонка", "fp", 10, -1, 1, delivered=True, suppressed=False)
        assert (await d.find_delivered_notification(5, "fp", "2000-01-01 00:00:00"))["price"] == 10
    finally:
        await d.close()


async def test_find_delivered_ignores_suppressed_undelivered_and_old(db):
    await db.add_notified(1, "x", "fp", 1, -1, 1, delivered=False, suppressed=True, reason="repeat")
    await db.add_notified(1, "x", "fp", 2, -1, 2, delivered=False, suppressed=False, reason="undelivered")
    await db.add_notified(1, "x", "fp", 3, -1, 3, delivered=True, suppressed=False,
                          created_at="2020-01-01 00:00:00")
    assert await db.find_delivered_notification(1, "fp", "2026-01-01 00:00:00") is None
    await db.add_notified(1, "x", "fp", 4, -1, 4, delivered=True, suppressed=False)
    got = await db.find_delivered_notification(1, "fp", "2026-01-01 00:00:00")
    assert got["price"] == 4


async def test_prune_removes_old_notified(db):
    await db.add_notified(1, "x", "fp", 1, -1, 1, delivered=True, suppressed=False,
                          created_at="2020-01-01 00:00:00")
    await db.add_notified(1, "x", "fp", 2, -1, 2, delivered=True, suppressed=False)
    counts = await db.prune_old_rows()
    assert counts["notified"] == 1
    async with db._db.execute("SELECT price FROM notified") as cur:
        assert [r[0] for r in await cur.fetchall()] == [2]
