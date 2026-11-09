"""Plugin pipeline and message wrapper for multi-tenant transformations and live sandbox preview."""

from __future__ import annotations

import inspect
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, List, Optional

from telethon.tl.types import (
    KeyboardButtonCallback,
    KeyboardButtonRow,
    KeyboardButtonSwitchInline,
    KeyboardButtonUrl,
    ReplyInlineMarkup,
)

from nb.plugin_models import InlineButtonMode, PluginConfig, STYLE_CODES, Style
from nb.utils import cleanup

log = logging.getLogger(__name__)


class NbMessage:
    def __init__(self, message: Any, *, download_dir: Optional[str] = None) -> None:
        self.message = message
        self.download_dir = download_dir
        self.raw_text: str = (
            getattr(message, "raw_text", None)
            or getattr(message, "text", None)
            or getattr(message, "message", None)
            or ""
        )
        self.text: str = self.raw_text
        self.reply_markup: Any = getattr(message, "reply_markup", None) or getattr(message, "buttons", None)
        self.new_file: Optional[str] = None
        self.file_type: str = self._detect_file_type(message)
        self.sender_id: str = str(getattr(message, "sender_id", "") or "")

    @staticmethod
    def _detect_file_type(message: Any) -> str:
        if getattr(message, "photo", None):
            return "photo"
        if getattr(message, "gif", None):
            return "gif"
        if getattr(message, "video_note", None):
            return "video_note"
        if getattr(message, "video", None):
            return "video"
        if getattr(message, "audio", None) or getattr(message, "voice", None):
            return "audio"
        if getattr(message, "sticker", None):
            return "sticker"
        if getattr(message, "contact", None):
            return "contact"
        if getattr(message, "document", None) or getattr(message, "media", None):
            return "document"
        return "nofile"

    async def get_file(self) -> Optional[str]:
        if hasattr(self.message, "download_media"):
            return await self.message.download_media(file=self.download_dir)
        return None

    def clear(self) -> None:
        if self.new_file and os.path.exists(self.new_file):
            cleanup(self.new_file)
            self.new_file = None


class NbPlugin:
    id_: str = "base"

    def modify(self, tm: NbMessage) -> Optional[NbMessage]:
        return tm


from .caption import NbCaption
from .filter import NbFilter
from .fmt import NbFmt
from .mark import NbMark
from .ocr import NbOcr
from .replace import NbReplace
from .spoiler import NbSpoiler


def _replace_in_string(original: str, replacements: dict) -> str:
    result = original or ""
    for old, new in (replacements or {}).items():
        if old:
            result = result.replace(old, new)
    return result


def _parse_replacements_raw(raw: str) -> dict:
    mapping: dict = {}
    for line in (raw or "").splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            if k.strip():
                mapping[k.strip()] = v.strip()
    return mapping


class NbInlineButtons(NbPlugin):
    id_ = "inline"

    def __init__(self, data) -> None:
        self.cfg = data
        self.url_replacements = dict(data.url_replacements or {})
        if not self.url_replacements and data.url_replacements_raw:
            self.url_replacements = _parse_replacements_raw(data.url_replacements_raw)
        self.text_replacements = dict(data.text_replacements or {})
        if not self.text_replacements and data.text_replacements_raw:
            self.text_replacements = _parse_replacements_raw(data.text_replacements_raw)

    def modify(self, tm: NbMessage) -> Optional[NbMessage]:
        if not self.cfg.check:
            return tm
        tm.reply_markup = self._process_markup(tm.reply_markup)
        return tm

    def _process_markup(self, reply_markup):
        if reply_markup is None:
            return None
        if not isinstance(reply_markup, ReplyInlineMarkup):
            return None  # non-inline markup (e.g. reply keyboard) is dropped
        if self.cfg.mode == InlineButtonMode.REMOVE:
            return None
        new_rows = []
        for row in reply_markup.rows:
            new_buttons = []
            for button in row.buttons:
                processed = self._process_button(button)
                if processed is not None:
                    new_buttons.append(processed)
            if new_buttons:
                new_rows.append(KeyboardButtonRow(buttons=new_buttons))
        if not new_rows:
            return None
        return ReplyInlineMarkup(rows=new_rows)

    def _process_button(self, button):
        btn_text = button.text or ""
        if self.cfg.mode == InlineButtonMode.REPLACE_ALL and self.text_replacements:
            btn_text = _replace_in_string(btn_text, self.text_replacements)
        if isinstance(button, KeyboardButtonUrl):
            url = button.url or ""
            if self.url_replacements:
                url = _replace_in_string(url, self.url_replacements)
            return KeyboardButtonUrl(text=btn_text, url=url)
        if isinstance(button, KeyboardButtonCallback):
            if self.cfg.mode == InlineButtonMode.REPLACE_ALL:
                return KeyboardButtonCallback(text=btn_text, data=button.data)
            return button
        if isinstance(button, KeyboardButtonSwitchInline):
            if self.cfg.mode == InlineButtonMode.REPLACE_ALL:
                return KeyboardButtonSwitchInline(
                    text=btn_text,
                    query=button.query,
                    same_peer=getattr(button, "same_peer", False),
                )
            return button
        return button


@dataclass
class DryRunPreviewResult:
    passed_filter: bool = True
    filtered_reason: str = ""
    original_text: str = ""
    transformed_text: str = ""
    applied_plugins: List[str] = field(default_factory=list)


class PluginPipeline:
    def __init__(self, config: Optional[PluginConfig] = None, *, download_dir: Optional[str] = None) -> None:
        self.config = config or PluginConfig()
        self.download_dir = download_dir
        self.plugins: List[NbPlugin] = []

    async def initialize(self) -> None:
        self.plugins.clear()
        if self.config.filter.check:
            self.plugins.append(NbFilter(self.config.filter))
        if self.config.ocr.check:
            self.plugins.append(NbOcr(self.config.ocr))
        if self.config.replace.check:
            self.plugins.append(NbReplace(self.config.replace))
        if self.config.caption.check:
            self.plugins.append(NbCaption(self.config.caption))
        if self.config.fmt.check:
            self.plugins.append(NbFmt(self.config.fmt))
        if self.config.mark.check:
            self.plugins.append(NbMark(self.config.mark))
        if self.config.spoiler.check:
            self.plugins.append(NbSpoiler(self.config.spoiler))
        if self.config.inline.check:
            self.plugins.append(NbInlineButtons(self.config.inline))

    async def apply(self, message: Any, *, skip_plugins: Optional[List[str]] = None) -> Optional[NbMessage]:
        if not self.plugins and any(
            getattr(getattr(self.config, k, None), "check", False)
            for k in ["filter", "ocr", "replace", "caption", "fmt", "spoiler", "mark", "inline"]
        ):
            await self.initialize()
        skip = set(skip_plugins or [])
        tm = message if isinstance(message, NbMessage) else NbMessage(message, download_dir=self.download_dir)
        for plugin in self.plugins:
            if plugin.id_ in skip:
                continue
            try:
                res = plugin.modify(tm)
                if inspect.isawaitable(res):
                    res = await res
            except Exception:
                # One bad plugin (bad regex, broken media, ...) must not kill
                # the whole task: log it, skip that plugin, keep sending.
                log.exception("Plugin %s failed on message, skipping plugin", plugin.id_)
                continue
            if res is None:
                tm.clear()
                return None
            tm = res
        return tm

    async def apply_group(
        self,
        messages: List[Any],
        *,
        skip_plugins: Optional[List[str]] = None,
        fail_open: bool = False,
        base_text: Optional[str] = None,
    ) -> List[NbMessage]:
        """Apply the pipeline to a group (album): plugins that define
        ``modify_group`` see the whole group at once (caption adds its
        header/footer only once per group); the rest run per message.
        One failing plugin is skipped, never fatal to the group. If every
        message is filtered out and fail_open is set, the originals are
        restored and sent unfiltered.
        """
        if not self.plugins and any(
            getattr(getattr(self.config, k, None), "check", False)
            for k in ["filter", "ocr", "replace", "caption", "fmt", "spoiler", "mark", "inline"]
        ):
            await self.initialize()
        tms: List[NbMessage] = [
            m if isinstance(m, NbMessage) else NbMessage(m, download_dir=self.download_dir)
            for m in messages
        ]
        if base_text and tms:
            tms[0].text = base_text
            tms[0].raw_text = base_text
        skip = set(skip_plugins or [])
        for plugin in self.plugins:
            if plugin.id_ in skip:
                continue
            try:
                if hasattr(plugin, "modify_group"):
                    res = plugin.modify_group(tms)
                    if inspect.isawaitable(res):
                        res = await res
                    tms = list(res) if res is not None else []
                else:
                    new_tms: List[NbMessage] = []
                    for tm in tms:
                        res = plugin.modify(tm)
                        if inspect.isawaitable(res):
                            res = await res
                        if res is not None:
                            new_tms.append(res)
                    tms = new_tms
            except Exception:
                log.exception("Plugin %s failed on group, skipping plugin", plugin.id_)
                continue
            tms = [tm for tm in tms if tm]
        if fail_open and not tms:
            tms = [
                m if isinstance(m, NbMessage) else NbMessage(m, download_dir=self.download_dir)
                for m in messages
            ]
            if base_text and tms:
                tms[0].text = base_text
                tms[0].raw_text = base_text
        return tms

    def dry_run_text(self, sample_text: str, *, file_type: str = "nofile", sender: str = "") -> DryRunPreviewResult:
        """插件沙盒实时测试器：无需等待频道发消息，直接测试过滤、替换、页眉页脚与排版效果。"""
        result = DryRunPreviewResult(original_text=sample_text, transformed_text=sample_text)
        text = sample_text or ""

        # 1. Filter
        f_cfg = self.config.filter
        if f_cfg.check:
            result.applied_plugins.append("filter")
            if sender and f_cfg.users.blacklist and sender in f_cfg.users.blacklist:
                result.passed_filter = False
                result.filtered_reason = f"命中发送者黑名单: {sender}"
                return result
            if f_cfg.users.whitelist and sender and sender not in f_cfg.users.whitelist:
                result.passed_filter = False
                result.filtered_reason = "未在发送者白名单中"
                return result
            if file_type and file_type in [x.value if hasattr(x, "value") else str(x) for x in f_cfg.files.blacklist]:
                result.passed_filter = False
                result.filtered_reason = f"命中媒体文件类型黑名单: {file_type}"
                return result
            if f_cfg.files.whitelist and file_type not in [x.value if hasattr(x, "value") else str(x) for x in f_cfg.files.whitelist]:
                result.passed_filter = False
                result.filtered_reason = f"未在媒体文件类型白名单中: {file_type}"
                return result
            for kw in f_cfg.text.blacklist:
                if not kw:
                    continue
                if (f_cfg.text.regex and re.search(kw, text, flags=0 if f_cfg.text.case_sensitive else re.I)) or (
                    not f_cfg.text.regex and (kw in text if f_cfg.text.case_sensitive else kw.lower() in text.lower())
                ):
                    result.passed_filter = False
                    result.filtered_reason = f"命中关键词黑名单: {kw}"
                    return result
            if f_cfg.text.whitelist:
                matched = False
                for kw in f_cfg.text.whitelist:
                    if not kw:
                        continue
                    if (f_cfg.text.regex and re.search(kw, text, flags=0 if f_cfg.text.case_sensitive else re.I)) or (
                        not f_cfg.text.regex and (kw in text if f_cfg.text.case_sensitive else kw.lower() in text.lower())
                    ):
                        matched = True
                        break
                if not matched:
                    result.passed_filter = False
                    result.filtered_reason = "未命中任何白名单关键词"
                    return result

        # 2. Replace
        r_cfg = self.config.replace
        if r_cfg.check:
            result.applied_plugins.append("replace")
            mapping = dict(r_cfg.text or {})
            if not mapping and r_cfg.text_raw:
                for line in r_cfg.text_raw.splitlines():
                    line = line.strip()
                    if ":" in line:
                        k, v = line.split(":", 1)
                        if k.strip():
                            mapping[k.strip()] = v.strip()
            for pat, rep in mapping.items():
                if not pat:
                    continue
                if rep in STYLE_CODES:
                    code = STYLE_CODES[rep]
                    if r_cfg.regex:
                        text = re.sub(pat, lambda m: f"{code}{m.group(0)}{code}", text)
                    else:
                        text = text.replace(pat, f"{code}{pat}{code}")
                elif r_cfg.regex:
                    text = re.sub(pat, rep, text)
                else:
                    text = text.replace(pat, rep)

        # 3. Caption (before fmt, same as the real pipeline)
        c_cfg = self.config.caption
        if c_cfg.check:
            result.applied_plugins.append("caption")
            if c_cfg.header:
                text = f"{c_cfg.header}\n{text}".strip()
            if c_cfg.footer:
                text = f"{text}\n{c_cfg.footer}".strip()

        # 4. Format
        fmt_cfg = self.config.fmt
        if fmt_cfg.check and fmt_cfg.style != Style.PRESERVE and text:
            result.applied_plugins.append(f"fmt({fmt_cfg.style.value})")
            code = STYLE_CODES.get(fmt_cfg.style.value, "")
            if code:
                text = f"{code}{text}{code}"

        # 5. Spoiler
        if self.config.spoiler.check:
            result.applied_plugins.append("spoiler")

        # 6. Mark
        if self.config.mark.check:
            result.applied_plugins.append(f"mark({self.config.mark.position})")

        # 7. Inline
        if self.config.inline.check:
            result.applied_plugins.append(f"inline({self.config.inline.mode.value})")

        result.transformed_text = text
        return result
