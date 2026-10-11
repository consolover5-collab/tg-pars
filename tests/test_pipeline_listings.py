"""Userbot._process_message: ранний отсев (ЧС, скам), повторы объявлений, учёт доставки."""

import asyncio
from types import SimpleNamespace

import pytest

from bot.dedup import DedupChecker
from bot.models import Config
from bot.userbot import Userbot
from db.database import Database

from tests.test_listing import SCAM_TEXTS

TEXT = "Продам колонку JBL Flip 5, отличное состояние. Цена 50€"


class Limiter:
    max_tokens = 99

    def consume(self) -> bool:
        return True


class FakeMsg:
    _next_id = 1000

    def __init__(self, text, sender=1, chat=-100500, username=None, photo=None):
        FakeMsg._next_id += 1
        self.id = FakeMsg._next_id
        self.text = text
        self.message = text
        self.sender_id = sender
        self.sender = SimpleNamespace(username=username)
        self.chat_id = chat
        self.chat = SimpleNamespace(username="baraholka")
        self.photo = photo
        self.grouped_id = None


@pytest.fixture
async def db(tmp_path):
    d = Database(str(tmp_path / "test.db"))
    await d.connect()
    yield d
    await d.close()


@pytest.fixture
def cfg(tmp_path) -> Config:
    return Config(**{
        "telegram": {"api_id": 1, "api_hash": "t", "phone": "+1", "session_name": str(tmp_path / "s"),
                     "bot_token": "42:T"},
        "monitoring": {"keywords": ["колонка"], "chats": ["x"]},
        "actions": {"dm_delay_min": 0, "dm_delay_max": 0},
    })


class Harness:
    def __init__(self, ub, sent):
        self.ub = ub
        self.sent = sent          # тексты уведомлений
        self.deliver = True       # False — уведомление не дошло

    async def run(self, msg):
        await self.ub._process_message(msg)


@pytest.fixture
async def h(cfg, db):
    sent = []
    state = {"ok": True}

    async def notify(text, matched_keyword=""):
        sent.append(text)
        return state["ok"]

    ub = Userbot(cfg, DedupChecker(db), Limiter(), Limiter(), db, notify_callback=notify)

    async def title(msg):
        return "Барахолка"

    ub._chat_title = title
    harness = Harness(ub, sent)
    harness.state = state
    return harness


async def _notified(db):
    async with db._db.execute("SELECT * FROM notified ORDER BY id") as cur:
        return [dict(r) for r in await cur.fetchall()]


# ── ранний отсев ────────────────────────────────────────────────────

async def test_scam_dropped_silently_before_matching(h, db):
    h.ub.matcher.match = lambda text: pytest.fail("скам не должен доходить до ключей")
    await h.run(FakeMsg(SCAM_TEXTS[1], sender=777))
    assert h.sent == []
    rows = await _notified(db)
    assert [(r["reason"], r["suppressed"], r["seller_id"]) for r in rows] == [("scam", 1, 777)]
    async with db._db.execute("SELECT COUNT(*) FROM messages") as cur:
        assert (await cur.fetchone())[0] == 0
    assert await db.count_notified_since(24) == {"scam": 1}


async def test_scam_does_not_spend_groq(h):
    h.ub.config.monitoring.use_text_nlp = True
    called = []

    async def fake_analyse(*a, **k):
        called.append(1)

    import bot.userbot as ubm
    orig = ubm.analyse_text
    ubm.analyse_text = fake_analyse
    try:
        await h.run(FakeMsg(SCAM_TEXTS[0]))
    finally:
        ubm.analyse_text = orig
    assert called == []


async def test_blocklisted_seller_dropped_by_id_and_username(h, db):
    h.ub.config.rules.opt_out_list = [555, "@badguy"]
    await h.run(FakeMsg(TEXT, sender=555))
    await h.run(FakeMsg(TEXT, sender=556, username="BadGuy"))
    await h.run(FakeMsg(TEXT, sender=557, username="honest"))
    assert len(h.sent) == 1                       # дошёл только честный
    reasons = [r["reason"] for r in await _notified(db) if r["suppressed"]]
    assert reasons == ["blocklist", "blocklist"]


# ── правило повтора ─────────────────────────────────────────────────

async def test_same_text_twice_one_notification(h, db):
    await h.run(FakeMsg(TEXT))
    await h.run(FakeMsg(TEXT, chat=-100777))      # кросспост в другой чат
    assert len(h.sent) == 1
    assert "🔄" not in "".join(h.sent)            # «Повтор» владельцу больше не шлём
    rows = await _notified(db)
    assert [(r["delivered"], r["suppressed"], r["reason"]) for r in rows] == [(1, 0, ""), (0, 1, "repeat")]
    assert await db.count_matches_since(24) == (1, 1)
    assert await db.count_notified_since(24) == {"repeat": 1}


async def test_different_text_same_item_notified(h):
    await h.run(FakeMsg(TEXT))
    await h.run(FakeMsg("Продаю колонку JBL Charge 4, новая, 70€"))
    assert len(h.sent) == 2


async def test_price_drop_notified_with_note(h):
    await h.run(FakeMsg(TEXT))                    # 50
    await h.run(FakeMsg(TEXT.replace("50€", "40€")))
    assert len(h.sent) == 2
    assert "📉 цена снижена: было 50" in h.sent[1]
    assert "📉" not in h.sent[0]
    # и дальше окно считается от сниженной цены
    await h.run(FakeMsg(TEXT.replace("50€", "40€")))
    assert len(h.sent) == 2


async def test_price_up_or_vanished_is_repeat(h):
    base = "Продам колонку JBL Flip 5, отличное состояние."
    await h.run(FakeMsg(base + " 50€"))
    await h.run(FakeMsg(base + " 60€"))   # выросла
    await h.run(FakeMsg(base))            # цена пропала
    assert len(h.sent) == 1


async def test_photo_without_text_not_suppressed(h, cfg):
    cfg.rules.vision_enabled = True
    cfg.vision.api_key = "k"
    cfg.monitoring.vision_require_listing_signal = False

    async def vision(msg):
        return {"type": "колонка", "price": 30}

    h.ub._try_vision = vision
    await h.run(FakeMsg("", photo=object()))
    await h.run(FakeMsg("", photo=object()))
    assert len(h.sent) == 2


async def test_no_dedup_seller_always_notified(h, cfg):
    cfg.actions.no_dedup_ids = [1]
    await h.run(FakeMsg(TEXT))
    await h.run(FakeMsg(TEXT))
    assert len(h.sent) == 2


async def test_undelivered_is_not_a_repeat(h, db):
    h.state["ok"] = False
    await h.run(FakeMsg(TEXT))
    h.state["ok"] = True
    await h.run(FakeMsg(TEXT))
    assert len(h.sent) == 2
    rows = await _notified(db)
    assert [(r["delivered"], r["reason"]) for r in rows] == [(0, "undelivered"), (1, "")]
    await h.run(FakeMsg(TEXT))                    # теперь доставлено — повтор
    assert len(h.sent) == 2


async def test_two_crossposts_at_once_give_one_notification(h, db):
    started = asyncio.Event()
    orig = h.ub.notify

    async def slow_notify(text, matched_keyword=""):
        started.set()
        await asyncio.sleep(0.1)                  # пока первый «отправляет», второй уже ждёт
        return await orig(text, matched_keyword)

    h.ub.notify = slow_notify
    msgs = [FakeMsg(TEXT, chat=-100 - i) for i in range(4)]
    await asyncio.gather(*(h.run(m) for m in msgs))
    assert len(h.sent) == 1
    rows = await _notified(db)
    assert sorted((r["delivered"], r["suppressed"]) for r in rows) == [(0, 1)] * 3 + [(1, 0)]
    assert h.ub._listing_locks == {}              # замки не копятся


async def test_other_sellers_not_blocked_by_lock(h):
    """Замок по (продавец, текст): медленная доставка одного не держит остальных."""
    gate = asyncio.Event()
    orig = h.ub.notify

    async def gated(text, matched_keyword=""):
        if "JBL Flip 5" in text:
            await gate.wait()
        return await orig(text, matched_keyword)

    h.ub.notify = gated
    slow = asyncio.create_task(h.run(FakeMsg(TEXT, sender=1)))
    await asyncio.sleep(0.05)
    await asyncio.wait_for(h.run(FakeMsg("Продаю колонку Sony, 20€", sender=2)), 2)
    assert len(h.sent) == 1
    gate.set()
    await slow
    assert len(h.sent) == 2


async def test_dm_cooldown_does_not_hide_new_listing(h, cfg, db):
    """Продавцу уже писали (кулдаун DM): другое объявление владельцу всё равно показываем, DM не шлём."""
    cfg.actions.auto_dm = True
    cfg.actions.dry_run = True
    await h.run(FakeMsg(TEXT, sender=9))
    await db.mark_dm_sent(9)
    dms = []
    orig = h.ub._send_dm_with_template

    async def spy(seller_id, text):
        dms.append(seller_id)
        return await orig(seller_id, text)

    h.ub._send_dm_with_template = spy
    await h.run(FakeMsg("Продаю колонку Sony, 20€", sender=9))
    assert len(h.sent) == 2
    assert dms == []


# ── формат уведомления ──────────────────────────────────────────────

async def test_notification_price_has_no_ruble_sign(h):
    await h.run(FakeMsg("Продам колонку JBL 12 500€"))
    assert "₽" not in h.sent[0]
    assert "💰 Цена: 500\n" in h.sent[0]
