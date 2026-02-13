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
        if not tms:
            return tms
        if not self._header and not self._footer:
            return tms

        # 收集所有非空文本
        all_texts = []
        for tm in tms:
            if tm.text and tm.text.strip():
                all_texts.append(tm.text.strip())

        # 构建完整 caption
        parts = []
        if self._header:
            parts.append(self._header)
        parts.extend(all_texts)
        if self._footer:
            parts.append(self._footer)

        combined = "\n\n".join(parts) if parts else ""

        # 只设置第一条消息的 text，其他消息保留原 media，text 设为空字符串
        tms[0].text = combined
        for i in range(1, len(tms)):
            tms[i].text = ""  # 关键：不清除 message 或 file_type！

        return tms
