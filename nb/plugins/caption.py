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
        if original.strip():
            parts.append(original)
        if self._footer:
            parts.append(self._footer)
        tm.text = "\n\n".join(parts) if parts else original
        return tm

    def modify_group(self, tms):
        """媒体组标题处理

        对于媒体组（相册），Telegram 只允许在第一个媒体上设置 caption。
        此方法收集组内所有文本（包括媒体的 caption），
        加上 header/footer，合并后设置到第一个媒体消息的 text 上。
        """
        if not tms:
            return tms

        if not self._header and not self._footer:
            for tm in tms:
                tm._caption_applied = True
            return tms

        # 按原始顺序收集所有非空文本
        all_texts = []
        for tm in tms:
            content = tm.raw_text or tm.text or ""
            if content.strip():
                all_texts.append(content.strip())

        # 去重保序
        seen = set()
        unique_texts = []
        for t in all_texts:
            if t not in seen:
                seen.add(t)
                unique_texts.append(t)

        # 构建 caption = header + 原始文本 + footer
        parts = []
        if self._header:
            parts.append(self._header)
        if unique_texts:
            parts.extend(unique_texts)
        if self._footer:
            parts.append(self._footer)
        combined = "\n\n".join(parts) if parts else ""

        # 找第一个媒体消息作为 caption 载体
        target_tm = None
        for tm in tms:
            if tm.file_type != "nofile":
                target_tm = tm
                break

        # 如果没有媒体（不太可能在媒体组中），用第一个消息
        if target_tm is None and tms:
            target_tm = tms[0]

        # 设置 caption 到目标消息
        if target_tm is not None:
            target_tm.text = combined
            target_tm._caption_applied = True

        # 其余消息清空 text 并标记
        for tm in tms:
            if tm is not target_tm:
                tm.text = ""
                tm._caption_applied = True

        logging.debug(
            f"[caption] modify_group: {len(tms)} msgs, "
            f"target_type={target_tm.file_type if target_tm else 'None'}, "
            f"caption_len={len(combined)}, texts_found={len(unique_texts)}"
        )

        return tms
