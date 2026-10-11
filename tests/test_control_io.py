"""control.py IO helpers: QR builder and async config save."""

import asyncio
import inspect
import json

from bot import control


def test_build_qr_png_returns_png():
    png = control._build_qr_png("tg://login?token=TEST")
    assert png[:8] == b"\x89PNG\r\n\x1a\n"


def test_save_config_is_async():
    assert inspect.iscoroutinefunction(control._save_config)


async def test_save_config_writes_file(config, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    class StubBot:
        pass

    stub = StubBot()
    stub.config = config
    monkeypatch.setattr(control, "_bot_instance", stub)

    await control._save_config()

    saved = json.loads((tmp_path / "config.json").read_text())
    assert saved["telegram"]["api_id"] == 1
    assert not (tmp_path / "config.json.tmp").exists()
