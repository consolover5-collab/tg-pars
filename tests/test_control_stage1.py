"""Этап 1: send_notification отвечает, дошло ли владельцу; лимиты из меню действуют сразу; heartbeat."""

from types import SimpleNamespace

import pytest

from bot import control
from bot.control import ControlBot, _format_heartbeat
from bot.models import ExtraRecipient
from bot.ratelimit import RateLimiter
from bot.userbot import Userbot
from db.database import Database

from tests.conftest import StubDedup, StubLimiter


class FakeTgBot:
    def __init__(self, fail_for=()):
        self.sent = []
        self.fail_for = set(fail_for)

    async def send_message(self, chat_id, text):
        if chat_id in self.fail_for:
            raise RuntimeError("blocked")
        self.sent.append((chat_id, text))


def _make_ctrl(config, db=None) -> ControlBot:
    """ControlBot без __init__: роутер aiogram цепляется к Dispatcher один раз на процесс."""
    c = ControlBot.__new__(ControlBot)
    c.config = config
    c.db = db
    c.userbot = None
    c.awaiting = {}
    c.pending_kw = {}
    c.owner_id = None
    c.dm_limiter = RateLimiter(15, 3600)
    c.vision_limiter = RateLimiter(3, 60)
    c._heartbeat_task = None
    c.bot = FakeTgBot()
    return c


@pytest.fixture
def ctrl(config, monkeypatch):
    c = _make_ctrl(config)
    monkeypatch.setattr(control, "_bot_instance", c)
    return c


# ── send_notification ───────────────────────────────────────────────

async def test_notification_delivered_true(ctrl):
    ctrl.owner_id = 7
    assert await ctrl.send_notification("hi", matched_keyword="x") is True
    assert ctrl.bot.sent == [(7, "hi")]


async def test_notification_owner_unknown_false(ctrl):
    assert await ctrl.send_notification("hi") is False


async def test_notification_owner_send_error_false(ctrl):
    ctrl.owner_id = 7
    ctrl.bot = FakeTgBot(fail_for={7})
    assert await ctrl.send_notification("hi") is False


async def test_extra_recipient_failure_does_not_affect_result(ctrl):
    ctrl.owner_id = 7
    ctrl.config.actions.extra_notify = [ExtraRecipient(user_id=8, name="extra")]
    ctrl.bot = FakeTgBot(fail_for={8})
    assert await ctrl.send_notification("hi", matched_keyword="x") is True


# ── лимиты из меню ──────────────────────────────────────────────────

async def test_menu_limit_edit_updates_live_limiters(ctrl, config, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    ub = Userbot(config, StubDedup(), ctrl.dm_limiter, ctrl.vision_limiter, db=None)
    ctrl.userbot = ub
    ctrl.owner_id = 7

    answers = []

    async def answer(text, **kw):
        answers.append(text)

    def msg(text):
        return SimpleNamespace(from_user=SimpleNamespace(id=7), text=text, caption=None, answer=answer)

    for action, text in (("edit_dm_limit", "30"), ("edit_vision_limit", "5"), ("edit_nlp_limit", "4")):
        ctrl.awaiting[7] = action
        await control.handle_text_input(msg(text))

    assert ctrl.dm_limiter.max_tokens == 30
    assert ctrl.vision_limiter.max_tokens == 5
    assert ub.nlp_limiter.max_tokens == 4
    assert ub.llm.limiter.max_tokens == 5 + 4        # общий лимитер = подлимиты вместе
    assert config.rate_limits.dm_per_hour == 30


# ── heartbeat ───────────────────────────────────────────────────────

def test_heartbeat_new_format_with_drops():
    text = _format_heartbeat(
        3, 5, 2, False, True,
        repeat_count=12, scam_count=3, blocklist_count=1,
        groq_line="Groq: лимит кончился в 07:48 UTC, распознавание стояло 9 ч",
    )
    assert text == (
        "💓 tg-pars: жив • юзербот ✅ • чатов 3 • за 24ч: 5 находок • отсеяно повторов: 12"
        " • скам отсеян: 3 • из ЧС отсеяно: 1"
        " • Groq: лимит кончился в 07:48 UTC, распознавание стояло 9 ч"
    )


def test_heartbeat_zero_counts_stay_quiet():
    text = _format_heartbeat(3, 5, 2, True, True, repeat_count=0)
    assert text == "💓 tg-pars: жив • юзербот ✅ • чатов 3 • за 24ч: 5 находок • отсеяно повторов: 0 • ⏸ пауза"


async def test_build_heartbeat_text_reads_db_and_llm(config, tmp_path, monkeypatch):
    db = Database(str(tmp_path / "h.db"))
    await db.connect()
    try:
        await db.add_notified(1, None, "fp", None, -1, 1, delivered=False, suppressed=True, reason="repeat")
        await db.add_notified(1, None, "fp", None, -1, 2, delivered=False, suppressed=True, reason="repeat")
        await db.add_notified(2, None, "fp2", None, -1, 3, delivered=False, suppressed=True, reason="scam")
        c = _make_ctrl(config, db=db)
        monkeypatch.setattr(control, "_bot_instance", c)
        ub = Userbot(config, StubDedup(), StubLimiter(), StubLimiter(), db=db)
        c.userbot = ub

        async def authorized():
            return True

        c._heartbeat_authorized = authorized
        text = await c._build_heartbeat_text()
        assert "отсеяно повторов: 2" in text and "скам отсеян: 1" in text
        assert "Groq" not in text                     # пауз не было — строки нет
        ub.llm._block(3600, "test")
        assert "Groq: лимит кончился" in await c._build_heartbeat_text()
    finally:
        await db.close()
