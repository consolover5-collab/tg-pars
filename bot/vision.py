"""Groq Vision API (OpenAI-compatible) — analyse photos for relevant items."""

import base64
import logging

from bot.llm import get_client
from bot.models import VisionConfig

logger = logging.getLogger(__name__)


def build_vision_prompt(keywords: list[str]) -> str:
    """Единый промпт классификации фото, собирается из текущих ключевых слов."""
    kw_list = ", ".join(keywords[:15]) or "товар"
    return (
        f"На фото один из товаров: {kw_list}? "
        "Если да — ответь: ТИП: ..., ЦЕНА: ... Если нет — ответь: НЕТ"
    )


async def analyse_image(
    image_bytes: bytes,
    prompt: str,
    config: VisionConfig,
    timeout: float = 30.0,
    llm=None,
) -> str | None:
    """Send image to Groq Vision and return the model's text reply (or None on error)."""
    b64 = base64.b64encode(image_bytes).decode()
    llm = llm or get_client(config)
    return await llm.chat(
        [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
                    },
                ],
            }
        ],
        max_tokens=256,
        temperature=0.2,
        kind="vision",
        timeout=timeout,
    )


def parse_vision_response(text: str) -> dict | None:
    """Parse vision model reply. Returns {'type': ..., 'price': ...} or None if 'НЕТ'.

    Handles both multi-line and single-line formats:
      "ТИП: телевизор, ЦЕНА: 30000"
      "ТИП: телевизор\\nЦЕНА: 30000"
    """
    if not text:
        return None
    upper = text.upper().strip()
    if upper.startswith("НЕТ") or upper == "НЕТ":
        return None

    import re

    result: dict = {"type": None, "price": None}

    # Normalize: split on newlines OR commas followed by known field names
    normalized = re.sub(r',\s*(?=(?:ТИП|ЦЕНА)\s*:)', '\n', text, flags=re.IGNORECASE)

    for line in normalized.splitlines():
        line = line.strip()
        if line.upper().startswith("ТИП:"):
            result["type"] = line.split(":", 1)[1].strip()
        elif line.upper().startswith("ЦЕНА:"):
            raw = line.split(":", 1)[1].strip()
            digits = "".join(c for c in raw if c.isdigit())
            if digits:
                result["price"] = int(digits)

    if result["type"]:
        return result
    return None
