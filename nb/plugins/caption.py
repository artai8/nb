from nb.plugins import NbMessage, NbPlugin


class NbCaption(NbPlugin):
    id_ = "caption"

    def __init__(self, data):
        self._header = data.header if data.header else ""
        self._footer = data.footer if data.footer else ""

    def modify(self, tm):
        if not self._header and not self._footer:
            return tm

        text = tm.text or ""
        has_content = bool(text.strip())

        if self._header and self._footer:
            if has_content:
                tm.text = self._header + "\n\n" + text + "\n\n" + self._footer
            else:
                tm.text = self._header + "\n\n" + self._footer
        elif self._header:
            if has_content:
                tm.text = self._header + "\n\n" + text
            else:
                tm.text = self._header
        elif self._footer:
            if has_content:
                tm.text = text + "\n\n" + self._footer
            else:
                tm.text = self._footer

        return tm

    def modify_group(self, tms):
        if not tms:
            return tms
        if not self._header and not self._footer:
            return tms

        text_indices = [i for i, tm in enumerate(tms) if tm.text and tm.text.strip()]

        if not text_indices:
            # 没有任何文本，在第一条上添加
            parts = []
            if self._header:
                parts.append(self._header)
            if self._footer:
                parts.append(self._footer)
            if parts:
                tms[0].text = "\n\n".join(parts)
            return tms

        if self._header:
            idx = text_indices[0]
            original = tms[idx].text.strip()
            if not original.startswith(self._header):
                tms[idx].text = self._header + "\n\n" + original

        if self._footer:
            idx = text_indices[-1]
            original = tms[idx].text.strip()
            if not original.endswith(self._footer):
                tms[idx].text = original + "\n\n" + self._footer

        return tms
