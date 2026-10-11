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

    assert counts == {"actions_log": 1, "messages": 1, "matches": 1}
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
