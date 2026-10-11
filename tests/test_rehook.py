"""update_monitored_chats: re-register NewMessage handler without process restart."""


async def test_update_replaces_handler(userbot, monkeypatch):
    async def fake_resolve(chats):
        return [f"resolved:{c}" for c in chats]

    monkeypatch.setattr(userbot, "_resolve_chats", fake_resolve)
    userbot._started = True

    userbot.config.monitoring.chats = ["@one"]
    assert await userbot.update_monitored_chats() is True
    assert len(userbot.client.list_event_handlers()) == 1

    old_handler = userbot._message_handler

    userbot.config.monitoring.chats = ["@one", "@two"]
    assert await userbot.update_monitored_chats() is True
    handlers = userbot.client.list_event_handlers()
    assert len(handlers) == 1  # старый снят, новый добавлен
    assert userbot._message_handler is not old_handler  # handler object actually swapped


async def test_failed_update_keeps_old_handler(userbot, monkeypatch):
    async def fake_resolve(chats):
        return [f"resolved:{c}" for c in chats]

    monkeypatch.setattr(userbot, "_resolve_chats", fake_resolve)
    userbot._started = True

    userbot.config.monitoring.chats = ["@one"]
    assert await userbot.update_monitored_chats() is True
    handlers_before = userbot.client.list_event_handlers()
    assert len(handlers_before) == 1
    old_handler = userbot._message_handler

    async def failing_resolve(chats):
        return []

    monkeypatch.setattr(userbot, "_resolve_chats", failing_resolve)
    userbot.config.monitoring.chats = ["@broken"]
    assert await userbot.update_monitored_chats() is False

    handlers_after = userbot.client.list_event_handlers()
    assert len(handlers_after) == len(handlers_before)  # old handler untouched
    assert userbot._message_handler is old_handler


async def test_update_with_empty_chats_removes_handler(userbot, monkeypatch):
    async def fake_resolve(chats):
        return [f"resolved:{c}" for c in chats]

    monkeypatch.setattr(userbot, "_resolve_chats", fake_resolve)
    userbot._started = True

    userbot.config.monitoring.chats = ["@one"]
    await userbot.update_monitored_chats()
    userbot.config.monitoring.chats = []
    assert await userbot.update_monitored_chats() is True
    assert len(userbot.client.list_event_handlers()) == 0


async def test_update_before_start_returns_false(userbot):
    assert await userbot.update_monitored_chats() is False
