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
    KeyboardButtonBuy,
    KeyboardButtonGame,
    KeyboardButtonRequestPhone,
    KeyboardButtonRequestGeoLocation,
    KeyboardButtonRequestPoll,
    KeyboardButtonWebView,
    KeyboardButtonSimpleWebView,
    KeyboardButtonUserProfile,
    InputKeyboardButtonUrlAuth,
)

from nb.config import CONFIG
from nb.plugin_models import ASYNC_PLUGIN_IDS, InlineButtonMode
from nb.utils import cleanup, stamp

PLUGIN_ORDER = [
    "filter", "ocr", "replace", "caption", "fmt", "mark", "sender"
]


def _replace_in_string(original: str, replacements: Dict[str, str]) -> str:
    """在字符串中执行多个替换"""
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
    """处理内联键盘"""
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
    """★ 完善：处理单个按钮，支持所有按钮类型"""
    btn_text = getattr(button, 'text', '') or ""

    # 处理文本替换
    if mode == InlineButtonMode.REPLACE_ALL and text_replacements:
        btn_text = _replace_in_string(btn_text, text_replacements)

    # ========== URL 按钮 ==========
    if isinstance(button, KeyboardButtonUrl):
        url = button.url or ""
        if url_replacements:
            url = _replace_in_string(url, url_replacements)
        return KeyboardButtonUrl(text=btn_text, url=url)

    # ========== 回调按钮 ==========
    if isinstance(button, KeyboardButtonCallback):
        if mode == InlineButtonMode.REPLACE_ALL:
            return KeyboardButtonCallback(
                text=btn_text,
                data=button.data,
                requires_password=getattr(button, 'requires_password', False),
            )
        return button

    # ========== 内联查询按钮 ==========
    if isinstance(button, KeyboardButtonSwitchInline):
        if mode == InlineButtonMode.REPLACE_ALL:
            return KeyboardButtonSwitchInline(
                text=btn_text,
                query=button.query,
                same_peer=getattr(button, 'same_peer', False),
                peer_types=getattr(button, 'peer_types', None),
            )
        return button

    # ========== 购买按钮 ==========
    if isinstance(button, KeyboardButtonBuy):
        if mode == InlineButtonMode.REPLACE_ALL:
            return KeyboardButtonBuy(text=btn_text)
        return button

    # ========== 游戏按钮 ==========
    if isinstance(button, KeyboardButtonGame):
        if mode == InlineButtonMode.REPLACE_ALL:
            return KeyboardButtonGame(text=btn_text)
        return button

    # ========== WebView 按钮 ==========
    if isinstance(button, KeyboardButtonWebView):
        url = button.url or ""
        if url_replacements:
            url = _replace_in_string(url, url_replacements)
        return KeyboardButtonWebView(text=btn_text, url=url)

    if isinstance(button, KeyboardButtonSimpleWebView):
        url = button.url or ""
        if url_replacements:
            url = _replace_in_string(url, url_replacements)
        return KeyboardButtonSimpleWebView(text=btn_text, url=url)

    # ========== 用户资料按钮 ==========
    if isinstance(button, KeyboardButtonUserProfile):
        if mode == InlineButtonMode.REPLACE_ALL:
            return KeyboardButtonUserProfile(
                text=btn_text,
                user_id=button.user_id,
            )
        return button

    # ========== 请求类按钮（通常不适合转发）==========
    if isinstance(button, (
        KeyboardButtonRequestPhone,
        KeyboardButtonRequestGeoLocation,
        KeyboardButtonRequestPoll,
    )):
        # 这些按钮通常在转发时无效，可以选择移除或保留
        if mode == InlineButtonMode.REMOVE:
            return None
        return button

    # ========== URL Auth 按钮 ==========
    if isinstance(button, InputKeyboardButtonUrlAuth):
        url = button.url or ""
        if url_replacements:
            url = _replace_in_string(url, url_replacements)
        # 这个类型比较特殊，可能需要保持原样
        return button

    # ========== 未知类型：保持原样 ==========
    logging.debug(f"未处理的按钮类型: {type(button).__name__}")
    return button


class NbMessage:
    """封装的消息对象，用于插件处理"""
    
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
        """构建处理后的回复标记"""
        original_markup = self.message.reply_markup
        if original_markup is None:
            return None

        inline_cfg = CONFIG.plugins.inline
        if not inline_cfg.check:
            # 插件未启用时，默认移除内联按钮（避免发送错误）
            return None

        return _process_reply_markup(
            original_markup,
            inline_cfg.mode,
            inline_cfg.url_replacements,
            inline_cfg.text_replacements,
        )

    async def get_file(self) -> str:
        """下载媒体文件"""
        if self.file_type == "nofile":
            raise FileNotFoundError("No file exists in this message.")
        downloaded = await self.message.download_media("")
        if downloaded:
            self.file = stamp(downloaded, str(self.sender_id))
            return self.file
        raise FileNotFoundError("Failed to download media.")

    def guess_file_type(self) -> str:
        """猜测文件类型"""
        for ft in ["photo", "video", "gif", "audio", "document", "sticker", "contact"]:
            if getattr(self.message, ft, None):
                return ft
        return "nofile"

    def clear(self) -> None:
        """清理临时文件"""
        if self.new_file and self.cleanup:
            cleanup(self.new_file)
            self.new_file = None


class NbPlugin:
    """插件基类"""
    id_ = "plugin"

    def __init__(self, data: Dict[str, Any]) -> None:
        self.data = data

    async def __ainit__(self) -> None:
        """异步初始化"""
        pass

    def modify(self, tm: NbMessage) -> NbMessage:
        """修改单条消息"""
        return tm

    def modify_group(self, tms: List[NbMessage]) -> List[NbMessage]:
        """修改媒体组消息"""
        return [self.modify(tm) for tm in tms if tm]


PLUGINS = CONFIG.plugins
_plugins: Dict[str, NbPlugin] = {}


def load_plugins() -> Dict[str, NbPlugin]:
    """加载所有启用的插件"""
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
    """对单条消息应用所有插件"""
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
    """对媒体组应用所有插件"""
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
    """加载需要异步初始化的插件"""
    for pid in ASYNC_PLUGIN_IDS:
        if pid in _plugins:
            await _plugins[pid].__ainit__()
            logging.info(f"🔌 异步插件已加载: {pid}")


# 启动时加载插件
_plugins = load_plugins()
