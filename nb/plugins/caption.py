from nb.plugins import NbMessage, NbPlugin


class NbCaption(NbPlugin):
    id_ = "caption"

    def __init__(self, data):
        self._header = data.header.strip() if data.header else ""
        self._footer = data.footer.strip() if data.footer else ""

    def modify(self, tm):
        text = tm.text or ""
        has = bool(text.strip())
        if self._header and not text.startswith(self._header):
            text = self._header + ("\n\n" if has else "") + text
        if self._footer and not text.endswith(self._footer):
            text = text + ("\n\n" if has else "") + self._footer
        tm.text = text
        return tm

    def modify_group(self, tms):
        if not tms:
            return tms
        text_indices = [i for i, tm in enumerate(tms) if tm.text and tm.text.strip()]
        if not text_indices:
            combined = ""
            if self._header:
                combined = self._header
            if self._footer:
                combined = (combined + "\n\n" + self._footer) if combined else self._footer
            if combined:
                tms[0].text = combined
            return tms
        if self._header:
            tm = tms[text_indices[0]]
            o = tm.text or ""
            if not o.startswith(self._header):
                tm.text = self._header + ("\n\n" if o.strip() else "") + o
        if self._footer:
            tm = tms[text_indices[-1]]
            o = tm.text or ""
            if not o.endswith(self._footer):
                tm.text = o + ("\n\n" if o.strip() else "") + self._footer
        return tms
