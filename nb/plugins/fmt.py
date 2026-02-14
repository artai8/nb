from nb.plugin_models import STYLE_CODES, Style
from nb.plugins import NbMessage, NbPlugin


class NbFmt(NbPlugin):
    id_ = "fmt"

    def __init__(self, data):
        self.format = data

    def modify(self, tm):
        if self.format.style is Style.PRESERVE or not tm.raw_text:
            return tm
        code = STYLE_CODES.get(self.format.style.value)
        if code:
            tm.text = f"{code}{tm.raw_text}{code}"
        return tm
