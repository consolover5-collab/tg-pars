import pytest

from bot.dedup import DedupChecker
from db.database import Database


@pytest.fixture
async def dedup(tmp_path):
    db = Database(str(tmp_path / "dedup.db"))
    await db.connect()
    yield DedupChecker(db)
    await db.close()


async def test_unseen_seller_not_dup(dedup):
    assert await dedup.is_seen(1) is False


async def test_registered_but_no_dm_not_dup(dedup):
    """Кулдаун ставится только после успешного DM — фейл DM не блокирует продавца."""
    await dedup.register(1, "chat", 10, "keyword", "колонка", 500)
    assert await dedup.is_seen(1) is False


async def test_dm_sent_makes_dup(dedup):
    await dedup.register(1, "chat", 10, "keyword", "колонка", 500)
    await dedup.mark_dm_sent(1)
    assert await dedup.is_seen(1) is True


async def test_new_listing_resets_cooldown(dedup):
    await dedup.register(1, "chat", 10, "keyword", "колонка", 500)
    await dedup.mark_dm_sent(1)
    await dedup.register(1, "chat", 11, "keyword", "колонка", 600)  # новый пост
    assert await dedup.is_seen(1) is False


async def test_cooldown_zero_means_forever(dedup):
    await dedup.register(1, "chat", 10, "keyword", "колонка", 500)
    await dedup.mark_dm_sent(1)
    assert await dedup.is_seen(1, cooldown_hours=0) is True
