"""Bot-media engine (ported from the original single-tenant nb).

Given a source post, find ``t.me/<bot>?start=<param>`` links (in the
post text, its URL entities, or its inline-button URLs), talk to the bot
with ``/start <param>``, collect the media album it returns (following
its "next page" buttons), and return the collected messages so the
caller can merge them into the forwarded album.

Deliberately NOT ported: the discussion-comment keyword paths --
collecting the source post's linked discussion group, finding a common
comment keyword, and triggering the bot from a comment. Those need live
discussion plumbing and are the most bot-specific part; the start-link
path below covers the primary use case (a channel post carries a
resource bot's start link). The original code is preserved in
``~/workspace/uu798-orig`` if those paths are ever needed.
"""
import asyncio
import logging
import re
import time
from typing import List, Optional
from urllib.parse import parse_qs, urlparse

from telethon.tl.functions.messages import GetBotCallbackAnswerRequest
from telethon.tl.types import (
    KeyboardButtonCallback,
    KeyboardButtonUrl,
    MessageEntityTextUrl,
    MessageEntityUrl,
    ReplyInlineMarkup,
)

from nb.multitenant.models import BotMediaSettings

logger = logging.getLogger("nb.bot_media")

_START_LINK_CACHE_TTL_SECONDS = 120

_NEXT_KEYWORDS = [
    "next", "more", "next page", "nextpage", "continue", "remaining",
    "send remaining", "下一页", "下页", "继续", "更多", "继续发送",
    "发送剩余", "剩余", "查看更多", "下一个", "翻页", "➡", ">",
]
_GET_ALL_KEYWORDS = [
    "get all", "getall", "all", "all files", "fetch all", "download all",
    "获取全部", "全部获取", "一键获取", "获取所有", "查看全部",
    "全部发送", "一键发送",
]


def _parse_lines(raw) -> List[str]:
    if not raw:
        return []
    if isinstance(raw, (list, tuple)):
        return [str(x).strip() for x in raw if str(x).strip()]
    lines = [line.strip() for line in str(raw).replace("\r", "\n").split("\n")]
    return [line for line in lines if line]


def _extract_tme_links(text: str) -> List[str]:
    if not text:
        return []
    candidates = re.findall(r"(https?://t\.me/[^\s]+|t\.me/[^\s]+)", text)
    return [c.strip(").,;\"'") for c in candidates]


def _extract_tme_links_from_entities(message) -> List[str]:
    if message is None:
        return []
    entities = getattr(message, "entities", None) or []
    text = getattr(message, "raw_text", None) or getattr(message, "text", "") or ""
    links: List[str] = []
    for ent in entities:
        if isinstance(ent, MessageEntityTextUrl):
            if ent.url:
                links.append(ent.url)
        elif isinstance(ent, MessageEntityUrl):
            if text:
                url = text[ent.offset: ent.offset + ent.length]
                if url:
                    links.append(url)
    return links


def _parse_tme_start_link(url: str):
    if not url:
        return None
    if url.startswith("t.me/"):
        url = "https://" + url
    try:
        parsed = urlparse(url)
    except Exception:
        return None
    path = parsed.path.lstrip("/")
    if not path:
        return None
    bot_username = path.split("/")[0]
    qs = parse_qs(parsed.query or "")
    start_param = qs.get("start", [None])[0]
    if not start_param:
        return None
    return bot_username, start_param


def _extract_start_links_from_markup(reply_markup, settings: BotMediaSettings):
    if reply_markup is None or not isinstance(reply_markup, ReplyInlineMarkup):
        return []
    blacklist = [t.lower() for t in (settings.tme_link_blacklist or [])]
    found = []
    for row in reply_markup.rows:
        for button in row.buttons:
            if isinstance(button, KeyboardButtonUrl):
                url = button.url or ""
                if blacklist and any(token in url.lower() for token in blacklist):
                    continue
                parsed = _parse_tme_start_link(url)
                if parsed:
                    found.append(parsed)
    return found


def _parse_start_links_from_text(text: str, settings: BotMediaSettings):
    blacklist = [t.lower() for t in (settings.tme_link_blacklist or [])]
    links = _extract_tme_links(text)
    if blacklist:
        links = [u for u in links if not any(t in u.lower() for t in blacklist)]
    return [p for p in (_parse_tme_start_link(u) for u in links) if p]


def _parse_start_links_from_entities(message, settings: BotMediaSettings):
    blacklist = [t.lower() for t in (settings.tme_link_blacklist or [])]
    links = _extract_tme_links_from_entities(message)
    if blacklist:
        links = [u for u in links if not any(t in u.lower() for t in blacklist)]
    return [p for p in (_parse_tme_start_link(u) for u in links) if p]


def _extract_bot_usernames(text: str) -> List[str]:
    if not text:
        return []
    matches = re.findall(r"@([A-Za-z0-9_]{5,})", text)
    return [name for name in matches if name.lower().endswith("bot")]


def _msg_has_media(msg) -> bool:
    if getattr(msg, "photo", None) or getattr(msg, "video", None) \
            or getattr(msg, "gif", None) or getattr(msg, "document", None):
        return True
    web_page = getattr(msg, "web_page", None)
    if web_page is not None:
        if getattr(web_page, "document", None) or getattr(web_page, "photo", None):
            return True
    return False


class BotMediaResolver:
    """Stateful resolver: one per tenant worker, cache shared across tasks."""

    def __init__(self):
        self._start_link_cache = {}

    async def resolve_for_group(self, client, messages, settings: BotMediaSettings):
        """First source message that yields bot media wins (original order)."""
        for message in messages:
            try:
                items = await self.resolve_from_message(client, message, settings)
            except Exception:
                logger.warning(
                    "bot media resolve failed for msg %s",
                    getattr(message, "id", "?"), exc_info=True,
                )
                continue
            if items:
                return items
        return []

    async def resolve_from_message(self, client, message, settings: BotMediaSettings):
        if not settings.enabled:
            return []
        raw_text = getattr(message, "raw_text", None) or getattr(message, "text", "") or ""
        collected = await self._collect_from_start_links(
            client, _parse_start_links_from_text(raw_text, settings), settings
        )
        if collected:
            return collected
        tail = []
        tail.extend(_parse_start_links_from_entities(message, settings))
        tail.extend(
            _extract_start_links_from_markup(getattr(message, "reply_markup", None), settings)
        )
        collected = await self._collect_from_start_links(client, tail, settings)
        if collected:
            return collected
        # Keyword trigger: the post mentions a bot and carries no start link;
        # send the post text to the bot and follow any start link it answers with.
        if not settings.enable_keyword_trigger:
            return []
        bot_names = _extract_bot_usernames(raw_text)
        if not bot_names or not raw_text.strip():
            return []
        collected_kw = []
        for bot_username in bot_names[:1]:
            try:
                bot = await client.get_entity(bot_username)
                latest = await client.get_messages(bot, limit=1)
                last_id = latest[0].id if latest else 0
                await client.send_message(bot, raw_text.strip())
                responses = await self._collect_new_messages(
                    client, bot, last_id, settings.wait_timeout, settings
                )
                for msg in responses:
                    for link in _parse_start_links_from_text(
                        getattr(msg, "raw_text", None) or getattr(msg, "text", "") or "",
                        settings,
                    ):
                        items = await self._start_bot_and_collect_album(
                            client, link[0], link[1], settings
                        )
                        if items:
                            collected_kw.extend(items)
                if collected_kw:
                    break
            except Exception:
                logger.warning(
                    "bot keyword trigger failed (%s)", bot_username, exc_info=True
                )
        return collected_kw

    async def _collect_from_start_links(self, client, links, settings: BotMediaSettings):
        if not links:
            return []
        deduped = []
        seen = set()
        for bot_username, start_param in links:
            key = ((bot_username or "").lower(), start_param or "")
            if key in seen:
                continue
            seen.add(key)
            deduped.append((bot_username, start_param))
        logger.info("bot media: trying start links %s", deduped)
        now = time.monotonic()
        for bot_username, start_param in deduped:
            cache_key = ((bot_username or "").lower(), start_param or "")
            cached = self._start_link_cache.get(cache_key)
            if cached is not None:
                cached_at, cached_items = cached
                if now - cached_at <= _START_LINK_CACHE_TTL_SECONDS and cached_items:
                    logger.info(
                        "bot media: cache hit @%s start=%s items=%d",
                        bot_username, start_param, len(cached_items),
                    )
                    return list(cached_items)
                self._start_link_cache.pop(cache_key, None)
            try:
                items = await self._start_bot_and_collect_album(
                    client, bot_username, start_param, settings
                )
                if items:
                    self._start_link_cache[cache_key] = (time.monotonic(), list(items))
                    logger.info(
                        "bot media: collected @%s items=%d",
                        bot_username, len(items),
                    )
                    return items
                logger.info("bot media: no resources @%s start=%s", bot_username, start_param)
            except Exception as e:
                logger.warning("bot media fetch failed (%s): %s", bot_username, e)
        return []

    async def _collect_new_messages(self, client, peer, min_id: int, timeout: float,
                                    settings: BotMediaSettings):
        start = asyncio.get_running_loop().time()
        seen = set()
        collected = []
        current_min_id = min_id
        while True:
            new_found = False
            async for msg in client.iter_messages(peer, min_id=current_min_id, reverse=True):
                if msg.id in seen:
                    continue
                seen.add(msg.id)
                collected.append(msg)
                new_found = True
                if msg.id > current_min_id:
                    current_min_id = msg.id
            if collected and not new_found:
                break
            if asyncio.get_running_loop().time() - start >= timeout:
                break
            await asyncio.sleep(settings.poll_interval)
        return collected

    async def _get_grouped_messages_from_bot(self, client, bot, grouped_id: int,
                                             settings: BotMediaSettings):
        result = []
        scan_limit = max(getattr(settings, "recent_limit", 50), 100)
        async for msg in client.iter_messages(bot, limit=scan_limit):
            if getattr(msg, "grouped_id", None) == grouped_id:
                result.append(msg)
        result.sort(key=lambda m: m.id)
        return result

    def _find_next_callback_button(self, reply_markup, settings: BotMediaSettings):
        if reply_markup is None or not isinstance(reply_markup, ReplyInlineMarkup):
            return None
        mode = settings.pagination_mode or "auto"
        custom_keywords = _parse_lines(settings.pagination_keywords)
        keywords = [] if mode == "any" else _NEXT_KEYWORDS + _GET_ALL_KEYWORDS + custom_keywords
        ignore_keywords = [k.lower() for k in _parse_lines(settings.pagination_ignore_keywords)]
        for row in reply_markup.rows:
            for button in row.buttons:
                if isinstance(button, KeyboardButtonCallback):
                    text = (button.text or "").strip().lower()
                    compact = text.replace(" ", "")
                    if ignore_keywords and any(k in text or k in compact for k in ignore_keywords):
                        continue
                    if mode == "any":
                        return button
                    if any(k in text or k in compact for k in keywords):
                        return button
        return None

    async def _start_bot_and_collect_album(self, client, bot_username: str,
                                           start_param: str, settings: BotMediaSettings):
        max_pages = settings.max_pages
        if not settings.enable_pagination:
            max_pages = 0
        wait_timeout = settings.wait_timeout
        logger.info("bot media: /start @%s %s", bot_username, start_param)
        bot = await client.get_entity(bot_username)
        latest = await client.get_messages(bot, limit=1)
        last_id = latest[0].id if latest else 0
        await client.send_message(bot, f"/start {start_param}")
        collected = []
        seen_grouped = set()
        seen_ids = set()
        pages = 0
        while pages <= max_pages:
            new_msgs = await self._collect_new_messages(
                client, bot, last_id, wait_timeout, settings
            )
            if not new_msgs:
                break
            last_id = max(m.id for m in new_msgs)
            for msg in new_msgs:
                if getattr(msg, "grouped_id", None):
                    if msg.grouped_id in seen_grouped:
                        if msg.id not in seen_ids and _msg_has_media(msg):
                            collected.append(msg)
                            seen_ids.add(msg.id)
                        continue
                    grouped = await self._get_grouped_messages_from_bot(
                        client, bot, msg.grouped_id, settings
                    )
                    for gmsg in grouped:
                        if _msg_has_media(gmsg) and gmsg.id not in seen_ids:
                            collected.append(gmsg)
                            seen_ids.add(gmsg.id)
                    seen_grouped.add(msg.grouped_id)
                else:
                    if _msg_has_media(msg) and msg.id not in seen_ids:
                        collected.append(msg)
                        seen_ids.add(msg.id)
            next_btn = None
            next_msg = None
            for msg in reversed(new_msgs):
                next_btn = self._find_next_callback_button(
                    getattr(msg, "reply_markup", None), settings
                )
                if next_btn:
                    next_msg = msg
                    break
            if next_btn and next_msg:
                try:
                    logger.info(
                        "bot media: paging @%s via %r", bot_username, next_btn.text
                    )
                    await client(GetBotCallbackAnswerRequest(
                        peer=bot, msg_id=next_msg.id, data=next_btn.data
                    ))
                    pages += 1
                    continue
                except Exception:
                    logger.info("bot media: page click failed @%s, stopping", bot_username)
                    break
            break
        collected.sort(key=lambda m: m.id)
        logger.info("bot media: done @%s collected=%d", bot_username, len(collected))
        return collected
