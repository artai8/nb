import logging
from nb.plugins import NbMessage, NbPlugin


class NbCaption(NbPlugin):
    id_ = "caption"

    def __init__(self, data):
        self._header = data.header if data.header else ""
        self._footer = data.footer if data.footer else ""

    def modify(self, tm):
        if not self._header and not self._footer:
            return tm

        original = tm.text if tm.text else ""

        parts = []
        if self._header:
            parts.append(self._header)
        if original:
            parts.append(original)
        if self._footer:
            parts.append(self._footer)

        tm.text = "\n\n".join(parts) if parts else original
        return tm

    def modify_group(self, tms):
        """媒体组：只在整个组的开头加header，结尾加footer，不对每条单独处理"""
        if not tms:
            return tms
        if not self._header and not self._footer:
            return tms

        # 找到第一个有文本的消息
        first_text_idx = None
        last_text_idx = None
        for i, tm in enumerate(tms):
            if tm.text and tm.text.strip():
                if first_text_idx is None:
                    first_text_idx = i
                last_text_idx = i

        if first_text_idx is None:
            # 没有任何文本的消息，在第一条上添加
            parts = []
            if self._header:
                parts.append(self._header)
            if self._footer:
                parts.append(self._footer)
            if parts:
                tms[0].text = "\n\n".join(parts)
            return tms

        # 只在第一个有文本的消息前加header
        if self._header:
            original = tms[first_text_idx].text or ""
            if not original.startswith(self._header):
                tms[first_text_idx].text = self._header + "\n\n" + original

        # 只在最后一个有文本的消息后加footer
        if self._footer:
            # 如果 header 和 footer 在同一条消息上
            idx = last_text_idx
            original = tms[idx].text or ""
            if not original.endswith(self._footer):
                tms[idx].text = original + "\n\n" + self._footer

        return tms
