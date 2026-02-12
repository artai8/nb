import inspect
import logging
from typing import Any, Dict, List, Optional

from telethon.tl.custom.message import Message
from telethon.tl.types import (
    ReplyInlineMarkup,
    KeyboardButtonCallback,
    KeyboardButtonUrl,
    KeyboardButtonSwitchInline,
    KeyboardButtonRow,
)

from nb.config import CONFIG
from nb.plugin_models import ASYNC_PLUGIN_IDS, InlineButtonMode
from nb.utils import cleanup, stamp

PLUGIN_ORDER = [
    "filter", "ocr", "replace", "caption", "fmt", "mark", "sender"
]


def _replace_in_string(original: str, replacements: Dict[str, str]) -> str:
    result = original
    for old, new in replacements.items():
        result = result.replace(old, new)
    return result


def _process_reply_markup(
    reply_markup,
    mode: InlineButtonMode,
    url_replacements: Dict[str, str],
    text_replacements: Dict[str, str],
):
    if reply_markup is None:
        return None

    if not isinstance(reply_markup, ReplyInlineMarkup):
        return None

    if mode == InlineButtonMode.REMOVE:
        return None

    new_rows = []
    for row in reply_markup.rows:
        new_buttons = []
        for button in row.buttons:
            new_button = _process_single_button(
                button, mode, url_replacements, text_replacements
            )
            if new_button is not None:
                new_buttons.append(new_button)
        if new_buttons:
            new_rows.append(KeyboardButtonRow(buttons=new_buttons))

    if not new_rows:
        return None
    return ReplyInlineMarkup(rows=new_rows)


def _process_single_button(button, mode, url_replacements, text_replacements):
    btn_text = button.text or ""

    if mode == InlineButtonMode.REPLACE_ALL and text_replacements:
        btn_text = _replace_in_string(btn_text, text_replacements)

    if isinstance(button, KeyboardButtonUrl):
        url = button.url or ""
        if url_replacements:
            url = _replace_in_string(url, url_replacements)
        return KeyboardButtonUrl(text=btn_text, url=url)

    if isinstance(button, KeyboardButtonCallback):
        if mode == InlineButtonMode.REPLACE_ALL:
            return KeyboardButtonCallback(
                text=btn_text,
                data=button.data,
                requires_password=getattr(button, 'requires_password', False),
            )
        return button

    if isinstance(button, KeyboardButtonSwitchInline):
        if mode == InlineButtonMode.REPLACE_ALL:
            return KeyboardButtonSwitchInline(
                text=btn_text,
                query=button.query,
                same_peer=getattr(button, 'same_peer', False),
            )
        return button

    return button


class NbMessage:
    def __init__(self, message: Message) -> None:
        self.message = message
        self.text = self.message.text or ""
        self.raw_text = self.message.raw_text or ""
        self.sender_id = self.message.sender_id
        self.file_type = self.guess_file_type()
        self.new_file = None
        self.cleanup = False
        self.reply_to = None
        self.client = self.message.client
        self.reply_markup = self._build_reply_markup()

    def _build_reply_markup(self):
        original_markup = self.message.reply_markup
        if original_markup is None:
            return None

        inline_cfg = CONFIG.plugins.inline
        if not inline_cfg.check:
            return None

        return _process_reply_markup(
            original_markup,
            inline_cfg.mode,
            inline_cfg.url_replacements,
            inline_cfg.text_replacements,
        )

    async def get_file(self) -> str:
        if self.file_type == "nofile":
            raise FileNotFoundError("No file exists in this message.")
        self.file = stamp(await self.message.download_media(""), self.sender_id)
        return self.file

    def guess_file_type(self) -> str:
        for ft in ["photo", "video", "gif", "audio", "document", "sticker", "contact"]:
            if getattr(self.message, ft, None):
                return ft
        return "nofile"

    def clear(self) -> None:
        if self.new_file and self.cleanup:
            cleanup(self.new_file)
            self.new_file = None


class NbPlugin:
    id_ = "plugin"

    def __init__(self, data: Dict[str, Any]) -> None:
        self.data = data

    async def __ainit__(self) -> None:
        pass

    def modify(self, tm: NbMessage) -> NbMessage:
        return tm

    def modify_group(self, tms: List[NbMessage]) -> List[NbMessage]:
        return [self.modify(tm) for tm in tms if tm]


PLUGINS = CONFIG.plugins
_plugins: Dict[str, NbPlugin] = {}


def load_plugins() -> Dict[str, NbPlugin]:
    global _plugins
    _plugins = {}

    for pid in PLUGIN_ORDER:
        cfg = getattr(PLUGINS, pid, None)
        if not cfg or not getattr(cfg, "check", False):
            continue

        try:
            mod = __import__(f"nb.plugins.{pid}", fromlist=[""])
            cls = getattr(mod, f"Nb{pid.title()}")
            plugin = cls(cfg)
            if plugin.id_ != pid:
                logging.error(f"ID mismatch: {plugin.id_} != {pid}")
                continue
            _plugins[pid] = plugin
            logging.info(f"✅ 插件加载: {pid}")
        except Exception as e:
            logging.error(f"❌ 加载失败 {pid}: {e}")

    return _plugins


async def apply_plugins(message: Message) -> Optional[NbMessage]:
    tm = NbMessage(message)
    for pid in PLUGIN_ORDER:
        if pid not in _plugins:
            continue
        plugin = _plugins[pid]
        try:
            if inspect.iscoroutinefunction(plugin.modify):
                ntm = await plugin.modify(tm)
            else:
                ntm = plugin.modify(tm)
            if not ntm:
                tm.clear()
                return None
            tm = ntm
        except Exception as e:
            logging.error(f"❌ 插件执行失败 [{pid}]: {e}")
    return tm


async def apply_plugins_to_group(messages: List[Message]) -> List[NbMessage]:
    tms = [NbMessage(msg) for msg in messages]
    for pid in PLUGIN_ORDER:
        if pid not in _plugins:
            continue
        plugin = _plugins[pid]
        try:
            if hasattr(plugin, 'modify_group'):
                if inspect.iscoroutinefunction(plugin.modify_group):
                    tms = await plugin.modify_group(tms)
                else:
                    tms = plugin.modify_group(tms)
            else:
                new_tms = []
                for tm in tms:
                    if inspect.iscoroutinefunction(plugin.modify):
                        result = await plugin.modify(tm)
                    else:
                        result = plugin.modify(tm)
                    if result:
                        new_tms.append(result)
                tms = new_tms
        except Exception as e:
            logging.error(f"❌ 组插件失败 [{pid}]: {e}")
        tms = [tm for tm in tms if tm]
    return tms


async def load_async_plugins() -> None:
    for pid in ASYNC_PLUGIN_IDS:
        if pid in _plugins:
            await _plugins[pid].__ainit__()
            logging.info(f"🔌 异步插件已加载: {pid}")


_plugins = load_plugins()
