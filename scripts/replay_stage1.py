"""Прогон правил этапа 1 (скам, повторы) на истории из КОПИИ dedup.db.

Берёт таблицу messages (уже прошедшие ключи и фильтр цены), проигрывает её по времени
через те же функции, что и бот: is_scam_list + judge_repeat. Исходный файл не меняется:
открывается только на чтение, состояние `notified` — во временной БД.

    .venv/bin/python scripts/replay_stage1.py /tmp/dedup-copy.db [--window-days 7] [--samples 10]
"""

import argparse
import asyncio
import json
import os
import random
import re
import sqlite3
import sys
import tempfile
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot.listing import is_scam_list, judge_repeat, text_fingerprint  # noqa: E402
from db.database import Database, format_ts  # noqa: E402


def _load(path: str) -> list[dict]:
    con = sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True)
    con.row_factory = sqlite3.Row
    rows = []
    for r in con.execute("SELECT id, source_chat, author, text, ts, meta FROM messages ORDER BY ts, rowid"):
        meta = json.loads(r["meta"] or "{}")
        rows.append({
            "id": r["id"],
            "seller": int(r["author"]),
            "text": r["text"] or "",
            "ts": datetime.fromisoformat(r["ts"]),
            "price": meta.get("price"),
            "item": meta.get("matched_value"),
            "chat": r["source_chat"],
            "link": meta.get("link"),
        })
    con.close()
    return rows


def _token_diff(a: str, b: str) -> str:
    """Чем различаются два текста: 'same' | 'case/punct' | 'prices' | 'other'."""
    if a == b:
        return "same"
    norm = lambda t: re.sub(r"[\W_]+", " ", t.lower()).strip()
    if norm(a) == norm(b):
        return "case/punct"
    ta, tb = norm(a).split(), norm(b).split()
    only = set(ta) ^ set(tb)
    if only and all(re.fullmatch(r"\d+|eur|euro|евро|руб\w*|тыс\w*|к|k|р", w) for w in only):
        return "prices"
    return "other"


def _snip(text: str, n: int = 120) -> str:
    return re.sub(r"\s+", " ", text).strip()[:n]


async def replay(rows: list[dict], window_days: int):
    workdir = tempfile.mkdtemp(prefix="stage1-replay-")
    db = Database(os.path.join(workdir, "replay.db"))
    await db.connect()
    verdicts = []   # по порядку rows
    delivered_by_key: dict[tuple[int, str], dict] = {}
    try:
        for row in rows:
            text = row["text"]
            if is_scam_list(text):
                verdicts.append({"row": row, "kind": "scam"})
                continue
            v = await judge_repeat(db, row["seller"], text, row["price"], window_days, row["ts"])
            fp = v.text_fp
            created = format_ts(row["ts"])
            if v.repeat:
                await db.add_notified(row["seller"], row["item"], fp, row["price"], None, None,
                                      delivered=False, suppressed=True, reason="repeat", created_at=created)
                prev = delivered_by_key.get((row["seller"], fp))
                verdicts.append({"row": row, "kind": "repeat", "prev": prev})
            else:
                await db.add_notified(row["seller"], row["item"], fp, row["price"], None, None,
                                      delivered=True, suppressed=False,
                                      reason="price_drop" if v.price_drop_from else "", created_at=created)
                if fp:
                    delivered_by_key[(row["seller"], fp)] = row
                kind = "price_drop" if v.price_drop_from else ("no_text" if not fp else "new")
                verdicts.append({"row": row, "kind": kind, "was": v.price_drop_from})
    finally:
        await db.close()
    return verdicts


def report(verdicts: list[dict], samples: int, seed: int, window_days: int):
    total = len(verdicts)
    by = {}
    for v in verdicts:
        by[v["kind"]] = by.get(v["kind"], 0) + 1
    passed = total - by.get("scam", 0) - by.get("repeat", 0)
    sellers_scam = {v["row"]["seller"] for v in verdicts if v["kind"] == "scam"}
    print(f"сообщений в истории: {total}, окно повтора: {window_days} дн.")
    print(f"скам: {by.get('scam', 0)} (аккаунтов: {len(sellers_scam)})")
    print(f"повторов: {by.get('repeat', 0)}")
    print(f"дошло до владельца: {passed} (новых {by.get('new', 0)}, 📉 {by.get('price_drop', 0)}, без текста {by.get('no_text', 0)})")

    diffs = {}
    other = []
    for v in verdicts:
        if v["kind"] == "repeat" and v.get("prev"):
            d = _token_diff(v["row"]["text"], v["prev"]["text"])
            diffs[d] = diffs.get(d, 0) + 1
            if d == "other":
                other.append(v)
    print(f"\nсравнение исходных текстов повтора с доставленным: {diffs}")
    for v in other[:10]:
        print(f"  ! {_snip(v['row']['text'], 100)!r}\n    vs {_snip(v['prev']['text'], 100)!r}")

    rnd = random.Random(seed)
    dropped = [v for v in verdicts if v["kind"] in ("scam", "repeat")]
    kept = [v for v in verdicts if v["kind"] not in ("scam", "repeat")]
    print(f"\n=== {samples} случайных ОТСЕЯННЫХ (seed {seed}) ===")
    for v in rnd.sample(dropped, min(samples, len(dropped))):
        r = v["row"]
        why = "скам-шаблон" if v["kind"] == "scam" else f"повтор доставленного {v['prev']['ts']:%m-%d %H:%M}" if v.get("prev") else "повтор"
        print(f"- [{r['ts']:%m-%d %H:%M} продавец {r['seller']}] {_snip(r['text'])!r}\n    причина: {why}")
    print(f"\n=== {samples} случайных ПРОПУЩЕННЫХ (дошли до владельца) ===")
    for v in rnd.sample(kept, min(samples, len(kept))):
        r = v["row"]
        why = {"new": "новый текст", "price_drop": f"цена снижена, было {v.get('was')}",
               "no_text": "нет текста"}[v["kind"]]
        print(f"- [{r['ts']:%m-%d %H:%M} продавец {r['seller']}] {_snip(r['text'])!r}\n    причина: {why}")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("db", help="КОПИЯ dedup.db (читается только на чтение)")
    ap.add_argument("--window-days", type=int, default=7)
    ap.add_argument("--samples", type=int, default=10)
    ap.add_argument("--seed", type=int, default=11)
    args = ap.parse_args()
    verdicts = asyncio.run(replay(_load(args.db), args.window_days))
    report(verdicts, args.samples, args.seed, args.window_days)


if __name__ == "__main__":
    main()
