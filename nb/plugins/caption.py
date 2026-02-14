import logging
from nb.plugins import NbMessage, NbPlugin


class NbCaption(NbPlugin):
    id_ = "caption"

    def __init__(self, data):
        self._header = data.header if data.header else ""
        self._footer = data.footer if data.footer else ""

    def modify(self, tm):
        """处理单条消息"""
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
        tm._caption_applied = True  # 标记已处理
        return tm

    def modify_group(self, tms):
        """处理媒体组 — 修复版
        
        关键修复：
        1. 仅在第一条消息添加 header/footer
        2. 保留其他所有消息的原始文本（不再清空！）
        3. 所有消息标记 _caption_applied=True 防止 send_message 重复处理
        """
        if not tms:
            return tms
        if not self._header and not self._footer:
            # 即使没有 header/footer，也要标记避免 send_message 错误合并
            for tm in tms:
                tm._caption_applied = True
            return tms

        # === 仅处理第一条消息 ===
        first_tm = tms[0]
        original_text = first_tm.text or ""
        parts = []
        if self._header:
            parts.append(self._header)
        if original_text.strip():
            parts.append(original_text)
        if self._footer:
            parts.append(self._footer)
        
        if parts:
            first_tm.text = "\n\n".join(parts)
        # 如果没有文本且无 header/footer，保持原样
        
        first_tm._caption_applied = True

        # === 其他消息：保留原始文本，仅标记已处理 ===
        for i in range(1, len(tms)):
            tms[i]._caption_applied = True

        return tms
