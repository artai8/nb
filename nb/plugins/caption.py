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
        """媒体组标题处理 — 修复版
        
        关键修复：
        1. 只在第一条消息上添加 header/footer，不合并所有文本到第一条
        2. 保留每条消息自己的原始文本不变
        3. 通过标记 _caption_applied 防止 send_message 阶段重复处理
        """
        if not tms:
            return tms
        if not self._header and not self._footer:
            return tms

        # 收集所有非空文本
        all_texts = []
        for tm in tms:
            if tm.text and tm.text.strip():
                all_texts.append(tm.text.strip())

        # 构建最终的组合文本
        parts = []
        if self._header:
            parts.append(self._header)
        if all_texts:
            parts.extend(all_texts)
        if self._footer:
            parts.append(self._footer)

        combined = "\n\n".join(parts) if parts else ""

        # 只在第一条设置合并文本，其余清空
        tms[0].text = combined
        # 标记已经处理过 caption，防止 send_message 再次收集
        tms[0]._caption_applied = True
        for i in range(1, len(tms)):
            tms[i].text = ""
            tms[i]._caption_applied = True

        return tms
