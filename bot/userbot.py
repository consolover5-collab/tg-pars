"""Telethon userbot: monitors chats in real-time, sends DMs to sellers."""

import asyncio
import logging
import random
import time

from telethon import TelegramClient, events
from telethon import errors
from telethon.errors import (
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    SessionPasswordNeededError,
)
from telethon.tl.types import Message

from bot.models import Config, ForwardMode
from bot.keywords import KeywordMatcher
from bot.price import extract_price
from bot.dedup import DedupChecker
from bot.ratelimit import RateLimiter
from bot.vision import analyse_image, build_vision_prompt, parse_vision_response
from bot.nlp import analyse_text, generate_dm, looks_like_listing, is_not_listing
from bot.processor import MessageProcessor
from db.database import Database

logger = logging.getLogger(__name__)

FLOOD_RETRY_CAP = 60  # секунд: floods короче — sleep + один retry; длиннее — пауза подсистемы


class Userbot:
    def __init__(
        self,
        config: Config,
        dedup: DedupChecker,
        dm_limiter: RateLimiter,
        vision_limiter: RateLimiter,
        db: Database,
        notify_callback=None,
    ):
        self.config = config
        self.dedup = dedup
        self.dm_limiter = dm_limiter
        self.vision_limiter = vision_limiter
        self.db = db
        self.notify = notify_callback  # async fn(text: str, matched_keyword: str = "")
        self.matcher = KeywordMatcher(
            config.monitoring.keywords,
            keyword_map=config.rules.keyword_map,
        )
        self.nlp_limiter = RateLimiter(
            max_tokens=config.monitoring.text_nlp_per_minute,
            period_seconds=60.0,
        )
        self.paused = False
        self.processor = MessageProcessor(config, db)

        self.client = TelegramClient(
            config.telegram.session_name,
            config.telegram.api_id,
            config.telegram.api_hash,
            entity_cache_limit=100,  # дефолт 5000 entity — лишние десятки MB RSS на 1GB-хосте
        )

        # album debounce: group_id -> list[Message]
        self._album_buf: dict[int, list[Message]] = {}
        self._album_tasks: dict[int, asyncio.Task] = {}
        self._chat_titles: dict[int, str] = {}  # chat_id -> title (титул меняется редко)
        self._started = False
        self._message_handler = None
        self._dm_paused_until: float = 0.0
        self._pending_qr_login = None
        self._phone_code_hash: str | None = None
        self._phone_code_requested_at: float | None = None
        self._last_code_delivery: str | None = None
        self._last_code_timeout: int | None = None

    async def start(self) -> bool:
        if self._started:
            return True
        await self.client.connect()
        if not await self.client.is_user_authorized():
            logger.error(
                "Userbot is not authorized yet. "
                "Use control bot: Settings -> Authorize userbot."
            )
            return False
        self.client.session.save()
        logger.info("Userbot authorised as %s", (await self.client.get_me()).first_name)

        chats = self.config.monitoring.chats
        if not chats:
            logger.warning("No chats to monitor")
            self._started = True
            return True

        resolved_ids = await self._resolve_chats(chats)
        if not resolved_ids:
            logger.error("No chats could be resolved, monitoring disabled")
            self._started = True
            return True

        self._register_handler(resolved_ids)
        logger.info("Monitoring %d chats: %s", len(chats), chats)
        self._started = True
        return True

    async def _resolve_chats(self, chats: list) -> list:
        """Pre-resolve chat refs to input entities for reliable event filtering."""
        resolved_ids = []
        for chat in chats:
            try:
                # Convert numeric strings to int — Telethon needs int for channel IDs
                chat_ref = int(chat) if isinstance(chat, str) and chat.lstrip("-").isdigit() else chat
                entity = await self.client.get_input_entity(chat_ref)
                resolved_ids.append(entity)
                logger.debug("Resolved chat %s -> %s", chat, entity)
            except Exception as e:
                logger.warning("Cannot resolve chat %s: %s", chat, e)
        return resolved_ids

    def _register_handler(self, resolved_ids: list):
        async def on_message(event: events.NewMessage.Event):
            try:
                if self.paused:
                    return
                msg: Message = event.message
                if msg.grouped_id:
                    await self._handle_album(msg)
                else:
                    await self._process_message(msg)
            except Exception:
                logger.exception("on_message error")

        self._message_handler = on_message
        self.client.add_event_handler(
            on_message, events.NewMessage(chats=resolved_ids, incoming=True)
        )

    async def update_monitored_chats(self) -> bool:
        """Перечитать config.monitoring.chats и перевесить handler без рестарта процесса."""
        if not self._started:
            return False
        chats = self.config.monitoring.chats
        if not chats:
            logger.warning("Chat list is now empty, monitoring disabled")
            if self._message_handler is not None:
                self.client.remove_event_handler(self._message_handler)
                self._message_handler = None
            return True
        resolved_ids = await self._resolve_chats(chats)
        if not resolved_ids:
            logger.error("No chats could be resolved after update")
            return False
        if self._message_handler is not None:
            self.client.remove_event_handler(self._message_handler)
            self._message_handler = None
        self._register_handler(resolved_ids)
        logger.info("Re-hooked monitoring for %d chats: %s", len(chats), chats)
        return True

    async def create_qr_login_link(self) -> str | None:
        await self.client.connect()
        if await self.client.is_user_authorized():
            return None
        self._pending_qr_login = await self.client.qr_login()
        return self._pending_qr_login.url

    async def wait_qr_login(self, timeout: int = 120) -> str:
        if not self._pending_qr_login:
            return "no_qr"
        try:
            await self._pending_qr_login.wait(timeout=timeout)
            self._pending_qr_login = None
            self.client.session.save()
            return "ok" if await self.client.is_user_authorized() else "failed"
        except asyncio.TimeoutError:
            self._pending_qr_login = None
            return "timeout"
        except SessionPasswordNeededError:
            self._pending_qr_login = None
            return "need_2fa"

    async def request_login_code(self, force_sms: bool = False) -> str:
        await self.client.connect()
        if await self.client.is_user_authorized():
            return "already_authorized"

        now = time.time()
        if self._phone_code_hash and self._phone_code_requested_at and now - self._phone_code_requested_at > 180:
            logger.info("request_login_code: dropping stale hash")
            self._phone_code_hash = None

        # Reuse pending request unless user explicitly asks for SMS fallback.
        if self._phone_code_hash and not force_sms:
            logger.info("request_login_code: reusing pending hash")
            return "already_sent"

        try:
            sent = await self.client.send_code_request(self.config.telegram.phone, force_sms=force_sms)
            if getattr(sent, "phone_code_hash", None):
                self._phone_code_hash = sent.phone_code_hash
            self._phone_code_requested_at = now
            self._last_code_delivery = type(getattr(sent, "type", None)).__name__
            timeout = getattr(sent, "timeout", None)
            self._last_code_timeout = int(timeout) if isinstance(timeout, (int, float)) else None
            logger.info(
                "request_login_code: type=%s force_sms=%s timeout=%s",
                self._last_code_delivery,
                force_sms,
                self._last_code_timeout,
            )
            return "sms_sent" if force_sms else "sent"
        except Exception as e:
            msg = str(e).lower()
            if "resendcoderequest" in msg or "all available options for this type of number were already used" in msg:
                logger.warning("request_login_code: resend limited by Telegram")
                return "already_sent" if self._phone_code_hash else "resend_limited"
            logger.error("request_login_code failed: %s", e)
            return "error"

    async def sign_in_with_code(self, code: str) -> str:
        code = "".join(code.split())  # strip spaces (user types "1 2 3 4 5")
        await self.client.connect()
        if not self._phone_code_hash:
            logger.warning("sign_in_with_code: no hash set")
            return "no_code_request"
        try:
            logger.info(f"sign_in_with_code: attempting with hash={self._phone_code_hash[:10]}...")
            await self.client.sign_in(
                self.config.telegram.phone,
                code,
                phone_code_hash=self._phone_code_hash,
            )
            self._phone_code_hash = None
            self._phone_code_requested_at = None
            self._last_code_delivery = None
            self._last_code_timeout = None
            self.client.session.save()
            return "ok" if await self.client.is_user_authorized() else "failed"
        except PhoneCodeInvalidError:
            return "invalid_code"
        except PhoneCodeExpiredError:
            self._phone_code_hash = None
            self._phone_code_requested_at = None
            self._last_code_delivery = None
            self._last_code_timeout = None
            return "expired_code"
        except SessionPasswordNeededError:
            return "need_2fa"
        except Exception as e:
            logger.error("Code sign-in failed: %s", e)
            return "error"

    def get_code_delivery_hint(self) -> str:
        t = self._last_code_delivery or ""
        if "Sms" in t:
            base = "Код отправлен по SMS."
        elif "App" in t:
            base = "Код отправлен в Telegram (сервисный чат 777000)."
        elif "Call" in t:
            base = "Код будет продиктован звонком Telegram."
        elif t:
            base = f"Тип доставки кода: {t}."
        else:
            base = ""
        if self._last_code_timeout:
            base = (base + f" Повторный запрос обычно доступен через ~{self._last_code_timeout}с.").strip()
        return base

    async def stop(self):
        # Cancel pending album tasks
        for task in self._album_tasks.values():
            task.cancel()
        self._album_tasks.clear()
        self._album_buf.clear()
        await self.client.disconnect()
        self._started = False
        logger.info("Userbot disconnected")

    # ── Album debounce ─────────────────────────────────────────────

    async def _handle_album(self, msg: Message):
        gid = msg.grouped_id
        if gid not in self._album_buf:
            self._album_buf[gid] = []
        self._album_buf[gid].append(msg)

        # Reset debounce timer
        if gid in self._album_tasks:
            self._album_tasks[gid].cancel()
        self._album_tasks[gid] = asyncio.create_task(self._flush_album(gid))

    async def _flush_album(self, gid: int):
        await asyncio.sleep(1.5)  # wait for all parts
        messages = self._album_buf.pop(gid, [])
        self._album_tasks.pop(gid, None)
        if messages:
            # Use first message with text, process photos from first message
            primary = messages[0]
            for m in messages:
                if m.text:
                    primary = m
                    break
            try:
                await self._process_message(primary)
            except Exception:
                logger.exception("_flush_album error (group_id=%s)", gid)

    # ── Main pipeline ──────────────────────────────────────────────

    def _msg_link(self, msg: Message) -> str:
        """Build a clickable Telegram message link."""
        try:
            if msg.chat.username:
                return f"https://t.me/{msg.chat.username}/{msg.id}"
        except AttributeError:
            pass
        cid = abs(msg.chat_id)
        if cid > 1_000_000_000:
            cid = int(str(cid)[3:])  # strip -100 prefix for t.me/c/ format
        return f"https://t.me/c/{cid}/{msg.id}"

    async def _process_message(self, msg: Message):
        seller_id = msg.sender_id
        if not seller_id:
            return
        seller_username = getattr(msg.sender, 'username', None) if msg.sender else None

        chat = await self._chat_title(msg)
        chat_external = str(msg.chat_id)
        match_type = None
        matched_value = None
        price = None
        nlp_dm_text: str | None = None

        # Step 1: keyword/synonym match on text
        if msg.text:
            # Skip messages that are clearly not listings (job ads, requests, etc.)
            if is_not_listing(msg.text):
                logger.debug("Skipped non-listing message: %s", msg.text[:60])
                return

            kw = self.matcher.match(msg.text)
            if kw:
                match_type = "keyword"
                # Resolve synonym stem back to category key (e.g. "акустик" → "колонка")
                resolved = self.matcher.resolve_key(kw)
                if resolved:
                    matched_value = resolved
                else:
                    kmap_val = self.config.rules.keyword_map.get(kw)
                    matched_value = kmap_val if isinstance(kmap_val, str) else kw
                price = extract_price(msg.text)

        # Step 1b: Groq text NLP fallback (when keywords miss + looks like a listing)
        if not match_type and msg.text and self.config.monitoring.use_text_nlp:
            if looks_like_listing(msg.text):
                targets = self.config.monitoring.keywords or list(self.config.rules.keyword_map.keys())
                nlp = await analyse_text(msg.text, targets, self.config, self.nlp_limiter)
                if nlp and nlp["match"]:
                    match_type = "nlp"
                    matched_value = nlp["type"] or targets[0] if targets else "товар"
                    price = price or nlp["price"]
                    nlp_dm_text = nlp["dm"] or None

        # Step 2: vision (if no match yet and photo present)
        if not match_type and msg.photo and self.config.rules.vision_enabled:
            vision_result = await self._try_vision(msg)
            if vision_result:
                vision_type = vision_result.get("type", "")
                # Validate: vision result must relate to one of our keywords
                if self._vision_matches_keywords(vision_type):
                    match_type = "vision"
                    matched_value = vision_type
                    price = vision_result.get("price")
                else:
                    logger.debug("Vision type '%s' not in keywords, skipping", vision_type)

        if not match_type:
            return  # no match

        # Step 3: price filter
        max_price = self.config.monitoring.max_price
        if max_price and price and price > max_price:
            logger.debug("Price %d > max %d, skipping", price, max_price)
            return

        # Prepare metadata
        link = self._msg_link(msg)
        meta = {
            "type": matched_value,
            "price": price,
            "link": link,
            "author": f"@{seller_username}" if seller_username else str(seller_id),
            "chat_title": chat,
            "source_chat": chat_external,
            "message_snippet": (msg.text or "")[:200],
            "match_type": match_type,
            "matched_value": matched_value,
        }

        # Store message
        msg_uuid = await self.processor.store_message(
            chat_external=chat_external,
            chat_title=chat,
            message_id=msg.id,
            author_id=seller_id,
            text=msg.text or "",
            meta=meta
        )

        # Step 4: dedup check (bypass for no_dedup_ids)
        _no_dedup_ids = self.config.actions.no_dedup_ids or []
        no_dedup = seller_id in _no_dedup_ids
        if not no_dedup and seller_username:
            _un = seller_username.lower()
            no_dedup = any(
                isinstance(e, str) and e.lstrip('@').lower() == _un
                for e in _no_dedup_ids
            )
        is_dup = False if no_dedup else await self.dedup.is_seen(
            seller_id, cooldown_hours=self.config.actions.dm_cooldown_hours
        )

        if is_dup:
            await self.dedup.record_match(
                seller_id, msg.chat_id, msg.id,
                match_type, matched_value, price, is_duplicate=True,
            )
            await self._notify_duplicate(chat, seller_id)
            await self.db.log_action(msg_uuid, "duplicate", "skipped", {"reason": "seller in cooldown"})
        else:
            # Всегда регистрируем продавца: matches.seller_id ссылается на seen_sellers
            # (FK enforced), а кулдаун для no_dedup-продавцов всё равно не проверяется.
            await self.dedup.register(
                seller_id, chat, msg.id, match_type, matched_value, price,
            )
            await self.dedup.record_match(
                seller_id, msg.chat_id, msg.id,
                match_type, matched_value, price, is_duplicate=False,
            )

            # Decide actions
            actions = self.processor.decide_actions(chat_external, seller_id, meta, username=seller_username)

            # Override DM text with Groq-generated version if applicable
            if actions.get("should_dm") and self.config.actions.use_groq_dm:
                groq_text = nlp_dm_text  # already generated during NLP step
                if not groq_text:
                    groq_text = await generate_dm(
                        msg.text or matched_value, matched_value, self.config, price=price
                    )
                if groq_text:
                    actions = dict(actions, dm_text=groq_text)

            dm_sent = False
            forward_sent = False

            if actions["should_forward"]:
                forward_sent = await self._forward_message(msg, meta, matched_keyword=matched_value)
                await self.db.log_action(
                    msg_uuid, "forward",
                    "success" if forward_sent else "failed",
                    {"mode": self.config.actions.forward_mode.value, "dry_run": self.config.actions.dry_run}
                )

            if actions["should_dm"]:
                dm_sent = await self._send_dm_with_template(seller_id, actions["dm_text"])
                await self.db.log_action(
                    msg_uuid, "dm",
                    "success" if dm_sent else "failed",
                    {"template_used": True, "groq_dm": self.config.actions.use_groq_dm,
                     "dry_run": self.config.actions.dry_run}
                )

            await self._notify_new(meta, dm_sent, forward_sent)

    # ── Vision ─────────────────────────────────────────────────────

    async def _try_vision(self, msg: Message) -> dict | None:
        if not self.config.vision.api_key:
            return None

        # Caption pre-filter: skip photos that don't look like listings
        if self.config.monitoring.vision_require_listing_signal:
            caption = (msg.text or msg.message or "").strip()
            if not looks_like_listing(caption):
                logger.debug("Vision skipped: caption has no listing signal")
                return None

        if not self.vision_limiter.consume():
            logger.debug("Vision rate limit reached, skipping")
            return None

        # Auto-generate vision prompt from current keywords
        prompt = build_vision_prompt(self.config.monitoring.keywords)

        try:
            photo_bytes = await self.client.download_media(msg, bytes)
            if not photo_bytes:
                return None
            reply = await analyse_image(
                photo_bytes,
                prompt,
                self.config.vision,
            )
            return parse_vision_response(reply) if reply else None
        except Exception as e:
            logger.error("Vision processing error: %s", e)
            return None


    # ── DM ─────────────────────────────────────────────────────────

    async def _send_dm_with_template(self, seller_id: int, text: str) -> bool:
        """Send DM with rendered template, with human-like delay."""
        if self.dm_pause_remaining() > 0:
            logger.warning("DM to %d skipped: FloodWait pause %.0fs left", seller_id, self.dm_pause_remaining())
            return False

        if not self.dm_limiter.consume():
            logger.warning("DM rate limit reached for seller %d", seller_id)
            return False

        # Human-like delay
        delay_min = self.config.actions.dm_delay_min
        delay_max = max(delay_min, self.config.actions.dm_delay_max)
        delay = random.uniform(delay_min, delay_max)

        if self.config.actions.dry_run:
            logger.info(
                "[DRY RUN] Would send DM to %d in %.0fs: %s", seller_id, delay, text[:100]
            )
            return True

        logger.info("Waiting %.0fs before DM to seller %d (human-like delay)", delay, seller_id)
        await asyncio.sleep(delay)

        for attempt in (1, 2):
            try:
                await self.client.send_message(seller_id, text)
                await self.dedup.mark_dm_sent(seller_id)
                logger.info("DM sent to seller %d", seller_id)
                return True
            except errors.FloodWaitError as e:
                if attempt == 1 and e.seconds <= FLOOD_RETRY_CAP:
                    logger.warning("FloodWait %ds on DM, retrying once", e.seconds)
                    await asyncio.sleep(e.seconds + 1)
                    continue
                await self._flood_pause(e.seconds, "DM")
                return False
            except Exception as e:
                logger.error("Failed to send DM to %d: %s", seller_id, e)
                return False
        return False

    async def _forward_message(self, msg: Message, meta: dict, matched_keyword: str = "") -> bool:
        """Forward message to main bot or send notification."""
        if self.dm_pause_remaining() > 0:
            logger.warning("Forward skipped: FloodWait pause %.0fs left", self.dm_pause_remaining())
            return False

        notify_chat_id = self.config.actions.notify_chat_id

        if self.config.actions.dry_run:
            logger.info("[DRY RUN] Would forward message to %s (mode=%s)", notify_chat_id, self.config.actions.forward_mode)
            return True

        try:
            if self.config.actions.forward_mode == ForwardMode.FORWARD_RAW:
                # Forward the actual message
                await self.client.forward_messages(notify_chat_id, msg)
                logger.info("Message forwarded to %s", notify_chat_id)
                return True
            else:
                # Send notification with metadata
                notification_text = self.processor.format_notification(meta, self.config.actions.forward_mode)
                if notification_text and self.notify:
                    await self.notify(notification_text, matched_keyword=matched_keyword)
                    logger.info("Notification sent")
                    return True
                return False
        except errors.FloodWaitError as e:
            await self._flood_pause(e.seconds, "forward")
            return False
        except Exception as e:
            logger.error("Failed to forward message: %s", e)
            return False

    # ── Notifications ──────────────────────────────────────────────

    async def _notify_new(self, meta: dict, dm_sent: bool, forward_sent: bool):
        # In notify_with_meta mode, _forward_message already sent a full notification
        if forward_sent and self.config.actions.forward_mode == ForwardMode.NOTIFY_WITH_META:
            return
        text = self.processor.format_notification(meta, ForwardMode.NOTIFY_WITH_META)
        extra = []
        if dm_sent:
            extra.append("✉️ DM отправлен продавцу")
        if forward_sent:
            extra.append("📤 Переслано")
        if extra:
            text += "\n" + "\n".join(extra)
        if self.notify:
            await self.notify(text, matched_keyword=meta.get("matched_value") or "")

    async def _notify_duplicate(self, chat, seller_id):
        text = (
            f"🔄 Повтор от того же продавца\n"
            f"📍 Чат: {chat}\n"
            f"ℹ️ DM уже отправлялся ранее"
        )
        if self.notify:
            await self.notify(text)  # no matched_keyword → only owner gets it

    # ── Validation ──────────────────────────────────────────────────

    def _vision_matches_keywords(self, vision_type: str) -> bool:
        """Check if vision-detected type relates to any of our keywords."""
        if not vision_type:
            return False
        vt = vision_type.lower()
        # Check against keywords list and keyword_map keys
        all_terms = [k.lower() for k in self.config.monitoring.keywords]
        all_terms.extend(k.lower() for k in self.config.rules.keyword_map.keys())
        # Also include synonym values
        for synonyms in self.config.rules.keyword_map.values():
            if isinstance(synonyms, list):
                all_terms.extend(s.lower() for s in synonyms)
        return any(term in vt or vt in term for term in all_terms)

    # ── Helpers ─────────────────────────────────────────────────────

    def dm_pause_remaining(self) -> float:
        """Сколько секунд осталось до конца FloodWait-паузы (0 = не на паузе)."""
        return max(0.0, self._dm_paused_until - time.monotonic())

    async def _flood_pause(self, seconds: float, what: str):
        self._dm_paused_until = time.monotonic() + seconds
        logger.warning("FloodWait %.0fs on %s — DM/forward paused until it expires", seconds, what)
        if self.notify:
            await self.notify(
                f"⚠️ Telegram FloodWait {seconds:.0f}с на {what}. "
                f"Рассылка приостановлена до истечения."
            )

    async def _chat_title(self, msg: Message) -> str:
        cached = self._chat_titles.get(msg.chat_id)
        if cached:
            return cached
        try:
            chat = await self.client.get_entity(msg.chat_id)
            title = getattr(chat, "title", None) or getattr(chat, "username", None) or str(msg.chat_id)
        except Exception:
            return str(msg.chat_id)
        self._chat_titles[msg.chat_id] = title
        return title
