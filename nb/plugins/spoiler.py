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
            setattr(msg, "_nb_spoiler", True)
            setattr(media, "spoiler", True)
            logging.info(f"🫥 已为消息 {getattr(msg, 'id', 'unknown')} 标记 spoiler")
        except Exception as err:
            logging.warning(f"⚠️ 剧透插件设置 spoiler 失败: {err}")
        return tm

    def modify(self, tm: NbMessage) -> NbMessage:
        return self._apply_spoiler(tm)

    def modify_group(self, tms):
        return [self._apply_spoiler(tm) for tm in tms if tm]
