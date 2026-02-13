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

        # 收集所有文本
        all_texts = []
        for tm in tms:
            if tm.text and tm.text.strip():
                all_texts.append(tm.text.strip())

        # 构建完整的caption：header + 所有原始文本 + footer
        parts = []
        if self._header:
            parts.append(self._header)
        parts.extend(all_texts)
        if self._footer:
            parts.append(self._footer)

        combined = "\n\n".join(parts) if parts else ""

        # 把组合文本放到第一条消息上，其余清空
        tms[0].text = combined
        for i in range(1, len(tms)):
            tms[i].text = ""

        return tms
