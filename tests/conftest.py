"""Shared fixtures: offline Config and Userbot with stub dependencies."""

import pytest

from bot.models import Config
from bot.userbot import Userbot


class StubDedup:
    def __init__(self):
        self.dm_sent: list[int] = []

    async def mark_dm_sent(self, seller_id: int):
        self.dm_sent.append(seller_id)


class StubLimiter:
    def consume(self) -> bool:
        return True


@pytest.fixture
def config(tmp_path) -> Config:
    return Config(**{
        "telegram": {
            "api_id": 1,
            "api_hash": "test",
            "phone": "+10000000000",
            "session_name": str(tmp_path / "test-session"),
            "bot_token": "42:TEST",
        },
        "actions": {"dm_delay_min": 0, "dm_delay_max": 0, "dry_run": False},
    })


@pytest.fixture
def userbot(config) -> Userbot:
    notifications: list[str] = []

    async def collect_notify(text: str, matched_keyword: str = ""):
        notifications.append(text)

    ub = Userbot(config, StubDedup(), StubLimiter(), StubLimiter(), db=None,
                 notify_callback=collect_notify)
    ub.test_notifications = notifications  # доступ из тестов
    return ub
