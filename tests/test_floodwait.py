"""FloodWait handling in Userbot._send_dm_with_template / _forward_message."""

from telethon import errors

from bot.models import ForwardMode


def flood(seconds: int) -> errors.FloodWaitError:
    return errors.FloodWaitError(request=None, capture=seconds)


async def test_dm_retries_once_after_short_floodwait(userbot, monkeypatch):
    calls = []

    async def fake_send(peer, text):
        calls.append(peer)
        if len(calls) == 1:
            raise flood(0)

    monkeypatch.setattr(userbot.client, "send_message", fake_send)
    ok = await userbot._send_dm_with_template(111, "hi")
    assert ok is True
    assert len(calls) == 2
    assert userbot.dedup.dm_sent == [111]


async def test_dm_long_floodwait_pauses_and_notifies(userbot, monkeypatch):
    calls = []

    async def fake_send(peer, text):
        calls.append(peer)
        raise flood(900)

    monkeypatch.setattr(userbot.client, "send_message", fake_send)
    ok = await userbot._send_dm_with_template(111, "hi")
    assert ok is False
    assert len(calls) == 1                      # ретрая при длинном флуде нет
    assert userbot.dm_pause_remaining() > 800   # подсистема на паузе
    assert any("FloodWait" in n for n in userbot.test_notifications)

    # во время паузы DM не отправляются вовсе
    ok2 = await userbot._send_dm_with_template(222, "hi")
    assert ok2 is False
    assert len(calls) == 1


async def test_forward_long_floodwait_pauses(userbot, monkeypatch):
    userbot.config.actions.forward_mode = ForwardMode.FORWARD_RAW

    async def fake_forward(peer, msg):
        raise flood(900)

    monkeypatch.setattr(userbot.client, "forward_messages", fake_forward)
    ok = await userbot._forward_message(msg=object(), meta={})
    assert ok is False
    assert userbot.dm_pause_remaining() > 800


async def test_generic_error_still_returns_false(userbot, monkeypatch):
    async def fake_send(peer, text):
        raise RuntimeError("boom")

    monkeypatch.setattr(userbot.client, "send_message", fake_send)
    ok = await userbot._send_dm_with_template(111, "hi")
    assert ok is False
    assert userbot.dm_pause_remaining() == 0
