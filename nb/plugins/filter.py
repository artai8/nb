"""User, media type, and keyword blacklist/whitelist filtering plugin."""

import re
from typing import Optional
from nb.plugins import NbMessage, NbPlugin


class NbFilter(NbPlugin):
    id_ = "filter"

    def __init__(self, data) -> None:
        self.filters = data

    def modify(self, tm: NbMessage) -> Optional[NbMessage]:
        if not self.filters.check:
            return tm

        # 1. Users filter (no sender_id guard: a channel post has an empty
        # sender_id, and it must NOT slip past a non-empty whitelist)
        if self.filters.users.blacklist and tm.sender_id in self.filters.users.blacklist:
            return None
        if self.filters.users.whitelist and tm.sender_id not in self.filters.users.whitelist:
            return None

        # 2. Files filter
        f_black = [x.value if hasattr(x, "value") else str(x) for x in self.filters.files.blacklist]
        f_white = [x.value if hasattr(x, "value") else str(x) for x in self.filters.files.whitelist]
        if f_black and tm.file_type in f_black:
            return None
        if f_white and tm.file_type not in f_white:
            return None

        # 3. Text filter
        text = tm.text or ""
        t_cfg = self.filters.text
        for kw in t_cfg.blacklist:
            if not kw:
                continue
            if t_cfg.regex:
                if re.search(kw, text, 0 if t_cfg.case_sensitive else re.IGNORECASE):
                    return None
            else:
                hay = text if t_cfg.case_sensitive else text.lower()
                needle = kw if t_cfg.case_sensitive else kw.lower()
                if needle in hay:
                    return None

        if t_cfg.whitelist:
            matched = False
            for kw in t_cfg.whitelist:
                if not kw:
                    continue
                if t_cfg.regex:
                    if re.search(kw, text, 0 if t_cfg.case_sensitive else re.IGNORECASE):
                        matched = True
                        break
                else:
                    hay = text if t_cfg.case_sensitive else text.lower()
                    needle = kw if t_cfg.case_sensitive else kw.lower()
                    if needle in hay:
                        matched = True
                        break
            if not matched:
                return None

        return tm


FilterPlugin = NbFilter
