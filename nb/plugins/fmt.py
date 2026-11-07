"""Text formatting style plugin."""

from nb.plugin_models import STYLE_CODES, Style
from nb.plugins import NbMessage, NbPlugin


class NbFmt(NbPlugin):
    id_ = "fmt"

    def __init__(self, data) -> None:
        self.format = data

    def modify(self, tm: NbMessage) -> NbMessage:
        if not self.format.check or self.format.style is Style.PRESERVE or not tm.text:
            return tm
        code = STYLE_CODES.get(self.format.style.value, "")
        if code:
            tm.text = f"{code}{tm.text}{code}"
        return tm


FmtPlugin = NbFmt
