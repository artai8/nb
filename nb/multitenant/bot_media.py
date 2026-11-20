"""Bot-media engine (ported from the original single-tenant nb).

Given a source post, find ``t.me/<bot>?start=<param>`` links (in the
post text, its URL entities, or its inline-button URLs), talk to the bot
with ``/start <param>``, collect the media album it returns (following
its "next page" buttons), and return the collected messages so the
caller can merge them into the forwarded album.

Deliberately NOT ported: none of the discussion-comment keyword paths
remain unported -- they live below (_trim_keyword,
_extract_comment_keyword, get_discussion_message,
_collect_discussion_comments, _find_common_comment_keyword,
_collect_start_links_from_keyword_reply,
trigger_comment_keyword_and_resolve_bot_media) and are wired into
resolve_from_message between the post-text start links and the
entities/markup tail links. The original code is preserved in
``~/workspace/uu798-orig``.
"""
import asyncio
import logging
import re
import time
from datetime import timedelta
from typing import List, Optional
from urllib.parse import parse_qs, urlparse

from telethon.tl.functions.channels import GetFullChannelRequest
from telethon.tl.functions.messages import (
    GetBotCallbackAnswerRequest,
    GetDiscussionMessageRequest,
)
from telethon.tl.types import (
    KeyboardButtonCallback,
    KeyboardButtonUrl,
    MessageEntityTextUrl,
    MessageEntityUrl,
    MessageService,
    ReplyInlineMarkup,
)

from nb.multitenant.models import BotMediaSettings

logger = logging.getLogger("nb.bot_media")

_START_LINK_CACHE_TTL_SECONDS = 120

# How far back to scan the linked discussion group for a post's mirror.
_MIRROR_SCAN_LIMIT = 300

# Date-window mirror search (round 4): mirrors are auto-forwarded within
# seconds of the post, so when the recent-history scan misses (busy
# groups push a mirror hundreds of messages back within the hour),
# scan the window just after the post's date instead of from newest.
_MIRROR_DATE_WINDOWS = ((timedelta(minutes=10), 500), (timedelta(hours=1), 1000))

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


# =====================================================================
#  Comment-keyword paths (ported from the original single-tenant nb)
# =====================================================================

def _trim_keyword(value: str) -> str:
    if not value:
        return value
    return value.strip().strip(" \"'“”‘’()（）[]【】{}<>《》")


def _get_reply_to_msg_id(message):
    reply_to = getattr(message, "reply_to", None)
    if reply_to is not None and hasattr(reply_to, "reply_to_msg_id"):
        return reply_to.reply_to_msg_id
    return getattr(message, "reply_to_msg_id", None)


def _extract_comment_keyword(text: str, settings: BotMediaSettings):
    """Keyword between the first matching prefix and the nearest suffix.

    E.g. "评论区回复 暗号123 获取资源" -> "暗号123".
    """
    if not text:
        return None
    prefixes = settings.comment_keyword_prefixes or []
    suffixes = settings.comment_keyword_suffixes or []
    if not prefixes or not suffixes:
        return None
    for prefix in prefixes:
        start = text.find(prefix)
        if start == -1:
            continue
        start_idx = start + len(prefix)
        ends = [text.find(suffix, start_idx) for suffix in suffixes]
        ends = [e for e in ends if e != -1]
        if not ends:
            continue
        keyword = _trim_keyword(text[start_idx:min(ends)])
        if keyword:
            return keyword
    return None


async def _linked_group_id(client, channel_id):
    """linked_chat_id of a channel, or None (no discussion group / error)."""
    try:
        full = await client(GetFullChannelRequest(channel_id))
        return getattr(full.full_chat, "linked_chat_id", None)
    except Exception as e:
        logger.warning(
            "bot media: linked-group lookup failed (channel=%s): %s",
            channel_id, e,
        )
        return None


def _linked_group_peer_id(linked_chat_id: int) -> int:
    """Usable peer id for a linked discussion group.

    GetFullChannel returns the BARE supergroup id (e.g. 2244996707);
    Telethon resolves a bare positive int as a *user*, so a cold client
    raises "Could not find the input entity for PeerUser" and the old
    code fell back to the same bare int, which iter_messages then also
    rejected -- the mirror scan silently found nothing. The -100 form
    (-1002244996707) resolves as a channel/supergroup directly.
    """
    linked = int(linked_chat_id)
    return linked if linked < 0 else int(f"-100{linked}")


async def get_discussion_message(client, channel_id, msg_id):
    try:
        result = await client(GetDiscussionMessageRequest(
            peer=channel_id, msg_id=msg_id,
        ))
        if result and getattr(result, "messages", None):
            return result.messages[0]
    except Exception as e:
        logger.warning(
            "bot media: get discussion message failed (channel=%s, msg=%s): %s",
            channel_id, msg_id, e,
        )
    # Fallback: the channel-peer query fails with MSG_ID_INVALID for
    # some channels even when the post HAS a discussion mirror -- 455's
    # source -1002112309458, proven 2026-11-19 (the same request fails
    # for EVERY post there, text or album, in every peer form, while
    # the linked group itself answers history reads fine). Find the
    # mirror in the linked group directly.
    return await _find_discussion_mirror(client, channel_id, msg_id)


async def _find_discussion_mirror(client, channel_id, msg_id,
                                  limit: int = _MIRROR_SCAN_LIMIT):
    """Mirror of a channel post in its linked discussion group, or None.

    Fast path: scan the group's recent history. Slow path (round 4):
    in a busy group the mirror is hundreds of messages back within the
    hour, so scan the window just after the post's own date (mirrors
    are auto-forwarded within seconds of the post). Fail-open: any
    failure returns None, exactly like the old behaviour of
    get_discussion_message (callers already treat None as "no
    discussion").
    """
    linked = await _linked_group_id(client, channel_id)
    if not linked:
        return None
    peer_id = _linked_group_peer_id(linked)
    try:
        peer = await client.get_entity(peer_id)
    except Exception:
        peer = peer_id
    try:
        async for message in client.iter_messages(peer, limit=limit):
            fwd = getattr(message, "fwd_from", None)
            if fwd is not None and getattr(fwd, "channel_post", None) == msg_id:
                return message
    except Exception as e:
        logger.warning(
            "bot media: mirror scan failed (channel=%s, msg=%s): %s",
            channel_id, msg_id, e,
        )
        return None
    # Date-window passes: the mirror sits just after the post's date.
    post = None
    try:
        post = await client.get_messages(channel_id, ids=msg_id)
    except Exception:
        post = None
    if isinstance(post, list):
        post = post[0] if post else None
    post_date = getattr(post, "date", None) if post is not None else None
    if post_date is None:
        return None
    for window, scan_limit in _MIRROR_DATE_WINDOWS:
        try:
            async for message in client.iter_messages(
                peer, offset_date=post_date + window, limit=scan_limit
            ):
                fwd = getattr(message, "fwd_from", None)
                if fwd is not None and getattr(fwd, "channel_post", None) == msg_id:
                    return message
        except Exception as e:
            logger.warning(
                "bot media: mirror date-window scan failed "
                "(channel=%s, msg=%s): %s",
                channel_id, msg_id, e,
            )
            return None
    return None


async def _discussion_thread_root(client, channel_id, post_id, mirror):
    """Mirror whose replies form the post's comment thread.

    Comments reply to the FIRST photo's mirror of an album post; any
    other member's mirror raises MSG_ID_INVALID on GetReplies (proven
    2026-11-19 on 455's source: the last photo's mirror fails, an older
    single post's mirror reads fine). A single (non-album) post's own
    mirror is already the root. Fail-open: any failure returns the
    given mirror unchanged.
    """
    if mirror is None:
        return None
    if getattr(mirror, "grouped_id", None) is None:
        return mirror
    try:
        post = await client.get_messages(channel_id, ids=post_id)
    except Exception:
        return mirror
    if post is None or getattr(post, "grouped_id", None) is None:
        return mirror
    grouped = post.grouped_id
    try:
        siblings = await client.get_messages(
            channel_id, ids=list(range(max(1, post_id - 9), post_id + 1))
        )
    except Exception:
        return mirror
    if not isinstance(siblings, list):
        siblings = [siblings]
    first_id = None
    for sib in siblings:
        if sib is not None and getattr(sib, "grouped_id", None) == grouped:
            sid = int(getattr(sib, "id", 0) or 0)
            if sid and (first_id is None or sid < first_id):
                first_id = sid
    if not first_id or first_id == post_id:
        return mirror
    root = await _find_discussion_mirror(client, channel_id, first_id)
    return root if root is not None else mirror


async def _read_comment_replies(client, peer, reply_to, settings: BotMediaSettings,
                              collect_all: bool = False):
    """Iterate replies to ``reply_to`` in ``peer``; return (comments, keyword_hint).

    keyword_hint is the first trimmed comment text whose count reaches 5
    (collection stops early once one does). Failures return ([], None).

    With collect_all=True the early stop is disabled: every reply up to
    recent_limit is returned (keyword_hint stays None). The merge flush
    needs the full thread -- a run of 5 identical text comments must not
    truncate media comments posted after them.
    """
    comments = []
    counts = {}
    keyword_hint = None
    try:
        async for comment in client.iter_messages(
            peer, reply_to=reply_to,
            reverse=True, limit=settings.recent_limit,
        ):
            if isinstance(comment, MessageService):
                continue
            comments.append(comment)
            text = _trim_keyword(
                (getattr(comment, "raw_text", None) or getattr(comment, "text", "") or "").strip()
            )
            if text:
                counts[text] = counts.get(text, 0) + 1
                if not collect_all and counts[text] >= 5:
                    keyword_hint = text
                    break
    except Exception as e:
        logger.warning(
            "bot media: comment replies failed peer=%s reply_to=%s: %s",
            peer, reply_to, e,
        )
        return [], None
    return comments, keyword_hint


async def _collect_discussion_comments(client, channel_id, post_id, settings: BotMediaSettings,
                                     collect_all: bool = False):
    """Return (disc_msg, comments, keyword_hint).

    keyword_hint is the first trimmed comment text whose count reaches 5
    (collection stops early once one does). With collect_all=True the
    early stop is disabled and every reply up to recent_limit is
    returned (see _read_comment_replies) -- the merge flush needs the
    full thread, not just the prefix before a repeated text.

    The channel post is read first (GetRepliesRequest on the channel
    peer): for album posts GetDiscussionMessageRequest returns the
    mirror of the LAST photo while the comment thread is rooted at the
    FIRST photo's mirror, so reading through the mirror can raise
    MSG_ID_INVALID and yield nothing. The mirror read is kept only as
    a fallback, used when the channel-peer read fails or finds nothing.
    Round 4: the fallback now resolves the thread root explicitly
    (_discussion_thread_root) instead of reading replies off whichever
    album member's mirror came back, and tries the original mirror too
    in case the root resolution misfired. A thread with no replies yet
    raises MSG_ID_INVALID on GetReplies -- _read_comment_replies
    already treats that as "no comments", not an error.
    """
    disc_msg = await get_discussion_message(client, channel_id, post_id)
    comments, keyword_hint = await _read_comment_replies(
        client, channel_id, post_id, settings, collect_all=collect_all
    )
    if comments:
        return disc_msg, comments, keyword_hint
    if disc_msg is None:
        return None, [], None
    root_msg = await _discussion_thread_root(client, channel_id, post_id, disc_msg)
    tried = set()
    for candidate in (root_msg, disc_msg):
        if candidate is None:
            continue
        key = (getattr(candidate, "chat_id", None), getattr(candidate, "id", None))
        if key in tried:
            continue
        tried.add(key)
        comments, keyword_hint = await _read_comment_replies(
            client, candidate.chat_id, candidate.id, settings,
            collect_all=collect_all,
        )
        if comments:
            return disc_msg, comments, keyword_hint
    return disc_msg, [], None


def _thread_root_msg_id(comments):
    """Most common reply_to_msg_id among the comments: the discussion
    thread's real root. Top-level comments reply to the thread root, so
    the most frequent value is the root even when a few comments are
    nested replies to other comments."""
    counts = {}
    order = []
    for comment in comments:
        root = _get_reply_to_msg_id(comment)
        if root is None:
            continue
        if root not in counts:
            counts[root] = 0
            order.append(root)
        counts[root] += 1
    best = None
    best_count = 0
    for root in order:
        if counts[root] > best_count:
            best = root
            best_count = counts[root]
    return best


def _keyword_post_target(disc_msg, comments):
    """(chat_id, reply_to_msg_id) for posting the keyword comment.

    Prefer the thread root the comments themselves reply to; fall back
    to the discussion mirror. The mirror can be the wrong photo of an
    album, and a keyword comment posted as a reply to it never reaches
    the bot watching the real thread.
    """
    root = _thread_root_msg_id(comments) if comments else None
    if root is not None and comments:
        chat_id = getattr(comments[0], "chat_id", None)
        if chat_id is not None:
            return chat_id, root
    if disc_msg is not None:
        return getattr(disc_msg, "chat_id", None), getattr(disc_msg, "id", None)
    return None, None


def _find_common_comment_keyword(comments) -> Optional[str]:
    """Most frequent trimmed comment text with count >= 3; earliest wins ties."""
    counts = {}
    order = []
    for comment in comments:
        text = _trim_keyword(
            (getattr(comment, "raw_text", None) or getattr(comment, "text", "") or "").strip()
        )
        if not text:
            continue
        if text not in counts:
            counts[text] = 0
            order.append(text)
        counts[text] += 1
    best = None
    best_count = 0
    best_idx = None
    for idx, text in enumerate(order):
        count = counts.get(text, 0)
        if count < 3:
            continue
        if count > best_count or (count == best_count and (best_idx is None or idx < best_idx)):
            best = text
            best_count = count
            best_idx = idx
    return best


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
        # (2a) Auto comment keyword: the post text itself carries a
        # "评论区回复 <keyword> 获取资源" style instruction; post the
        # keyword as a comment and follow the bot's reply.
        if settings.auto_comment_trigger_enabled:
            try:
                keyword = _extract_comment_keyword(raw_text, settings)
                if keyword:
                    items = await self.trigger_comment_keyword_and_resolve_bot_media(
                        client, getattr(message, "chat_id", None),
                        getattr(message, "id", None), keyword, settings,
                    )
                    if items:
                        return items
            except Exception:
                logger.warning(
                    "bot media comment keyword trigger failed", exc_info=True
                )
        # (2b) Passive discussion-comment scan (channel posts only):
        # keyword hint -> start links sitting in comments -> common keyword.
        if getattr(message, "post", False):
            try:
                items = await self._resolve_from_discussion_comments(
                    client, message, settings
                )
                if items:
                    return items
            except Exception:
                logger.warning(
                    "bot media discussion comment scan failed", exc_info=True
                )
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

    async def _collect_start_links_from_keyword_reply(self, client, disc_msg, keyword,
                                                      settings: BotMediaSettings,
                                                      send_keyword: bool = True,
                                                      post_target=None):
        """Post ``keyword`` as a reply in the discussion group (unless
        send_keyword is False) and poll for a bot reply carrying start
        links. Buttons/markup first, then text/entities. The keyword
        comment is posted from the bound user account, as in the original.

        ``post_target`` is an optional (chat_id, reply_to_msg_id) pair
        for the keyword comment; when omitted the discussion mirror
        (disc_msg.chat_id, disc_msg.id) is used. Callers pass the real
        thread root here, since the mirror can be the wrong photo of an
        album post.
        """
        if post_target is not None:
            thread_chat_id, thread_root_id = post_target
        elif disc_msg is not None:
            thread_chat_id, thread_root_id = disc_msg.chat_id, disc_msg.id
        else:
            return []
        latest = await client.get_messages(thread_chat_id, limit=1)
        last_id = latest[0].id if latest else 0
        request_msg_id = None
        if send_keyword:
            try:
                sent = await client.send_message(
                    thread_chat_id, keyword, reply_to=thread_root_id
                )
                request_msg_id = getattr(sent, "id", None)
            except Exception:
                logger.warning(
                    "bot media: failed to post keyword comment", exc_info=True
                )
                return []
        links = []
        wait_timeout = settings.wait_timeout
        total_timeout = max(wait_timeout * 2, 8)
        start = asyncio.get_running_loop().time()
        while True:
            responses = await self._collect_new_messages(
                client, thread_chat_id, last_id, wait_timeout, settings
            )
            if responses:
                last_id = max(m.id for m in responses)
            matched = []
            for msg in responses:
                if request_msg_id is None:
                    matched.append(msg)
                    continue
                reply_to_msg_id = _get_reply_to_msg_id(msg)
                text = (
                    getattr(msg, "raw_text", None) or getattr(msg, "text", "") or ""
                ).lower()
                if reply_to_msg_id == request_msg_id or keyword.lower() in text:
                    matched.append(msg)
            for msg in matched:
                button_links = _extract_start_links_from_markup(
                    getattr(msg, "reply_markup", None), settings
                )
                if button_links:
                    return button_links
            for msg in matched:
                links.extend(_parse_start_links_from_text(
                    getattr(msg, "raw_text", None) or getattr(msg, "text", "") or "",
                    settings,
                ))
                links.extend(_parse_start_links_from_entities(msg, settings))
            if links:
                return links
            if asyncio.get_running_loop().time() - start >= total_timeout:
                break
        return links

    async def trigger_comment_keyword_and_resolve_bot_media(
        self, client, channel_id, post_id, keyword, settings: BotMediaSettings,
    ):
        keyword = _trim_keyword(keyword or "")
        if not keyword:
            return []
        disc_msg, comments, _hint = await _collect_discussion_comments(
            client, channel_id, post_id, settings
        )
        post_target = _keyword_post_target(disc_msg, comments)
        if post_target[0] is None:
            return []
        links = await self._collect_start_links_from_keyword_reply(
            client, disc_msg, keyword, settings, send_keyword=True,
            post_target=post_target,
        )
        if not links:
            return []
        return await self._collect_from_start_links(client, links, settings)

    async def _resolve_from_discussion_comments(self, client, message, settings: BotMediaSettings):
        """Passive scan of a channel post's discussion comments, in order:
        (a) keyword hint (a comment text repeated 5+ times) -> trigger its
        reply; (b) start links sitting directly in the comments (text,
        then markup, then entities); (c) common comment keyword (3+)
        -> trigger its reply. First hit wins.
        """
        disc_msg, comments, keyword_hint = await _collect_discussion_comments(
            client, getattr(message, "chat_id", None),
            getattr(message, "id", None), settings,
        )
        if not comments:
            return []
        post_target = _keyword_post_target(disc_msg, comments)
        if keyword_hint and post_target[0] is not None:
            links = await self._collect_start_links_from_keyword_reply(
                client, disc_msg, keyword_hint, settings,
                post_target=post_target,
            )
            items = await self._collect_from_start_links(client, links, settings)
            if items:
                return items
        for src_fn in (
            lambda c: _parse_start_links_from_text(
                getattr(c, "raw_text", None) or getattr(c, "text", "") or "", settings
            ),
            lambda c: _extract_start_links_from_markup(
                getattr(c, "reply_markup", None), settings
            ),
            lambda c: _parse_start_links_from_entities(c, settings),
        ):
            all_links = []
            for comment in comments:
                all_links.extend(src_fn(comment))
            items = await self._collect_from_start_links(client, all_links, settings)
            if items:
                return items
        if settings.comment_keyword_from_comments_enabled and post_target[0] is not None:
            keyword = _find_common_comment_keyword(comments)
            if keyword:
                links = await self._collect_start_links_from_keyword_reply(
                    client, disc_msg, keyword, settings,
                    post_target=post_target,
                )
                items = await self._collect_from_start_links(client, links, settings)
                if items:
                    return items
        return []

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
