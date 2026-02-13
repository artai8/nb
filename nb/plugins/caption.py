from nb.plugins import NbMessage, NbPlugin


class NbCaption(NbPlugin):
    id_ = "caption"

    def __init__(self, data):
        self._header = data.header if data.header else ""
        self._footer = data.footer if data.footer else ""

    def modify(self, tm):
        if not self._header and not self._footer:
            return tm

        text = tm.text if tm.text else ""
        original_text = text.strip()

        parts = []
        if self._header:
            parts.append(self._header)
        if original_text:
            parts.append(original_text)
        if self._footer:
            parts.append(self._footer)

        tm.text = "\n\n".join(parts)
        return tm

    def modify_group(self, tms):
        if not tms:
            return tms
        if not self._header and not self._footer:
            return tms

        # 找到第一个有文本的消息添加header
        # 找到最后一个有文本的消息添加footer
        # 如果都没文本，就在第一条上添加
        text_indices = [i for i, tm in enumerate(tms) if tm.text and tm.text.strip()]

        if not text_indices:
            # 没有任何文本，在第一条上添加 header + footer
            parts = []
            if self._header:
                parts.append(self._header)
            if self._footer:
                parts.append(self._footer)
            if parts:
                tms[0].text = "\n\n".join(parts)
            return tms

        # 在第一个有文本的消息上添加 header
        if self._header:
            idx = text_indices[0]
            original = tms[idx].text.strip()
            if not original.startswith(self._header):
                tms[idx].text = self._header + "\n\n" + original

        # 在最后一个有文本的消息上添加 footer
        if self._footer:
            idx = text_indices[-1]
            original = tms[idx].text.strip()
            if not original.endswith(self._footer):
                tms[idx].text = original + "\n\n" + self._footer

        return tms
