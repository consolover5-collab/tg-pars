"""Пробник моделей Groq по ключу бота: картинка + JSON-текст, с reasoning и без.

Запуск на kc (ключ читается из config.json, не печатается):
    ssh kc 'cd ~/tg-parsing-ux-refactor && python3 -' < scripts/groq_probe.py
Модели — в списке внизу.
"""
import json, urllib.request, urllib.error, base64, zlib, struct, time, sys
c = json.load(open("config.json")); key = c["vision"]["api_key"]
URL = "https://api.groq.com/openai/v1/chat/completions"

def png(w, h, px):
    raw = b"".join(b"\x00" + b"".join(bytes(px(x, y)) for x in range(w)) for y in range(h))
    def ch(t, d): return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
    return b"\x89PNG\r\n\x1a\n" + ch(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) + ch(b"IDAT", zlib.compress(raw)) + ch(b"IEND", b"")
img = png(96, 96, lambda x, y: (220, 20, 20) if x < 48 else (20, 40, 220))
b64 = base64.b64encode(img).decode()

def call(model, messages, extra, max_tokens, temp, json_mode=False):
    p = {"model": model, "messages": messages, "max_tokens": max_tokens, "temperature": temp}
    if json_mode: p["response_format"] = {"type": "json_object"}
    p.update(extra)
    req = urllib.request.Request(URL, data=json.dumps(p).encode(), headers={"Authorization": "Bearer " + key, "Content-Type": "application/json", "User-Agent": "Mozilla/5.0"})
    t = time.time()
    try:
        r = urllib.request.urlopen(req, timeout=40); d = json.load(r); h = r.headers
        m = d["choices"][0]; msg = m["message"]
        return "200 %.1fs finish=%s ctok=%s content=%r reasoning=%s rl_req=%s/%s" % (time.time()-t, m.get("finish_reason"), d["usage"].get("completion_tokens"), (msg.get("content") or "")[:90], "есть" if msg.get("reasoning") else "нет", h.get("x-ratelimit-remaining-requests"), h.get("x-ratelimit-limit-requests"))
    except urllib.error.HTTPError as e:
        return "HTTP %s %s" % (e.code, e.read().decode()[:160].replace("\n", " "))
    except Exception as e:
        return "ERR %s %s" % (type(e).__name__, str(e)[:100])

vis = [{"role": "user", "content": [{"type": "text", "text": "Какого цвета левая и правая половина картинки? Ответь кратко, по-русски."}, {"type": "image_url", "image_url": {"url": "data:image/png;base64," + b64}}]}]
txt = [{"role": "system", "content": "Верни JSON вида {\"intent\": \"...\", \"keyword\": \"...\"}."}, {"role": "user", "content": "хочу следить за объявлениями про велосипед Stels"}]
TUNED = {"qwen/qwen3.8-27b": {"reasoning_effort": "none"}, "openai/gpt-oss-120b": {"reasoning_effort": "low", "include_reasoning": False}, "openai/gpt-oss-20b": {"reasoning_effort": "low", "include_reasoning": False}}
for model in ["qwen/qwen3.8-27b", "openai/gpt-oss-120b", "openai/gpt-oss-20b"]:
    print("==", model)
    print(" картинка, как сейчас (256 ток, t=0.2):", call(model, vis, {}, 256, 0.2)); time.sleep(2)
    print(" картинка, reasoning выключен/low      :", call(model, vis, TUNED[model], 256, 0.2)); time.sleep(2)
    print(" JSON-текст, как сейчас (150 ток)      :", call(model, txt, {}, 150, 0.1, True)); time.sleep(2)
    print(" JSON-текст, reasoning выключен/low    :", call(model, txt, TUNED[model], 150, 0.1, True)); time.sleep(2)
