"""Caption header and footer plugin."""

from nb.plugins import NbMessage, NbPlugin


class NbCaption(NbPlugin):
    id_ = "caption"

    def __init__(self, data) -> None:
        self.caption = data

    def modify(self, tm: NbMessage) -> NbMessage:
        if not self.caption.check:
            return tm
        parts = []
        if self.caption.header:
            parts.append(self.caption.header)
        if tm.text:
            parts.append(tm.text)
        if self.caption.footer:
            parts.append(self.caption.footer)
        tm.text = "\n".join(parts).strip()
        return tm


CaptionPlugin = NbCaption
