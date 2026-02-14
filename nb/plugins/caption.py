# nb/plugins/caption.py

import logging
from typing import List
from nb.plugins import NbMessage, NbPlugin

class NbCaption(NbPlugin):
    id_ = "caption"

    def __init__(self, data) -> None:
        self.caption = data
        self._header = data.header.strip() if data.header else ""
        self._footer = data.footer.strip() if data.footer else ""

    def modify(self, tm: NbMessage) -> NbMessage:
        original_text = tm.text or ""
        has_content = bool(original_text.strip())
        
        # 默认允许添加
        should_add_header = True
        should_add_footer = True

        # 如果是评论，则由标记位决定
        if tm.is_comment:
            should_add_header = tm.is_first_comment
            should_add_footer = tm.is_last_comment

        final_text = original_text

        if should_add_header and self._header and not final_text.startswith(self._header):
            sep = "\n\n" if has_content else ""
            final_text = self._header + sep + final_text

        if should_add_footer and self._footer and not final_text.endswith(self._footer):
            sep = "\n\n" if final_text.strip() else ""
            final_text += sep + self._footer

        tm.text = final_text
        return tm

    def modify_group(self, tms: List[NbMessage]) -> List[NbMessage]:
        if not tms: return tms
        
        # 针对评论组的简化处理：组内第一条负责 Header，最后一条负责 Footer
        is_comm = tms[0].is_comment
        
        text_indices = [i for i, tm in enumerate(tms) if tm.text and tm.text.strip()]
        
        if not text_indices:
            # 全组无文字，在第一条加
            if is_comm:
                if tms[0].is_first_comment:
                    tms[0].text = self._header + (("\n\n" + self._footer) if tms[0].is_last_comment else "")
                elif tms[0].is_last_comment:
                    tms[0].text = self._footer
            else:
                tms[0].text = (self._header + "\n\n" + self._footer).strip()
            return tms

        # 评论模式下的组处理
        if is_comm:
            if tms[0].is_first_comment:
                idx = text_indices[0]
                tms[idx].text = self._header + "\n\n" + tms[idx].text
            if tms[0].is_last_comment:
                idx = text_indices[-1]
                tms[idx].text = tms[idx].text + "\n\n" + self._footer
        else:
            # 普通主贴组处理
            first_idx = text_indices[0]
            last_idx = text_indices[-1]
            if self._header:
                tms[first_idx].text = self._header + "\n\n" + tms[first_idx].text
            if self._footer:
                tms[last_idx].text = tms[last_idx].text + "\n\n" + self._footer
                
        return tms
