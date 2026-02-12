from typing import List
from nb.utils import replace as utils_replace
from nb.plugins import NbMessage, NbPlugin


class NbReplace(NbPlugin):
    id_ = "replace"

    def __init__(self, data):
        self.replace = data

    def modify(self, tm):
        if not tm.raw_text:
            return tm
        text = tm.raw_text
        for original, new in self.replace.text.items():
            text = utils_replace(original, new, text, self.replace.regex)
        tm.text = text
        return tm

    def modify_group(self, tms):
        for tm in tms:
            if tm.raw_text:
                text = tm.raw_text
                for original, new in self.replace.text.items():
                    text = utils_replace(original, new, text, self.replace.regex)
                tm.text = text
        return tms
