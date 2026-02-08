# nb/plugins/__init__.py —— 无循环导入版本

"""Subpackage of nb: plugins."""

import inspect
import logging
from typing import Any, Dict, List, Union

from telethon.tl.custom.message import Message

from nb.config import CONFIG
from nb.plugin_models import ASYNC_PLUGIN_IDS
from nb.utils import cleanup, stamp


class TgcfMessage:
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


class TgcfPlugin:
    id_ = "plugin"

    def __init__(self, data: Dict[str, Any]) -> None:
        self.data = data

    async def __ainit__(self) -> None:
        pass

    def modify(self, tm: TgcfMessage) -> TgcfMessage:
        return tm

    def modify_group(self, tms: List[TgcfMessage]) -> List[TgcfMessage]:
        return [self.modify(tm) for tm in tms if tm]


PLUGINS = CONFIG.plugins
_plugins = {}


def load_plugins() -> Dict[str, TgcfPlugin]:
    global _plugins
    _plugins = {}

    plugin_order = [
        "filter", "ocr", "replace", "caption", "fmt", "mark"
    ]

    for pid in plugin_order:
        cfg = getattr(PLUGINS, pid, None)
        if not cfg or not getattr(cfg, "check", False):
            continue

        try:
            mod = __import__(f"nb.plugins.{pid}", fromlist=[""])
            cls = getattr(mod, f"Tgcf{pid.title()}")
            plugin = cls(cfg)
            if plugin.id_ != pid:
                logging.error(f"ID mismatch: {plugin.id_} != {pid}")
                continue
            _plugins[pid] = plugin
            logging.info(f"✅ 插件加载: {pid}")
        except Exception as e:
            logging.error(f"❌ 加载失败 {pid}: {e}")

    return _plugins


async def apply_plugins(message: Message) -> TgcfMessage:
    tm = TgcfMessage(message)
    for pid in ["filter", "ocr", "replace", "caption", "fmt", "mark"]:
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


async def apply_plugins_to_group(messages: List[Message]) -> List[TgcfMessage]:
    tms = [TgcfMessage(msg) for msg in messages]
    for pid in ["filter", "ocr", "replace", "caption", "fmt", "mark"]:
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
                tms = [await plugin.modify(tm) if inspect.iscoroutinefunction(plugin.modify) else plugin.modify(tm) for tm in tms]
        except Exception as e:
            logging.error(f"❌ 组插件失败 [{pid}]: {e}")
        else:
            tms = [tm for tm in tms if tm]
    return tms


async def load_async_plugins() -> None:
    for pid in ASYNC_PLUGIN_IDS:
        if pid in _plugins:
            await _plugins[pid].__ainit__()
            logging.info(f"🔌 异步插件已加载: {pid}")


_plugins = load_plugins()
