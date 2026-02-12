import logging
from typing import List
from nb.plugin_models import TextFilter
from nb.plugins import NbMessage, NbPlugin
from nb.utils import match


class NbFilter(NbPlugin):
    id_ = "filter"

    def __init__(self, data):
        self.filters = data
        textf = self.filters.text
        if not textf.case_sensitive and not textf.regex:
            textf.blacklist = [i.lower() for i in textf.blacklist]
            textf.whitelist = [i.lower() for i in textf.whitelist]

    def modify(self, tm):
        if self.users_safe(tm) and self.files_safe(tm) and self.text_safe(tm):
            return tm
        return None

    def modify_group(self, tms):
        return [tm for tm in tms if self.modify(tm)]

    def text_safe(self, tm):
        flist = self.filters.text
        text = tm.text if flist.case_sensitive else tm.text.lower()
        if not text and not flist.whitelist:
            return True
        for f in flist.blacklist:
            if match(f, text, flist.regex):
                return False
        if not flist.whitelist:
            return True
        return any(match(a, text, flist.regex) for a in flist.whitelist)

    def users_safe(self, tm):
        flist = self.filters.users
        sender = str(tm.sender_id)
        if sender in flist.blacklist:
            return False
        if not flist.whitelist:
            return True
        return sender in flist.whitelist

    def files_safe(self, tm):
        flist = self.filters.files
        ft = tm.file_type
        bl = [f.value if hasattr(f, 'value') else f for f in flist.blacklist]
        wl = [f.value if hasattr(f, 'value') else f for f in flist.whitelist]
        if ft in bl:
            return False
        if not wl:
            return True
        return ft in wl
