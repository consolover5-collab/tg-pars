"""owner_only outer middleware: global access gate for the control bot.

Covers bot.control.owner_only directly (registered on both router.message
and router.callback_query, outermost so it runs before every other
middleware and handler).
"""

from types import SimpleNamespace

from bot import control


class StubBotInstance:
    def __init__(self, owner_id):
        self.owner_id = owner_id


def _make_handler():
    calls = []

    async def handler(event, data):
        calls.append(event)
        return "handled"

    handler.calls = calls
    return handler


# ── owner set: only the owner passes ────────────────────────────────

async def test_owner_message_passes(monkeypatch):
    monkeypatch.setattr(control, "_bot_instance", StubBotInstance(owner_id=111))
    handler = _make_handler()
    event = SimpleNamespace(from_user=SimpleNamespace(id=111), text="/menu")

    result = await control.owner_only(handler, event, {})

    assert result == "handled"
    assert handler.calls == [event]


async def test_owner_callback_passes(monkeypatch):
    monkeypatch.setattr(control, "_bot_instance", StubBotInstance(owner_id=111))
    handler = _make_handler()
    event = SimpleNamespace(from_user=SimpleNamespace(id=111), data="menu")

    result = await control.owner_only(handler, event, {})

    assert result == "handled"
    assert handler.calls == [event]


async def test_non_owner_message_blocked(monkeypatch, caplog):
    monkeypatch.setattr(control, "_bot_instance", StubBotInstance(owner_id=111))
    handler = _make_handler()
    event = SimpleNamespace(from_user=SimpleNamespace(id=999), text="/menu")

    with caplog.at_level("DEBUG", logger="bot.control"):
        result = await control.owner_only(handler, event, {})

    assert result is None
    assert handler.calls == []
    assert "non-owner" in caplog.text


async def test_non_owner_callback_blocked(monkeypatch):
    monkeypatch.setattr(control, "_bot_instance", StubBotInstance(owner_id=111))
    handler = _make_handler()
    event = SimpleNamespace(from_user=SimpleNamespace(id=999), data="menu")

    result = await control.owner_only(handler, event, {})

    assert result is None
    assert handler.calls == []


# ── owner not yet captured: only /start passes ──────────────────────

async def test_owner_none_start_passes(monkeypatch):
    monkeypatch.setattr(control, "_bot_instance", StubBotInstance(owner_id=None))
    handler = _make_handler()
    event = SimpleNamespace(from_user=SimpleNamespace(id=555), text="/start")

    result = await control.owner_only(handler, event, {})

    assert result == "handled"
    assert handler.calls == [event]


async def test_owner_none_start_with_mention_and_payload_passes(monkeypatch):
    """/start@BotName payload (deep-link form) is still recognized as /start."""
    monkeypatch.setattr(control, "_bot_instance", StubBotInstance(owner_id=None))
    handler = _make_handler()
    event = SimpleNamespace(from_user=SimpleNamespace(id=555), text="/start@SomeBot payload")

    result = await control.owner_only(handler, event, {})

    assert result == "handled"


async def test_owner_none_other_message_blocked(monkeypatch, caplog):
    monkeypatch.setattr(control, "_bot_instance", StubBotInstance(owner_id=None))
    handler = _make_handler()
    event = SimpleNamespace(from_user=SimpleNamespace(id=555), text="/menu")

    with caplog.at_level("DEBUG", logger="bot.control"):
        result = await control.owner_only(handler, event, {})

    assert result is None
    assert handler.calls == []
    assert "owner not set yet" in caplog.text


async def test_owner_none_callback_blocked(monkeypatch):
    """A callback can never bootstrap the owner (only /start can)."""
    monkeypatch.setattr(control, "_bot_instance", StubBotInstance(owner_id=None))
    handler = _make_handler()
    event = SimpleNamespace(from_user=SimpleNamespace(id=555), data="menu")

    result = await control.owner_only(handler, event, {})

    assert result is None
    assert handler.calls == []


# ── no from_user (e.g. channel post) is always blocked ──────────────

async def test_no_from_user_blocked_even_for_start(monkeypatch):
    monkeypatch.setattr(control, "_bot_instance", StubBotInstance(owner_id=None))
    handler = _make_handler()
    event = SimpleNamespace(from_user=None, text="/start")

    result = await control.owner_only(handler, event, {})

    assert result is None
    assert handler.calls == []


async def test_no_from_user_blocked_with_owner_set(monkeypatch):
    monkeypatch.setattr(control, "_bot_instance", StubBotInstance(owner_id=111))
    handler = _make_handler()
    event = SimpleNamespace(from_user=None, data="menu")

    result = await control.owner_only(handler, event, {})

    assert result is None
    assert handler.calls == []


# ── defensive: middleware called before ControlBot exists ───────────

async def test_no_bot_instance_blocks_non_start(monkeypatch):
    monkeypatch.setattr(control, "_bot_instance", None)
    handler = _make_handler()
    event = SimpleNamespace(from_user=SimpleNamespace(id=1), text="/menu")

    result = await control.owner_only(handler, event, {})

    assert result is None
    assert handler.calls == []
