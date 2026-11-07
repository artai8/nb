"""Media and text spoiler masking plugin."""

from nb.plugins import NbMessage, NbPlugin


class NbSpoiler(NbPlugin):
    id_ = "spoiler"

    def __init__(self, data) -> None:
        self.data = data

    def modify(self, tm: NbMessage) -> NbMessage:
        if not self.data.check:
            return tm
        if getattr(tm.message, "media", None) is not None:
            try:
                tm.message.media.spoiler = True
            except Exception:
                pass
        return tm


SpoilerPlugin = NbSpoiler
