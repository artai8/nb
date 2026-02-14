import pytesseract
from PIL import Image

from nb.plugins import NbMessage, NbPlugin
from nb.utils import cleanup


class NbOcr(NbPlugin):
    id_ = "ocr"

    def __init__(self, data) -> None:
        pass

    async def modify(self, tm: NbMessage) -> NbMessage:

        if not tm.file_type in ["photo"]:
            return tm

        file = await tm.get_file()
        tm.text = pytesseract.image_to_string(Image.open(file))
        cleanup(file)
        return tm
