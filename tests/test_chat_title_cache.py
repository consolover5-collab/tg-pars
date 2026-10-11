"""_chat_title must hit the network at most once per chat_id."""

from types import SimpleNamespace


async def test_chat_title_cached(userbot, monkeypatch):
    calls = []

    async def fake_get_entity(chat_id):
        calls.append(chat_id)
        return SimpleNamespace(title="Барахолка", username=None)

    monkeypatch.setattr(userbot.client, "get_entity", fake_get_entity)
    msg = SimpleNamespace(chat_id=-100123)

    assert await userbot._chat_title(msg) == "Барахолка"
    assert await userbot._chat_title(msg) == "Барахолка"
    assert len(calls) == 1


async def test_chat_title_failure_not_cached(userbot, monkeypatch):
    calls = []

    async def fake_get_entity(chat_id):
        calls.append(chat_id)
        raise RuntimeError("offline")

    monkeypatch.setattr(userbot.client, "get_entity", fake_get_entity)
    msg = SimpleNamespace(chat_id=-100123)

    assert await userbot._chat_title(msg) == "-100123"
    assert await userbot._chat_title(msg) == "-100123"
    assert len(calls) == 2  # фейл не кешируем — попробуем снова
