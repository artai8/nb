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
        """媒体组标题处理 — 完全修复版
        
        核心问题：混合媒体组（文字+图片+视频）中，不能简单用 tms[0]，
        因为 tms[0] 可能是纯文字消息，导致媒体消息丢失内容。
        
        修复策略：
        1. 区分纯文字消息和媒体消息
        2. 收集所有文本内容（包括纯文字和媒体的 caption）
        3. 将合并后的 caption 放到第一个媒体消息上
        4. 如果没有媒体，才放到第一个纯文字消息上
        5. 所有消息标记 _caption_applied，防止重复处理
        """
        if not tms:
            return tms
        
        # 标记所有消息已处理（即使无 header/footer，也要防止 send_message 重复收集）
        if not self._header and not self._footer:
            for tm in tms:
                tm._caption_applied = True
            return tms

        # 分类：媒体消息 vs 纯文字消息
        media_tms = [tm for tm in tms if tm.file_type != "nofile"]
        text_tms = [tm for tm in tms if tm.file_type == "nofile"]

        # 收集所有文本内容（按原始顺序）
        all_texts = []
        
        # 按原始顺序遍历，保持文本顺序
        for tm in tms:
            if tm.file_type == "nofile":
                # 纯文字消息：取 text 或 raw_text
                content = tm.text or tm.raw_text or ""
                if content.strip():
                    all_texts.append(content.strip())
            else:
                # 媒体消息：取 caption（raw_text 避免已格式化）
                content = tm.raw_text or tm.text or ""
                if content.strip():
                    all_texts.append(content.strip())

        # 去重同时保持顺序
        seen = set()
        unique_texts = []
        for t in all_texts:
            if t not in seen:
                seen.add(t)
                unique_texts.append(t)

        # 构建最终 caption
        parts = []
        if self._header:
            parts.append(self._header)
        if unique_texts:
            parts.extend(unique_texts)
        if self._footer:
            parts.append(self._footer)
        
        combined = "\n\n".join(parts) if parts else ""

        # 关键：找到正确的目标消息设置 caption
        target_tm = None
        
        if media_tms:
            # 优先放到第一个媒体消息
            target_tm = media_tms[0]
            target_tm.text = combined
            target_tm._caption_applied = True
            
            # 其他媒体清空 text（避免重复），但保留所有属性
            for tm in media_tms[1:]:
                tm.text = ""
                tm._caption_applied = True
            
            # 纯文字消息：内容已合并到 caption，清空
            for tm in text_tms:
                tm.text = ""
                tm._caption_applied = True
                
        else:
            # 没有媒体，全是文字
            if text_tms:
                target_tm = text_tms[0]
                target_tm.text = combined
                target_tm._caption_applied = True
                
                for tm in text_tms[1:]:
                    tm.text = ""
                    tm._caption_applied = True

        return tms
