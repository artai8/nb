"""Regex and literal text replacement plugin."""

import re
from nb.plugin_models import STYLE_CODES
from nb.plugins import NbMessage, NbPlugin


class NbReplace(NbPlugin):
    id_ = "replace"

    def __init__(self, data) -> None:
        self.replace = data

    def modify(self, tm: NbMessage) -> NbMessage:
        if not self.replace.check or not tm.text:
            return tm
        mapping = dict(self.replace.text or {})
        if not mapping and self.replace.text_raw:
            for line in self.replace.text_raw.splitlines():
                if ":" in line:
                    k, v = line.split(":", 1)
                    if k.strip():
                        mapping[k.strip()] = v.strip()
        text = tm.text
        for pattern, repl in mapping.items():
            if not pattern:
                continue
            if repl in STYLE_CODES:
                # style-name replacement: wrap the matched text in the style
                # markers instead of inserting the style name literally
                code = STYLE_CODES[repl]
                if self.replace.regex:
                    text = re.sub(pattern, lambda m: f"{code}{m.group(0)}{code}", text)
                else:
                    text = text.replace(pattern, f"{code}{pattern}{code}")
            elif self.replace.regex:
                text = re.sub(pattern, repl, text)
            else:
                text = text.replace(pattern, repl)
        tm.text = text
        return tm


ReplacePlugin = NbReplace
