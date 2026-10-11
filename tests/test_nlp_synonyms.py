"""expand_synonyms: дубли и само ключевое слово отбрасываются."""

import json
from types import SimpleNamespace

from aiohttp import web
from aiohttp.test_utils import TestServer

from bot import http, nlp
from bot.models import VisionConfig


async def test_expand_synonyms_dedup():
    reply = {"synonyms": ["Осушитель", "dehumidifier", "осушка", "Dehumidifier", " осушка ", "влагопоглотитель"]}

    async def handler(request):
        content = json.dumps(reply, ensure_ascii=False)
        return web.json_response({"choices": [{"message": {"content": content}}]})

    app = web.Application()
    app.router.add_post("/", handler)
    server = TestServer(app)
    await server.start_server()
    try:
        cfg = SimpleNamespace(vision=VisionConfig(api_key="x", base_url=str(server.make_url("/"))))
        got = await nlp.expand_synonyms("осушитель", ["влагопоглотитель"], cfg)
        assert got == ["dehumidifier", "осушка"]
    finally:
        await server.close()
        await http.close_session()
