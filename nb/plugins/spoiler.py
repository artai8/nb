import logging

from nb.plugins import NbMessage, NbPlugin


class NbSpoiler(NbPlugin):
    id_ = "spoiler"

    def __init__(self, data) -> None:
        self.data = data
        logging.info("🫥 加载剧透插件")

    def _apply_spoiler(self, tm: NbMessage) -> NbMessage:
        msg = tm.message
        if not msg or not getattr(msg, "media", None):
            return tm
        media = msg.media
        try:
            setattr(media, "spoiler", True)
        except Exception:
            pass
        return tm

    def modify(self, tm: NbMessage) -> NbMessage:
        return self._apply_spoiler(tm)

    def modify_group(self, tms):
        return [self._apply_spoiler(tm) for tm in tms if tm]
